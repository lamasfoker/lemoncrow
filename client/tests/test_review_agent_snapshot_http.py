"""P3m live loopback HTTP lease -> source -> Bubblewrap, not a hosted pilot.

An ephemeral local HTTP handler implements the same scoped worker API. The
client still verifies the real Ed25519 signature and every byte digest; no
external service, model provider, deployment or mutable checkout is used.
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from lemoncrow_client.review_agent_execution import execute_approved_leased_agent
from lemoncrow_client.review_companion_protocol import CompanionPolicy, parse_task
from lemoncrow_client.review_worker_client import ReviewWorkerClient, WorkerBinding
from test_review_worker_client import NOW, sign


def test_signed_snapshot_real_http_process_and_cleanup(tmp_path: Path) -> None:
    payload = b"# from the exact hosted revision\ndef feature(): return 7\n"
    digest = hashlib.sha256(payload).hexdigest()
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    envelope = sign(
        key,
        spec_overrides={
            "execution_class": "review_intelligence",
            "requires_repository_execution": False,
            "policy_digest": "b" * 64,
            "capability": "review_intelligence:agent_task",
            "scope": {"task_id": "task-1", "task_kind": "investigate", "max_model_calls": 1, "max_runtime_ms": 4_000},
        },
    )
    requested: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            assert self.headers["Authorization"] == "Bearer worker-secret-at-least-20-chars"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requested.append(self.path)
            if self.path.endswith("/claim"):
                answer = {
                    "job": {
                        "job_id": "job-1",
                        "review_id": "review-1",
                        "revision_id": "revision-1",
                        "execution_class": "review_intelligence",
                    },
                    "envelope": envelope,
                }
            else:
                assert body["envelope"] == envelope
                if self.path.endswith("/heartbeat"):
                    answer = {"job_id": "job-1", "state": "running"}
                elif self.path.endswith("/context"):
                    answer = {
                        "job_id": "job-1",
                        "review_id": "review-1",
                        "revision_id": "revision-1",
                        "snapshot_id": "snapshot-1",
                        "semantic_snapshot_digest": "a" * 64,
                        "attempt": 1,
                        "side": "new",
                        "manifest_truncated": False,
                        "manifest": {
                            "src/module.py": {"content_digest": digest, "size": len(payload), "mode": 0o100644}
                        },
                    }
                elif self.path.endswith("/content"):
                    assert body["digest"] == digest and body["side"] == "new"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("X-LemonCrow-Content-Digest", digest)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                else:
                    raise AssertionError("unexpected HTTP endpoint: " + self.path)
            raw = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        binding = WorkerBinding(
            origin=f"http://127.0.0.1:{server.server_port}",
            org_id="org-1",
            worker_id="machine-1",
            repo_id="repo-1",
            bearer="worker-secret-at-least-20-chars",
            trusted_key_id="lease-key-1",
            trusted_public_key_hex=pub.hex(),
        )
        client = ReviewWorkerClient(binding, clock=lambda: NOW)
        lease = client.claim(execution_class="review_intelligence", model_route_id="route-1")
        assert lease is not None
        task = parse_task(
            {
                "schema_version": 1,
                "type": "task_offer",
                "task_id": "task-1",
                "review_id": "review-1",
                "repo_id": "repo-1",
                "snapshot_sha256": "a" * 64,
                "policy_sha256": "b" * 64,
                "task_kind": "investigate",
                "executor": "author_agent",
                "recipe_id": "",
                "deadline_unix_ms": int((NOW + 20) * 1000),
                "max_runtime_ms": 4_000,
                "max_model_calls": 1,
            }
        )
        policy = CompanionPolicy(
            repo_id="repo-1",
            snapshot_sha256="a" * 64,
            policy_sha256="b" * 64,
            enabled_tasks=frozenset({"investigate"}),
            max_runtime_ms=10_000,
            max_model_calls=1,
        )
        repo = tmp_path / "mutable-checkout"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / "src").mkdir()
        (repo / "src/module.py").write_text("def feature(): return 404\n")
        agent = tmp_path / "fixed-local-agent"
        agent.write_text(
            "#!/usr/bin/python3\nfrom pathlib import Path\n"
            "print(Path('src/module.py').read_text().strip())\n"
            "try: Path('src/module.py').write_text('bad')\n"
            "except OSError: print('read_only')\n"
        )
        agent.chmod(0o700)
        result = execute_approved_leased_agent(
            client,
            lease,
            task,
            policy,
            provider="codex",
            executable=agent,
            repository=repo,
            locally_enabled=frozenset({"codex"}),
            explicit_operator_approval=True,
        )
        assert result.status == "exited", result.output
        assert "def feature(): return 7" in result.output and "read_only" in result.output
        assert "404" not in result.output
        assert result.provenance == "author_agent_unverified" and not result.model_usage_qualified
        assert (repo / "src/module.py").read_text() == "def feature(): return 404\n"
        assert requested.count("/v1/review-workers/job-1/context") == 1
        assert requested.count("/v1/review-workers/job-1/content") == 1
        assert all("/result" not in request for request in requested)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

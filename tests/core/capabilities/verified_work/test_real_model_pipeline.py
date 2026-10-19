from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lemoncrow.core.capabilities.verified_work import build_opportunity, evaluate_eligibility
from lemoncrow.core.capabilities.verified_work.settings import VerifiedWorkSettings
from lemoncrow.pro.capabilities.model_setup.endpoints import add_endpoint
from lemoncrow.pro.capabilities.model_setup.transport import set_active_store_root
from lemoncrow.pro.capabilities.verified_work.consent import grant_auto, repository_identity
from lemoncrow.pro.capabilities.verified_work.outcomes import record_model_outcome
from lemoncrow.pro.capabilities.verified_work.runtime import run_enforced_repair


class _RepairHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        assert body.get("model") == "repair-model"
        content = json.dumps(
            {
                "summary": "repair Python syntax",
                "edits": [
                    {
                        "path": "bad.py",
                        "old": "def broken(:\n",
                        "new": "def broken():\n    pass\n",
                    }
                ],
            }
        )
        response = {
            "id": "chatcmpl-vw",
            "object": "chat.completion",
            "created": 1,
            "model": "repair-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
        }
        encoded = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_real_openai_compatible_worker_repairs_and_promotes(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "bad.py"
    source.write_text("def broken():\n    pass\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    source.write_text("def broken(:\n", encoding="utf-8")

    initial = subprocess.run(
        ["python", "-m", "py_compile", "bad.py"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert initial.returncode != 0
    opportunity = build_opportunity(
        session_id="real-model",
        workspace=repo,
        command="python -m py_compile bad.py",
        cwd=repo,
        exit_code=initial.returncode,
        stdout=initial.stdout,
        stderr=initial.stderr,
    )
    assert opportunity is not None

    server = ThreadingHTTPServer(("127.0.0.1", 0), _RepairHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = tmp_path / "lc"
    try:
        endpoint = f"http://127.0.0.1:{server.server_address[1]}/v1"
        add_endpoint(root, endpoint, name="fake-local", models=["repair-model"], locus="local")
        set_active_store_root(root)
        settings = VerifiedWorkSettings(
            mode="enforce",
            model="custom/fake-local/repair-model",
            model_policy="explicit",
            max_attempts=1,
            kinds=("syntax",),
            auto_min_samples=1,
        )
        decision = evaluate_eligibility(opportunity, settings)
        assert decision.eligible is True
        identity = repository_identity(repo)
        assert identity is not None
        grant_auto(identity, [("custom/fake-local/repair-model", "syntax")], root=root)
        record_model_outcome(
            "custom/fake-local/repair-model",
            "syntax",
            verified=True,
            promoted=False,
            latency_ms=100,
            input_tokens=1,
            output_tokens=1,
            provider_cost_usd=0.0,
            mode="shadow",
            root=root,
            repo=identity.key,
        )

        result = run_enforced_repair(opportunity, decision, settings, host="claude")
    finally:
        set_active_store_root(None)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.verified is True
    assert result.promoted is True
    assert result.repair_record_id
    assert result.model == "custom/fake-local/repair-model"
    assert result.input_tokens == 50
    assert result.output_tokens == 20
    assert source.read_text(encoding="utf-8") == "def broken():\n    pass\n"
    final = subprocess.run(["python", "-m", "py_compile", "bad.py"], cwd=repo, check=False)
    assert final.returncode == 0

"""P3m: pinned signed lease -> authenticated source -> clean offline subprocess.

Tests exercise actual digest transfers, fail-closed path/cap/content/cancel
admission and real Bubblewrap. Provider fixture is a local fake, not an LLM.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_client.review_agent_adapters import AgentInvocationRefused
from lemoncrow_client.review_agent_execution import execute_approved_leased_agent
from lemoncrow_client.review_agent_snapshot import (
    AgentSnapshotLimits,
    AgentSnapshotUnavailable,
)
from lemoncrow_client.transport import Response
from test_review_worker_client import MemoryTransport

pytest_plugins = ["test_review_agent_execution"]


class SnapshotTransport(MemoryTransport):
    def __init__(self, base: MemoryTransport, context: dict[str, Any], contents: dict[str, bytes]) -> None:
        super().__init__(base.reply)
        self.context_reply = context
        self.contents = contents
        self.downloads: list[tuple[str, str]] = []

    def post(self, path: str, *, body: Any, headers: Any, **kwargs: Any) -> Response:
        if path.endswith("/context"):
            self.calls.append((path, dict(body), dict(headers)))
            return Response(200, self.context_reply)
        return super().post(path, body=body, headers=headers, **kwargs)

    def post_binary(self, path: str, *, body: Any, headers: Any) -> tuple[int, dict[str, str], bytes]:
        assert path.endswith("/content")
        assert headers["Authorization"].startswith("Bearer worker-secret-")
        self.downloads.append((body["digest"], body["side"]))
        value = self.contents[body["digest"]]
        return 200, {"content-type": "application/octet-stream", "content-digest": body["digest"]}, value


@pytest.fixture
def leased_source(
    signed_job: tuple[Any, ...],
) -> tuple[tuple[Any, ...], SnapshotTransport]:
    client, lease, task, policy, repo, snapshot, provider, wire = signed_job
    source = b"def feature(): return 2\n"
    digest = hashlib.sha256(source).hexdigest()
    context = {
        "job_id": lease.job_id,
        "review_id": lease.review_id,
        "revision_id": lease.revision_id,
        "snapshot_id": lease.snapshot_id,
        "attempt": lease.attempt,
        "semantic_snapshot_digest": lease.snapshot_digest,
        "side": "new",
        "manifest_truncated": False,
        "manifest": {"src/app.py": {"content_digest": digest, "size": len(source), "mode": 0o100644}},
    }
    transport = SnapshotTransport(wire, context, {digest: source})
    client.transport = transport
    return (client, lease, task, policy, repo, snapshot, provider, transport), transport


def _execute(job: tuple[Any, ...], *, approved: bool = True) -> Any:
    client, lease, task, policy, repo, _unused, provider, _wire = job
    return execute_approved_leased_agent(
        client,
        lease,
        task,
        policy,
        provider="codex",
        executable=provider,
        repository=repo,
        locally_enabled=frozenset({"codex"}),
        explicit_operator_approval=approved,
    )


def test_real_signed_lease_fetch_verification_and_offline_process(
    leased_source: tuple[tuple[Any, ...], SnapshotTransport],
    tmp_path: Path,
) -> None:
    job, wire = leased_source
    provider = job[6]
    provider.write_text(
        "#!/usr/bin/python3\nfrom pathlib import Path\n"
        "print(Path('src/app.py').read_text().strip())\n"
        "try: Path('src/app.py').write_text('BAD')\n"
        "except OSError: print('read_only')\n"
    )
    provider.chmod(0o700)
    import tempfile
    from unittest.mock import patch

    with patch.object(tempfile, "tempdir", str(tmp_path)):
        outcome = _execute(job)
    assert outcome.status == "exited", outcome.output
    assert "def feature(): return 2" in outcome.output
    assert "read_only" in outcome.output
    assert outcome.provenance == "author_agent_unverified" and not outcome.model_usage_qualified
    assert len(wire.downloads) == 1 and wire.downloads[0][1] == "new"
    assert not list(tmp_path.glob("lc-review-agent-source-*"))
    assert not any(path.endswith("/result") for path, _, _ in wire.calls)
    assert not (job[4] / "src").exists()  # mutable checkout was never used


def test_local_denial_precedes_any_snapshot_request(
    leased_source: tuple[tuple[Any, ...], SnapshotTransport],
) -> None:
    job, wire = leased_source
    with pytest.raises(AgentInvocationRefused, match="approval"):
        _execute(job, approved=False)
    assert not wire.downloads
    assert not any(path.endswith("/context") for path, _, _ in wire.calls)


@pytest.mark.parametrize(
    "change",
    [
        {"side": "old"},
        {"manifest_truncated": True},
        {"manifest_truncated": None},
        {"attempt": 9},
        {"snapshot_id": "another-snapshot"},
        {"semantic_snapshot_digest": "f" * 64},
        {"manifest": {}},
    ],
)
def test_bad_context_denied_before_any_bytes(
    leased_source: tuple[tuple[Any, ...], SnapshotTransport],
    change: dict[str, Any],
) -> None:
    job, wire = leased_source
    wire.context_reply.update(change)
    with pytest.raises(ValueError):
        _execute(job)
    assert not wire.downloads


@pytest.mark.parametrize(
    "name",
    [
        "../secret",
        "/etc/passwd",
        "dir//file.py",
        "dir/../secret",
        "dir/./a",
        ".git/config",
        "src/.claude/settings.json",
        ".env",
        "settings/.env.local",
        "file.pem",
        "id_ed25519",
        "node_modules/index.js",
        "a\\b",
        "a\nroot",
        "C:/secret",
    ],
)
def test_unsafe_path_denied_without_content_request(
    leased_source: tuple[tuple[Any, ...], SnapshotTransport],
    name: str,
) -> None:
    job, wire = leased_source
    entry = next(iter(wire.context_reply["manifest"].values()))
    wire.context_reply["manifest"] = {name: entry}
    with pytest.raises(AgentSnapshotUnavailable):
        _execute(job)
    assert wire.downloads == []


@pytest.mark.parametrize(
    "entry",
    [
        {"size": True},
        {"size": 10_000_000},
        {"size": -1},
        {"mode": 0o120777},
        {"mode": 0o040755},
        {"mode": True},
        {"content_digest": "garbage"},
    ],
)
def test_manifest_metadata_cannot_bypass_limits(
    leased_source: tuple[tuple[Any, ...], SnapshotTransport],
    entry: dict[str, Any],
) -> None:
    job, wire = leased_source
    wire.context_reply["manifest"]["src/app.py"].update(entry)
    with pytest.raises(AgentSnapshotUnavailable):
        _execute(job)
    assert not wire.downloads


def test_file_as_ancestor_denied_before_fetch(
    leased_source: tuple[tuple[Any, ...], SnapshotTransport],
) -> None:
    job, wire = leased_source
    entry = next(iter(wire.context_reply["manifest"].values()))
    wire.context_reply["manifest"] = {"src": entry, "src/app.py": entry}
    with pytest.raises(AgentSnapshotUnavailable, match="collisions"):
        _execute(job)
    assert wire.downloads == []


def test_corrupt_binary_or_size_refuses_and_cleans_partial_tree(
    leased_source: tuple[tuple[Any, ...], SnapshotTransport],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, wire = leased_source
    digest = next(iter(wire.contents))
    wire.contents[digest] = b"tampered"
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    with pytest.raises(ValueError, match=r"corrupt|differs"):
        _execute(job)
    assert not list(tmp_path.glob("lc-review-agent-source-*"))
    assert not any(path.endswith("/result") for path, _, _ in wire.calls)


def test_revoked_during_multi_file_materialization_stops_and_cleans(
    leased_source: tuple[tuple[Any, ...], SnapshotTransport],
) -> None:
    job, wire = leased_source
    extra = b"another source file\n"
    digest = hashlib.sha256(extra).hexdigest()
    wire.contents[digest] = extra
    wire.context_reply["manifest"]["src/b.py"] = {
        "content_digest": digest,
        "mode": 0o100644,
        "size": len(extra),
    }
    original = wire.post_binary

    def revoke_after_first_fetch(path: str, *, body: Any, headers: Any) -> tuple[int, dict[str, str], bytes]:
        result = original(path, body=body, headers=headers)
        wire.heartbeat_state = "cancel_requested"
        return result

    wire.post_binary = revoke_after_first_fetch  # type: ignore[method-assign]
    with pytest.raises(AgentSnapshotUnavailable, match="revoked"):
        _execute(job)
    assert len(wire.downloads) == 1
    assert not any(path.endswith("/result") for path, _, _ in wire.calls)


def test_limits_and_empty_or_unbounded_manifest_refused(
    leased_source: tuple[tuple[Any, ...], SnapshotTransport],
) -> None:
    job, wire = leased_source
    for size in (False, -1, 0, 50_000):
        with pytest.raises(ValueError):
            AgentSnapshotLimits(max_files=size)
    wire.context_reply["manifest"]["src/app.py"]["size"] = 30
    wire.context_reply["manifest"]["src/another.py"] = {
        **wire.context_reply["manifest"]["src/app.py"],
        "size": 30,
    }
    with pytest.raises(AgentSnapshotUnavailable, match="total source"):
        _execute_with_source_limits(job, AgentSnapshotLimits(max_file_bytes=30, max_total_bytes=30))


def _execute_with_source_limits(job: tuple[Any, ...], limits: AgentSnapshotLimits) -> Any:
    client, lease, task, policy, repo, _unused, provider, _wire = job
    return execute_approved_leased_agent(
        client,
        lease,
        task,
        policy,
        provider="codex",
        executable=provider,
        repository=repo,
        locally_enabled=frozenset({"codex"}),
        explicit_operator_approval=True,
        source_limits=limits,
    )

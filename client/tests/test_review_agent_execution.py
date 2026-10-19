"""Signed task scope -> read-only offline subprocess, with real Bubblewrap.

Executable fixtures stand in for local providers; no Claude/Codex/OpenCode
model execution, credential forwarding, billed calls or success publication.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from lemoncrow_client.review_agent_adapters import AgentInvocationRefused
from lemoncrow_client.review_agent_execution import execute_approved_offline_agent
from lemoncrow_client.review_companion_protocol import CompanionPolicy, parse_task
from lemoncrow_client.review_worker_client import ReviewWorkerClient, WorkerBinding, WorkerLease
from test_review_worker_client import NOW, MemoryTransport, sign


@pytest.fixture
def fixture_authority() -> tuple[WorkerBinding, Ed25519PrivateKey]:
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    binding = WorkerBinding(
        origin="https://app.lemoncrow.com",
        org_id="org-1",
        worker_id="machine-1",
        repo_id="repo-1",
        bearer="worker-secret-at-least-20-chars",
        trusted_key_id="lease-key-1",
        trusted_public_key_hex=public.hex(),
    )
    return binding, key


@pytest.fixture
def signed_job(
    fixture_authority: tuple[Any, Any], tmp_path: Path
) -> tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, Path, Path, Path, MemoryTransport]:
    binding, key = fixture_authority
    scope = {"task_id": "task-1", "task_kind": "investigate", "max_model_calls": 1, "max_runtime_ms": 4_000}
    envelope = sign(
        key,
        spec_overrides={
            "execution_class": "review_intelligence",
            "requires_repository_execution": False,
            "policy_digest": "b" * 64,
            "capability": "review_intelligence:agent_task",
            "scope": scope,
        },
    )
    response = {
        "job": {
            "job_id": "job-1",
            "review_id": "review-1",
            "revision_id": "revision-1",
            "execution_class": "review_intelligence",
        },
        "envelope": envelope,
    }
    wire = MemoryTransport(response)
    client = ReviewWorkerClient(binding, transport=wire, clock=lambda: NOW)
    lease = client.claim(execution_class="review_intelligence", model_route_id="local-approved")
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
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "file.py").write_text("def feature(): return 1\n")
    provider = tmp_path / "agent-provider"
    provider.write_text("#!/usr/bin/python3\nfrom pathlib import Path\nprint(Path('file.py').read_text().strip())\n")
    provider.chmod(0o700)
    return client, lease, task, policy, repo, snapshot, provider, wire


def execute(
    job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, Path, Path, Path, MemoryTransport],
    *,
    approved: bool = True,
) -> Any:
    client, lease, task, policy, repo, snapshot, provider, _ = job
    return execute_approved_offline_agent(
        client,
        lease,
        task,
        policy,
        provider="codex",
        executable=provider,
        repository=repo,
        snapshot=snapshot,
        locally_enabled=frozenset({"codex"}),
        explicit_operator_approval=approved,
    )


def test_explicit_signed_grant_runs_only_offline_unverified(
    signed_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, Path, Path, Path, MemoryTransport],
) -> None:
    result = execute(signed_job)
    assert result.status == "exited", result.output
    assert "def feature(): return 1" in result.output
    assert result.provenance == "author_agent_unverified"
    assert result.model_usage_qualified is False
    assert not any(path.endswith("/result") for path, _, _ in signed_job[-1].calls)
    assert (signed_job[-3] / "file.py").read_text() == "def feature(): return 1\n"


def test_no_approval_or_invalid_signed_scope_does_not_run(
    signed_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, Path, Path, Path, MemoryTransport],
) -> None:
    with pytest.raises(AgentInvocationRefused, match="approval"):
        execute(signed_job, approved=False)
    client, lease, task, policy, repo, snapshot, provider, _ = signed_job
    invalid = replace(lease, spec={**lease.spec, "scope": {"task_id": "task-1", "task_kind": "investigate"}})
    with pytest.raises(ValueError, match="signed contract"):
        execute_approved_offline_agent(
            client,
            invalid,
            task,
            policy,
            provider="codex",
            executable=provider,
            repository=repo,
            snapshot=snapshot,
            locally_enabled=frozenset({"codex"}),
            explicit_operator_approval=True,
        )


def test_cancelled_lease_refused_before_dispatch(
    signed_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, Path, Path, Path, MemoryTransport],
) -> None:
    signed_job[-1].heartbeat_state = "cancel_requested"
    with pytest.raises(AgentInvocationRefused, match="cancelled"):
        execute(signed_job)


def test_wrong_repo_snapshot_or_policy_cannot_execute(
    signed_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, Path, Path, Path, MemoryTransport],
) -> None:
    client, lease, task, policy, repo, snapshot, provider, _ = signed_job
    for changed in (
        replace(task, repo_id="another-repo"),
        replace(task, snapshot_sha256="c" * 64),
        replace(task, policy_sha256="d" * 64),
    ):
        with pytest.raises(AgentInvocationRefused):
            execute_approved_offline_agent(
                client,
                lease,
                changed,
                policy,
                provider="codex",
                executable=provider,
                repository=repo,
                snapshot=snapshot,
                locally_enabled=frozenset({"codex"}),
                explicit_operator_approval=True,
            )


def test_revocation_during_real_process_kills_offline_agent(
    signed_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, Path, Path, Path, MemoryTransport],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    client, lease, task, policy, repo, snapshot, provider, wire = signed_job
    provider.write_text("#!/usr/bin/python3\nimport time\ntime.sleep(10)\n")
    provider.chmod(0o700)
    original_post = wire.post
    checks = 0

    def cancel_after_first_heartbeat(path: str, *, body: Any, headers: Any, **kwargs: Any) -> Any:
        nonlocal checks
        if path.endswith("/heartbeat"):
            checks += 1
            if checks > 1:
                wire.heartbeat_state = "cancel_requested"
        return original_post(path, body=body, headers=headers, **kwargs)

    monkeypatch.setattr(wire, "post", cancel_after_first_heartbeat)
    started = time.monotonic()
    outcome = execute_approved_offline_agent(
        client,
        lease,
        task,
        policy,
        provider="codex",
        executable=provider,
        repository=repo,
        snapshot=snapshot,
        locally_enabled=frozenset({"codex"}),
        explicit_operator_approval=True,
    )
    assert outcome.status == "cancelled"
    assert checks >= 2
    assert time.monotonic() - started < 5
    assert not any(path.endswith("/result") for path, _, _ in wire.calls)

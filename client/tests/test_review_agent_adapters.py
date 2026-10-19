"""Signed, read-only local agent invocation contracts; never launch models in tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from lemoncrow_client.review_agent_adapters import (
    AgentInvocationRefused,
    LocalAgentInvocation,
    build_agent_invocation,
)
from lemoncrow_client.review_companion_protocol import CompanionPolicy, CompanionTask, parse_task
from lemoncrow_client.review_worker_client import WorkerLease

NOW = 1_900_000_000
DIGEST = "a" * 64
POLICY = "b" * 64


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    binary = tmp_path / "agent-bin"
    binary.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    binary.chmod(0o700)
    return repo, binary


def typed(kind: str = "investigate") -> CompanionTask:
    return parse_task(
        {
            "schema_version": 1,
            "type": "task_offer",
            "task_id": "task-1",
            "review_id": "review-1",
            "repo_id": "repo-1",
            "snapshot_sha256": DIGEST,
            "policy_sha256": POLICY,
            "task_kind": kind,
            "executor": "author_agent",
            "recipe_id": "",
            "deadline_unix_ms": (NOW + 30) * 1000,
            "max_runtime_ms": 20_000,
            "max_model_calls": 1,
        }
    )


def operator(kind: str = "investigate", *, approve: bool = False) -> CompanionPolicy:
    return CompanionPolicy(
        repo_id="repo-1",
        snapshot_sha256=DIGEST,
        policy_sha256=POLICY,
        enabled_tasks=frozenset({kind}),
        allow_fix_proposals=approve,
        max_runtime_ms=30_000,
        max_model_calls=1,
    )


def claimed(kind: str = "investigate") -> WorkerLease:
    return WorkerLease(
        job_id="job-1",
        review_id="review-1",
        revision_id="revision-1",
        snapshot_id="snapshot-1",
        snapshot_digest=DIGEST,
        policy_digest=POLICY,
        execution_class="review_intelligence",
        capability="review_intelligence:agent_task",
        attempt=1,
        deadline_at=NOW + 40,
        expires_at=NOW + 40,
        envelope="signed-by-host-not-inferred",
        spec={
            "repo_id": "repo-1",
            "execution_locus": "customer",
            "scope": {"task_id": "task-1", "task_kind": kind},
        },
    )


@pytest.mark.parametrize("provider", ["claude_code", "codex", "opencode"])
def test_three_local_provider_invocations_are_fresh_and_read_only(workspace: tuple[Path, Path], provider: str) -> None:
    repo, binary = workspace
    result = build_agent_invocation(
        provider,
        executable=binary,
        repo=repo,
        task=typed(),
        policy=operator(),
        lease=claimed(),
        now_unix_ms=NOW * 1000,
        locally_enabled=frozenset({provider}),
    )
    assert isinstance(result, LocalAgentInvocation)
    assert result.executable == str(binary)
    assert result.argv[0] == str(binary)
    assert result.stdin.startswith("You are a local read-only")
    assert result.cwd == repo
    assert result.max_runtime_seconds == 20
    assert result.result_provenance == "author_agent_unverified"
    assert "--resume" not in result.argv and "--continue" not in result.argv
    assert "--attach" not in result.argv and "--auto" not in result.argv
    if provider == "claude_code":
        assert "--permission-mode" in result.argv and "plan" in result.argv
        assert "--no-session-persistence" in result.argv
        assert "--max-turns" in result.argv and "1" in result.argv
        assert "Read,Glob,Grep" in result.argv
    elif provider == "codex":
        assert "--sandbox" in result.argv and "read-only" in result.argv
        assert "--ephemeral" in result.argv and "--ignore-user-config" in result.argv
    else:
        assert result.environment_overrides["OPENCODE_PERMISSION"].startswith('{"*"')
        assert '"edit"' not in result.environment_overrides["OPENCODE_PERMISSION"]
        assert "plan" in result.argv


def test_fix_proposals_require_explicit_operator_opt_in_and_remain_read_only(
    workspace: tuple[Path, Path],
) -> None:
    repo, binary = workspace
    with pytest.raises(AgentInvocationRefused, match="fix_proposals_not_approved"):
        build_agent_invocation(
            "claude_code",
            executable=binary,
            repo=repo,
            task=typed("propose_fix"),
            policy=operator("propose_fix"),
            lease=claimed("propose_fix"),
            now_unix_ms=NOW * 1000,
            locally_enabled=frozenset({"claude_code"}),
        )
    result = build_agent_invocation(
        "claude_code",
        executable=binary,
        repo=repo,
        task=typed("propose_fix"),
        policy=operator("propose_fix", approve=True),
        lease=claimed("propose_fix"),
        now_unix_ms=NOW * 1000,
        locally_enabled=frozenset({"claude_code"}),
    )
    assert "Do not apply the diff" in result.stdin
    assert "--permission-mode" in result.argv
    assert result.result_provenance == "author_agent_unverified"


@pytest.mark.parametrize(
    "mutation",
    [
        {"review_id": "foreign-review"},
        {"snapshot_digest": "c" * 64},
        {"policy_digest": "d" * 64},
        {"execution_class": "runtime_validation"},
        {"expires_at": NOW - 1},
        {"deadline_at": NOW - 1},
        {
            "spec": {
                "repo_id": "other-repo",
                "execution_locus": "customer",
                "scope": {"task_id": "task-1", "task_kind": "investigate"},
            }
        },
        {
            "spec": {
                "repo_id": "repo-1",
                "execution_locus": "managed",
                "scope": {"task_id": "task-1", "task_kind": "investigate"},
            }
        },
        {
            "spec": {
                "repo_id": "repo-1",
                "execution_locus": "customer",
                "scope": {"task_id": "other-task", "task_kind": "investigate"},
            }
        },
    ],
)
def test_stale_or_cross_scope_signed_lease_never_dispatches(
    workspace: tuple[Path, Path], mutation: dict[str, object]
) -> None:
    repo, binary = workspace
    with pytest.raises(AgentInvocationRefused):
        build_agent_invocation(
            "claude_code",
            executable=binary,
            repo=repo,
            task=typed(),
            policy=operator(),
            lease=replace(claimed(), **mutation),
            now_unix_ms=NOW * 1000,
            locally_enabled=frozenset({"claude_code"}),
        )


def test_disabled_provider_refused(workspace: tuple[Path, Path]) -> None:
    repo, binary = workspace
    with pytest.raises(AgentInvocationRefused, match="not been enabled"):
        build_agent_invocation(
            "claude_code",
            executable=binary,
            repo=repo,
            task=typed(),
            policy=operator(),
            lease=claimed(),
            now_unix_ms=NOW * 1000,
            locally_enabled=frozenset(),
        )


def test_expired_or_budget_escalated_offer_refused(workspace: tuple[Path, Path]) -> None:
    repo, binary = workspace
    for changed in (
        replace(typed(), deadline_unix_ms=(NOW - 1) * 1000),
        replace(typed(), max_runtime_ms=90_000),
        replace(typed(), max_model_calls=3),
    ):
        with pytest.raises(AgentInvocationRefused):
            build_agent_invocation(
                "claude_code",
                executable=binary,
                repo=repo,
                task=changed,
                policy=operator(),
                lease=claimed(),
                now_unix_ms=NOW * 1000,
                locally_enabled=frozenset({"claude_code"}),
            )


def test_not_an_existing_agent_session(workspace: tuple[Path, Path]) -> None:
    repo, binary = workspace
    with pytest.raises(ValueError, match="never independent verification"):
        LocalAgentInvocation(
            provider="codex",
            executable=str(binary),
            argv=(str(binary), "exec", "resume"),
            stdin="prompt",
            cwd=repo,
            environment_overrides={},
            task_id="task-1",
            review_id="review-1",
            snapshot_digest=DIGEST,
            max_runtime_seconds=5,
            result_provenance="independent_verified",
        )
    with pytest.raises(ValueError, match="prior session"):
        LocalAgentInvocation(
            provider="codex",
            executable=str(binary),
            argv=(str(binary), "exec", "--resume"),
            stdin="prompt",
            cwd=repo,
            environment_overrides={},
            task_id="task-1",
            review_id="review-1",
            snapshot_digest=DIGEST,
            max_runtime_seconds=5,
        )

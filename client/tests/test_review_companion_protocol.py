"""Review Companion protocol negatives; acceptance is not an execution grant."""

from __future__ import annotations

from dataclasses import replace

import pytest
from lemoncrow_client.review_companion_protocol import CompanionPolicy, admit_task, parse_task

S = "a" * 64
P = "b" * 64


def offer(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "schema_version": 1,
        "type": "task_offer",
        "task_id": "task-1",
        "review_id": "r-123",
        "repo_id": "repo-456",
        "snapshot_sha256": S,
        "policy_sha256": P,
        "task_kind": "investigate",
        "executor": "author_agent",
        "recipe_id": "",
        "deadline_unix_ms": 10_000,
        "max_runtime_ms": 10_000,
        "max_model_calls": 2,
    }
    data.update(overrides)
    return data


def policy(**overrides: object) -> CompanionPolicy:
    return replace(
        CompanionPolicy(
            repo_id="repo-456",
            snapshot_sha256=S,
            policy_sha256=P,
            enabled_tasks=frozenset({"investigate", "verify_recipe"}),
            allowed_recipe_ids=frozenset({"pytest.focused"}),
            max_runtime_ms=60_000,
        ),
        **overrides,
    )


def test_read_only_investigation_is_typed_but_not_automatic_execution() -> None:
    task = parse_task(offer())
    assert task.task_kind == "investigate"
    assert admit_task(task, policy(), now_unix_ms=1_000).status == "accepted"
    assert admit_task(task, policy(enabled_tasks=frozenset()), now_unix_ms=1_000).reason == "capability_not_enabled"


@pytest.mark.parametrize(
    "mutation",
    [
        {"command": "rm -rf /"},
        {"shell": "whoami"},
        {"task_kind": "shell"},
        {"task_kind": "agent_run"},
        {"executor": "localhost"},
        {"recipe_id": "test && cat ~/.ssh/id_rsa"},
        {"task_id": "../malicious"},
        {"snapshot_sha256": "abc"},
        {"policy_sha256": "A" * 64},
        {"deadline_unix_ms": True},
        {"max_runtime_ms": -1},
        {"max_model_calls": 101},
        {"schema_version": 2},
        {"type": "execute_shell"},
    ],
)
def test_rejects_untyped_or_unsafe_tasks(mutation: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        parse_task(offer(**mutation))


def test_expiry_repo_and_snapshot_currentness() -> None:
    task = parse_task(offer())
    assert admit_task(task, policy(repo_id="other"), now_unix_ms=1_000).reason == "repository_not_paired"
    assert admit_task(task, policy(snapshot_sha256="c" * 64), now_unix_ms=1_000).status == "stale"
    assert admit_task(task, policy(policy_sha256="d" * 64), now_unix_ms=1_000).status == "stale"
    assert admit_task(task, policy(), now_unix_ms=10_000).status == "expired"


def test_agent_can_propose_fix_only_with_local_approval() -> None:
    task = parse_task(offer(task_kind="propose_fix"))
    enabled = policy(enabled_tasks=frozenset({"propose_fix"}))
    assert admit_task(task, enabled, now_unix_ms=1_000).reason == "fix_proposals_not_approved"
    assert admit_task(task, replace(enabled, allow_fix_proposals=True), now_unix_ms=1_000).status == "accepted"


def test_verification_cannot_run_in_agent_or_inject_new_recipe() -> None:
    with pytest.raises(ValueError, match="separate executor"):
        parse_task(
            offer(task_kind="verify_recipe", recipe_id="pytest.focused", executor="author_agent", max_model_calls=0)
        )
    with pytest.raises(ValueError, match="budget"):
        parse_task(
            offer(task_kind="verify_recipe", recipe_id="pytest.focused", executor="author_machine", max_model_calls=2)
        )
    task = parse_task(
        offer(task_kind="verify_recipe", recipe_id="pytest.focused", executor="author_machine", max_model_calls=0)
    )
    assert admit_task(task, policy(), now_unix_ms=1_000).status == "accepted"
    assert (
        admit_task(replace(task, recipe_id="all.tests"), policy(), now_unix_ms=1_000).reason == "recipe_not_allowlisted"
    )
    independent = replace(task, executor="independent_worker")
    assert admit_task(independent, policy(), now_unix_ms=1_000).status == "accepted"
    # Admission alone does not make author-machine results independent.
    assert task.executor == "author_machine"
    assert independent.executor == "independent_worker"


def test_local_budget_prevents_model_spend_or_unbounded_execution() -> None:
    task = parse_task(offer(max_model_calls=5, max_runtime_ms=100_000))
    assert admit_task(task, policy(), now_unix_ms=1_000).reason == "budget_exceeds_local_policy"
    task = parse_task(offer(max_model_calls=5, max_runtime_ms=60_000))
    assert admit_task(task, policy(max_model_calls=4), now_unix_ms=1_000).reason == "budget_exceeds_local_policy"

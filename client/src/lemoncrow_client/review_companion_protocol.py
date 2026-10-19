"""Review Companion v1 task admission, independent of MCP and network transport.

The hosted service may OFFER bounded, typed work, never a shell/agent prompt
with execution authority. This module is the *local* second gate: endpoint
identity, signed job envelope, trusted-base run policy, and agent-specific
permissions must also be checked by the eventual P3/P5 transport/executors.
It intentionally does not execute tasks or open a network socket.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

__all__ = ["CompanionPolicy", "CompanionTask", "TaskDecision", "admit_task", "parse_task"]

TaskKind = Literal["investigate", "propose_fix", "verify_recipe"]
TaskExecutor = Literal["author_agent", "author_machine", "independent_worker"]
DecisionStatus = Literal["accepted", "rejected", "stale", "expired", "unsupported"]

_TASK_KINDS: Final[frozenset[str]] = frozenset({"investigate", "propose_fix", "verify_recipe"})
_EXECUTORS: Final[frozenset[str]] = frozenset({"author_agent", "author_machine", "independent_worker"})
_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "type",
        "task_id",
        "review_id",
        "repo_id",
        "snapshot_sha256",
        "policy_sha256",
        "task_kind",
        "executor",
        "recipe_id",
        "deadline_unix_ms",
        "max_runtime_ms",
        "max_model_calls",
    }
)
_ID: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_DIGEST: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}\Z")
_RECIPE: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_.-]{0,79}\Z")


@dataclass(frozen=True, slots=True)
class CompanionTask:
    """Typed work offer. Parsing *does not* grant permission to execute."""

    task_id: str
    review_id: str
    repo_id: str
    snapshot_sha256: str
    policy_sha256: str
    task_kind: TaskKind
    executor: TaskExecutor
    recipe_id: str
    deadline_unix_ms: int
    max_runtime_ms: int
    max_model_calls: int


@dataclass(frozen=True, slots=True)
class CompanionPolicy:
    """Local operator capabilities; do not construct from a server task offer."""

    repo_id: str
    snapshot_sha256: str
    policy_sha256: str
    enabled_tasks: frozenset[str]
    allowed_recipe_ids: frozenset[str] = frozenset()
    max_runtime_ms: int = 120_000
    max_model_calls: int = 5
    # The agent may suggest edits only when the author opted into that adapter;
    # accepted suggestions still need Review/human judgment to be published.
    allow_fix_proposals: bool = False


@dataclass(frozen=True, slots=True)
class TaskDecision:
    status: DecisionStatus
    reason: str


def _identifier(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{name} must be a bounded opaque identifier")
    return value


def _digest(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _integer(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def parse_task(payload: Mapping[str, object]) -> CompanionTask:
    """Fail closed for unknown fields, shell commands or ambiguous capabilities."""
    if frozenset(payload) != _FIELDS:
        raise ValueError("companion task has missing or unsupported fields")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("unsupported companion protocol version")
    if payload["type"] != "task_offer":
        raise ValueError("unsupported companion message type")
    task_kind = payload["task_kind"]
    executor = payload["executor"]
    if type(task_kind) is not str or task_kind not in _TASK_KINDS:
        raise ValueError("unsupported task capability")
    if type(executor) is not str or executor not in _EXECUTORS:
        raise ValueError("unsupported execution locus")
    recipe_id = payload["recipe_id"]
    if not isinstance(recipe_id, str) or (recipe_id and not _RECIPE.fullmatch(recipe_id)):
        raise ValueError("invalid verification recipe identifier")
    if (task_kind == "verify_recipe") != bool(recipe_id):
        raise ValueError("recipe is required only for verify_recipe")
    if (task_kind == "investigate" or task_kind == "propose_fix") and executor != "author_agent":
        raise ValueError("agent investigations and fix proposals require an author agent")
    if task_kind == "verify_recipe" and executor == "author_agent":
        raise ValueError("verification requires a separate executor, not the agent")
    max_model_calls = _integer(payload["max_model_calls"], name="max_model_calls", minimum=0, maximum=100)
    if task_kind == "verify_recipe" and max_model_calls:
        raise ValueError("a verification recipe cannot acquire an agent model budget")
    if task_kind != "verify_recipe" and max_model_calls == 0:
        raise ValueError("an agent task must have a positive bounded model budget")
    return CompanionTask(
        task_id=_identifier(payload["task_id"], name="task_id"),
        review_id=_identifier(payload["review_id"], name="review_id"),
        repo_id=_identifier(payload["repo_id"], name="repo_id"),
        snapshot_sha256=_digest(payload["snapshot_sha256"], name="snapshot_sha256"),
        policy_sha256=_digest(payload["policy_sha256"], name="policy_sha256"),
        task_kind=task_kind,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        recipe_id=recipe_id,
        deadline_unix_ms=_integer(payload["deadline_unix_ms"], name="deadline_unix_ms", minimum=1, maximum=2**63 - 1),
        max_runtime_ms=_integer(payload["max_runtime_ms"], name="max_runtime_ms", minimum=1, maximum=3_600_000),
        max_model_calls=max_model_calls,
    )


def admit_task(task: CompanionTask, policy: CompanionPolicy, *, now_unix_ms: int) -> TaskDecision:
    """Operator-side intent check before any executor even sees a task.

    Admission is necessary but never sufficient: a later authenticated worker
    must validate the signed job lease and trusted run-plan at execution time.
    This function cannot assert that tests passed or a provider was independent.
    """
    if task.repo_id != policy.repo_id:
        return TaskDecision("rejected", "repository_not_paired")
    if task.snapshot_sha256 != policy.snapshot_sha256 or task.policy_sha256 != policy.policy_sha256:
        return TaskDecision("stale", "snapshot_or_policy_changed")
    if task.deadline_unix_ms <= now_unix_ms:
        return TaskDecision("expired", "task_deadline_passed")
    if task.task_kind not in policy.enabled_tasks:
        return TaskDecision("unsupported", "capability_not_enabled")
    if task.max_runtime_ms > policy.max_runtime_ms or task.max_model_calls > policy.max_model_calls:
        return TaskDecision("rejected", "budget_exceeds_local_policy")
    if task.task_kind == "propose_fix" and not policy.allow_fix_proposals:
        return TaskDecision("rejected", "fix_proposals_not_approved")
    if task.task_kind == "verify_recipe" and task.recipe_id not in policy.allowed_recipe_ids:
        return TaskDecision("rejected", "recipe_not_allowlisted")
    return TaskDecision("accepted", "local_policy_allows_offer")

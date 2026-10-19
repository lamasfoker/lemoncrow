"""Exact blocking reasons in product words (Phase 10 item 11, P10-LAND).

One vocabulary for every surface that says why a Change cannot land yet: the
Brief and Review Status (readiness ``blocking_reasons``), the Inbox (built
from the content-free readiness record, so only ids, owners, categories and
states are read) and the SCM commit status LemonCrow publishes. Actions are
short imperative phrases ("Finish review", "Waiting for checks"); internal
words (condition ids, generations, digests) never reach a person.

Pure functions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from lemoncrow.pro.capabilities.review.readiness import SATISFIED_STATES

STATUS_CONTEXT: Final[str] = "LemonCrow / Merge readiness"
STATUS_DESCRIPTION_MAX: Final[int] = 140
"""GitHub's commit status description limit; longer text is cut with an ellipsis, never rejected."""
REASONS_WIRE_LIMIT: Final[int] = 20

_EXACT: Final[Mapping[str, str]] = {
    "latest-snapshot-reviewed": "Finish review",
    "behavior-changes-judged": "Judge behavior changes",
    "human-change-requests": "Resolve requested changes",
    "human-comments": "Resolve open comments",
    "human-approval-state": "Changes requested",
    "manual-verification": "Record manual checks",
    "policy-authority": "Review policy unavailable",
    "policy-generation": "Review policy changed",
    "run-plan-required": "Required checks not run",
    "supply-chain-policy": "Supply-chain evidence",
    "browser-regression": "Browser checks",
    "browser-regression-policy": "Browser checks",
    "independent-ai-review": "Independent AI review",
    "org-policy:minimum-approvals": "Approval needed",
}
_PREFIX: Final[tuple[tuple[str, str], ...]] = (
    ("org-policy:reviewer:", "Approval needed"),
    ("org-policy:scope:", "Finish required review scope"),
    ("org-policy:check:", "Waiting for checks"),
    ("change-set:", "Finish the change set"),
    ("stack:", "Land dependencies first"),
    ("premerge:", "Pre-merge checks"),
    ("readiness-error:", "Readiness unavailable"),
    ("scm-requirement:", "Provider requirement"),
)


def action_for(row: Mapping[str, Any]) -> str:
    """The short product phrase for one unsatisfied condition (wire or record row)."""

    identifier = str(row.get("id") or "")
    state = str(row.get("state") or "")
    category = str(row.get("category") or "")
    if identifier == "merge-result":
        return "Merged result fails checks" if state == "fail" else "Waiting for merge result"
    if identifier == "verification" or category == "verification":
        if identifier in _EXACT:
            return _EXACT[identifier]
        return "Fix failing checks" if state == "fail" else "Waiting for checks"
    if identifier in _EXACT:
        return _EXACT[identifier]
    for prefix, action in _PREFIX:
        if identifier.startswith(prefix):
            return action
    if str(row.get("owner") or "") == "scm":
        return "Provider requirement"
    if category == "approval":
        return "Approval needed"
    if category == "human_review":
        return "Finish review"
    return str(row.get("label") or "").strip() or "Not ready"


def blocking_reasons(
    conditions: Sequence[Mapping[str, Any]], *, limit: int = REASONS_WIRE_LIMIT, skip: Sequence[str] = ()
) -> list[dict[str, str]]:
    """Every unsatisfied condition as ``{id, owner, state, action}``, LemonCrow-owned first.

    Bounded by *limit* rows (the caller keeps the exact count); conditions in
    *skip* (bypassed by break-glass) are left out.
    """

    skipped = set(skip)
    rows = [
        row
        for row in conditions
        if str(row.get("state") or "") not in SATISFIED_STATES and str(row.get("id") or "") not in skipped
    ]
    rows.sort(key=lambda row: 1 if str(row.get("owner") or "") == "scm" else 0)
    return [
        {
            "id": str(row.get("id") or ""),
            "owner": str(row.get("owner") or "lemoncrow"),
            "state": str(row.get("state") or ""),
            "action": action_for(row),
        }
        for row in rows[: max(0, limit)]
    ]


def summary_text(reasons: Sequence[Mapping[str, Any]], *, total: int | None = None, shown: int = 3) -> str:
    """``Finish review · Waiting for checks · 2 more`` (distinct actions, in order)."""

    actions: list[str] = []
    for row in reasons:
        action = str(row.get("action") or "")
        if action and action not in actions:
            actions.append(action)
    count = len(actions) if total is None else max(total, len(actions))
    text = " · ".join(actions[:shown])
    extra = count - min(len(actions), shown)
    return f"{text} · {extra} more" if text and extra > 0 else text


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= STATUS_DESCRIPTION_MAX else text[: STATUS_DESCRIPTION_MAX - 1].rstrip() + "…"


def scm_status(
    readiness: Mapping[str, Any],
    *,
    auto_approval: Mapping[str, Any] | None = None,
    break_glass: Mapping[str, Any] | None = None,
    reviewed: bool = False,
) -> dict[str, str]:
    """The commit status LemonCrow publishes for the reviewed head: ``{state, description, context}``.

    Reflects LemonCrow-owned conditions only (the SCM owns its own). Readiness
    here is evaluated as the policy principal, so its own review progress is
    replaced by the team's: *reviewed* (a current human approval) or a standing
    auto-approval finishes review. ``state`` is GitHub's vocabulary:
    ``success``, ``pending`` or ``failure``.
    """

    conditions = [row for row in readiness.get("conditions") or () if isinstance(row, Mapping)]
    bypassed = [str(item) for item in (break_glass or {}).get("bypassed") or ()]
    local = [
        row for row in blocking_reasons(conditions, limit=len(conditions) or 1, skip=bypassed) if row["owner"] != "scm"
    ]
    if break_glass and not local:
        due = str(break_glass.get("follow_up_due_at") or "")[:10]
        actor = str(break_glass.get("actor_id") or "a responder")
        return {
            "state": "success",
            "description": _clip(f"Break-glass by {actor}; follow-up review due {due}".strip()),
            "context": STATUS_CONTEXT,
        }
    approved = auto_approval is not None and str(auto_approval.get("state") or "") == "approved"
    standing_in = {"latest-snapshot-reviewed"} if approved or reviewed else set()
    remaining = [row for row in local if row["id"] not in standing_in]
    if not remaining:
        return {
            "state": "success",
            "description": "Approved by LemonCrow policy" if approved else "Merge readiness: ready",
            "context": STATUS_CONTEXT,
        }
    failing = any(row["state"] == "fail" for row in remaining)
    text = summary_text(remaining, total=len({row["action"] for row in remaining}))
    return {
        "state": "failure" if failing else "pending",
        "description": _clip(("Blocked: " if failing else "Waiting: ") + text),
        "context": STATUS_CONTEXT,
    }


def merge_result_status(readiness: Mapping[str, Any]) -> dict[str, str] | None:
    """The status for the merge result commit (merge queue group), or ``None`` when none is observed."""

    row = next(
        (
            row
            for row in readiness.get("conditions") or ()
            if isinstance(row, Mapping) and row.get("id") == "merge-result"
        ),
        None,
    )
    if row is None or not str(row.get("target_sha") or ""):
        return None
    state = str(row.get("state") or "")
    if state == "pass":
        return {"state": "success", "description": "Merge result: no new failures", "context": STATUS_CONTEXT}
    if state == "fail":
        return {
            "state": "failure",
            "description": _clip("Merged result fails checks: " + str(row.get("detail") or "")),
            "context": STATUS_CONTEXT,
        }
    return {"state": "pending", "description": "Waiting for merge result checks", "context": STATUS_CONTEXT}


__all__ = [
    "REASONS_WIRE_LIMIT",
    "STATUS_CONTEXT",
    "STATUS_DESCRIPTION_MAX",
    "action_for",
    "blocking_reasons",
    "merge_result_status",
    "scm_status",
    "summary_text",
]

"""Break-glass landing (Phase 10 item 20, P10-LAND).

An authorized responder (named by organization policy, a person, never an
agent, automation or service principal) may land an emergency Change with
named LemonCrow blockers bypassed. Break-glass:

* records actor, reason, incident link and the exact bypassed requirements
  (one ``review_derived`` record, kind :data:`RECORD_KIND`, plus an activity
  event);
* arms Merge when ready through the SCM's own landing mechanism only (native
  auto-merge / merge queue at the exact reviewed head). Branch protection is
  the SCM's: anything it still requires is satisfied, or bypassed by the
  responder's own SCM permission, in the SCM -- LemonCrow never calls an admin
  merge or sets a bypass;
* opens a mandatory follow-up review of the landed Snapshot with a deadline;
  a human LGTM or changes-requested outcome on that Snapshot by someone other
  than the responder, recorded after the break-glass, completes it, and a
  missed deadline raises attention for the owners.

Only the named blockers are bypassed: revalidation still withdraws the landing
when any other LemonCrow-owned condition regresses. SCM-owned requirements
cannot be named (the SCM owns their bypass).

Writer: :func:`record_break_glass`. Closer: the Snapshot/Review deletion
cascade owns retention; the follow-up state is derived from outcomes on read,
so nothing else writes it.
"""

from __future__ import annotations

import secrets
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from lemoncrow.pro.capabilities.review.freshness import format_timestamp, parse_timestamp
from lemoncrow.pro.capabilities.review.readiness import SATISFIED_STATES

RECORD_KIND: Final[str] = "break_glass"
MAX_REASON_CHARS: Final[int] = 2000
MIN_REASON_CHARS: Final[int] = 10
MAX_INCIDENT_URL_CHARS: Final[int] = 500
MAX_BYPASSED: Final[int] = 32
FOLLOW_UP_OUTCOMES: Final[frozenset[str]] = frozenset({"lgtm", "changes_requested"})
#: Conditions that explain others and can never be named as bypassed.
_NOT_BYPASSABLE: Final[frozenset[str]] = frozenset({"policy-generation"})


class BreakGlassRefused(ValueError):
    """Break-glass is not allowed for this caller or request; ``status`` is the HTTP status to answer."""

    def __init__(self, message: str, *, status: int = 403) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class BreakGlassRequest:
    reason: str
    incident_url: str
    bypass: tuple[str, ...]
    expected_revision_id: str
    expected_conditions_hash: str


def authorize(principal: Any | None, *, responders: Sequence[str], may_manage: bool, enabled: bool) -> str:
    """The responder's principal id; raises :class:`BreakGlassRefused` for anyone else.

    *principal* is an ``action_approval_api.ApprovalPrincipal`` (the embedding
    server's human/agent/service classification). Only ``human`` passes;
    agents, automation, service and control-plane identities never do, even
    when policy names their id.
    """

    kind = str(getattr(principal, "kind", "") or "")
    principal_id = str(getattr(principal, "principal_id", "") or "")
    if principal is None or kind != "human" or not principal_id:
        why = str(getattr(principal, "reason", "") or "")
        raise BreakGlassRefused(
            "Break-glass is only for an authorized person; agents, automation and service principals can never "
            "use it" + (f" ({why})" if why else "") + "."
        )
    if not enabled or not responders:
        raise BreakGlassRefused("Organization policy names no break-glass responders for this repository.")
    if principal_id not in responders:
        raise BreakGlassRefused(f"{principal_id} is not an authorized break-glass responder for this repository.")
    if not may_manage:
        raise BreakGlassRefused("Break-glass needs review management permission on this repository.")
    return principal_id


def parse_request(body: Mapping[str, Any]) -> BreakGlassRequest:
    """Validate one break-glass request body; raises :class:`BreakGlassRefused` (400) when invalid."""

    reason = str(body.get("reason") or "").strip()
    if len(reason) < MIN_REASON_CHARS or len(reason) > MAX_REASON_CHARS:
        raise BreakGlassRefused(f"reason must be {MIN_REASON_CHARS} to {MAX_REASON_CHARS} characters", status=400)
    incident = str(body.get("incident_url") or "").strip()
    parsed = urllib.parse.urlparse(incident)
    if len(incident) > MAX_INCIDENT_URL_CHARS or parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise BreakGlassRefused(
            f"incident_url must be an http(s) link of at most {MAX_INCIDENT_URL_CHARS} characters", status=400
        )
    raw = body.get("bypass")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_BYPASSED:
        raise BreakGlassRefused(f"bypass must list 1 to {MAX_BYPASSED} readiness condition ids", status=400)
    bypass: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip() or len(item) > 300:
            raise BreakGlassRefused("bypass entries must be readiness condition ids", status=400)
        if item.strip() not in bypass:
            bypass.append(item.strip())
    revision = str(body.get("expected_revision_id") or "").strip()
    conditions = str(body.get("expected_conditions_hash") or "").strip()
    if not revision or not conditions:
        raise BreakGlassRefused("expected_revision_id and expected_conditions_hash are required", status=400)
    return BreakGlassRequest(
        reason=reason,
        incident_url=incident,
        bypass=tuple(bypass),
        expected_revision_id=revision,
        expected_conditions_hash=conditions,
    )


def bypassed_conditions(conditions: Sequence[Mapping[str, Any]], bypass: Sequence[str]) -> list[dict[str, str]]:
    """The named blockers as recorded; raises (409) unless they are exactly the LemonCrow-owned blockers.

    Every named id must be a current LemonCrow-owned blocker (an SCM-owned
    requirement is bypassed in the SCM, never here), and every LemonCrow-owned
    blocker must be named: break-glass is explicit about everything it skips.
    """

    by_id = {str(row.get("id") or ""): row for row in conditions}
    local = {
        key: row
        for key, row in by_id.items()
        if str(row.get("owner") or "lemoncrow") != "scm"
        and str(row.get("state") or "") not in SATISFIED_STATES
        and key not in _NOT_BYPASSABLE
    }
    unknown = [item for item in bypass if item not in by_id]
    if unknown:
        raise BreakGlassRefused(f"not a current readiness condition: {', '.join(unknown[:4])}", status=409)
    scm = [item for item in bypass if str(by_id[item].get("owner") or "") == "scm"]
    if scm:
        raise BreakGlassRefused(
            f"SCM-owned requirements are bypassed in the SCM itself, never by LemonCrow: {', '.join(scm[:4])}",
            status=409,
        )
    not_blocking = [item for item in bypass if item not in local]
    if not_blocking:
        raise BreakGlassRefused(f"not a current LemonCrow blocker: {', '.join(not_blocking[:4])}", status=409)
    unnamed = [key for key in local if key not in bypass]
    if unnamed:
        labels = ", ".join(str(local[key].get("label") or key) for key in unnamed[:4])
        raise BreakGlassRefused(f"name every LemonCrow blocker to bypass; still blocking: {labels}", status=409)
    return [
        {
            "id": item,
            "label": str(by_id[item].get("label") or ""),
            "state": str(by_id[item].get("state") or ""),
            "reason": str(by_id[item].get("reason") or ""),
        }
        for item in bypass
    ]


def ledger(store: Any) -> Any:
    from lemoncrow.pro.capabilities.review.derived_records import DerivedRecordLedger

    return DerivedRecordLedger(store, RECORD_KIND)


def record_break_glass(
    store: Any,
    *,
    review_id: str,
    revision: Any,
    actor_id: str,
    request: BreakGlassRequest,
    bypassed: Sequence[Mapping[str, str]],
    readiness: Mapping[str, Any],
    follow_up_hours: int,
    now: str,
) -> dict[str, Any]:
    """Store one break-glass record with its follow-up deadline (insert-once)."""

    instant = parse_timestamp(now)
    if instant is None:
        raise ValueError("now must be an aware ISO-8601 instant")
    generation = readiness.get("policy_generation")
    payload = {
        "id": f"bg_{secrets.token_hex(8)}",
        "review_id": review_id,
        "revision_id": revision.id,
        "head_sha": str(getattr(revision, "head_sha", "") or ""),
        "tree_fingerprint": str(getattr(revision, "tree_fingerprint", "") or ""),
        "actor_id": actor_id,
        "actor_type": "human",
        "reason": request.reason,
        "incident_url": request.incident_url,
        "bypassed": [dict(row) for row in bypassed],
        "conditions_hash": str(readiness.get("conditions_hash") or ""),
        "policy_generation": str(generation.get("digest") or "") if isinstance(generation, Mapping) else "",
        "created_at": now,
        "follow_up": {
            "due_at": format_timestamp(instant + timedelta(hours=follow_up_hours)),
            "hours": follow_up_hours,
        },
    }
    _created, stored = ledger(store).create(review_id, revision.id, payload)
    return stored


def records(store: Any, revision_ids: Iterable[str]) -> list[dict[str, Any]]:
    """Every break-glass record on these Snapshots, oldest first."""

    rows = [payload for _revision, _stamp, payload in ledger(store).rows(revision_ids)]
    return sorted(rows, key=lambda row: (str(row.get("created_at") or ""), str(row.get("id") or "")))


def follow_up_state(record: Mapping[str, Any], outcomes: Iterable[Any], *, now: datetime) -> dict[str, Any]:
    """``open``, ``overdue`` or ``completed``: derived from outcomes, never stored.

    Completed by a human LGTM or changes-requested outcome on the landed
    Snapshot, recorded after the break-glass, by someone other than the
    responder (the person who skipped review cannot also close it).
    """

    created = parse_timestamp(record.get("created_at"))
    follow_up = record.get("follow_up") if isinstance(record.get("follow_up"), Mapping) else {}
    assert isinstance(follow_up, Mapping)
    due = parse_timestamp(follow_up.get("due_at"))
    for outcome in outcomes:
        at = parse_timestamp(getattr(outcome, "created_at", ""))
        if (
            getattr(outcome, "revision_id", "") == record.get("revision_id")
            and getattr(outcome, "outcome", "") in FOLLOW_UP_OUTCOMES
            and getattr(outcome, "reviewer_id", "") != record.get("actor_id")
            and at is not None
            and created is not None
            and at >= created
        ):
            return {
                "state": "completed",
                "due_at": str(follow_up.get("due_at") or ""),
                "completed_at": str(getattr(outcome, "created_at", "")),
                "completed_by": str(getattr(outcome, "reviewer_id", "")),
            }
    overdue = due is None or now >= due
    return {
        "state": "overdue" if overdue else "open",
        "due_at": str(follow_up.get("due_at") or ""),
        "completed_at": "",
        "completed_by": "",
    }


def record_wire(record: Mapping[str, Any], outcomes: Iterable[Any], *, now: datetime) -> dict[str, Any]:
    return {
        "id": str(record.get("id") or ""),
        "revision_id": str(record.get("revision_id") or ""),
        "actor_id": str(record.get("actor_id") or ""),
        "reason": str(record.get("reason") or ""),
        "incident_url": str(record.get("incident_url") or ""),
        "bypassed": [dict(row) for row in record.get("bypassed") or () if isinstance(row, Mapping)],
        "created_at": str(record.get("created_at") or ""),
        "policy_generation": str(record.get("policy_generation") or ""),
        "follow_up": follow_up_state(record, outcomes, now=now),
    }


def decision_detail(record: Mapping[str, Any]) -> dict[str, str]:
    """The signed-record decision detail of one break-glass (strings only)."""

    bypassed = [str(row.get("id") or "") for row in record.get("bypassed") or () if isinstance(row, Mapping)]
    follow_up = record.get("follow_up") if isinstance(record.get("follow_up"), Mapping) else {}
    assert isinstance(follow_up, Mapping)
    return {
        "basis": "break_glass",
        "break_glass_id": str(record.get("id") or ""),
        "actor": str(record.get("actor_id") or ""),
        "reason": str(record.get("reason") or ""),
        "incident": str(record.get("incident_url") or ""),
        "bypassed": ",".join(bypassed),
        "conditions_hash": str(record.get("conditions_hash") or ""),
        "policy_generation": str(record.get("policy_generation") or ""),
        "follow_up_due_at": str(follow_up.get("due_at") or ""),
    }


def effective_blockers(conditions: Sequence[Mapping[str, Any]], bypassed: Iterable[str]) -> list[Mapping[str, Any]]:
    """LemonCrow-owned blockers that remain once the named ones are bypassed."""

    skipped = set(bypassed)
    return [
        row
        for row in conditions
        if str(row.get("owner") or "lemoncrow") != "scm"
        and str(row.get("state") or "") not in SATISFIED_STATES
        and str(row.get("id") or "") not in skipped
        and str(row.get("id") or "") not in _NOT_BYPASSABLE
    ]


__all__ = [
    "MAX_BYPASSED",
    "MAX_INCIDENT_URL_CHARS",
    "MAX_REASON_CHARS",
    "MIN_REASON_CHARS",
    "RECORD_KIND",
    "BreakGlassRefused",
    "BreakGlassRequest",
    "authorize",
    "bypassed_conditions",
    "decision_detail",
    "effective_blockers",
    "follow_up_state",
    "parse_request",
    "record_break_glass",
    "record_wire",
    "records",
]

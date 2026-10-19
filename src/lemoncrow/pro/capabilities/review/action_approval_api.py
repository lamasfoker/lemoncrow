"""Reader routes for human approval of risky agent actions (Phase 12 item 14).

``POST /reviews/{id}/agent-approvals`` files a request (the host hook), ``.../claim``
is the hook's poll that consumes an approval, and ``.../decision`` is the human
decision. The server re-classifies every action itself; the hook's classes can
only add to what needs a human, never remove. A decision needs a *human*
principal from :class:`ApprovalPrincipal` -- an embedder that supplies no
authority refuses every decision (fail closed).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status

from lemoncrow.pro.capabilities.review.action_approval import (
    ACTION_KINDS,
    DEFAULT_DECISION_TTL,
    GATED_CLASSES,
    MAX_ACTION_CHARS,
    MAX_APPROVALS_PER_REVIEW,
    MAX_DECISION_TTL,
    MAX_NOTE_CHARS,
    MAX_PENDING_PER_SESSION,
    MIN_DECISION_TTL,
    RISK_LABELS,
    ApprovalPolicy,
    action_allowed_by,
    action_digest,
    approval_ledger,
    approval_wire,
    classify_action,
    effective_status,
    iso,
    new_request,
    normalize_action,
    reusable_approval,
    validate_pattern,
)
from lemoncrow.pro.capabilities.review.derived_records import RecordConflict

PrincipalKind = Literal["human", "agent", "service", "unknown"]


@dataclass(frozen=True, slots=True)
class ApprovalPrincipal:
    """Who is calling, as the embedding server established it."""

    kind: PrincipalKind
    principal_id: str
    reason: str = ""


@dataclass(frozen=True)
class ApprovalDeps:
    store: Any
    development_store: Any
    session: Callable[[str], Any]
    mutable_session: Callable[[str], Any]
    latest: Callable[[Any], Any]
    authority: Callable[[], ApprovalPrincipal] | None
    """The calling principal; ``None`` refuses every decision."""
    policy: Callable[[], ApprovalPolicy | None]
    """This server's configured classes; ``None`` when they cannot be read."""
    separate_requester: bool = True
    """Refuse a decision by the principal that filed the request (hosted: the agent's own credential)."""
    clock: Callable[[], datetime] | None = None
    """Test seam; ``None`` reads ``action_approval.utc_now`` at each call."""


_REQUEST_KEYS = frozenset({"host", "session_id", "kind", "action", "cwd", "classes"})
_CLAIM_KEYS = frozenset({"host", "session_id", "kind", "action", "cwd"})
_DECISION_KEYS = frozenset({"decision", "expected_action_digest", "scope", "pattern", "ttl_seconds", "note"})
_HOST = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")


def _refuse(code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


def _strict(body: Mapping[str, Any], allowed: frozenset[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unsupported approval field(s): {', '.join(unknown)}")


def _agent_identity(body: Mapping[str, Any]) -> tuple[str, str]:
    host = str(body.get("host") or "").strip().lower()
    session_id = str(body.get("session_id") or "").strip()
    if not _HOST.fullmatch(host) or not _SESSION.fullmatch(session_id):
        raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, "host and session_id identify the agent session")
    return host, session_id


def _action(body: Mapping[str, Any]) -> tuple[str, str, str]:
    kind = str(body.get("kind") or "")
    if kind not in ACTION_KINDS:
        raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, f"kind must be one of {', '.join(ACTION_KINDS)}")
    raw = body.get("action")
    if not isinstance(raw, str):
        raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, "action must be the exact command or path")
    action = normalize_action(kind, raw)
    if not action:
        raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, "action must be the exact command or path")
    if len(action) > MAX_ACTION_CHARS:
        # Never truncated: a shortened action would approve something else.
        raise _refuse(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"this action is longer than {MAX_ACTION_CHARS} characters and cannot be approved exactly",
        )
    cwd = body.get("cwd", "")
    if not isinstance(cwd, str) or len(cwd) > 4096:
        raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, "cwd must be a path")
    return kind, action, cwd.strip()


def register_action_approval_routes(router: APIRouter, deps: ApprovalDeps) -> None:
    """Add the agent-approval routes to *router*."""

    store = deps.store
    ledger = approval_ledger(store)

    def now_utc() -> datetime:
        from lemoncrow.pro.capabilities.review import action_approval

        return deps.clock() if deps.clock is not None else action_approval.utc_now()

    def revision_ids(review_id: str) -> list[str]:
        return [item.id for item in store.list_revisions(review_id)]

    def records(review_id: str) -> list[tuple[str, dict[str, Any]]]:
        rows = [(revision_id, payload) for revision_id, _stamp, payload in ledger.rows(revision_ids(review_id))]
        rows.sort(key=lambda item: (str(item[1].get("requested_at") or ""), str(item[1].get("id") or "")))
        return rows

    def find(review_id: str, approval_id: str) -> tuple[str, dict[str, Any]]:
        found = ledger.get(revision_ids(review_id), approval_id)
        if found is None or found[1].get("review_id") != review_id:
            raise _refuse(status.HTTP_404_NOT_FOUND, "no such approval request")
        return found

    def principal() -> ApprovalPrincipal:
        if deps.authority is None:
            return ApprovalPrincipal("unknown", "", "this server has no human approval authority")
        try:
            return deps.authority()
        except Exception as exc:  # an unresolvable caller is never a human
            return ApprovalPrincipal("unknown", "", f"the caller could not be identified ({type(exc).__name__})")

    def policy() -> ApprovalPolicy | None:
        try:
            return deps.policy()
        except Exception:
            return None

    def expire(review_id: str, revision_id: str, record: dict[str, Any]) -> dict[str, Any]:
        """Persist lazy expiry of a pending request (best effort; the effective status already says so)."""

        if record.get("status") != "pending" or effective_status(record, now_utc()) != "expired":
            return record

        def change(current: dict[str, Any]) -> dict[str, Any]:
            if current.get("status") != "pending":
                raise RecordConflict("already decided")
            return {**current, "status": "expired"}

        try:
            return ledger.update(review_id, revision_id, str(record["id"]), change)
        except (RecordConflict, LookupError):
            return record

    @router.get("/reviews/{review_id}/agent-approvals")
    def _get_agent_approvals(review_id: str) -> dict[str, Any]:
        session = deps.session(review_id)
        now = now_utc()
        rows = records(session.id)
        current = policy()
        return {
            "approvals": [approval_wire(payload, now, revision_id=revision_id) for revision_id, payload in rows][::-1][
                :200
            ],
            "total": len(rows),
            "policy": current.to_wire() if current is not None else None,
            "labels": dict(RISK_LABELS),
            "limits": {
                "min_ttl_seconds": MIN_DECISION_TTL,
                "max_ttl_seconds": MAX_DECISION_TTL,
                "default_ttl_seconds": DEFAULT_DECISION_TTL,
            },
        }

    @router.post("/reviews/{review_id}/agent-approvals")
    async def _post_agent_approval(review_id: str, request: Request) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.api import _json_object

        session = deps.mutable_session(review_id)
        body = await _json_object(request)
        _strict(body, _REQUEST_KEYS)
        host, session_id = _agent_identity(body)
        kind, action, cwd = _action(body)
        declared_raw = body.get("classes", [])
        if not isinstance(declared_raw, list) or any(item not in GATED_CLASSES for item in declared_raw):
            raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, f"classes are among {', '.join(GATED_CLASSES)}")
        declared = frozenset(str(item) for item in declared_raw)
        configured = policy()
        found = classify_action(kind, action, repo_root=cwd or str(session.repo_root or ""))
        # The hook's classes only add: what this server classifies as configured
        # also needs a human even when the hook did not say so.
        server_required = configured.required(found) if configured is not None else found
        classes = declared | server_required
        if not classes:
            raise _refuse(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "this action is not in a risk class that needs approval"
            )
        now = now_utc()
        rows = records(session.id)
        payloads = [payload for _revision, payload in rows]
        reusable = reusable_approval(
            payloads, host=host, session_id=session_id, kind=kind, action=action, cwd=cwd, classes=classes, now=now
        )
        if reusable is not None:
            revision_id = next(rev for rev, payload in rows if payload is reusable)
            return {"approval": approval_wire(reusable, now, revision_id=revision_id), "reused": True}
        digest = action_digest(kind, action)
        for revision_id, payload in rows:
            if (
                effective_status(payload, now) == "pending"
                and payload.get("host") == host
                and payload.get("session_id") == session_id
                and payload.get("action_digest") == digest
                and payload.get("cwd") == cwd
                and classes <= frozenset(payload.get("classes") or ())
            ):
                return {"approval": approval_wire(payload, now, revision_id=revision_id), "reused": True}
        pending = sum(
            1
            for _revision, payload in rows
            if effective_status(payload, now) == "pending"
            and payload.get("host") == host
            and payload.get("session_id") == session_id
        )
        if pending >= MAX_PENDING_PER_SESSION:
            raise _refuse(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"this agent session already has {pending} approval requests waiting; decide or let them expire",
            )
        if len(rows) >= MAX_APPROVALS_PER_REVIEW:
            raise _refuse(
                status.HTTP_409_CONFLICT,
                f"this Review already holds {len(rows)} approval requests, the most it keeps; start a new Review",
            )
        revision = deps.latest(session)
        requester = principal()
        record = new_request(
            approval_id="apr_" + uuid.uuid4().hex[:24],
            review_id=session.id,
            host=host,
            session_id=session_id,
            kind=kind,
            action=action,
            cwd=cwd,
            classes=classes,
            requested_by=requester.principal_id or f"agent:{host}:{session_id}",
            now=now,
        )
        _created, stored = ledger.create(session.id, revision.id, record)
        return {"approval": approval_wire(stored, now, revision_id=revision.id), "reused": False}

    @router.post("/reviews/{review_id}/agent-approvals/{approval_id}/claim")
    async def _post_agent_approval_claim(review_id: str, approval_id: str, request: Request) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.api import _json_object

        session = deps.session(review_id)
        body = await _json_object(request)
        _strict(body, _CLAIM_KEYS)
        host, session_id = _agent_identity(body)
        kind, action, cwd = _action(body)
        revision_id, record = find(session.id, approval_id)
        if record.get("host") != host or record.get("session_id") != session_id:
            raise _refuse(status.HTTP_403_FORBIDDEN, "this approval belongs to another agent session")
        record = expire(session.id, revision_id, record)
        now = now_utc()
        state = effective_status(record, now)
        if state != "approved":
            return {"approval": approval_wire(record, now, revision_id=revision_id), "granted": False, "reason": state}
        if not action_allowed_by(record, kind=kind, action=action, cwd=cwd):
            return {
                "approval": approval_wire(record, now, revision_id=revision_id),
                "granted": False,
                "reason": "the approved action differs from this one",
            }
        if record.get("scope") == "pattern":
            return {"approval": approval_wire(record, now, revision_id=revision_id), "granted": True, "reason": ""}

        def claim(current: dict[str, Any]) -> dict[str, Any]:
            if effective_status(current, now_utc()) != "approved" or current.get("claimed_at"):
                raise RecordConflict("this one-time approval was already used")
            return {**current, "claimed_at": iso(now_utc())}

        try:
            claimed = ledger.update(session.id, revision_id, approval_id, claim)
        except RecordConflict as exc:
            return {
                "approval": approval_wire(record, now, revision_id=revision_id),
                "granted": False,
                "reason": str(exc),
            }
        return {
            "approval": approval_wire(claimed, now_utc(), revision_id=revision_id),
            "granted": True,
            "reason": "",
        }

    @router.post("/reviews/{review_id}/agent-approvals/{approval_id}/decision")
    async def _post_agent_approval_decision(review_id: str, approval_id: str, request: Request) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.api import _json_object

        caller = principal()
        if caller.kind != "human" or not caller.principal_id:
            raise _refuse(
                status.HTTP_403_FORBIDDEN,
                "only a signed-in human can approve or deny an agent action; agent and service credentials are "
                f"refused{': ' + caller.reason if caller.reason else ''}",
            )
        session = deps.mutable_session(review_id)
        body = await _json_object(request)
        _strict(body, _DECISION_KEYS)
        decision = body.get("decision")
        if decision not in {"approve", "deny"}:
            raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, "decision is approve or deny")
        expected = str(body.get("expected_action_digest") or "")
        if not expected:
            raise _refuse(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "a decision must name the exact action it decides (digest)"
            )
        note = body.get("note", "")
        if not isinstance(note, str) or len(note) > MAX_NOTE_CHARS:
            raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, f"note is at most {MAX_NOTE_CHARS} characters")
        revision_id, record = find(session.id, approval_id)
        scope = ""
        pattern = ""
        ttl = 0
        if decision == "approve":
            scope = str(body.get("scope") or "once")
            if scope not in {"once", "pattern"}:
                raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, "scope is once or pattern")
            raw_ttl = body.get("ttl_seconds", DEFAULT_DECISION_TTL)
            if not isinstance(raw_ttl, int) or isinstance(raw_ttl, bool):
                raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, "ttl_seconds is a whole number of seconds")
            if not MIN_DECISION_TTL <= raw_ttl <= MAX_DECISION_TTL:
                raise _refuse(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"an approval lasts between {MIN_DECISION_TTL} and {MAX_DECISION_TTL} seconds",
                )
            ttl = raw_ttl
            if scope == "pattern":
                raw_pattern = body.get("pattern")
                if not isinstance(raw_pattern, str) or len(raw_pattern) > MAX_ACTION_CHARS:
                    raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, "a pattern approval names its pattern")
                try:
                    pattern = validate_pattern(str(record.get("kind")), raw_pattern, str(record.get("action")))
                except ValueError as exc:
                    raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
            elif "pattern" in body and body.get("pattern"):
                raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, "a one-time approval takes no pattern")
        elif any(key in body for key in ("scope", "pattern", "ttl_seconds")):
            raise _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, "a denial takes no scope, pattern or ttl")

        def change(current: dict[str, Any]) -> dict[str, Any]:
            now = now_utc()
            if effective_status(current, now) != "pending":
                raise RecordConflict(f"this request is already {effective_status(current, now)}")
            if current.get("action_digest") != expected:
                raise RecordConflict("the request names a different action than the one you reviewed")
            if deps.separate_requester and current.get("requested_by") == caller.principal_id:
                raise PermissionError("the principal whose agent filed this request cannot decide it")
            return {
                **current,
                "status": "approved" if decision == "approve" else "denied",
                "decided_by": caller.principal_id,
                "decided_kind": caller.kind,
                "decided_at": iso(now),
                "scope": scope,
                "pattern": pattern,
                "valid_until": iso(now + timedelta(seconds=ttl)) if decision == "approve" else "",
                "note": note.strip(),
            }

        try:
            decided = ledger.update(session.id, revision_id, approval_id, change)
        except PermissionError as exc:
            raise _refuse(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        except RecordConflict as exc:
            raise _refuse(status.HTTP_409_CONFLICT, str(exc)) from exc
        recorded = _record_decision(deps, session.id, decided)
        return {
            "approval": approval_wire(decided, now_utc(), revision_id=revision_id),
            "development_recorded": recorded,
        }


def _record_decision(deps: ApprovalDeps, review_id: str, record: Mapping[str, Any]) -> bool:
    """Append the decision to the Development ledger (approved/denied, by whom, exact action)."""

    from lemoncrow.pro.capabilities.review.development import DevelopmentUnavailable, record_approval_decision

    development = deps.development_store
    if development is None or not development.available():
        return False
    approved = record.get("status") == "approved"
    label = RISK_LABELS.get(str(record.get("risk_class") or ""), "Agent action")
    scope = f"pattern {record.get('pattern')}" if record.get("scope") == "pattern" else "this exact action, once"
    detail_lines = [
        f"{'Approved' if approved else 'Denied'} by {record.get('decided_by')} at {record.get('decided_at')}",
        f"Action ({record.get('kind')}): {record.get('action')}",
    ]
    if approved:
        detail_lines.append(f"Scope: {scope}; valid until {record.get('valid_until')}")
    if record.get("note"):
        detail_lines.append(f"Note: {record.get('note')}")
    try:
        record_approval_decision(
            development,
            review_id,
            approval_id=str(record.get("id") or ""),
            host=str(record.get("host") or ""),
            session_id=str(record.get("session_id") or ""),
            status="approved" if approved else "denied",
            summary=f"{'Approved' if approved else 'Denied'} {label.lower()}: {str(record.get('action') or '')[:200]}",
            detail="\n".join(detail_lines),
            action_digest=str(record.get("action_digest") or ""),
            at=str(record.get("decided_at") or ""),
        )
    except DevelopmentUnavailable:
        return False
    return True


__all__ = ["ApprovalDeps", "ApprovalPrincipal", "register_action_approval_routes"]

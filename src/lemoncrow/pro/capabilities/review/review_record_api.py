"""Signed review record routes (Phase 10 item 19).

The export lives in the existing Review Status sheet; these routes add no
authority of their own beyond the composition's :class:`RecordSigningAuthority`
(hosted: organization keys in the control plane, requiring ``review:manage``;
local: the reviewer's opted-in personal key).

* ``GET  /reviews/{id}/record`` -- whether a decision exists, what an export
  would sign with, and the records already emitted;
* ``POST /reviews/{id}/record`` -- emit a record for the latest Snapshot's
  decision: ``{"signing": "auto"}`` signs or refuses (409, never silently
  unsigned); ``{"signing": "unsigned"}`` emits an explicitly UNSIGNED record;
* ``GET  /reviews/{id}/records/{record_id}`` -- the DSSE envelope file;
* ``GET  /reviews/{id}/records/{record_id}/verify`` -- the offline verifier's
  verdict against the published keys, plus a recomputation of every stored
  section from the store.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Final, cast

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response

from lemoncrow.pro.capabilities.review.record_signing import (
    RecordSigner,
    RecordSigningAuthority,
    RecordSigningUnavailable,
    SignedReviewRecord,
    SigningStatus,
    get_record,
    list_records,
    sign_review_record,
    signing_label,
)
from lemoncrow.pro.capabilities.review.record_verify import RecordVerification, parse_key_set, verify_record
from lemoncrow.pro.capabilities.review.review_record import (
    RecordContext,
    RecordSchemaError,
    ReviewDecision,
    SigningIdentity,
    build_statement,
    compare_with_store,
    current_decision,
    statement_digest,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewActivityEvent, ReviewRevision, ReviewSession

_LOG = logging.getLogger(__name__)
_SIGNING_CHOICES = frozenset({"auto", "unsigned"})


@dataclass(frozen=True, slots=True)
class RecordDeps:
    """What the routes need from the Review API (sessions are already repository-scoped there)."""

    store: Any
    session: Callable[[str], ReviewSession]
    latest: Callable[[ReviewSession], ReviewRevision]
    display_number: Callable[[ReviewRevision], int]
    #: The readiness core's wire for the latest Snapshot evaluated at one instant.
    readiness_at: Callable[[ReviewSession, str], Mapping[str, Any]]
    #: The effective policy generation wire of one Snapshot (no evaluation).
    policy_generation: Callable[[ReviewSession, ReviewRevision], Mapping[str, Any]]
    #: The Snapshot's effective run plan wire, or ``None`` without a planner.
    run_plan: Callable[[ReviewSession, ReviewRevision], Mapping[str, Any] | None]
    authority: RecordSigningAuthority
    #: Durable repository identity named in the record (never a local path).
    repository: Callable[[ReviewSession], str]
    now: Callable[[], str]


def _record_context(
    deps: RecordDeps, session: ReviewSession, revision: ReviewRevision, issued_at: str
) -> RecordContext:
    issuer_kind, issuer_id = deps.authority.issuer(session)
    exporter_id, exporter_type = deps.authority.exporter(session)
    return RecordContext(
        store=deps.store,
        session=session,
        revision=revision,
        snapshot_number=deps.display_number(revision),
        readiness=deps.readiness_at(session, issued_at),
        run_plan=deps.run_plan(session, revision),
        issuer_kind=issuer_kind,
        issuer_id=issuer_id,
        repository=deps.repository(session),
        exported_by_id=exporter_id,
        exported_by_type=exporter_type,
    )


def export_record(
    deps: RecordDeps,
    session: ReviewSession,
    *,
    signing: str = "auto",
    decision: ReviewDecision | None = None,
) -> SignedReviewRecord:
    """Emit one record for *session*'s latest Snapshot (the shared export/landing/merge path).

    *decision* defaults to the stored decision (:func:`current_decision`);
    P10-LAND passes its landing decision. Raises ``LookupError`` when no
    decision exists and :class:`RecordSigningUnavailable` when a signature was
    asked for and cannot be produced (audited as a refusal either way).
    """

    if signing not in _SIGNING_CHOICES:
        raise ValueError("signing must be auto or unsigned")
    revision = deps.latest(session)
    chosen = decision or current_decision(deps.store, session, revision)
    if chosen is None:
        raise LookupError(
            "no decision exists for this Snapshot yet; record an approval or changes-requested outcome, "
            "or land the Change, before exporting a review record"
        )
    signer = None
    if signing == "auto":
        try:
            signer = deps.authority.signer(session)
        except RecordSigningUnavailable as exc:
            deps.authority.audit(
                session, "review.record.refuse", allowed=False, detail={"code": exc.code, "reason": exc.reason}
            )
            raise
    issued_at = deps.now()
    try:
        record = sign_review_record(
            _record_context(deps, session, revision, issued_at), chosen, signer=signer, issued_at=issued_at
        )
    except RecordSigningUnavailable as exc:
        deps.authority.audit(
            session, "review.record.refuse", allowed=False, detail={"code": exc.code, "reason": exc.reason}
        )
        raise
    exporter_id, exporter_type = deps.authority.exporter(session)
    deps.store.record_activity(
        ReviewActivityEvent(
            id="",
            review_id=session.id,
            revision_id=revision.id,
            kind="record.exported",
            actor_id=exporter_id,
            actor_type=cast(Any, exporter_type if exporter_type in {"human", "agent"} else "unknown"),
            subject_type="review_record",
            subject_id=record.record_id,
            summary=signing_label(record.signing_state, record.signing_mode, record.key_id)[:500],
            detail_json=json.dumps(
                {
                    "record_id": record.record_id,
                    "payload_digest": record.payload_digest,
                    "signing_state": record.signing_state,
                    "signing_mode": record.signing_mode,
                    "key_id": record.key_id,
                    "decision_kind": record.decision_kind,
                    "issued_at": record.issued_at,
                },
                sort_keys=True,
            ),
        )
    )
    deps.authority.audit(
        session,
        "review.record.sign" if record.signed else "review.record.unsigned",
        allowed=True,
        detail={"record_id": record.record_id, "key_id": record.key_id, "signing_mode": record.signing_mode},
    )
    return record


#: Who ``exported_by`` names when a provider-observed merge (not a person) emitted the record.
MERGE_OBSERVER: Final[str] = "lemoncrow-merge-watch"


class _ObservedMerge:
    """The composition's signing authority, with the observed merge as the exporter."""

    def __init__(self, inner: RecordSigningAuthority, observer: str) -> None:
        self._inner = inner
        self._observer = observer

    def status(self, session: ReviewSession) -> SigningStatus:
        return self._inner.status(session)

    def signer(self, session: ReviewSession) -> RecordSigner:
        return self._inner.signer(session)

    def public_keys(self, session: ReviewSession) -> Mapping[str, Any]:
        return self._inner.public_keys(session)

    def exporter(self, session: ReviewSession) -> tuple[str, str]:
        del session
        return self._observer, "system"

    def issuer(self, session: ReviewSession) -> tuple[str, str]:
        return self._inner.issuer(session)

    def audit(self, session: ReviewSession, event: str, *, allowed: bool, detail: Mapping[str, str]) -> None:
        self._inner.audit(session, event, allowed=allowed, detail=detail)


def _merge_emitted(record: SignedReviewRecord) -> bool:
    """A record the merge observation emitted (a landing decision's own record never counts).

    Told apart by who exported it, never by the decision: a merge that rests on
    an auto-approval records that landing decision too, and is still the merge's.
    """

    predicate = record.statement.get("predicate")
    exporter = predicate.get("exported_by") if isinstance(predicate, Mapping) else None
    return isinstance(exporter, Mapping) and exporter.get("type") == "system" and exporter.get("id") != LANDING_OBSERVER


def _decision_wire(record: SignedReviewRecord) -> Mapping[str, Any]:
    predicate = record.statement.get("predicate")
    decision = predicate.get("decision") if isinstance(predicate, Mapping) else None
    return decision if isinstance(decision, Mapping) else {}


#: Who ``exported_by`` names on the record of a landing decision (P10-LAND).
LANDING_OBSERVER: Final[str] = "lemoncrow-landing"


def emit_landing_record(deps: RecordDeps, session: ReviewSession, decision: ReviewDecision) -> dict[str, Any]:
    """Sign the record of one landing decision (auto-approval, break-glass); never unsigned.

    Idempotent per decision (``reference`` and outcome). Without a usable
    signing key nothing is emitted: the refusal is audited and the result says
    so (an explicit export can still follow); it never suppresses the merge
    record, which is a separate observation.
    """

    if decision.kind != "landing":
        raise ValueError("only a landing decision is recorded here")
    for existing in list_records(deps.store, session.id):
        seen = _decision_wire(existing)
        if (
            seen.get("kind") == "landing"
            and seen.get("reference") == decision.reference
            and seen.get("outcome") == decision.outcome
        ):
            return {"status": "already_recorded", "record_id": existing.record_id}
    observed = replace(deps, authority=_ObservedMerge(deps.authority, LANDING_OBSERVER))
    status = observed.authority.status(session)
    if not status.available:
        observed.authority.audit(
            session, "review.record.refuse", allowed=False, detail={"code": status.code, "reason": status.reason}
        )
        return {"status": "not_signed", "code": status.code}
    try:
        record = export_record(observed, session, signing="auto", decision=decision)
    except RecordSigningUnavailable as exc:
        return {"status": "not_signed", "code": exc.code}
    return {"status": "recorded", "record_id": record.record_id, "key_id": record.key_id}


def emit_on_provider_merge(
    deps: RecordDeps, session: ReviewSession, *, observer: str = MERGE_OBSERVER
) -> dict[str, Any]:
    """Automatic record of a provider-observed merge: sign it, or note why not; never unsigned.

    Idempotent per Snapshot: a record this path already emitted, or an absence
    it already noted, is the answer to every later observation of that merge.
    Without a decision nothing happens. Without a usable signing key nothing is
    emitted automatically -- the absence is Review activity (``record.not_emitted``)
    and a refusal audit; an explicit export can still follow.
    """

    revision = deps.latest(session)
    for existing in list_records(deps.store, session.id):
        if existing.revision_id == revision.id and _merge_emitted(existing):
            return {"status": "already_recorded", "record_id": existing.record_id}
    noted = deps.store.list_activity_events(session.id, kinds=("record.not_emitted",), newest_first=True, limit=50)
    if any(event.revision_id == revision.id for event in noted):
        return {"status": "already_noted"}
    decision = current_decision(deps.store, session, revision)
    if decision is None:
        return {"status": "no_decision"}
    observed = replace(deps, authority=_ObservedMerge(deps.authority, observer))
    status = observed.authority.status(session)
    if not status.available:
        observed.authority.audit(
            session, "review.record.refuse", allowed=False, detail={"code": status.code, "reason": status.reason}
        )
        return _note_not_emitted(deps, session, revision, observer, code=status.code, reason=status.reason)
    try:
        record = export_record(observed, session, signing="auto", decision=decision)
    except RecordSigningUnavailable as exc:
        return _note_not_emitted(deps, session, revision, observer, code=exc.code, reason=exc.reason)
    return {"status": "recorded", "record_id": record.record_id, "key_id": record.key_id}


def _note_not_emitted(
    deps: RecordDeps, session: ReviewSession, revision: ReviewRevision, observer: str, *, code: str, reason: str
) -> dict[str, Any]:
    deps.store.record_activity(
        ReviewActivityEvent(
            id="",
            review_id=session.id,
            revision_id=revision.id,
            kind="record.not_emitted",
            actor_id=observer,
            subject_type="review_record",
            summary=f"No review record was emitted for the merge: {reason}"[:500],
            detail_json=json.dumps({"code": code, "trigger": "provider_merge"}, sort_keys=True),
        )
    )
    return {"status": "not_signed", "code": code}


def reproduced_digest(deps: RecordDeps, session: ReviewSession, record: SignedReviewRecord) -> str:
    """Rebuild *record*'s payload from the store and digest it (``""``: not reproducible here).

    Everything is recomputed from stored rows as of the recorded instant except
    what the signer asserts rather than the store holds: the issuer, exporter,
    signing identity and the decision wire (the decision is checked against
    its stored row separately by :func:`compare_with_store`). Readiness can
    only be re-evaluated for the latest Snapshot, so an older Snapshot's record
    is not reproducible here.
    """

    predicate = record.statement.get("predicate")
    if not isinstance(predicate, Mapping):
        return ""
    revision = deps.store.get_revision(record.revision_id)
    if revision is None or revision.review_id != session.id or deps.latest(session).id != revision.id:
        return ""
    readiness = predicate.get("readiness")
    issuer = predicate.get("issuer")
    exporter = predicate.get("exported_by")
    decision = predicate.get("decision")
    signing = predicate.get("signing")
    if not all(isinstance(item, Mapping) for item in (readiness, issuer, exporter, decision, signing)):
        return ""
    assert isinstance(readiness, Mapping) and isinstance(issuer, Mapping) and isinstance(exporter, Mapping)
    assert isinstance(decision, Mapping) and isinstance(signing, Mapping)
    evaluated_at = str(readiness.get("evaluated_at") or "")
    if not evaluated_at:
        return ""
    try:
        context = RecordContext(
            store=deps.store,
            session=session,
            revision=revision,
            snapshot_number=deps.display_number(revision),
            readiness=deps.readiness_at(session, evaluated_at),
            run_plan=deps.run_plan(session, revision),
            issuer_kind=str(issuer.get("kind") or ""),
            issuer_id=str(issuer.get("id") or ""),
            repository=deps.repository(session),
            exported_by_id=str(exporter.get("id") or ""),
            exported_by_type=str(exporter.get("type") or ""),
        )
        rebuilt = build_statement(
            context,
            ReviewDecision.from_wire(decision),
            signing=SigningIdentity.from_wire(signing),
            issued_at=str(predicate.get("issued_at") or ""),
        )
    except (RecordSchemaError, ValueError, TypeError):
        return ""
    return statement_digest(rebuilt)


def verify_stored_record(deps: RecordDeps, session: ReviewSession, record: SignedReviewRecord) -> dict[str, Any]:
    """The offline verdict against the published keys plus a store recomputation of every section."""

    try:
        keys = parse_key_set(deps.authority.public_keys(session))
        verification = verify_record(record.envelope, keys=keys)
    except ValueError as exc:
        verification = RecordVerification("invalid", (f"the published key set is unusable: {exc}",))
    revision = deps.store.get_revision(record.revision_id)
    sections: dict[str, str] = {}
    if revision is not None and revision.review_id == session.id and record.statement:
        latest = deps.latest(session)
        readiness = record.statement.get("predicate", {}).get("readiness", {})
        evaluated_at = str(readiness.get("evaluated_at") or "") if isinstance(readiness, Mapping) else ""
        readiness_at = deps.readiness_at(session, evaluated_at) if latest.id == revision.id and evaluated_at else None
        sections = compare_with_store(
            record.statement,
            store=deps.store,
            session=session,
            revision=revision,
            snapshot_number=deps.display_number(revision),
            current_policy_generation=deps.policy_generation(session, revision),
            readiness_at=readiness_at,
            run_plan=deps.run_plan(session, revision),
        )
    else:
        sections = {"snapshot": "mismatch"}
    rebuilt = reproduced_digest(deps, session, record)
    return {
        "verification": verification.to_wire(),
        "store": sections,
        "matches_store": bool(sections) and all(value != "mismatch" for value in sections.values()),
        # The payload digest recomputed from the store ("" when not reproducible here).
        "reproduced_digest": rebuilt,
        "reproduced": bool(rebuilt) and rebuilt == record.payload_digest,
    }


def register_review_record_routes(router: APIRouter, deps: RecordDeps) -> None:
    from starlette.concurrency import run_in_threadpool

    from lemoncrow.pro.capabilities.review.api import _json_object

    def _status(review_id: str) -> dict[str, Any]:
        session = deps.session(review_id)
        revision = deps.latest(session)
        decision = current_decision(deps.store, session, revision)
        signing = deps.authority.status(session)
        return {
            "review_id": session.id,
            "snapshot_id": revision.id,
            "snapshot_number": deps.display_number(revision),
            "decision": decision.to_wire() if decision is not None else None,
            "signing": signing.to_wire(),
            "records": [item.summary() for item in list_records(deps.store, session.id)],
        }

    @router.get("/reviews/{review_id}/record")
    async def _get_record_status(review_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], await run_in_threadpool(_status, review_id))

    @router.post("/reviews/{review_id}/record", status_code=status.HTTP_201_CREATED)
    async def _post_record(review_id: str, request: Request) -> dict[str, Any]:
        body = await _json_object(request)
        signing = str(body.get("signing") or "auto")
        if signing not in _SIGNING_CHOICES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="signing must be auto or unsigned")
        session = deps.session(review_id)
        if session.status == "archived":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="restore this discarded Change before exporting a record"
            )
        expected = str(body.get("expected_revision_id") or "").strip()

        def _export() -> dict[str, Any]:
            revision = deps.latest(session)
            if expected and expected != revision.id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="a newer Snapshot exists; reload Review Status before exporting its record",
                )
            try:
                record = export_record(deps, session, signing=signing)
            except LookupError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            except RecordSigningUnavailable as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": exc.code,
                        "message": exc.reason,
                        "signing": deps.authority.status(session).to_wire(),
                    },
                ) from exc
            except RecordSchemaError as exc:
                _LOG.warning("review record for %s failed its schema", session.id, exc_info=True)
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            verdict = verify_stored_record(deps, session, record)
            return {"record": record.summary(), "envelope": dict(record.envelope), **verdict}

        return cast(dict[str, Any], await run_in_threadpool(_export))

    def _stored(review_id: str, record_id: str) -> tuple[ReviewSession, SignedReviewRecord]:
        session = deps.session(review_id)
        record = get_record(deps.store, session.id, record_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such review record")
        return session, record

    @router.get("/reviews/{review_id}/records/{record_id}")
    async def _get_record(review_id: str, record_id: str) -> Response:
        _session, record = await run_in_threadpool(_stored, review_id, record_id)
        body = json.dumps(dict(record.envelope), sort_keys=True, indent=2) + "\n"
        suffix = "dsse.json" if record.signed else "unsigned.dsse.json"
        return Response(
            content=body,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="review-record-{record.record_id}.{suffix}"',
                "Cache-Control": "no-store",
                "X-LemonCrow-Record-Signing": record.signing_state,
            },
        )

    @router.get("/reviews/{review_id}/records/{record_id}/verify")
    async def _verify_record(review_id: str, record_id: str) -> dict[str, Any]:
        def _verify() -> dict[str, Any]:
            session, record = _stored(review_id, record_id)
            return {"record": record.summary(), **verify_stored_record(deps, session, record)}

        return cast(dict[str, Any], await run_in_threadpool(_verify))


__all__ = [
    "LANDING_OBSERVER",
    "RecordDeps",
    "emit_landing_record",
    "export_record",
    "register_review_record_routes",
    "reproduced_digest",
    "verify_stored_record",
]

"""Signed review record: the attested content (Phase 10 item 19).

A review record is an in-toto Statement v1 whose predicate binds one decided
Review Snapshot to everything the decision rested on: Snapshot identity
(review, revision, head/base SHAs, tree and source digests), evidence and check
results with their executor and attestation classes, human judgments and the
outcome, waivers, the readiness state and policy generation it was evaluated
under, the run plan digest, and the time and actor of the decision.

This module is pure content: no key material, no signing. The payload is
canonical JSON (sorted keys, no insignificant whitespace, UTF-8, no floats), so
its digest is reproducible from stored state. Rows are read *as of* the record
instant (``created_at <= as_of``) from append-only tables, so a later outcome
or evidence row never makes an earlier record disagree with the store; a
section that does disagree names exactly what changed.

Signing lives in :mod:`.record_signing`, verification in :mod:`.record_verify`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal

from lemoncrow.pro.capabilities.review.freshness import parse_timestamp
from lemoncrow.pro.capabilities.review.readiness import conditions_digest
from lemoncrow.pro.capabilities.review.session_models import (
    ReviewDecisionEvent,
    ReviewEvidence,
    ReviewMarkEvent,
    ReviewOutcome,
    ReviewRevision,
    ReviewSession,
)

STATEMENT_TYPE: Final[str] = "https://in-toto.io/Statement/v1"
#: Versioned predicate type. A breaking predicate change is a new ``/vN`` URI.
PREDICATE_TYPE: Final[str] = "https://lemoncrow.dev/attestations/review-record/v1"
#: DSSE ``payloadType`` of an in-toto Statement.
PAYLOAD_TYPE: Final[str] = "application/vnd.in-toto+json"
RECORD_SCHEMA_VERSION: Final[int] = 1

SigningMode = Literal["organization_key", "user_key", "sigstore", "unsigned"]
SIGNING_MODES: Final[tuple[str, ...]] = ("organization_key", "user_key", "sigstore", "unsigned")
DecisionKind = Literal["human_outcome", "landing", "provider_merge"]
DECISION_KINDS: Final[tuple[str, ...]] = ("human_outcome", "landing", "provider_merge")
#: Stated inside every unsigned payload so no consumer can mistake it for a signed one.
UNSIGNED_NOTICE: Final[str] = (
    "UNSIGNED: this record carries no signature and is not evidence of who produced it or that it is unaltered."
)
#: The relationship kind under which records are persisted on their Change.
RECORD_RELATIONSHIP_KIND: Final[str] = "review_record"

_DECISION_OUTCOMES: Final[Mapping[str, str]] = {"lgtm": "approved", "changes_requested": "changes_requested"}


class RecordSchemaError(ValueError):
    """A statement that does not satisfy the review-record predicate schema."""


def canonical_json(value: object) -> bytes:
    """The one byte encoding of a record payload (floats and NaN are refused)."""

    _refuse_floats(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _refuse_floats(value: object) -> None:
    if isinstance(value, float):
        raise RecordSchemaError("review record payloads carry no floating-point numbers")
    if isinstance(value, Mapping):
        for item in value.values():
            _refuse_floats(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _refuse_floats(item)


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _digest_of(value: object) -> str:
    return sha256_digest(canonical_json(value))


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    """The decision a record is emitted after: who decided what, when.

    ``kind``: ``human_outcome`` (a reviewer's LGTM / changes requested on the
    Snapshot), ``landing`` (P10-LAND's landing decision, supplied through
    :func:`.record_signing.sign_review_record`) or ``provider_merge`` (the SCM
    merged the reviewed head). ``reference`` names the stored row the decision
    is (an outcome id, a landing relationship id).
    """

    kind: str
    outcome: str
    actor_id: str
    actor_type: str
    decided_at: str
    reference: str = ""
    detail: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in DECISION_KINDS:
            raise RecordSchemaError(f"unknown decision kind {self.kind!r}")
        if not self.outcome or not self.actor_id or parse_timestamp(self.decided_at) is None:
            raise RecordSchemaError("a decision names its outcome, actor and an aware decision time")

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "outcome": self.outcome,
            "actor": {"id": self.actor_id, "type": self.actor_type or "unknown"},
            "decided_at": self.decided_at,
            "reference": self.reference,
            "detail": {str(key): str(value) for key, value in sorted(self.detail.items())},
        }

    @classmethod
    def from_wire(cls, wire: Mapping[str, Any]) -> ReviewDecision:
        actor = _mapping(wire.get("actor"))
        return cls(
            kind=str(wire.get("kind") or ""),
            outcome=str(wire.get("outcome") or ""),
            actor_id=str(actor.get("id") or ""),
            actor_type=str(actor.get("type") or ""),
            decided_at=str(wire.get("decided_at") or ""),
            reference=str(wire.get("reference") or ""),
            detail={str(key): str(value) for key, value in _mapping(wire.get("detail")).items()},
        )


@dataclass(frozen=True, slots=True)
class SigningIdentity:
    """Who signs (or that nobody does), stated inside the payload itself.

    A payload that says ``unsigned`` but carries a signature, or says signed but
    carries none, fails verification: stripping a signature can never turn a
    signed record into an "unsigned but otherwise fine" one, and an unsigned
    record can never be presented as signed.
    """

    mode: str
    key_id: str = ""
    algorithm: str = ""
    issuer_kind: str = ""
    issuer_id: str = ""

    def __post_init__(self) -> None:
        if self.mode not in SIGNING_MODES:
            raise RecordSchemaError(f"unknown signing mode {self.mode!r}")
        if self.mode != "unsigned" and (not self.key_id or not self.algorithm):
            raise RecordSchemaError("a signing identity names its key id and algorithm")
        if self.mode == "unsigned" and (self.key_id or self.algorithm):
            raise RecordSchemaError("an unsigned record names no key")

    @property
    def signed(self) -> bool:
        return self.mode != "unsigned"

    def to_wire(self) -> dict[str, Any]:
        if not self.signed:
            return {"state": "unsigned", "mode": "unsigned", "key_id": "", "algorithm": "", "notice": UNSIGNED_NOTICE}
        return {"state": "signed", "mode": self.mode, "key_id": self.key_id, "algorithm": self.algorithm, "notice": ""}

    @classmethod
    def from_wire(cls, wire: Mapping[str, Any]) -> SigningIdentity:
        return cls(
            mode=str(wire.get("mode") or ""),
            key_id=str(wire.get("key_id") or ""),
            algorithm=str(wire.get("algorithm") or ""),
        )


UNSIGNED: Final[SigningIdentity] = SigningIdentity(mode="unsigned")


@dataclass(frozen=True, slots=True)
class RecordFacts:
    """Stored rows a record binds, read as of one instant."""

    evidence: tuple[ReviewEvidence, ...]
    waivers: tuple[ReviewEvidence, ...]
    outcomes: tuple[ReviewOutcome, ...]
    mark_events: tuple[ReviewMarkEvent, ...]
    decision_events: tuple[ReviewDecisionEvent, ...]


def _at_or_before(created_at: str, as_of: datetime) -> bool:
    instant = parse_timestamp(created_at)
    # An unreadable row timestamp is never silently included or dropped: it
    # counts as "before" so the record still binds it and a reader sees it.
    return instant is None or instant <= as_of


def gather_record_facts(store: Any, session: ReviewSession, revision: ReviewRevision, *, as_of: str) -> RecordFacts:
    """Read the rows a record binds from the store, as of *as_of* (append-only tables)."""

    instant = parse_timestamp(as_of)
    if instant is None:
        raise RecordSchemaError("as_of must be an aware ISO-8601 instant")
    every_evidence = tuple(item for item in store.list_evidence(session.id) if _at_or_before(item.created_at, instant))
    evidence = tuple(item for item in every_evidence if item.revision_id == revision.id)
    # A review-scoped waiver can be granted on an earlier Snapshot and still
    # apply; every waiver on the Change is bound, each with its own Snapshot.
    waivers = tuple(item for item in every_evidence if item.waived_requirement_key)
    outcomes = tuple(item for item in store.list_outcomes(session.id) if _at_or_before(item.created_at, instant))
    mark_events = tuple(
        item for item in store.list_review_mark_events(session.id) if _at_or_before(item.created_at, instant)
    )
    decision_events = tuple(
        item
        for item in store.list_decision_events(session.id, reviewer_id=None)
        if _at_or_before(item.created_at, instant)
    )
    return RecordFacts(
        evidence=evidence,
        waivers=waivers,
        outcomes=outcomes,
        mark_events=mark_events,
        decision_events=decision_events,
    )


def current_decision(store: Any, session: ReviewSession, revision: ReviewRevision) -> ReviewDecision | None:
    """The decision on *revision* a record may be emitted after, or ``None``.

    A provider-observed merge of this Snapshot wins (it is the landing that
    happened); otherwise the newest human LGTM / changes-requested outcome on
    this exact Snapshot; otherwise the policy auto-approval in force on it, or
    the break-glass its landing rests on (P10-LAND). A ``comment`` outcome is
    not a decision.
    """

    from lemoncrow.pro.capabilities.review.auto_approval import (
        AUTO_APPROVAL_ACTOR,
        IN_FORCE_STATES,
        auto_approval_relationship,
        decision_for_record,
    )
    from lemoncrow.pro.capabilities.review.landing import landing_metadata, landing_relationship

    approval = auto_approval_relationship(store, session.id)
    approved_here = (
        approval is not None
        and approval.status in IN_FORCE_STATES
        and str(landing_metadata(approval).get("revision_id") or "") == revision.id
    )
    landing = landing_relationship(store, session.id)
    if landing is not None and landing.status == "merged":
        metadata = landing_metadata(landing)
        merged_revision = str(metadata.get("revision_id") or "")
        if merged_revision == revision.id:
            detail = {"mechanism": str(metadata.get("mechanism") or "")}
            # P10-LAND: what the landing rested on is part of the merge record.
            if metadata.get("basis"):
                detail["basis"] = str(metadata.get("basis"))
            if metadata.get("break_glass_id"):
                detail["break_glass_id"] = str(metadata.get("break_glass_id"))
                detail["bypassed"] = ",".join(str(item) for item in metadata.get("bypassed") or ())
            if approved_here:
                detail["auto_approval"] = str(landing_metadata(approval).get("decision_digest") or "")
            return ReviewDecision(
                kind="provider_merge",
                outcome="merged",
                actor_id=landing.provider or "scm",
                actor_type="system",
                decided_at=landing.updated_at,
                reference=landing.id,
                detail=detail,
            )
    for outcome in store.list_outcomes(session.id):  # newest first
        if outcome.revision_id != revision.id or outcome.outcome not in _DECISION_OUTCOMES:
            continue
        return ReviewDecision(
            kind="human_outcome",
            outcome=_DECISION_OUTCOMES[outcome.outcome],
            actor_id=outcome.reviewer_id,
            actor_type="human",
            decided_at=outcome.created_at,
            reference=outcome.id,
        )
    if approved_here and approval is not None:
        # No human decision: the policy auto-approval in force on this Snapshot is the decision.
        return ReviewDecision(
            kind="landing",
            outcome="auto_approved",
            actor_id=AUTO_APPROVAL_ACTOR,
            actor_type="system",
            decided_at=str(landing_metadata(approval).get("decided_at") or approval.updated_at),
            reference=approval.id,
            detail=decision_for_record(approval),
        )
    if landing is not None and landing.status != "stale":
        # No review decision: the break-glass this Snapshot's landing rests on is the decision.
        from lemoncrow.pro.capabilities.review import break_glass

        metadata = landing_metadata(landing)
        if metadata.get("basis") == "break_glass" and str(metadata.get("revision_id") or "") == revision.id:
            reference = str(metadata.get("break_glass_id") or "")
            record = next(
                (row for row in break_glass.records(store, [revision.id]) if row.get("id") == reference), None
            )
            if record is not None:
                return ReviewDecision(
                    kind="landing",
                    outcome="break_glass",
                    actor_id=str(record.get("actor_id") or ""),
                    actor_type="human",
                    decided_at=str(record.get("created_at") or ""),
                    reference=reference,
                    detail=break_glass.decision_detail(record),
                )
    return None


# --------------------------------------------------------------------------- #
# predicate sections
# --------------------------------------------------------------------------- #


def snapshot_section(session: ReviewSession, revision: ReviewRevision, *, number: int) -> dict[str, Any]:
    return {
        "review_id": session.id,
        "revision_id": revision.id,
        "number": int(number),
        "head_sha": revision.head_sha,
        "base_sha": revision.base_sha,
        "merge_base_sha": revision.merge_base_sha,
        "commit_sha": revision.commit_sha,
        "tree_fingerprint": revision.tree_fingerprint,
        "source_fingerprint": revision.source_fingerprint,
        "packet_sha256": revision.packet_sha256,
        "dirty": bool(revision.dirty),
    }


def _evidence_row(item: ReviewEvidence) -> dict[str, Any]:
    return {
        "id": item.id,
        "revision_id": item.revision_id,
        "kind": item.kind,
        "title": item.title,
        "stable_key": item.stable_key,
        "attestation_class": item.attestation_class,
        "executor_class": item.executor_class,
        "execution_intent": item.execution_intent,
        "state": item.verification_status,
        "source": item.source,
        "provider": item.provider,
        "provider_revision": item.provider_revision,
        "attested_by": item.attested_by,
        "observed_at": item.observed_at,
        "created_at": item.created_at,
        "content_hash": item.content_hash,
        "execution_verification_digest": (
            sha256_digest(item.execution_verification.encode("utf-8")) if item.execution_verification else ""
        ),
        "waived_requirement_key": item.waived_requirement_key,
    }


def evidence_section(facts: RecordFacts) -> list[dict[str, Any]]:
    return [_evidence_row(item) for item in sorted(facts.evidence, key=lambda row: row.id)]


def waivers_section(facts: RecordFacts) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in sorted(facts.waivers, key=lambda row: row.id):
        try:
            scope = json.loads(item.attestation_scope or "{}")
        except ValueError:
            scope = {}
        scope = scope if isinstance(scope, Mapping) else {}
        out.append(
            {
                "evidence_id": item.id,
                "revision_id": item.revision_id,
                "requirement_key": item.waived_requirement_key,
                "actor": item.attested_by or item.source_ref,
                "reason": item.detail,
                "scope": str(scope.get("scope") or ""),
                "expires_at": str(scope.get("expires_at") or ""),
                "policy_generation": str(scope.get("policy_generation") or ""),
                "created_at": item.created_at,
            }
        )
    return out


def judgments_section(facts: RecordFacts) -> dict[str, Any]:
    """Outcomes in full; per-target judgments and decisions as digest + counts.

    Per-target judgment events can number in the thousands on a large Change,
    so they are bound by a digest over their canonical rows (reproducible from
    the store) plus per-state counts rather than copied into every record.
    """

    outcomes = [
        {
            "id": item.id,
            "revision_id": item.revision_id,
            "reviewer_id": item.reviewer_id,
            "outcome": item.outcome,
            "created_at": item.created_at,
            "summary_digest": sha256_digest(item.summary.encode("utf-8")) if item.summary else "",
        }
        for item in sorted(facts.outcomes, key=lambda row: (row.created_at, row.id))
    ]
    marks = [
        {
            "id": item.id,
            "reviewer_id": item.reviewer_id,
            "unit_key": item.unit_key,
            "revision_id": item.revision_id,
            "event_kind": item.event_kind,
            "to_state": item.to_state,
            "content_fingerprint": item.content_fingerprint,
            "actor_type": item.actor_type,
            "created_at": item.created_at,
        }
        for item in sorted(facts.mark_events, key=lambda row: (row.created_at, row.id))
    ]
    by_state: dict[str, int] = {}
    for row in marks:
        state = str(row["to_state"] or "none")
        by_state[state] = by_state.get(state, 0) + 1
    decisions = [
        {
            "id": item.id,
            "decision_key": item.decision_key,
            "reviewer_id": item.reviewer_id,
            "revision_id": item.revision_id,
            "action": item.action,
            "disposition": item.disposition,
            "option_key": item.option_key,
            "semantic_fingerprint": item.semantic_fingerprint,
            "implementation_fingerprint": item.implementation_fingerprint,
            "actor_type": item.actor_type,
            "created_at": item.created_at,
        }
        for item in sorted(facts.decision_events, key=lambda row: (row.created_at, row.id))
    ]
    return {
        "outcomes": outcomes,
        "target_judgments": {"count": len(marks), "by_state": by_state, "digest": _digest_of(marks)},
        "decisions": {"count": len(decisions), "digest": _digest_of(decisions)},
    }


def readiness_section(readiness: Mapping[str, Any]) -> dict[str, Any]:
    """The readiness evaluation the decision was recorded under (the readiness core's wire)."""

    landing = readiness.get("landing") if isinstance(readiness.get("landing"), Mapping) else {}
    conditions = [
        {
            "id": str(item.get("id") or ""),
            "label": str(item.get("label") or ""),
            "state": str(item.get("state") or ""),
            "detail": str(item.get("detail") or ""),
            "owner": str(item.get("owner") or ""),
            "category": str(item.get("category") or ""),
            "url": str(item.get("url") or ""),
            "reason": str(item.get("reason") or ""),
            "evidence_ids": sorted(
                str(ref.get("evidence_id") or "")
                for ref in item.get("evidence_refs", ()) or ()
                if isinstance(ref, Mapping) and ref.get("evidence_id")
            ),
        }
        for item in readiness.get("conditions", ()) or ()
        if isinstance(item, Mapping)
    ]
    return {
        "ready": bool(readiness.get("ready")),
        "armable": bool(readiness.get("armable")),
        "blocker_count": int(readiness.get("blocker_count") or 0),
        "conditions_hash": str(readiness.get("conditions_hash") or ""),
        "evaluated_at": str(readiness.get("evaluated_at") or ""),
        "valid_until": str(readiness.get("valid_until") or ""),
        "landing_state": str((landing or {}).get("state") or ""),
        "conditions": conditions,
    }


def policy_section(policy_generation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "digest": str(policy_generation.get("digest") or ""),
        "authority": str(policy_generation.get("authority") or ""),
        "layers": [
            {
                "layer": str(layer.get("layer") or ""),
                "source": str(layer.get("source") or ""),
                "digest": str(layer.get("digest") or ""),
                "state": str(layer.get("state") or ""),
            }
            for layer in policy_generation.get("layers", ()) or ()
            if isinstance(layer, Mapping)
        ],
    }


def run_plan_section(run_plan: Mapping[str, Any] | None) -> dict[str, Any]:
    if not run_plan:
        return {"digest": "", "parent_digest": "", "inputs_digest": "", "state": "no_plan"}
    if run_plan.get("error"):
        # A plan that could not be computed is recorded as such, never as "no plan".
        return {"digest": "", "parent_digest": "", "inputs_digest": "", "state": "unavailable"}
    return {
        "digest": str(run_plan.get("digest") or ""),
        "parent_digest": str(run_plan.get("parent_digest") or ""),
        "inputs_digest": str(run_plan.get("inputs_digest") or ""),
        "state": "planned" if run_plan.get("digest") else "no_plan",
    }


# --------------------------------------------------------------------------- #
# statement
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RecordContext:
    """Everything one record reads besides the decision and the signer.

    ``readiness`` is the readiness core's wire for *revision* evaluated at
    ``issued_at`` (it carries ``policy_generation``); ``run_plan`` the Snapshot's
    effective run plan wire (``None`` without a planner).
    """

    store: Any
    session: ReviewSession
    revision: ReviewRevision
    snapshot_number: int
    readiness: Mapping[str, Any]
    run_plan: Mapping[str, Any] | None
    issuer_kind: str
    issuer_id: str
    repository: str
    exported_by_id: str
    exported_by_type: str = "human"


def build_statement(
    context: RecordContext,
    decision: ReviewDecision,
    *,
    signing: SigningIdentity,
    issued_at: str,
    facts: RecordFacts | None = None,
) -> dict[str, Any]:
    """The in-toto Statement for one decided Snapshot (pure given *facts*)."""

    if parse_timestamp(issued_at) is None:
        raise RecordSchemaError("issued_at must be an aware ISO-8601 instant")
    session, revision = context.session, context.revision
    if revision.review_id != session.id:
        raise RecordSchemaError("the Snapshot does not belong to this Review")
    rows = facts if facts is not None else gather_record_facts(context.store, session, revision, as_of=issued_at)
    policy = context.readiness.get("policy_generation")
    digest: dict[str, str] = {"lemoncrowTreeFingerprint": revision.tree_fingerprint}
    if revision.head_sha:
        digest["gitCommit"] = revision.head_sha
    predicate: dict[str, Any] = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "issuer": {"kind": context.issuer_kind, "id": context.issuer_id, "service": "lemoncrow"},
        "issued_at": issued_at,
        "as_of": issued_at,
        "exported_by": {"id": context.exported_by_id, "type": context.exported_by_type},
        "signing": signing.to_wire(),
        "review": {
            "id": session.id,
            "subject_type": session.subject_type,
            "repository": context.repository,
            "source_ref": session.source_ref,
            "title": session.title,
        },
        "snapshot": snapshot_section(session, revision, number=context.snapshot_number),
        "decision": decision.to_wire(),
        "readiness": readiness_section(context.readiness),
        "policy_generation": policy_section(policy if isinstance(policy, Mapping) else {}),
        "run_plan": run_plan_section(context.run_plan),
        "evidence": evidence_section(rows),
        "waivers": waivers_section(rows),
        "judgments": judgments_section(rows),
    }
    statement = {
        "_type": STATEMENT_TYPE,
        "subject": [{"name": f"lemoncrow-review/{session.id}/snapshot/{revision.id}", "digest": digest}],
        "predicateType": PREDICATE_TYPE,
        "predicate": predicate,
    }
    errors = validate_statement(statement)
    if errors:
        raise RecordSchemaError("; ".join(errors))
    return statement


def statement_digest(statement: Mapping[str, Any]) -> str:
    return sha256_digest(canonical_json(statement))


_PREDICATE_SECTIONS: Final[Mapping[str, type]] = {
    "schema_version": int,
    "issuer": dict,
    "issued_at": str,
    "as_of": str,
    "exported_by": dict,
    "signing": dict,
    "review": dict,
    "snapshot": dict,
    "decision": dict,
    "readiness": dict,
    "policy_generation": dict,
    "run_plan": dict,
    "evidence": list,
    "waivers": list,
    "judgments": dict,
}
_SNAPSHOT_KEYS: Final[tuple[str, ...]] = ("review_id", "revision_id", "tree_fingerprint", "head_sha", "base_sha")


def validate_statement(statement: object) -> list[str]:
    """Predicate schema violations (empty when the statement is a valid v1 review record)."""

    if not isinstance(statement, Mapping):
        return ["statement is not a JSON object"]
    errors: list[str] = []
    if statement.get("_type") != STATEMENT_TYPE:
        errors.append(f"_type is not {STATEMENT_TYPE}")
    if statement.get("predicateType") != PREDICATE_TYPE:
        errors.append(f"predicateType is not {PREDICATE_TYPE}")
    subject = statement.get("subject")
    if (
        not isinstance(subject, list)
        or len(subject) != 1
        or not isinstance(subject[0], Mapping)
        or not isinstance(subject[0].get("digest"), Mapping)
        or not subject[0].get("digest")
    ):
        errors.append("subject must name exactly one Snapshot with a digest")
    predicate = statement.get("predicate")
    if not isinstance(predicate, Mapping):
        return [*errors, "predicate is not a JSON object"]
    for key, kind in _PREDICATE_SECTIONS.items():
        value = predicate.get(key)
        if not isinstance(value, kind) or (isinstance(value, bool) and kind is int):
            errors.append(f"predicate.{key} is missing or not a {kind.__name__}")
    if errors:
        return errors
    if predicate["schema_version"] != RECORD_SCHEMA_VERSION:
        errors.append(f"unsupported predicate schema_version {predicate['schema_version']!r}")
    if parse_timestamp(predicate["issued_at"]) is None:
        errors.append("predicate.issued_at is not an aware ISO-8601 instant")
    signing = predicate["signing"]
    if signing.get("state") not in {"signed", "unsigned"} or signing.get("mode") not in SIGNING_MODES:
        errors.append("predicate.signing names an unknown state or mode")
    elif (signing.get("state") == "unsigned") != (signing.get("mode") == "unsigned"):
        errors.append("predicate.signing state and mode disagree")
    elif signing.get("state") == "unsigned" and signing.get("notice") != UNSIGNED_NOTICE:
        errors.append("an unsigned record must carry the unsigned notice")
    elif signing.get("state") == "signed" and (not signing.get("key_id") or not signing.get("algorithm")):
        errors.append("a signed record names its key id and algorithm")
    snapshot = predicate["snapshot"]
    for key in _SNAPSHOT_KEYS:
        if not isinstance(snapshot.get(key), str):
            errors.append(f"predicate.snapshot.{key} is missing")
    if not snapshot.get("review_id") or not snapshot.get("revision_id") or not snapshot.get("tree_fingerprint"):
        errors.append("predicate.snapshot does not identify the Snapshot")
    decision = predicate["decision"]
    if decision.get("kind") not in DECISION_KINDS or not decision.get("outcome"):
        errors.append("predicate.decision names no known decision")
    actor = decision.get("actor")
    if not isinstance(actor, Mapping) or not actor.get("id") or parse_timestamp(decision.get("decided_at")) is None:
        errors.append("predicate.decision names no actor or decision time")
    for row in predicate["evidence"]:
        if not isinstance(row, Mapping) or not isinstance(row.get("executor_class"), str):
            errors.append("every evidence row names its executor class (empty when not an execution result)")
            break
        if not isinstance(row.get("attestation_class"), str):
            errors.append("every evidence row names its attestation class")
            break
    if not str(predicate["policy_generation"].get("digest") or "").startswith("policy:"):
        errors.append("predicate.policy_generation names no policy generation digest")
    readiness = predicate["readiness"]
    if not isinstance(readiness.get("conditions"), list) or not isinstance(readiness.get("ready"), bool):
        errors.append("predicate.readiness carries no readiness state")
    judgments = predicate["judgments"]
    if not isinstance(judgments.get("outcomes"), list):
        errors.append("predicate.judgments carries no outcomes")
    return errors


def readiness_consistent(readiness: Mapping[str, Any], *, policy_generation: str) -> bool:
    """Whether a recorded readiness section's ``conditions_hash`` is its own conditions' hash."""

    conditions = readiness.get("conditions")
    if not isinstance(conditions, list):
        return False
    recomputed = conditions_digest(
        [item for item in conditions if isinstance(item, Mapping)], policy_generation=policy_generation
    )
    return recomputed == str(readiness.get("conditions_hash") or "")


StoreSection = Literal["match", "mismatch", "not_checked"]


def compare_with_store(
    statement: Mapping[str, Any],
    *,
    store: Any,
    session: ReviewSession,
    revision: ReviewRevision | None,
    snapshot_number: int,
    current_policy_generation: Mapping[str, Any] | None,
    readiness_at: Mapping[str, Any] | None,
    run_plan: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Recompute every stored section of *statement* and say which still match.

    ``readiness_at`` is the readiness core re-evaluated at the recorded
    ``evaluated_at`` (``None``: not re-evaluated). A section that changed after
    signing (a later policy edit, say) is reported as ``mismatch``, never
    silently accepted.
    """

    predicate = statement.get("predicate") if isinstance(statement, Mapping) else None
    if not isinstance(predicate, Mapping) or revision is None:
        return {"snapshot": "mismatch"}
    as_of = str(predicate.get("as_of") or "")
    facts = gather_record_facts(store, session, revision, as_of=as_of)

    def verdict(same: bool) -> str:
        return "match" if same else "mismatch"

    out: dict[str, str] = {
        "snapshot": verdict(predicate.get("snapshot") == snapshot_section(session, revision, number=snapshot_number)),
        "evidence": verdict(predicate.get("evidence") == evidence_section(facts)),
        "waivers": verdict(predicate.get("waivers") == waivers_section(facts)),
        "judgments": verdict(predicate.get("judgments") == judgments_section(facts)),
    }
    recorded_policy = predicate.get("policy_generation")
    recorded_digest = str(recorded_policy.get("digest") or "") if isinstance(recorded_policy, Mapping) else ""
    if current_policy_generation is None:
        out["policy_generation"] = "not_checked"
    else:
        out["policy_generation"] = verdict(recorded_policy == policy_section(current_policy_generation))
    readiness = predicate.get("readiness")
    if not isinstance(readiness, Mapping) or not readiness_consistent(readiness, policy_generation=recorded_digest):
        out["readiness"] = "mismatch"
    elif readiness_at is None:
        out["readiness"] = "not_checked"
    else:
        out["readiness"] = verdict(readiness == readiness_section(readiness_at))
    out["run_plan"] = verdict(predicate.get("run_plan") == run_plan_section(run_plan))
    decision = predicate.get("decision")
    if isinstance(decision, Mapping) and decision.get("kind") == "human_outcome":
        reference = str(decision.get("reference") or "")
        actor = decision.get("actor") if isinstance(decision.get("actor"), Mapping) else {}
        row = next((item for item in facts.outcomes if item.id == reference), None)
        out["decision"] = verdict(
            row is not None
            and row.revision_id == revision.id
            and _DECISION_OUTCOMES.get(row.outcome) == decision.get("outcome")
            and row.reviewer_id == (actor or {}).get("id")
            and row.created_at == decision.get("decided_at")
        )
    elif isinstance(decision, Mapping) and decision.get("kind") == "landing":
        out["decision"] = verdict(_landing_decision_stored(store, session, revision, decision))
    else:
        out["decision"] = "not_checked"
    return out


def _landing_decision_stored(
    store: Any, session: ReviewSession, revision: ReviewRevision, decision: Mapping[str, Any]
) -> bool:
    """Does the store still hold the landing decision a record names (P10-LAND)?"""

    from lemoncrow.pro.capabilities.review import break_glass
    from lemoncrow.pro.capabilities.review.auto_approval import auto_approval_relationship
    from lemoncrow.pro.capabilities.review.landing import landing_metadata

    reference = str(decision.get("reference") or "")
    detail = _mapping(decision.get("detail"))
    actor = _mapping(decision.get("actor"))
    if decision.get("outcome") == "auto_approved":
        approval = auto_approval_relationship(store, session.id)
        metadata = landing_metadata(approval)
        return (
            approval is not None
            and approval.id == reference
            and str(metadata.get("revision_id") or "") == revision.id
            and str(metadata.get("decision_digest") or "") == str(detail.get("decision_digest") or "")
        )
    if decision.get("outcome") == "break_glass":
        record = next((row for row in break_glass.records(store, [revision.id]) if row.get("id") == reference), None)
        return (
            record is not None
            and str(record.get("actor_id") or "") == str(actor.get("id") or "")
            and break_glass.decision_detail(record) == {str(key): str(value) for key, value in detail.items()}
        )
    return False


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def summarize(statement: Mapping[str, Any]) -> dict[str, Any]:
    """Display facts of a statement (never a verification verdict)."""

    predicate = _mapping(statement.get("predicate"))
    snapshot = _mapping(predicate.get("snapshot"))
    decision = _mapping(predicate.get("decision"))
    signing = _mapping(predicate.get("signing"))
    policy = _mapping(predicate.get("policy_generation"))
    readiness = _mapping(predicate.get("readiness"))
    actor = _mapping(decision.get("actor"))
    return {
        "review_id": str(snapshot.get("review_id") or ""),
        "revision_id": str(snapshot.get("revision_id") or ""),
        "snapshot_number": snapshot.get("number"),
        "head_sha": str(snapshot.get("head_sha") or ""),
        "decision": str(decision.get("outcome") or ""),
        "decision_kind": str(decision.get("kind") or ""),
        "decided_by": str(actor.get("id") or ""),
        "decided_at": str(decision.get("decided_at") or ""),
        "issued_at": str(predicate.get("issued_at") or ""),
        "signing_state": str(signing.get("state") or ""),
        "signing_mode": str(signing.get("mode") or ""),
        "key_id": str(signing.get("key_id") or ""),
        "policy_generation": str(policy.get("digest") or ""),
        "ready": readiness.get("ready") is True,
    }


def relationship_rows(store: Any, review_id: str) -> Sequence[Any]:
    return tuple(store.list_relationships(review_id, kind=RECORD_RELATIONSHIP_KIND))


def iter_waiver_keys(statement: Mapping[str, Any]) -> Iterable[str]:
    predicate = _mapping(statement.get("predicate"))
    for row in predicate.get("waivers", ()) or ():
        if isinstance(row, Mapping):
            yield str(row.get("requirement_key") or "")


def cited_evidence_ids(statement: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """Every evidence id a statement cites, with where: ``evidence`` or ``condition:<id>``.

    The two places :func:`evidence_section` and :func:`readiness_section` write
    evidence identity, read back for the trust incident runbook (Gate T-J): a
    v1 record names reused results only by evidence id.
    """

    predicate = _mapping(statement.get("predicate"))
    places: dict[str, list[str]] = {}
    for row in predicate.get("evidence", ()) or ():
        if isinstance(row, Mapping) and row.get("id"):
            places.setdefault(str(row["id"]), []).append("evidence")
    for condition in _mapping(predicate.get("readiness")).get("conditions", ()) or ():
        if not isinstance(condition, Mapping):
            continue
        for evidence_id in condition.get("evidence_ids", ()) or ():
            if evidence_id:
                places.setdefault(str(evidence_id), []).append(f"condition:{condition.get('id') or ''}")
    return {key: tuple(dict.fromkeys(value)) for key, value in places.items()}


__all__ = [
    "DECISION_KINDS",
    "PAYLOAD_TYPE",
    "PREDICATE_TYPE",
    "RECORD_RELATIONSHIP_KIND",
    "RECORD_SCHEMA_VERSION",
    "SIGNING_MODES",
    "STATEMENT_TYPE",
    "UNSIGNED",
    "UNSIGNED_NOTICE",
    "DecisionKind",
    "RecordContext",
    "RecordFacts",
    "RecordSchemaError",
    "ReviewDecision",
    "SigningIdentity",
    "SigningMode",
    "build_statement",
    "canonical_json",
    "cited_evidence_ids",
    "compare_with_store",
    "current_decision",
    "evidence_section",
    "gather_record_facts",
    "judgments_section",
    "policy_section",
    "readiness_consistent",
    "readiness_section",
    "run_plan_section",
    "sha256_digest",
    "snapshot_section",
    "statement_digest",
    "summarize",
    "validate_statement",
    "waivers_section",
]

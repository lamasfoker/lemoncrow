"""Archive/discard and hard-delete safety (Phase 12, P12-AUTO item 12).

Discarding a Review (``status=archived``) is reversible and never deletes: the
rows stay until retention cleanup, a later maintenance event, removes them.
Before the discard, two things a reviewer can lose are named, each with what
happens to it:

* ``pending_feedback``: comments that were never sent anywhere;
* ``armed_landing``: an armed Merge-when-ready intent (or a provider
  cancellation already owed for one).

The discard is refused (409, with the hazards) unless the request names every
present hazard in ``acknowledged_hazards`` and carries the ``hazard_digest``
the reviewer was shown. The acknowledged outcomes are recorded as activity; an
owed provider cancellation stays on the landing record (``provider_cancel:
pending``) and visible until the provider confirms it.

Hard delete removes an archived Review immediately. It requires a separate
privilege (hosted: ``review:delete``; local: the server owner), an exact
identity confirmation (the full Review id or its exact title) and an audit
record written before anything is removed; without an audit sink it is refused.
The durable record of what was deleted is a tombstone in the Review store
itself (:class:`~lemoncrow.pro.capabilities.review.session_models.ReviewDeletionTombstone`):
who, which Review and repository, when, the request id that correlates it to
the audit sink, a SHA-256 of the exact confirmation and the row counts. The
store writes it in the deletion's own transaction; if it cannot be written,
nothing is deleted.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

from lemoncrow.pro.capabilities.review.landing import (
    ARMED_LANDING_STATES,
    landing_metadata,
    landing_relationship,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewDeletionTombstone

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.session_models import ReviewSession

HazardId = Literal["pending_feedback", "armed_landing"]
HAZARD_IDS: Final[tuple[HazardId, ...]] = ("pending_feedback", "armed_landing")
DISCARD_ACTIVITY: Final[str] = "review.discard_acknowledged"
_HAZARD_LABELS: Final[Mapping[str, str]] = {
    "pending_feedback": "Pending feedback",
    "armed_landing": "Armed landing",
}


@dataclass(frozen=True, slots=True)
class ArchiveHazard:
    id: HazardId
    label: str
    count: int
    detail: str
    outcome: str
    """What the discard does to it, in words the confirmation shows."""
    subject_ids: tuple[str, ...] = ()
    provider: str = ""
    owed_cancellation: bool = False

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "count": self.count,
            "detail": self.detail,
            "outcome": self.outcome,
            "subject_ids": list(self.subject_ids),
            "provider": self.provider,
            "owed_cancellation": self.owed_cancellation,
        }


@dataclass(frozen=True, slots=True)
class ArchiveHazards:
    review_id: str
    hazards: tuple[ArchiveHazard, ...] = ()

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.hazards)

    @property
    def digest(self) -> str:
        document = [
            {"id": item.id, "count": item.count, "subjects": sorted(item.subject_ids), "owed": item.owed_cancellation}
            for item in self.hazards
        ]
        raw = json.dumps([self.review_id, document], sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_wire(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "hazards": [item.to_wire() for item in self.hazards],
            "hazard_ids": list(self.ids),
            "hazard_digest": self.digest,
            "confirmation_required": bool(self.hazards),
            "deletes_now": False,
            "retention": "A discarded Review is kept (and restorable) until retention cleanup removes it later.",
        }


def _pending_feedback(store: Any, session: ReviewSession) -> tuple[str, ...]:
    """Open root human comments on the current Snapshot that were never sent anywhere."""

    from lemoncrow.pro.capabilities.review.handoff import feedback_state, feedback_version, is_open_root_feedback

    revision = store.latest_revision(session.id)
    if revision is None:
        return ()
    pending: list[str] = []
    for annotation in store.list_annotations(session.id):
        if not is_open_root_feedback(annotation):
            continue
        version = feedback_version(store.list_annotation_versions(annotation.id))
        deliveries = store.list_deliveries(annotation.id)
        on_provider = any(
            item.target_type == "github_review" and item.annotation_version == version and item.state == "sent"
            for item in deliveries
        )
        state, _matching = feedback_state(annotation, revision_id=revision.id, version=version, deliveries=deliveries)
        if state == "unpublished" and not on_provider:
            pending.append(annotation.id)
    return tuple(pending)


def archive_hazards(store: Any, session: ReviewSession, *, landing_delegate_available: bool = True) -> ArchiveHazards:
    hazards: list[ArchiveHazard] = []
    pending = _pending_feedback(store, session)
    if pending:
        hazards.append(
            ArchiveHazard(
                id="pending_feedback",
                label=_HAZARD_LABELS["pending_feedback"],
                count=len(pending),
                detail=f"{len(pending)} comment{'s' if len(pending) != 1 else ''} not yet sent to the author or the provider.",
                outcome=(
                    "Nothing is sent. The comments stay inside the discarded Review, readable and restorable, "
                    "until retention cleanup deletes the Review later."
                ),
                subject_ids=pending,
            )
        )
    landing = landing_relationship(store, session.id)
    metadata = landing_metadata(landing)
    owed = metadata.get("provider_cancel") == "pending"
    if landing is not None and (landing.status in ARMED_LANDING_STATES or owed):
        provider = str(landing.provider or "") or "the provider"
        if landing_delegate_available:
            outcome = (
                f"Merge when ready is withdrawn: LemonCrow asks {provider} to cancel it. The cancellation stays "
                f"owed, and shown on this Review, until {provider} confirms it."
            )
        else:
            outcome = "Merge when ready is withdrawn; no provider holds it, so it ends here."
        hazards.append(
            ArchiveHazard(
                id="armed_landing",
                label=_HAZARD_LABELS["armed_landing"],
                count=1,
                detail=(
                    f"A provider cancellation is already owed ({metadata.get('detail') or 'not yet confirmed'})."
                    if owed
                    else f"Merge when ready is {landing.status}."
                ),
                outcome=outcome,
                subject_ids=(landing.id,),
                provider=str(landing.provider or ""),
                owed_cancellation=owed,
            )
        )
    return ArchiveHazards(review_id=session.id, hazards=tuple(hazards))


class ArchiveConfirmationRequired(Exception):
    """The discard names hazards the request did not acknowledge."""

    def __init__(self, hazards: ArchiveHazards, message: str) -> None:
        super().__init__(message)
        self.hazards = hazards
        self.message = message

    def detail(self) -> dict[str, Any]:
        return {"code": "archive_confirmation_required", "message": self.message, **self.hazards.to_wire()}


def require_acknowledged(hazards: ArchiveHazards, body: Mapping[str, Any]) -> tuple[str, ...]:
    """The acknowledged hazard ids, or :class:`ArchiveConfirmationRequired`.

    Every present hazard must be named, and the digest must be the one shown:
    a hazard that appeared after the dialog opened is never acknowledged by it.
    """

    if not hazards.hazards:
        return ()
    raw = body.get("acknowledged_hazards")
    named = {str(item) for item in raw} if isinstance(raw, (list, tuple)) else set()
    missing = [item for item in hazards.ids if item not in named]
    if missing:
        labels = " and ".join(_HAZARD_LABELS[item].lower() for item in hazards.ids)
        raise ArchiveConfirmationRequired(
            hazards,
            f"Discarding this Review needs explicit confirmation naming its {labels}.",
        )
    if str(body.get("hazard_digest") or "") != hazards.digest:
        raise ArchiveConfirmationRequired(
            hazards, "What discarding affects changed since it was confirmed; review it and confirm again."
        )
    return hazards.ids


def record_discard(
    store: Any,
    session: ReviewSession,
    hazards: ArchiveHazards,
    *,
    actor_id: str,
    actor_type: str = "human",
) -> dict[str, Any]:
    """Record what the confirmed discard did to each hazard, and what is still owed."""

    from lemoncrow.pro.capabilities.review.session_models import ReviewActivityEvent

    landing = landing_relationship(store, session.id)
    metadata = landing_metadata(landing)
    owed = landing is not None and metadata.get("provider_cancel") == "pending"
    outcomes = {
        item.id: {
            "outcome": item.outcome,
            "count": item.count,
        }
        for item in hazards.hazards
    }
    if "armed_landing" in outcomes and landing is not None:
        outcomes["armed_landing"].update(
            {
                "landing_status": landing.status,
                "provider_cancel": str(metadata.get("provider_cancel") or ""),
                "cancellation_owed": owed,
                "detail": str(metadata.get("detail") or ""),
            }
        )
    if hazards.hazards:
        revision = store.latest_revision(session.id)
        store.record_activity(
            ReviewActivityEvent(
                id="",
                review_id=session.id,
                revision_id=str(getattr(revision, "id", "") or ""),
                kind=DISCARD_ACTIVITY,  # type: ignore[arg-type]
                actor_id=actor_id,
                actor_type=actor_type,  # type: ignore[arg-type]
                subject_type="review",
                subject_id=session.id,
                summary=(
                    "Discarded after confirming "
                    + " and ".join(item.label.lower() for item in hazards.hazards)
                    + ("; the provider cancellation is still owed" if owed else "")
                    + "."
                ),
                detail_json=json.dumps(
                    {"acknowledged": list(hazards.ids), "outcomes": outcomes}, ensure_ascii=False, sort_keys=True
                ),
            )
        )
    return {"acknowledged": list(hazards.ids), "outcomes": outcomes, "cancellation_owed": owed}


def owed_cancellation(store: Any, review_id: str) -> dict[str, Any] | None:
    """An owed provider cancellation, shown until the provider confirms it."""

    landing = landing_relationship(store, review_id)
    metadata = landing_metadata(landing)
    if landing is None or metadata.get("provider_cancel") != "pending":
        return None
    return {
        "provider": str(landing.provider or ""),
        "landing_status": landing.status,
        "reason": str(metadata.get("cancel_reason") or ""),
        "detail": str(metadata.get("detail") or ""),
        "error": str(metadata.get("watch_error") or ""),
    }


# --------------------------------------------------------------------------- #
# Hard delete
# --------------------------------------------------------------------------- #

HARD_DELETE_EVENT: Final[str] = "review.hard_delete"
DeletionAudit = Callable[[Mapping[str, Any]], None]


class HardDeleteRefused(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def identity_matches(session: ReviewSession, confirmation: str) -> bool:
    """Exactly the full Review id (bare or ``r/``-prefixed) or exactly its non-empty title."""

    if not confirmation:
        return False
    if confirmation in {session.id, f"r/{session.id}"}:
        return True
    return bool(session.title) and confirmation == session.title


def authorize_hard_delete(
    session: ReviewSession,
    body: Mapping[str, Any],
    *,
    allowed: bool,
    audit: DeletionAudit | None,
) -> str:
    """The confirmation the caller typed, or :class:`HardDeleteRefused`."""

    if not allowed:
        raise HardDeleteRefused(403, "Deleting a Review permanently needs the separate delete privilege.")
    if audit is None:
        raise HardDeleteRefused(409, "Permanent deletion is unavailable: this server has no deletion audit sink.")
    if session.status != "archived":
        raise HardDeleteRefused(409, "only archived reviews can be deleted")
    confirmation = str(body.get("confirm_identity") or "")
    if not identity_matches(session, confirmation):
        raise HardDeleteRefused(
            400,
            "Permanent deletion needs confirm_identity: the exact Review id or its exact title.",
        )
    return confirmation


@dataclass(frozen=True, slots=True)
class DeletionContext:
    """Who deletes, in which repository, under which request (the audit correlation id)."""

    actor_id: str
    repo_id: str
    request_id: str


def _confirmed_by(session: ReviewSession, confirmation: str) -> Literal["id", "title"]:
    return "id" if confirmation in {session.id, f"r/{session.id}"} else "title"


def deletion_tombstone(session: ReviewSession, context: DeletionContext, confirmation: str) -> ReviewDeletionTombstone:
    """The tombstone a hard delete asks the store to write (the store adds counts, time and chain)."""

    return ReviewDeletionTombstone(
        review_id=session.id,
        repo_id=context.repo_id,
        actor_id=context.actor_id,
        request_id=context.request_id,
        confirmation_sha256=hashlib.sha256(confirmation.encode("utf-8")).hexdigest(),
        confirmed_by=_confirmed_by(session, confirmation),
    )


def deletion_record(
    session: ReviewSession,
    *,
    actor_id: str,
    confirmation: str,
    outcome: str,
    request_id: str = "",
) -> dict[str, Any]:
    return {
        "event": HARD_DELETE_EVENT,
        "at": datetime.now(UTC).isoformat(),
        "review_id": session.id,
        "title": session.title,
        "repository": session.repo_root,
        "actor_id": actor_id,
        "request_id": request_id,
        "confirmed_by": _confirmed_by(session, confirmation),
        "outcome": outcome,
    }


def jsonl_deletion_audit(path: Path) -> DeletionAudit:
    """Local append-only audit file under the Review store (outlives the deleted rows)."""

    def _write(record: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(dict(record), ensure_ascii=False, sort_keys=True) + "\n"
        # lc-debt: unbounded append-only local audit file; one line per hard delete
        # (a rare, privileged action). Rotate with the store backup if it ever grows.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    return _write


def hazard_labels(ids: Sequence[str]) -> str:
    return " and ".join(_HAZARD_LABELS.get(item, item).lower() for item in ids)


__all__ = [
    "DISCARD_ACTIVITY",
    "HARD_DELETE_EVENT",
    "HAZARD_IDS",
    "ArchiveConfirmationRequired",
    "ArchiveHazard",
    "ArchiveHazards",
    "DeletionAudit",
    "DeletionContext",
    "HardDeleteRefused",
    "archive_hazards",
    "authorize_hard_delete",
    "deletion_record",
    "deletion_tombstone",
    "hazard_labels",
    "identity_matches",
    "jsonl_deletion_audit",
    "owed_cancellation",
    "record_discard",
    "require_acknowledged",
]

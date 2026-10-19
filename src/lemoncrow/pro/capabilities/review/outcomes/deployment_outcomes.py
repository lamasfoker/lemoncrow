"""Store-bound deployment outcomes (P15-DEPLOY items 1, 4, 7, 10).

* **Ledger** -- one ``review_derived`` row per (merged Review, environment)
  under the merged Snapshot (kind ``deployment_outcome``, scope
  ``env:<environment>``), written by compare-and-set so concurrent webhook
  deliveries converge. It holds the Review's relevant deployments; the
  environment outcome is derived from it on read.
* **Evidence** (item 4) -- immutable ``review_evidence`` rows of kind
  ``deployment``, freshness class ``external_observation``, appended under
  the merged Snapshot. Nothing here edits the readiness record, a human
  judgment, the signed review record or any existing evidence row: rows are
  only added (and raw rows deleted by the retention closer).
* **Attention triggers** (item 7) -- :func:`outcome_attention_triggers` is
  the transition API the hosted ReviewAttention reconciler consumes; a failed
  or rolled-back production deployment is the first source, P15-OBS adds
  contradiction/incident sources to :data:`OUTCOME_TRIGGER_SOURCES`.
* **Retention closer** (item 10) -- :func:`close_observation_windows` keeps a
  summary row per deployment and expires the raw signal (raw evidence rows and
  per-status transitions) once each deployment's observation window closed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Final, Literal

from lemoncrow.pro.capabilities.review.outcomes.deployments import (
    ATTENTION_STATES,
    CONTAINING,
    MAX_ENVIRONMENTS_PER_REVIEW,
    DeploymentEntry,
    DeploymentObservation,
    DeploymentSettings,
    EnvironmentOutcome,
    MergedCommit,
    Relation,
    connector_status,
    derive_outcome,
    environment_key,
    sentence,
    status_line,
    update_entries,
)

DEPLOYMENT_ROW_KIND: Final[str] = "deployment_outcome"
DEPLOYMENT_EVIDENCE_KIND: Final[str] = "deployment"
OBSERVED_ACTIVITY: Final[str] = "deployment.observed"
SUMMARIZED_ACTIVITY: Final[str] = "deployment.summarized"
_ROW_STAMP: Final[str] = "deployment-v1"
_OPEN: Final[str] = f"{_ROW_STAMP}:open:"
_CLOSED: Final[str] = f"{_ROW_STAMP}:closed:"
_CAS_ATTEMPTS: Final[int] = 8
# lc-debt: the retention closer reads at most this many open ledger rows per store per pass (oldest
# first); upgrade path: loop until a pass returns fewer rows if a store keeps more windows open.
CLOSER_BATCH: Final[int] = 500


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EnvironmentRecord:
    """One merged Review's deployments in one environment (the stored row)."""

    review_id: str
    revision_id: str
    environment: str
    merged: MergedCommit
    entries: tuple[DeploymentEntry, ...] = ()
    entries_dropped: int = 0
    version: int = 0
    updated_at: str = ""

    @property
    def key(self) -> str:
        return environment_key(self.environment)

    @property
    def open(self) -> bool:
        return any(not entry.closed for entry in self.entries)

    @property
    def stamp(self) -> str:
        return f"{_OPEN if self.open else _CLOSED}{self.version}"

    def outcome(self) -> EnvironmentOutcome | None:
        return derive_outcome(self.environment, self.entries, self.merged, dropped=self.entries_dropped)

    def to_wire(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "revision_id": self.revision_id,
            "environment": self.environment,
            "merged": self.merged.to_wire(),
            "entries": [entry.to_wire() for entry in self.entries],
            "entries_dropped": self.entries_dropped,
            "version": self.version,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> EnvironmentRecord | None:
        merged_raw = raw.get("merged")
        merged = MergedCommit.from_wire(merged_raw) if isinstance(merged_raw, Mapping) else None
        if merged is None:
            return None
        try:
            entries = tuple(
                DeploymentEntry.from_wire(item) for item in raw.get("entries") or () if isinstance(item, Mapping)
            )
        except (KeyError, TypeError, ValueError):
            return None
        return cls(
            review_id=str(raw.get("review_id") or ""),
            revision_id=str(raw.get("revision_id") or ""),
            environment=str(raw.get("environment") or ""),
            merged=merged,
            entries=entries,
            entries_dropped=int(raw.get("entries_dropped") or 0),
            version=int(raw.get("version") or 0),
            updated_at=str(raw.get("updated_at") or ""),
        )


class DeploymentLedger:
    """``review_derived`` rows (kind ``deployment_outcome``) of one Review store."""

    def __init__(self, store: Any) -> None:
        self._store = store

    @staticmethod
    def _scope(environment: str) -> str:
        return f"env:{environment_key(environment)}"

    def _row(self, record: EnvironmentRecord) -> Any:
        from lemoncrow.pro.capabilities.review.store import DerivedResult

        return DerivedResult(
            review_id=record.review_id,
            revision_id=record.revision_id,
            kind=DEPLOYMENT_ROW_KIND,
            scope_key=self._scope(record.environment),
            stamp=record.stamp,
            payload=record.to_wire(),
        )

    def records(self, review_id: str) -> dict[str, tuple[str, EnvironmentRecord]]:
        """Environment key -> ``(stamp, record)`` for one Review (every Snapshot of it)."""

        revision_ids = [revision.id for revision in self._store.list_revisions(review_id)]
        if not revision_ids:
            return {}
        out: dict[str, tuple[str, EnvironmentRecord]] = {}
        for (_revision_id, _scope), (stamp, payload) in self._store.derived_results(
            DEPLOYMENT_ROW_KIND, revision_ids
        ).items():
            if not str(stamp).startswith(_ROW_STAMP + ":"):
                continue
            record = EnvironmentRecord.from_wire(payload)
            if record is None or record.review_id != review_id:
                continue
            held = out.get(record.key)
            if held is None or (record.version, record.updated_at) > (held[1].version, held[1].updated_at):
                out[record.key] = (str(stamp), record)
        return out

    def write(self, record: EnvironmentRecord, *, expected_stamp: str | None) -> bool:
        """Insert a new row (``expected_stamp=None``) or compare-and-set an existing one."""

        if expected_stamp is None:
            created, _stamp, _payload = self._store.insert_derived_result(self._row(record))
            return bool(created)
        return bool(self._store.compare_and_set_derived_result(self._row(record), expected_stamp=expected_stamp))

    def open_rows(self, *, limit: int = CLOSER_BATCH) -> tuple[tuple[str, EnvironmentRecord], ...]:
        """Rows with at least one deployment whose observation window is still open (oldest first)."""

        out: list[tuple[str, EnvironmentRecord]] = []
        for row in self._store.derived_results_by_kind(DEPLOYMENT_ROW_KIND, stamp_prefix=_OPEN, limit=limit):
            record = EnvironmentRecord.from_wire(row.payload)
            if record is not None:
                out.append((row.stamp, record))
        return tuple(out)


# --------------------------------------------------------------------------- #
# Applying observations
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StateChange:
    """One environment outcome that moved for one Review (what Activity records)."""

    review_id: str
    environment: str
    previous: str
    current: str
    deciding_id: str


@dataclass(frozen=True)
class ApplyResult:
    review_id: str
    changed: tuple[str, ...] = ()
    """Environment keys whose stored row changed."""
    state_changes: tuple[StateChange, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    environments_dropped: int = 0

    @property
    def touched(self) -> bool:
        return bool(self.changed)


def raw_source_ref(entry: DeploymentEntry) -> str:
    """Stable dedupe key of one raw observation row: deployment and its provider version."""

    return f"deployment:{entry.provider}:{entry.deployment_id}@{_digest(entry.version)}"


def summary_source_ref(entry: DeploymentEntry) -> str:
    return f"deployment-summary:{entry.provider}:{entry.deployment_id}"


def _raw_prefix(entry: DeploymentEntry) -> str:
    return f"deployment:{entry.provider}:{entry.deployment_id}@"


def _evidence(
    record: EnvironmentRecord, entry: DeploymentEntry, *, summary: bool, outcome: EnvironmentOutcome | None
) -> Any:
    from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence

    detail: dict[str, Any] = {
        "freshness_class": "external_observation",
        "deployment_id": entry.deployment_id,
        "environment": entry.environment,
        "state": entry.state,
        "relation": entry.relation,
        "commit": entry.commit,
        "merged_commit": record.merged.sha,
        "created_at": entry.created_at,
        "status_at": entry.status_at,
        "window_closes_at": entry.window_closes_at,
        "production": entry.production,
        "traffic_percent": entry.traffic_percent,
        "source_timestamp": entry.status_at or entry.created_at,
    }
    if summary:
        detail.update(
            {
                "summary": True,
                "succeeded": entry.succeeded,
                "transitions": len(entry.transitions) + entry.transitions_dropped,
                "environment_state": "" if outcome is None else outcome.state,
            }
        )
        title = f"Deployment to {entry.environment} (summary): {entry.state}"
        source_ref = summary_source_ref(entry)
    else:
        detail["raw"] = True
        title = f"Deployment to {entry.environment}: {entry.state}"
        source_ref = raw_source_ref(entry)
    return ReviewEvidence(
        id="",
        review_id=record.review_id,
        revision_id=record.revision_id,
        kind=DEPLOYMENT_EVIDENCE_KIND,  # type: ignore[arg-type]
        title=title[:200],
        url=entry.url,
        source="external",
        source_ref=source_ref,
        detail=json.dumps(detail, sort_keys=True),
        provider=entry.provider,
        provider_revision=entry.commit,
        environment=entry.environment,
        remote_identity=f"deployment:{entry.deployment_id}",
        observed_at=entry.status_at or entry.created_at,
    )


def _evidence_worthy(entry: DeploymentEntry, outcome: EnvironmentOutcome | None) -> bool:
    """Containing deployments, and the one that decided a rollback, are this Review's outcome evidence."""

    return entry.relation in CONTAINING or (
        outcome is not None and outcome.state == "rolled_back" and outcome.deciding_id == entry.deployment_id
    )


def _activity(
    store: Any,
    record: EnvironmentRecord,
    *,
    kind: str,
    summary: str,
    detail: Mapping[str, Any],
    actor_id: str,
) -> None:
    from lemoncrow.pro.capabilities.review.session_models import ReviewActivityEvent

    store.record_activity(
        ReviewActivityEvent(
            id="",
            review_id=record.review_id,
            revision_id=record.revision_id,
            kind=kind,  # type: ignore[arg-type]
            actor_id=actor_id,
            actor_type="agent",
            subject_type="deployment",
            subject_id=f"deployment:{record.key}",
            summary=summary[:500],
            detail_json=json.dumps(dict(detail), sort_keys=True),
        )
    )


def merged_revision_id(store: Any, session: Any) -> str:
    """The Snapshot a merged Review's outcomes attach to: its current (merged) Snapshot."""

    current = str(getattr(session, "current_revision_id", "") or "")
    if current:
        return current
    latest = store.latest_revision(session.id)
    return "" if latest is None else str(latest.id)


def apply_observations(
    store: Any,
    session: Any,
    merged: MergedCommit,
    observations: Iterable[DeploymentObservation],
    relations: Mapping[str, Relation],
    settings: DeploymentSettings,
    *,
    actor_id: str,
    now: str | None = None,
) -> ApplyResult:
    """Merge provider deployment observations into one merged Review's ledger.

    Idempotent: replayed or older observations change nothing (no row write,
    no evidence, no activity). ``relations`` maps deployment key -> relation
    (:func:`~.deployments.relation_for`); a missing key is ``unknown``.
    """

    instant = now or utc_now()
    if not settings.configured:
        return ApplyResult(session.id)
    by_env: dict[str, list[DeploymentObservation]] = {}
    names: dict[str, str] = {}
    for observation in observations:
        key = observation.environment_key
        if not key:
            continue
        by_env.setdefault(key, []).append(observation)
        names.setdefault(key, observation.environment)
    if not by_env:
        return ApplyResult(session.id)
    ledger = DeploymentLedger(store)
    revision_id = merged_revision_id(store, session)
    if not revision_id:
        return ApplyResult(session.id)
    changed: list[str] = []
    changes: list[StateChange] = []
    evidence_ids: list[str] = []
    dropped_envs = 0
    for key in sorted(by_env):
        for _attempt in range(_CAS_ATTEMPTS):
            held = ledger.records(session.id)
            current = held.get(key)
            if current is None and len(held) >= MAX_ENVIRONMENTS_PER_REVIEW:
                dropped_envs += 1
                break
            prior = (
                current[1]
                if current is not None
                else EnvironmentRecord(
                    review_id=session.id, revision_id=revision_id, environment=names[key], merged=merged
                )
            )
            entries, dropped = update_entries(
                prior.entries,
                by_env[key],
                relations,
                settings,
                now=instant,
                prior_dropped=prior.entries_dropped,
            )
            if entries == prior.entries and dropped == prior.entries_dropped:
                break
            if not entries and current is None:
                break
            record = replace(
                prior, entries=entries, entries_dropped=dropped, version=prior.version + 1, updated_at=instant
            )
            if not ledger.write(record, expected_stamp=None if current is None else current[0]):
                continue
            changed.append(key)
            before = prior.outcome()
            after = record.outcome()
            evidence_ids.extend(_append_raw_evidence(store, record, prior, after))
            moved = (before.state if before else "", before.deciding_id if before else "") != (
                after.state if after else "",
                after.deciding_id if after else "",
            )
            if after is not None and moved:
                change = StateChange(
                    review_id=session.id,
                    environment=record.environment,
                    previous="" if before is None else before.state,
                    current=after.state,
                    deciding_id=after.deciding_id,
                )
                changes.append(change)
                _activity(
                    store,
                    record,
                    kind=OBSERVED_ACTIVITY,
                    summary=sentence(after),
                    detail={
                        "environment": record.environment,
                        "state": after.state,
                        "previous_state": change.previous,
                        "deployment_id": after.deciding_id,
                        "commit": after.commit,
                        "relation": after.relation,
                        "merged_commit": merged.sha,
                        "production": after.production,
                        "provider": after.provider,
                    },
                    actor_id=actor_id,
                )
            break
    return ApplyResult(session.id, tuple(changed), tuple(changes), tuple(evidence_ids), dropped_envs)


def _append_raw_evidence(
    store: Any, record: EnvironmentRecord, prior: EnvironmentRecord, outcome: EnvironmentOutcome | None
) -> list[str]:
    """One immutable row per new state of each outcome-relevant deployment (deduplicated by version)."""

    before = {entry.key: entry for entry in prior.entries}
    written: list[str] = []
    for entry in record.entries:
        if entry.closed or not _evidence_worthy(entry, outcome):
            continue
        old = before.get(entry.key)
        if old is not None and old.state == entry.state and _evidence_worthy(old, prior.outcome()):
            continue
        if entry.transitions_dropped:
            continue  # past the per-deployment transition bound: no more raw rows
        source_ref = raw_source_ref(entry)
        if store.get_evidence_by_source(record.review_id, "external", source_ref) is not None:
            continue
        stored = store.add_evidence(_evidence(record, entry, summary=False, outcome=outcome))
        written.append(str(stored.id))
    return written


# --------------------------------------------------------------------------- #
# Attention triggers (the transition API P15-OBS extends)
# --------------------------------------------------------------------------- #

OutcomeTriggerKind = Literal["deployment_failed", "deployment_rolled_back", "behavior_contradicted", "incident_linked"]
OUTCOME_TRIGGER_KINDS: Final[tuple[str, ...]] = (
    "deployment_failed",
    "deployment_rolled_back",
    "behavior_contradicted",
    "incident_linked",
)


@dataclass(frozen=True)
class OutcomeAttentionTrigger:
    """One post-merge outcome that needs the author and reviewers now.

    The hosted reconciler turns each trigger into one ``post_merge_outcome``
    ReviewAttention reason per active author/reviewer, keyed by ``subject_id``:
    the same trigger re-derived (replay, another reconcile) is the same reason,
    so it is added -- and pushed -- exactly once. A trigger holds while
    ``open_until`` is in the future; afterwards it clears on the next reconcile.
    """

    kind: OutcomeTriggerKind
    subject_type: str
    subject_id: str
    fact: str
    fact_ref: str
    revision_id: str
    observed_at: str = ""
    open_until: str = ""
    actor_id: str = ""

    def active(self, now: str) -> bool:
        return not self.open_until or self.open_until > now

    def to_wire(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "fact": self.fact,
            "fact_ref": self.fact_ref,
            "revision_id": self.revision_id,
            "observed_at": self.observed_at,
            "open_until": self.open_until,
            "actor_id": self.actor_id,
        }


OutcomeTriggerSource = Callable[[Any, Any, str], Sequence[OutcomeAttentionTrigger]]
"""``(store, session, now) -> triggers``; one source per outcome signal family."""


def deployment_attention_triggers(store: Any, session: Any, now: str) -> tuple[OutcomeAttentionTrigger, ...]:
    """A failed or rolled-back production deployment of this Review's change, while its window is open."""

    out: list[OutcomeAttentionTrigger] = []
    records = DeploymentLedger(store).records(session.id)
    for key in sorted(records):
        record = records[key][1]
        outcome = record.outcome()
        if outcome is None or not outcome.production or outcome.state not in ATTENTION_STATES:
            continue
        trigger = OutcomeAttentionTrigger(
            kind="deployment_failed" if outcome.state == "failed" else "deployment_rolled_back",
            subject_type="deployment",
            subject_id=f"deployment:{outcome.key}",
            fact=sentence(outcome),
            fact_ref=f"deployment:{outcome.provider}:{outcome.deciding_id}@{outcome.state}",
            revision_id=record.revision_id,
            observed_at=outcome.since,
            open_until=outcome.open_until,
        )
        if trigger.active(now):
            out.append(trigger)
    return tuple(out)


def contradiction_attention_triggers(store: Any, session: Any, now: str) -> tuple[OutcomeAttentionTrigger, ...]:
    """Production contradicted a prediction of this Review (P15-OBS)."""

    from lemoncrow.pro.capabilities.review.outcomes.observation_outcomes import observation_attention_triggers

    return observation_attention_triggers(store, session, now)


def incident_link_attention_triggers(store: Any, session: Any, now: str) -> tuple[OutcomeAttentionTrigger, ...]:
    """An incident was linked to this Review, advisory (P15-OBS)."""

    from lemoncrow.pro.capabilities.review.outcomes.incidents import incident_attention_triggers

    return incident_attention_triggers(store, session, now)


OBSERVATION_TRIGGER_SOURCES: Final[tuple[OutcomeTriggerSource, ...]] = (
    contradiction_attention_triggers,
    incident_link_attention_triggers,
)
OUTCOME_TRIGGER_SOURCES: Final[tuple[OutcomeTriggerSource, ...]] = (
    deployment_attention_triggers,
    *OBSERVATION_TRIGGER_SOURCES,
)
"""Deployment outcomes, then P15-OBS contradictions and incident links; the reconciler is unchanged."""


def outcome_attention_triggers(
    store: Any,
    session: Any,
    *,
    now: str | None = None,
    sources: Sequence[OutcomeTriggerSource] = OUTCOME_TRIGGER_SOURCES,
) -> tuple[OutcomeAttentionTrigger, ...]:
    """Every active outcome trigger of one Review, deduplicated by subject (first source wins)."""

    if str(getattr(session, "status", "")) == "archived":
        return ()
    instant = now or utc_now()
    seen: dict[str, OutcomeAttentionTrigger] = {}
    for source in sources:
        for trigger in source(store, session, instant):
            if trigger.kind in OUTCOME_TRIGGER_KINDS and trigger.active(instant):
                seen.setdefault(trigger.subject_id, trigger)
    return tuple(seen.values())


# --------------------------------------------------------------------------- #
# Retention closer (item 10)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ClosedWindow:
    review_id: str
    environment: str
    deployment_ids: tuple[str, ...]
    raw_rows_deleted: int = 0
    summaries_written: int = 0


@dataclass(frozen=True)
class CloseReport:
    closed: tuple[ClosedWindow, ...] = ()
    scanned: int = 0
    conflicts: int = 0
    review_ids: tuple[str, ...] = field(default=())


def _close_entry(entry: DeploymentEntry) -> DeploymentEntry:
    return replace(
        entry, closed=True, transitions=(), transitions_dropped=entry.transitions_dropped + len(entry.transitions)
    )


def close_observation_windows(
    store: Any, *, now: str | None = None, limit: int = CLOSER_BATCH, actor_id: str = "lemoncrow_retention"
) -> CloseReport:
    """Summarize deployments whose observation window closed and expire their raw signal.

    Per closing deployment: write one summary evidence row (idempotent by
    source ref), delete its raw evidence rows, then freeze its ledger entry
    (state, relation, success flag kept; per-status transitions dropped) by
    compare-and-set. A crash between steps is repaired by the next pass. The
    environment outcome stays readable from the frozen entries.
    """

    from lemoncrow.pro.capabilities.review.outcomes.observation_outcomes import close_observations

    instant = now or utc_now()
    # Production observations (P15-OBS) close first, while their deployment's entry still holds its signal.
    observed = close_observations(store, now=instant)
    ledger = DeploymentLedger(store)
    closed: list[ClosedWindow] = []
    scanned = conflicts = 0
    for stamp, record in ledger.open_rows(limit=limit):
        scanned += 1
        due = [entry for entry in record.entries if not entry.closed and entry.window_closes_at <= instant]
        if not due:
            continue
        outcome = record.outcome()
        summaries = deleted = 0
        raw_rows = [
            row
            for row in store.list_evidence(record.review_id)
            if row.kind == DEPLOYMENT_EVIDENCE_KIND and row.source == "external"
        ]
        for entry in due:
            if _evidence_worthy(entry, outcome) or any(
                row.source_ref.startswith(_raw_prefix(entry)) for row in raw_rows
            ):
                if store.get_evidence_by_source(record.review_id, "external", summary_source_ref(entry)) is None:
                    store.add_evidence(_evidence(record, _close_entry(entry), summary=True, outcome=outcome))
                    summaries += 1
            for row in raw_rows:
                if row.source_ref.startswith(_raw_prefix(entry)):
                    deleted += int(store.delete_evidence(row.id))
        due_keys = {entry.key for entry in due}
        frozen = tuple(_close_entry(entry) if entry.key in due_keys else entry for entry in record.entries)
        updated = replace(record, entries=frozen, version=record.version + 1, updated_at=instant)
        if not ledger.write(updated, expected_stamp=stamp):
            conflicts += 1  # a concurrent observation won; the next pass closes it
            continue
        after = updated.outcome()
        _activity(
            store,
            updated,
            kind=SUMMARIZED_ACTIVITY,
            summary=(
                f"Observation window closed for {len(due)} {record.environment} deployment"
                f"{'s' if len(due) != 1 else ''}; summary kept, raw signal expired."
            ),
            detail={
                "environment": record.environment,
                "deployment_ids": [entry.deployment_id for entry in due],
                "state": "" if after is None else after.state,
                "raw_rows_deleted": deleted,
            },
            actor_id=actor_id,
        )
        closed.append(
            ClosedWindow(
                review_id=record.review_id,
                environment=record.environment,
                deployment_ids=tuple(entry.deployment_id for entry in due),
                raw_rows_deleted=deleted,
                summaries_written=summaries,
            )
        )
    review_ids = tuple(dict.fromkeys((*observed.review_ids, *(item.review_id for item in closed))))
    return CloseReport(tuple(closed), scanned, conflicts, review_ids)


# --------------------------------------------------------------------------- #
# Projection (API)
# --------------------------------------------------------------------------- #


def deployment_projection(
    store: Any,
    session: Any,
    settings: DeploymentSettings,
    *,
    merged: MergedCommit | None = None,
    locus: Any = None,
) -> dict[str, Any]:
    """What Review Status shows; ``{"configured": false, ...}`` and nothing else when no connector is configured."""

    status = connector_status(settings, locus=locus)
    if not settings.configured:
        return {"configured": False, "connector": status.to_wire()}
    records = DeploymentLedger(store).records(session.id)
    outcomes = [outcome for key in sorted(records) if (outcome := records[key][1].outcome()) is not None]
    shown = outcomes[:MAX_ENVIRONMENTS_PER_REVIEW]
    merged_commit = merged or next((record.merged for _stamp, record in records.values()), None)
    return {
        "configured": True,
        "connector": status.to_wire(),
        "settings": settings.to_wire(),
        "merged": None if merged_commit is None else merged_commit.to_wire(),
        "status_line": status_line(shown),
        "environments": [outcome.to_wire() for outcome in shown],
        "environments_dropped": len(outcomes) - len(shown),
        "freshness_class": "external_observation",
        # Outcomes are post-merge observations: they never enter readiness.
        "in_readiness": False,
    }


__all__ = [
    "DEPLOYMENT_EVIDENCE_KIND",
    "DEPLOYMENT_ROW_KIND",
    "OBSERVATION_TRIGGER_SOURCES",
    "OBSERVED_ACTIVITY",
    "OUTCOME_TRIGGER_KINDS",
    "OUTCOME_TRIGGER_SOURCES",
    "SUMMARIZED_ACTIVITY",
    "ApplyResult",
    "CloseReport",
    "ClosedWindow",
    "DeploymentLedger",
    "EnvironmentRecord",
    "OutcomeAttentionTrigger",
    "OutcomeTriggerKind",
    "OutcomeTriggerSource",
    "StateChange",
    "apply_observations",
    "close_observation_windows",
    "contradiction_attention_triggers",
    "deployment_attention_triggers",
    "deployment_projection",
    "incident_link_attention_triggers",
    "merged_revision_id",
    "outcome_attention_triggers",
    "raw_source_ref",
    "summary_source_ref",
]

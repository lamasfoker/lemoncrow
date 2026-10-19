"""Store-bound predicted vs observed (P15-OBS items 3, 5, 7, 9, 10).

* **Ledger** -- one ``review_derived`` row per (merged Review, production
  deployment) under the merged Snapshot (kind ``production_observation``,
  scope ``deploy:<provider>:<deployment_id>``), written by compare-and-set.
  It freezes the predictions (behavior targets and the side-effect inventory
  of the merged Snapshot), the bounded window and the budgeted query plan when
  production first runs the change, then collects the connectors' aggregates.
* **Outcomes** -- each prediction ends ``confirmed``, ``contradicted`` or
  ``not_observed`` with its reason. A contradiction is recorded (Activity,
  attention) as soon as it is observed; the rest is decided when the window
  closes.
* **Evidence** -- one summary row of kind ``deployment`` (freshness class
  ``external_observation``, ``detail.observation = true``) per closed window,
  appended under the merged Snapshot. Nothing here edits the readiness
  record, a human judgment, the signed review record or any existing row.
* **Retention** -- closing a window drops every aggregate and incident signal
  (the raw signal) from the row and keeps the outcomes and the summary.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

from lemoncrow.pro.capabilities.review.outcomes.deployment_outcomes import (
    DEPLOYMENT_EVIDENCE_KIND,
    DeploymentLedger,
    EnvironmentRecord,
    OutcomeAttentionTrigger,
    utc_now,
)
from lemoncrow.pro.capabilities.review.outcomes.deployments import (
    CONTAINING,
    DeploymentEntry,
    environment_key,
    shifted,
    sortable_time,
)
from lemoncrow.pro.capabilities.review.outcomes.incidents import (
    Candidate,
    IncidentLink,
    correlate,
    review_links,
    store_links,
)
from lemoncrow.pro.capabilities.review.outcomes.observability import (
    MAX_PREDICTIONS,
    AggregateRefused,
    IncidentSignal,
    ObservabilitySettings,
    ObservationWindow,
    Prediction,
    SignalAggregate,
    SignalQuery,
    TargetOutcome,
    compare,
    observation_window,
    outcome_counts,
    plan_queries,
    predictions_from_deltas,
    sanitize_aggregate,
)

_LOG = logging.getLogger(__name__)

OBSERVATION_ROW_KIND: Final[str] = "production_observation"
CONTRADICTED_ACTIVITY: Final[str] = "observation.contradicted"
SUMMARIZED_ACTIVITY: Final[str] = "observation.summarized"
SUMMARY_PREFIX: Final[str] = "observation-summary:"
_ROW_STAMP: Final[str] = "observation-v1"
_OPEN: Final[str] = f"{_ROW_STAMP}:open:"
_CLOSING: Final[str] = f"{_ROW_STAMP}:closing:"
"""Frozen by the closer's winning compare-and-set; its summary is not yet published."""
_CLOSED: Final[str] = f"{_ROW_STAMP}:closed:"
_CAS_ATTEMPTS: Final[int] = 8
CLOSE_GRACE_HOURS: Final[int] = 6
"""How long after the window end a missing final answer is waited for before the window closes anyway."""
ATTENTION_HOLD_HOURS: Final[int] = 72
# lc-debt: each pass (start, plan, close) reads at most this many rows per store, oldest first; upgrade path:
# page until a pass returns fewer rows once a store keeps more production windows open at once.
SCAN_BATCH: Final[int] = 500
OBSERVER_ACTOR: Final[str] = "lemoncrow_observability"


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ObservationRecord:
    """One production deployment of one merged Review, observed against its predictions."""

    review_id: str
    revision_id: str
    environment: str
    provider: str
    deployment_id: str
    merged_commit: str
    window: ObservationWindow
    predictions: tuple[Prediction, ...] = ()
    predictions_dropped: int = 0
    queries: tuple[SignalQuery, ...] = ()
    unplanned: tuple[tuple[str, str], ...] = ()
    budget: int = 0
    services: tuple[str, ...] = ()
    connectors: tuple[str, ...] = ()
    latency_ratio: float = 1.25
    query_count_ratio: float = 1.5
    answers: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    """Query id -> sanitized aggregate: the raw signal, dropped when the window closes."""
    spent: int = 0
    outcomes: tuple[TargetOutcome, ...] = ()
    contradicted_at: Mapping[str, str] = field(default_factory=dict)
    closed: bool = False
    closed_at: str = ""
    close_reason: str = ""
    raw_dropped: int = 0
    version: int = 0
    updated_at: str = ""
    summary_pending: bool = False
    """Closed by a winning compare-and-set whose summary, contradictions and Activity are not yet published."""
    closing_contradictions: tuple[str, ...] = ()
    """Prediction keys first contradicted by the close, announced with the summary."""

    @property
    def scope(self) -> str:
        return scope_key(self.provider, self.deployment_id)

    @property
    def stamp(self) -> str:
        if self.closed:
            return f"{_CLOSING if self.summary_pending else _CLOSED}{self.version}"
        return f"{_OPEN}{self.version}"

    @property
    def remaining_budget(self) -> int:
        return max(0, self.budget - self.spent)

    @property
    def routes(self) -> frozenset[str]:
        return frozenset(item.subject for item in self.predictions if item.kind == "route")

    @property
    def hosts(self) -> frozenset[str]:
        return frozenset(item.subject for item in self.predictions if item.kind == "outbound_host")

    def thresholds(self) -> ObservabilitySettings:
        return ObservabilitySettings(
            latency_regression_ratio=self.latency_ratio, query_count_regression_ratio=self.query_count_ratio
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "revision_id": self.revision_id,
            "environment": self.environment,
            "provider": self.provider,
            "deployment_id": self.deployment_id,
            "merged_commit": self.merged_commit,
            "window": self.window.to_wire(),
            "predictions": [item.to_wire() for item in self.predictions],
            "predictions_dropped": self.predictions_dropped,
            "queries": [item.to_wire() for item in self.queries],
            "unplanned": [list(item) for item in self.unplanned],
            "budget": self.budget,
            "services": list(self.services),
            "connectors": list(self.connectors),
            "latency_ratio": self.latency_ratio,
            "query_count_ratio": self.query_count_ratio,
            "answers": {key: dict(value) for key, value in sorted(self.answers.items())},
            "spent": self.spent,
            "outcomes": [item.to_wire() for item in self.outcomes],
            "contradicted_at": dict(sorted(self.contradicted_at.items())),
            "closed": self.closed,
            "closed_at": self.closed_at,
            "close_reason": self.close_reason,
            "raw_dropped": self.raw_dropped,
            "version": self.version,
            "updated_at": self.updated_at,
            # Only while a close is mid-publication, so every other row keeps its exact wire.
            **(
                {"summary_pending": True, "closing_contradictions": list(self.closing_contradictions)}
                if self.summary_pending
                else {}
            ),
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> ObservationRecord | None:
        window_raw = raw.get("window")
        window = ObservationWindow.from_wire(window_raw) if isinstance(window_raw, Mapping) else None
        if window is None:
            return None
        try:
            answers_raw = raw.get("answers") or {}
            return cls(
                review_id=str(raw.get("review_id") or ""),
                revision_id=str(raw.get("revision_id") or ""),
                environment=str(raw.get("environment") or ""),
                provider=str(raw.get("provider") or ""),
                deployment_id=str(raw.get("deployment_id") or ""),
                merged_commit=str(raw.get("merged_commit") or ""),
                window=window,
                predictions=tuple(
                    Prediction.from_wire(item) for item in raw.get("predictions") or () if isinstance(item, Mapping)
                ),
                predictions_dropped=int(raw.get("predictions_dropped") or 0),
                queries=tuple(
                    query
                    for item in raw.get("queries") or ()
                    if isinstance(item, Mapping) and (query := SignalQuery.from_wire(item)) is not None
                ),
                unplanned=tuple(
                    (str(item[0]), str(item[1]))
                    for item in raw.get("unplanned") or ()
                    if isinstance(item, list | tuple) and len(item) == 2
                ),
                budget=int(raw.get("budget") or 0),
                services=tuple(str(item) for item in raw.get("services") or ()),
                connectors=tuple(str(item) for item in raw.get("connectors") or ()),
                latency_ratio=float(raw.get("latency_ratio") or 1.25),
                query_count_ratio=float(raw.get("query_count_ratio") or 1.5),
                answers={
                    str(key): dict(value)
                    for key, value in (answers_raw.items() if isinstance(answers_raw, Mapping) else ())
                    if isinstance(value, Mapping)
                },
                spent=int(raw.get("spent") or 0),
                outcomes=tuple(
                    TargetOutcome.from_wire(item) for item in raw.get("outcomes") or () if isinstance(item, Mapping)
                ),
                contradicted_at={str(key): str(value) for key, value in dict(raw.get("contradicted_at") or {}).items()},
                closed=bool(raw.get("closed")),
                closed_at=str(raw.get("closed_at") or ""),
                close_reason=str(raw.get("close_reason") or ""),
                raw_dropped=int(raw.get("raw_dropped") or 0),
                version=int(raw.get("version") or 0),
                updated_at=str(raw.get("updated_at") or ""),
                summary_pending=bool(raw.get("summary_pending")) and bool(raw.get("closed")),
                closing_contradictions=tuple(str(item) for item in raw.get("closing_contradictions") or ()),
            )
        except (TypeError, ValueError):
            return None


def scope_key(provider: str, deployment_id: str) -> str:
    return f"deploy:{provider}:{deployment_id}"


class ObservationLedger:
    """``review_derived`` rows (kind ``production_observation``) of one Review store."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def _row(self, record: ObservationRecord) -> Any:
        from lemoncrow.pro.capabilities.review.store import DerivedResult

        return DerivedResult(
            review_id=record.review_id,
            revision_id=record.revision_id,
            kind=OBSERVATION_ROW_KIND,
            scope_key=record.scope,
            stamp=record.stamp,
            payload=record.to_wire(),
        )

    def records(self, review_id: str) -> dict[str, tuple[str, ObservationRecord]]:
        """Scope -> ``(stamp, record)`` for one Review (every Snapshot of it)."""

        revision_ids = [revision.id for revision in self._store.list_revisions(review_id)]
        if not revision_ids:
            return {}
        out: dict[str, tuple[str, ObservationRecord]] = {}
        for (_revision_id, scope), (stamp, payload) in self._store.derived_results(
            OBSERVATION_ROW_KIND, revision_ids
        ).items():
            if not str(stamp).startswith(_ROW_STAMP + ":"):
                continue
            record = ObservationRecord.from_wire(payload)
            if record is not None and record.review_id == review_id:
                out[scope] = (str(stamp), record)
        return out

    def exists(self, revision_id: str, scope: str) -> bool:
        return (revision_id, scope) in self._store.derived_results(OBSERVATION_ROW_KIND, [revision_id])

    def write(self, record: ObservationRecord, *, expected_stamp: str | None) -> bool:
        if expected_stamp is None:
            created, _stamp, _payload = self._store.insert_derived_result(self._row(record))
            return bool(created)
        return bool(self._store.compare_and_set_derived_result(self._row(record), expected_stamp=expected_stamp))

    def open_rows(self, *, limit: int = SCAN_BATCH) -> tuple[tuple[str, ObservationRecord], ...]:
        return self._rows(_OPEN, limit=limit)

    def closing_rows(self, *, limit: int = SCAN_BATCH) -> tuple[tuple[str, ObservationRecord], ...]:
        """Rows a closer froze whose summary is not yet published (a crash or a lost race left them)."""

        return self._rows(_CLOSING, limit=limit)

    def _rows(self, prefix: str, *, limit: int) -> tuple[tuple[str, ObservationRecord], ...]:
        out: list[tuple[str, ObservationRecord]] = []
        for row in self._store.derived_results_by_kind(OBSERVATION_ROW_KIND, stamp_prefix=prefix, limit=limit):
            record = ObservationRecord.from_wire(row.payload)
            if record is not None:
                out.append((row.stamp, record))
        return tuple(out)


# --------------------------------------------------------------------------- #
# Predictions of the merged Snapshot
# --------------------------------------------------------------------------- #


def merged_deltas(store: Any, session: Any, revision: Any) -> list[Any]:
    """Behavior targets and inventoried side effects of the merged Snapshot, from what is stored.

    The static side-effect report row (when it ran) and every registered
    behavior producer over the Snapshot's stored evidence; nothing is
    recomputed against a checkout.
    """

    from lemoncrow.pro.capabilities.review.behavior import (
        BehaviorContext,
        collect_behavior,
        registered_behavior_producers,
    )
    from lemoncrow.pro.capabilities.review.side_effects import DERIVED_KIND, PRODUCER_ID, STAMP, StaticReport

    static = StaticReport("not_run", "no stored static analysis")
    row = store.derived_results(DERIVED_KIND, [revision.id]).get((revision.id, "current"))
    if row is not None and row[0] == STAMP:
        try:
            static = StaticReport.from_wire(row[1])
        except (KeyError, TypeError, ValueError):
            pass
    deltas: list[Any] = list(static.deltas) if static.status == "ran" else []
    evidence = tuple(
        item
        for item in store.list_evidence(session.id)
        if item.revision_id == revision.id and item.execution_intent != "explicit_historical"
    )
    try:
        collection = collect_behavior(
            BehaviorContext(
                review_id=session.id,
                revision=revision,
                evidence=evidence,
                read_artifact=store.read_evidence_artifact,
                prepared={PRODUCER_ID: static},
            ),
            registered_behavior_producers(),
        )
        deltas.extend(collection.deltas)
    except Exception:  # a producer failure degrades to the static inventory, never blocks observation
        _LOG.warning("behavior collection failed for production observation review_id=%s", session.id)
    return deltas


def _observed_entry(record: EnvironmentRecord) -> tuple[DeploymentEntry, str] | None:
    """The production deployment that first ran this change, and when it succeeded."""

    candidates = sorted(
        (
            entry
            for entry in record.entries
            if entry.production and entry.relation in CONTAINING and entry.succeeded and not entry.closed
        ),
        key=lambda entry: entry.order,
    )
    for entry in candidates:
        succeeded_at = next((when for state, when in entry.transitions if state == "success" and when), "")
        deployed_at = sortable_time(succeeded_at or entry.status_at)
        if deployed_at:
            return entry, deployed_at
    return None


def start_observations(
    store: Any, settings: ObservabilitySettings, *, now: str | None = None, limit: int = SCAN_BATCH
) -> tuple[str, ...]:
    """Open one observation per production deployment that runs a merged Review's change.

    Does nothing without a configured connector. Returns the Review ids whose
    observation started now.
    """

    if not settings.configured:
        return ()
    instant = now or utc_now()
    ledger = ObservationLedger(store)
    started: list[str] = []
    for _stamp, record in DeploymentLedger(store).open_rows(limit=limit):
        found = _observed_entry(record)
        if found is None:
            continue
        entry, deployed_at = found
        scope = scope_key(entry.provider, entry.deployment_id)
        if ledger.exists(record.revision_id, scope):
            continue
        session = store.get_session(record.review_id)
        revision = store.get_revision(record.revision_id)
        if session is None or revision is None or str(session.status) == "archived":
            continue
        window = observation_window(deployed_at, settings, deployment_window_closes_at=entry.window_closes_at)
        if window is None:
            continue
        predictions, dropped = predictions_from_deltas(merged_deltas(store, session, revision))
        plan = plan_queries(predictions, settings, window)
        observation = ObservationRecord(
            review_id=record.review_id,
            revision_id=record.revision_id,
            environment=record.environment,
            provider=entry.provider,
            deployment_id=entry.deployment_id,
            merged_commit=record.merged.sha,
            window=window,
            predictions=predictions,
            predictions_dropped=dropped,
            queries=plan.queries,
            unplanned=plan.unplanned,
            budget=settings.query_budget,
            services=settings.services,
            connectors=tuple(item.id for item in settings.connectors),
            latency_ratio=settings.latency_regression_ratio,
            query_count_ratio=settings.query_count_regression_ratio,
            outcomes=evaluate_predictions(predictions, plan.queries, {}, plan.unplanned, window, settings, final=False),
            updated_at=instant,
        )
        if ledger.write(observation, expected_stamp=None):
            started.append(record.review_id)
    return tuple(dict.fromkeys(started))


# --------------------------------------------------------------------------- #
# Predicted vs observed
# --------------------------------------------------------------------------- #


def evaluate_predictions(
    predictions: Sequence[Prediction],
    queries: Sequence[SignalQuery],
    answers: Mapping[str, Mapping[str, Any]],
    unplanned: Sequence[tuple[str, str]],
    window: ObservationWindow,
    thresholds: ObservabilitySettings,
    *,
    final: bool,
) -> tuple[TargetOutcome, ...]:
    by_key: dict[str, dict[tuple[str, str], SignalAggregate]] = {}
    for query in queries:
        raw = answers.get(query.id)
        if raw is None:
            continue
        try:
            aggregate = sanitize_aggregate(raw)
        except AggregateRefused:
            continue
        for key in query.prediction_keys:
            by_key.setdefault(key, {})[(query.signal, query.checkpoint)] = aggregate
    reasons = dict(unplanned)
    return tuple(
        compare(
            prediction,
            by_key.get(prediction.key, {}),
            thresholds,
            window,
            final=final,
            unplanned=reasons.get(prediction.key, ""),
        )
        for prediction in predictions
    )


def _evaluate(
    record: ObservationRecord, answers: Mapping[str, Mapping[str, Any]], *, final: bool
) -> tuple[TargetOutcome, ...]:
    outcomes = evaluate_predictions(
        record.predictions, record.queries, answers, record.unplanned, record.window, record.thresholds(), final=final
    )
    # A contradiction, once observed, is never withdrawn by a later quiet checkpoint.
    held = {item.key: item for item in record.outcomes if item.state == "contradicted"}
    return tuple(held.get(item.key, item) if item.state != "contradicted" else item for item in outcomes)


def _activity(store: Any, record: ObservationRecord, *, kind: str, summary: str, detail: Mapping[str, Any]) -> None:
    from lemoncrow.pro.capabilities.review.session_models import ReviewActivityEvent

    store.record_activity(
        ReviewActivityEvent(
            id="",
            review_id=record.review_id,
            revision_id=record.revision_id,
            kind=kind,  # type: ignore[arg-type]
            actor_id=OBSERVER_ACTOR,
            actor_type="agent",
            subject_type="deployment",
            subject_id=f"observation:{record.scope}",
            summary=summary[:500],
            detail_json=json.dumps(dict(detail), sort_keys=True),
        )
    )


def _announce_contradictions(store: Any, record: ObservationRecord, keys: Sequence[str]) -> None:
    by_key = {item.key: item for item in record.outcomes}
    for key in keys:
        outcome = by_key.get(key)
        if outcome is None:
            continue
        _activity(
            store,
            record,
            kind=CONTRADICTED_ACTIVITY,
            summary=f"Production contradicted a prediction in {record.environment}: {outcome.reason}",
            detail={
                "environment": record.environment,
                "deployment_id": record.deployment_id,
                "behavior_key": key,
                "subject": outcome.subject,
                "signals": list(outcome.signals),
                "freshness_class": "external_observation",
            },
        )


# --------------------------------------------------------------------------- #
# Plans out, aggregates in
# --------------------------------------------------------------------------- #


def plan_wire(record: ObservationRecord, *, now: str) -> dict[str, Any] | None:
    """The due, unanswered queries of one open window that fit its remaining budget (``None``: nothing due)."""

    if record.closed or now > shifted(record.window.end, hours=CLOSE_GRACE_HOURS):
        return None
    remaining = record.remaining_budget
    due: list[SignalQuery] = []
    for query in sorted(record.queries, key=lambda item: (item.checkpoint != "final", item.due_at, item.id)):
        if query.id in record.answers or not query.due_at or query.due_at > now or query.cost > remaining:
            continue
        due.append(query)
        remaining -= query.cost
    if not due:
        return None
    return {
        "review_id": record.review_id,
        "revision_id": record.revision_id,
        "scope": record.scope,
        "environment": record.environment,
        "deployment_id": record.deployment_id,
        "window": record.window.to_wire(),
        "queries": [item.to_wire() for item in due],
        "remaining_budget": record.remaining_budget,
        "routes": sorted(record.routes),
        "hosts": sorted(record.hosts),
    }


def due_plans(store: Any, *, now: str | None = None, limit: int = SCAN_BATCH) -> list[dict[str, Any]]:
    instant = now or utc_now()
    return [
        plan
        for _stamp, record in ObservationLedger(store).open_rows(limit=limit)
        if (plan := plan_wire(record, now=instant)) is not None
    ]


@dataclass(frozen=True)
class RecordResult:
    accepted: tuple[str, ...] = ()
    refused: tuple[tuple[str, str], ...] = ()
    contradicted: tuple[str, ...] = ()
    incidents: tuple[IncidentLink, ...] = ()
    closed: bool = False


def _scoped_incident(incident: IncidentSignal, record: ObservationRecord) -> IncidentSignal:
    """Keep only labels that name this deployment's own subjects (defense in depth after the runner)."""

    return IncidentSignal(
        source=incident.source,
        id=incident.id,
        name=incident.name,
        service=incident.service if incident.service in record.services else "",
        route=incident.route if incident.route in record.routes else "",
        host=incident.host if incident.host in record.hosts else "",
        started_at=incident.started_at,
        fingerprint=incident.fingerprint,
    )


def record_aggregates(
    store: Any,
    review_id: str,
    scope: str,
    aggregates: Iterable[Any],
    *,
    now: str | None = None,
) -> RecordResult:
    """Accept a connector's aggregates for one window: each must be planned, due, unanswered and in budget.

    Anything that is not a bounded aggregate is refused and never stored.
    Raises ``LookupError`` when the window does not exist.
    """

    instant = now or utc_now()
    batch = list(aggregates)
    ledger = ObservationLedger(store)
    for _attempt in range(_CAS_ATTEMPTS):
        held = ledger.records(review_id).get(scope)
        if held is None:
            raise LookupError(scope)
        stamp, record = held
        if record.closed or instant > shifted(record.window.end, hours=CLOSE_GRACE_HOURS):
            return RecordResult(refused=tuple(("", "window_closed") for _ in batch), closed=True)
        queries = {item.id: item for item in record.queries}
        answers = {key: dict(value) for key, value in record.answers.items()}
        spent = record.spent
        accepted: list[str] = []
        refused: list[tuple[str, str]] = []
        incidents: list[IncidentSignal] = []
        for raw in batch:
            try:
                aggregate = sanitize_aggregate(raw)
            except AggregateRefused:
                refused.append(
                    (str(raw.get("query_id") or "")[:40] if isinstance(raw, Mapping) else "", "not_aggregate")
                )
                continue
            query = queries.get(aggregate.query_id)
            if query is None:
                refused.append((aggregate.query_id, "unknown_query"))
            elif query.id in answers:
                refused.append((query.id, "already_answered"))
            elif query.due_at > instant:
                refused.append((query.id, "not_due"))
            elif spent + query.cost > record.budget:
                refused.append((query.id, "budget"))
            else:
                scoped = tuple(_scoped_incident(item, record) for item in aggregate.incidents)
                stored = replace(aggregate, incidents=scoped, observed_at=instant)
                answers[query.id] = stored.to_wire()
                spent += query.cost
                accepted.append(query.id)
                if query.signal == "incident_scan":
                    incidents.extend(scoped)
        if not accepted:
            return RecordResult(refused=tuple(refused))
        outcomes = _evaluate(record, answers, final=False)
        fresh = [
            item.key for item in outcomes if item.state == "contradicted" and item.key not in record.contradicted_at
        ]
        updated = replace(
            record,
            answers=answers,
            spent=spent,
            outcomes=outcomes,
            contradicted_at={**record.contradicted_at, **{key: instant for key in fresh}},
            version=record.version + 1,
            updated_at=instant,
        )
        if not ledger.write(updated, expected_stamp=stamp):
            continue
        _announce_contradictions(store, updated, fresh)
        links = link_incidents(store, incidents, now=instant)
        return RecordResult(tuple(accepted), tuple(refused), tuple(fresh), links)
    raise RuntimeError("production observation write conflicted repeatedly")


def link_incidents(store: Any, incidents: Sequence[IncidentSignal], *, now: str) -> tuple[IncidentLink, ...]:
    """Correlate incident signals against every open production window of this store (advisory)."""

    if not incidents:
        return ()
    candidates = [
        Candidate(
            review_id=record.review_id,
            revision_id=record.revision_id,
            environment=record.environment,
            deployment_id=record.deployment_id,
            window=record.window,
            services=record.services,
            routes=record.routes,
            hosts=record.hosts,
        )
        for _stamp, record in ObservationLedger(store).open_rows()
    ]
    links: list[IncidentLink] = []
    for incident in {item.key: item for item in incidents}.values():
        links.extend(correlate(incident, candidates))
    return store_links(store, links, now=now, actor_id=OBSERVER_ACTOR)


# --------------------------------------------------------------------------- #
# Closer (raw signal expiry) and the summary evidence
# --------------------------------------------------------------------------- #


def summary_source_ref(record: ObservationRecord) -> str:
    return f"{SUMMARY_PREFIX}{record.provider}:{record.deployment_id}"


def _summary_evidence(record: ObservationRecord) -> Any:
    from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence

    counts = outcome_counts(record.outcomes)
    detail = {
        "freshness_class": "external_observation",
        "observation": True,
        "summary": True,
        "environment": record.environment,
        "deployment_id": record.deployment_id,
        "merged_commit": record.merged_commit,
        "window": record.window.to_wire(),
        "close_reason": record.close_reason,
        "counts": counts,
        "outcomes": [
            {key: item.to_wire()[key] for key in ("key", "kind", "subject", "change", "state", "reason")}
            for item in record.outcomes[:MAX_PREDICTIONS]
        ],
        "predictions_dropped": record.predictions_dropped,
        "queries_planned": len(record.queries),
        "budget": record.budget,
        "spent": record.spent,
        "connectors": list(record.connectors),
        "source_timestamp": record.closed_at,
    }
    title = (
        f"Production observation in {record.environment}: {counts['confirmed']} confirmed, "
        f"{counts['contradicted']} contradicted, {counts['not_observed']} not observed"
    )
    return ReviewEvidence(
        id="",
        review_id=record.review_id,
        revision_id=record.revision_id,
        kind=DEPLOYMENT_EVIDENCE_KIND,  # type: ignore[arg-type]
        title=title[:200],
        source="external",
        source_ref=summary_source_ref(record),
        detail=json.dumps(detail, sort_keys=True),
        provider=record.provider,
        provider_revision=record.merged_commit,
        environment=record.environment,
        remote_identity=f"deployment:{record.deployment_id}",
        observed_at=record.closed_at,
    )


@dataclass(frozen=True)
class ObservationCloseReport:
    review_ids: tuple[str, ...] = ()
    closed: int = 0
    conflicts: int = 0
    raw_dropped: int = 0
    failed: int = 0
    """Rows whose close raised (a malformed or poison row); each is isolated and retried by the next pass."""


def _rolled_back(store: Any, record: ObservationRecord) -> bool:
    held = DeploymentLedger(store).records(record.review_id).get(environment_key(record.environment))
    outcome = None if held is None else held[1].outcome()
    return outcome is not None and outcome.state == "rolled_back"


CLOSE_CLAIM_MINUTES: Final[int] = 5
"""How long a closer's claim on a frozen row is honoured before another pass takes over its publication."""


def _published_activity(store: Any, record: ObservationRecord) -> tuple[set[str], bool]:
    """Contradiction keys already announced for this window, and whether its summary Activity exists."""

    subject = f"observation:{record.scope}"
    announced: set[str] = set()
    summarized = False
    # lc-debt: reads the newest 500 contradiction/summary events of the Review; a Review with more such
    # events (many windows) may miss an older announcement and announce it again. Upgrade path: an
    # activity lookup by subject id.
    for event in store.list_activity_events(
        record.review_id, kinds=(CONTRADICTED_ACTIVITY, SUMMARIZED_ACTIVITY), limit=500, newest_first=True
    ):
        if event.subject_id != subject:
            continue
        if event.kind == SUMMARIZED_ACTIVITY:
            summarized = True
            continue
        try:
            detail = json.loads(event.detail_json or "{}")
        except ValueError:
            continue
        if isinstance(detail, Mapping) and detail.get("deployment_id") == record.deployment_id:
            announced.add(str(detail.get("behavior_key") or ""))
    return announced, summarized


def _publish_close(store: Any, record: ObservationRecord) -> None:
    """Publish one frozen window: summary evidence, first contradictions, the summarized Activity.

    Reads only the frozen row (nothing can change it any more) and skips each
    part that an earlier, interrupted publication of the same row already wrote.
    """

    if store.get_evidence_by_source(record.review_id, "external", summary_source_ref(record)) is None:
        store.add_evidence(_summary_evidence(record))
    announced, summarized = _published_activity(store, record)
    _announce_contradictions(store, record, [key for key in record.closing_contradictions if key not in announced])
    if summarized:
        return
    counts = outcome_counts(record.outcomes)
    _activity(
        store,
        record,
        kind=SUMMARIZED_ACTIVITY,
        summary=(
            f"Production observation closed in {record.environment}: {counts['confirmed']} confirmed, "
            f"{counts['contradicted']} contradicted, {counts['not_observed']} not observed; summary kept, "
            "raw signal expired."
        ),
        detail={
            "environment": record.environment,
            "deployment_id": record.deployment_id,
            "counts": counts,
            "close_reason": record.close_reason,
            "raw_dropped": record.raw_dropped,
        },
    )


def _finish_close(store: Any, ledger: ObservationLedger, stamp: str, record: ObservationRecord, *, now: str) -> bool:
    """Publish a frozen row this pass holds the claim on, then mark it closed (compare-and-set)."""

    _publish_close(store, record)
    done = replace(record, summary_pending=False, closing_contradictions=(), version=record.version + 1, updated_at=now)
    return ledger.write(done, expected_stamp=stamp)


def _recover_closing(store: Any, ledger: ObservationLedger, *, now: str, limit: int) -> tuple[list[str], int, int]:
    """Finish publication of rows whose closer stopped after freezing them (crash, lost process)."""

    finished: list[str] = []
    conflicts = failed = 0
    for stamp, record in ledger.closing_rows(limit=limit):
        if record.updated_at and now < shifted(record.updated_at, hours=CLOSE_CLAIM_MINUTES / 60):
            continue  # its closer may still be publishing
        try:
            claimed = replace(record, version=record.version + 1, updated_at=now)
            if not ledger.write(claimed, expected_stamp=stamp):
                conflicts += 1
                continue
            if _finish_close(store, ledger, claimed.stamp, claimed, now=now):
                finished.append(record.review_id)
        except Exception:
            # One poison row never stops the pass for every other deployment; it stays closing and is
            # retried after its claim lapses.
            failed += 1
            _LOG.exception("observation close recovery failed review_id=%s", record.review_id)
    return finished, conflicts, failed


def _close_one(
    store: Any, ledger: ObservationLedger, stamp: str, record: ObservationRecord, *, now: str
) -> tuple[str, int]:
    """Close one open row if it is due: ``("skipped" | "conflict" | "frozen" | "closed", raw dropped)``."""

    finals = [item for item in record.queries if item.checkpoint == "final"]
    answered = all(item.id in record.answers for item in finals)
    rolled_back = _rolled_back(store, record)
    grace_over = now >= shifted(record.window.end, hours=CLOSE_GRACE_HOURS)
    if not (rolled_back or (now >= record.window.end and (answered or grace_over))):
        return "skipped", 0
    outcomes = _evaluate(record, record.answers, final=True)
    if rolled_back:
        outcomes = tuple(
            (
                replace(item, reason="Not observed: production rolled back before the observation window closed.")
                if item.state == "not_observed" and item.kind != "unobservable"
                else item
            )
            for item in outcomes
        )
    fresh = [item.key for item in outcomes if item.state == "contradicted" and item.key not in record.contradicted_at]
    frozen = replace(
        record,
        answers={},
        outcomes=outcomes,
        contradicted_at={**record.contradicted_at, **{key: now for key in fresh}},
        closed=True,
        closed_at=now,
        close_reason="rolled_back" if rolled_back else "window",
        raw_dropped=record.raw_dropped + len(record.answers),
        version=record.version + 1,
        updated_at=now,
        summary_pending=True,
        closing_contradictions=tuple(fresh),
    )
    # The compare-and-set decides first: a closer holding a stale read publishes nothing.
    if not ledger.write(frozen, expected_stamp=stamp):
        return "conflict", 0
    return ("closed" if _finish_close(store, ledger, frozen.stamp, frozen, now=now) else "frozen"), len(record.answers)


def close_observations(store: Any, *, now: str | None = None, limit: int = SCAN_BATCH) -> ObservationCloseReport:
    """Decide every outcome of windows that ended (or whose change was rolled back) and expire the raw signal.

    Per closing window, two compare-and-sets around the publication:

    1. freeze the open row (outcomes decided, aggregates and incident signals
       dropped) as ``closing``. A closer that loses this race publishes
       nothing; the row it read was stale.
    2. publish the summary evidence, the newly contradicted predictions and the
       summarized Activity from the frozen row -- which no worker answer can
       change any more -- then mark it ``closed``.

    A pass first finishes rows an earlier closer froze but did not publish
    (after :data:`CLOSE_CLAIM_MINUTES`). Runs whether or not a connector is
    still configured. Each row is isolated: one that raises is counted in
    ``failed``, logged, and left for the next pass while the rest close.
    lc-debt: a takeover after CLOSE_CLAIM_MINUTES of a closer that is still
    publishing can append one duplicate Activity line (evidence and announced
    keys are re-checked); upgrade path: one store transaction for the
    compare-and-set and the publication.
    """

    instant = now or utc_now()
    ledger = ObservationLedger(store)
    recovered, conflicts, failed = _recover_closing(store, ledger, now=instant, limit=limit)
    review_ids: list[str] = list(recovered)
    closed = len(recovered)
    dropped = 0
    for stamp, record in ledger.open_rows(limit=limit):
        try:
            state, raw = _close_one(store, ledger, stamp, record, now=instant)
        except Exception:
            # A frozen row whose publication raised stays closing and is recovered by a later pass.
            failed += 1
            _LOG.exception("observation close failed review_id=%s", record.review_id)
            continue
        dropped += raw
        if state == "conflict":
            conflicts += 1
        elif state == "closed":
            closed += 1
            review_ids.append(record.review_id)
    return ObservationCloseReport(tuple(dict.fromkeys(review_ids)), closed, conflicts, dropped, failed)


# --------------------------------------------------------------------------- #
# Attention (the P15-DEPLOY transition API) and the projection
# --------------------------------------------------------------------------- #


def observation_attention_triggers(store: Any, session: Any, now: str) -> tuple[OutcomeAttentionTrigger, ...]:
    """Production contradicted a prediction of this Review: held for ATTENTION_HOLD_HOURS after the latest one."""

    out: list[OutcomeAttentionTrigger] = []
    records = ObservationLedger(store).records(session.id)
    for scope in sorted(records):
        record = records[scope][1]
        contradicted = [item for item in record.outcomes if item.state == "contradicted"]
        if not contradicted:
            continue
        latest = max((record.contradicted_at.get(item.key, "") for item in contradicted), default="")
        more = f" (and {len(contradicted) - 1} more)" if len(contradicted) > 1 else ""
        trigger = OutcomeAttentionTrigger(
            kind="behavior_contradicted",
            subject_type="deployment",
            subject_id=f"observation:{record.scope}",
            fact=f"Production contradicted a prediction in {record.environment}: {contradicted[0].reason}{more}",
            fact_ref=f"observation:{record.scope}@{len(contradicted)}:{latest}",
            revision_id=record.revision_id,
            observed_at=latest,
            open_until=shifted(latest, hours=ATTENTION_HOLD_HOURS) if latest else "",
        )
        if trigger.active(now):
            out.append(trigger)
    return tuple(out)


def _status_line(records: Sequence[ObservationRecord], now: str) -> str:
    if not records:
        return "No production observation yet"
    counts = outcome_counts(item for record in records for item in record.outcomes)
    parts = [
        f"{counts[state]} {label}"
        for state, label in (
            ("contradicted", "contradicted"),
            ("confirmed", "confirmed"),
            ("not_observed", "not observed"),
        )
        if counts[state]
    ]
    observing = [record for record in records if not record.closed]
    if observing:
        until = max(record.window.end for record in observing)
        head = f"Observing production until {until[:16].replace('T', ' ')} UTC"
        return f"{head}: {', '.join(parts)}" if parts else head
    return "Production: " + (", ".join(parts) if parts else "nothing to observe")


def observation_projection(
    store: Any, session: Any, settings: ObservabilitySettings, *, now: str | None = None
) -> dict[str, Any]:
    """Predicted vs observed and incident links for Review Status; ``{"configured": false}`` without a connector."""

    if not settings.configured:
        return {"configured": False}
    instant = now or utc_now()
    held = ObservationLedger(store).records(session.id)
    records = sorted((record for _stamp, record in held.values()), key=lambda item: (item.window.start, item.scope))
    links = review_links(store, session.id)
    return {
        "configured": True,
        "locus": settings.locus,
        "connectors": [{"id": item.id, "kind": item.kind} for item in settings.connectors],
        "status_line": _status_line(records, instant),
        "observations": [
            {
                "environment": record.environment,
                "deployment_id": record.deployment_id,
                "provider": record.provider,
                "merged_commit": record.merged_commit,
                "window": record.window.to_wire(),
                "state": "closed" if record.closed else "observing",
                "close_reason": record.close_reason,
                "counts": outcome_counts(record.outcomes),
                "outcomes": [item.to_wire() for item in record.outcomes],
                "predictions_dropped": record.predictions_dropped,
                "budget": record.budget,
                "spent": record.spent,
                "queries_planned": len(record.queries),
                "queries_answered": len(record.answers) if not record.closed else None,
            }
            for record in records
        ],
        "incidents": [link.to_wire() for link in reversed(links)][:20],
        "incidents_total": len(links),
        "freshness_class": "external_observation",
        # Production outcomes are post-merge observations: they never enter readiness.
        "in_readiness": False,
    }


def production_records(store: Any, review_id: str) -> tuple[ObservationRecord, ...]:
    return tuple(record for _stamp, record in ObservationLedger(store).records(review_id).values())


__all__ = [
    "ATTENTION_HOLD_HOURS",
    "CLOSE_CLAIM_MINUTES",
    "CLOSE_GRACE_HOURS",
    "CONTRADICTED_ACTIVITY",
    "OBSERVATION_ROW_KIND",
    "SCAN_BATCH",
    "SUMMARIZED_ACTIVITY",
    "ObservationCloseReport",
    "ObservationLedger",
    "ObservationRecord",
    "RecordResult",
    "close_observations",
    "due_plans",
    "evaluate_predictions",
    "link_incidents",
    "merged_deltas",
    "observation_attention_triggers",
    "observation_projection",
    "plan_wire",
    "production_records",
    "record_aggregates",
    "scope_key",
    "start_observations",
    "summary_source_ref",
]

"""Author track record raises scrutiny (Phase 11 item 16).

Scored **per agent/model, never per human**. The author of the Change under
Review must be an agent known from exact provenance in an agent-only Review
(``actor_type == "agent"``); a human, mixed or unknown author gets no record,
no stored value, no API field and no UI. Only past Changes attributable to that
same agent count: an agent-only Review whose every Snapshot carries exact
provenance naming the same host and model.

Window: Reviews of this repository store updated in the last
:data:`WINDOW_DAYS` days, newest first, at most :data:`MAX_REVIEWS_EXAMINED`
examined (the current Review excluded). Past the bound the record is
**unknown**, not clean: the result carries ``truncated`` and the run plan shows
an ``unknown`` note (``track_record_assessment``), even when no metric is poor.
Project Knowledge (PK0) cannot replace this scan yet: its index is keyed by
repository subject and carries no agent/model dimension or outcome rows, so the
migration to an indexed read is assigned to Trust T7 (see ``docs/status``).

Metrics (each with a minimum sample and a threshold; ``>=`` is poor):

* ``revert_rate`` -- attributed Changes with a ``rolled_back`` deployment
  outcome in any environment / attributed Changes with a settled deployment
  outcome (P15-DEPLOY). Poor at >= 20% over at least 5 deployed Changes.
* ``escaped_defects`` -- attributed Changes whose *production* deployment
  outcome is ``failed`` or ``rolled_back``. Poor at >= 2.
* ``contradicted_claims`` -- R5 agent claim verdicts on each attributed
  Change's latest Snapshot (Phase 9R item 8): contradicted / (confirmed +
  contradicted). Poor at >= 30% over at least 5 decided claims.
* ``confirmed_findings`` -- AI-review findings a reviewer confirmed
  (``confirmed_by_reviewer`` or ``fixed``) per attributed Change. Poor at >=
  1.0 per Change over at least 5 Changes.

A poor metric becomes an ``author_track_record`` escalation fact; the existing
P11-ESC escalation raises the Auto rung within its ceiling and the Review
budget, and the reason names the agent/model and the metric.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from lemoncrow.pro.capabilities.review.independence import AgentAuthor, authorship, exact_agent_author

WINDOW_DAYS: Final[int] = 90
# lc-debt: examines at most 200 Reviews (one store pass per Review: Snapshots, deployment
# ledger, latest claim verdicts, annotations) when a Snapshot's plan is first built; past it the
# record is reported unknown. Upgrade path: Trust T7 reads an outcome-linked history from the
# Project Knowledge/outcome index keyed by (repository, agent host, agent model).
MAX_REVIEWS_EXAMINED: Final[int] = 200
_PAGE: Final[int] = 50

MIN_DEPLOYED_CHANGES: Final[int] = 5
REVERT_RATE_THRESHOLD: Final[float] = 0.2
ESCAPED_DEFECTS_THRESHOLD: Final[int] = 2
MIN_DECIDED_CLAIMS: Final[int] = 5
CONTRADICTED_RATE_THRESHOLD: Final[float] = 0.3
MIN_CHANGES_FOR_FINDINGS: Final[int] = 5
CONFIRMED_FINDINGS_PER_CHANGE_THRESHOLD: Final[float] = 1.0

METRICS: Final[tuple[str, ...]] = ("revert_rate", "escaped_defects", "contradicted_claims", "confirmed_findings")
_SETTLED_DEPLOYMENT_STATES: Final[frozenset[str]] = frozenset({"deployed", "failed", "rolled_back", "superseded"})
_ESCAPED_STATES: Final[frozenset[str]] = frozenset({"failed", "rolled_back"})
_CONFIRMED_DISPOSITIONS: Final[frozenset[str]] = frozenset({"confirmed_by_reviewer", "fixed"})


@dataclass(frozen=True, slots=True)
class ChangeOutcome:
    """The outcome facts of one past Change attributed to the agent."""

    review_id: str
    deployed: bool = False
    reverted: bool = False
    escaped: bool = False
    claims_confirmed: int = 0
    claims_contradicted: int = 0
    confirmed_findings: int = 0


@dataclass(frozen=True, slots=True)
class TrackRecord:
    agent: AgentAuthor
    changes: tuple[ChangeOutcome, ...] = ()
    examined: int = 0
    #: More Reviews in the window than :data:`MAX_REVIEWS_EXAMINED`.
    truncated: bool = False

    @property
    def scope(self) -> str:
        bound = f"the {MAX_REVIEWS_EXAMINED} most recent Reviews" if self.truncated else "Reviews"
        return f"{bound} of the last {WINDOW_DAYS} days"

    @property
    def unknown(self) -> str:
        """Why this record is incomplete (``""`` when every in-window Review was examined)."""

        if not self.truncated:
            return ""
        return (
            f"author track record of {self.agent.label} is unknown beyond the {MAX_REVIEWS_EXAMINED} most recent "
            f"Reviews of the last {WINDOW_DAYS} days"
        )

    def poor(self) -> tuple[tuple[str, str], ...]:
        """``(metric, detail)`` for every metric past its threshold."""

        out: list[tuple[str, str]] = []
        count = len(self.changes)
        deployed = sum(1 for row in self.changes if row.deployed)
        reverted = sum(1 for row in self.changes if row.reverted)
        if deployed >= MIN_DEPLOYED_CHANGES and reverted / deployed >= REVERT_RATE_THRESHOLD:
            out.append(
                (
                    "revert_rate",
                    f"revert rate {reverted} of {deployed} deployed Changes rolled back "
                    f"({round(100 * reverted / deployed)}%) in {self.scope}",
                )
            )
        escaped = sum(1 for row in self.changes if row.escaped)
        if escaped >= ESCAPED_DEFECTS_THRESHOLD:
            out.append(
                (
                    "escaped_defects",
                    f"escaped defects: {escaped} Changes failed or were rolled back in production in {self.scope}",
                )
            )
        confirmed = sum(row.claims_confirmed for row in self.changes)
        contradicted = sum(row.claims_contradicted for row in self.changes)
        decided = confirmed + contradicted
        if decided >= MIN_DECIDED_CLAIMS and contradicted / decided >= CONTRADICTED_RATE_THRESHOLD:
            out.append(
                (
                    "contradicted_claims",
                    f"contradicted claims: {contradicted} of {decided} verified claims "
                    f"({round(100 * contradicted / decided)}%) in {self.scope}",
                )
            )
        findings = sum(row.confirmed_findings for row in self.changes)
        if count >= MIN_CHANGES_FOR_FINDINGS and findings / count >= CONFIRMED_FINDINGS_PER_CHANGE_THRESHOLD:
            out.append(
                (
                    "confirmed_findings",
                    f"confirmed findings: {findings} reviewer-confirmed AI findings across {count} Changes "
                    f"({findings / count:.1f} per Change) in {self.scope}",
                )
            )
        return tuple(out)


def scored_author(session: Any, revision: Any) -> AgentAuthor | None:
    """The agent whose record applies, or ``None`` -- never a human, mixed or unknown author."""

    if str(getattr(session, "actor_type", "") or "") != "agent":
        return None
    kind, author = authorship(session, revision)
    return author if kind == "agent" else None


def change_agent(store: Any, session: Any) -> AgentAuthor | None:
    """The one agent a past agent-only Review is attributable to (every Snapshot exact, same agent)."""

    if str(getattr(session, "actor_type", "") or "") != "agent":
        return None
    agents: set[AgentAuthor] = set()
    for revision in store.list_revisions(session.id):
        agent = exact_agent_author(revision.provenance_host, revision.provenance_model, revision.provenance_certainty)
        if agent is None:
            return None
        agents.add(agent)
    return next(iter(agents)) if len(agents) == 1 else None


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def change_outcome(store: Any, session: Any) -> ChangeOutcome:
    from lemoncrow.pro.capabilities.review.check_api import latest_results
    from lemoncrow.pro.capabilities.review.outcomes.deployment_outcomes import DeploymentLedger
    from lemoncrow.pro.capabilities.review.outcomes.observation_outcomes import production_records

    deployed = reverted = escaped = False
    for _key, (_stamp, record) in DeploymentLedger(store).records(session.id).items():
        outcome = record.outcome()
        if outcome is None or outcome.state not in _SETTLED_DEPLOYMENT_STATES:
            continue
        deployed = True
        reverted = reverted or outcome.state == "rolled_back"
        escaped = escaped or (outcome.production and outcome.state in _ESCAPED_STATES)
    # P15-OBS: production contradicted a prediction (an incident correlation never counts), and a
    # Revert linked to this Change.
    for observed in production_records(store, session.id):
        deployed = True
        escaped = escaped or any(item.state == "contradicted" for item in observed.outcomes)
    reverted = reverted or any(
        item.relation == "reverted_by" for item in store.list_relationships(session.id, kind="change")
    )
    confirmed = contradicted = 0
    latest = store.latest_revision(session.id)
    if latest is not None:
        differentials, _verdicts = latest_results(store, latest)
        for differential in differentials.values():
            for row in differential.get("claims") or ():
                verdict = row.get("verdict") if isinstance(row, Mapping) else ""
                confirmed += int(verdict == "confirmed")
                contradicted += int(verdict == "contradicted")
    findings = sum(
        1
        for annotation in store.list_annotations(session.id)
        if annotation.source == "ai_review"
        and not annotation.parent_id
        and str(getattr(annotation, "finding_disposition", "") or "") in _CONFIRMED_DISPOSITIONS
    )
    return ChangeOutcome(
        review_id=session.id,
        deployed=deployed,
        reverted=reverted,
        escaped=escaped,
        claims_confirmed=confirmed,
        claims_contradicted=contradicted,
        confirmed_findings=findings,
    )


def track_record(store: Any, agent: AgentAuthor, *, exclude_review_id: str, now: datetime | None = None) -> TrackRecord:
    """*agent*'s record over this store's bounded window."""

    cutoff = (now or datetime.now(UTC)) - timedelta(days=WINDOW_DAYS)
    changes: list[ChangeOutcome] = []
    examined = 0
    truncated = False
    cursor: tuple[str, str] | None = None
    while True:
        page = store.list_sessions(status="", limit=_PAGE, after=cursor)
        if not page:
            break
        for session in page:
            updated = _parse(str(session.updated_at or ""))
            if updated is not None and updated < cutoff:
                return TrackRecord(agent, tuple(changes), examined, truncated)
            if session.id == exclude_review_id or updated is None:
                continue
            if examined >= MAX_REVIEWS_EXAMINED:
                truncated = True
                return TrackRecord(agent, tuple(changes), examined, truncated)
            examined += 1
            if change_agent(store, session) != agent:
                continue
            changes.append(change_outcome(store, session))
        cursor = (str(page[-1].updated_at), str(page[-1].id))
        if len(page) < _PAGE:
            break
    return TrackRecord(agent, tuple(changes), examined, truncated)


@dataclass(frozen=True, slots=True)
class TrackRecordAssessment:
    """What the plan learns from one agent's record: poor-metric rows and any unknown coverage."""

    #: ``(agent, metric, detail)`` for every poor metric.
    rows: tuple[tuple[str, str, str], ...] = ()
    #: Set when the bounded window did not cover every in-window Review.
    unknown: str = ""


def track_record_assessment(
    store: Any, session: Any, revision: Any, *, now: datetime | None = None
) -> TrackRecordAssessment | None:
    """The scored agent's assessment, or ``None`` for any non-agent author."""

    agent = scored_author(session, revision)
    if agent is None:
        return None
    record = track_record(store, agent, exclude_review_id=str(session.id), now=now)
    return TrackRecordAssessment(
        tuple((agent.label, metric, detail) for metric, detail in record.poor()), record.unknown
    )


def track_record_facts(
    store: Any, session: Any, revision: Any, *, now: datetime | None = None
) -> tuple[tuple[str, str, str], ...]:
    """``(agent, metric, detail)`` escalation rows for a poor record; ``()`` for any non-agent author."""

    assessment = track_record_assessment(store, session, revision, now=now)
    return assessment.rows if assessment else ()


__all__ = [
    "CONFIRMED_FINDINGS_PER_CHANGE_THRESHOLD",
    "CONTRADICTED_RATE_THRESHOLD",
    "ESCAPED_DEFECTS_THRESHOLD",
    "MAX_REVIEWS_EXAMINED",
    "METRICS",
    "MIN_CHANGES_FOR_FINDINGS",
    "MIN_DECIDED_CLAIMS",
    "MIN_DEPLOYED_CHANGES",
    "REVERT_RATE_THRESHOLD",
    "WINDOW_DAYS",
    "ChangeOutcome",
    "TrackRecord",
    "TrackRecordAssessment",
    "change_agent",
    "change_outcome",
    "scored_author",
    "track_record",
    "track_record_assessment",
    "track_record_facts",
]

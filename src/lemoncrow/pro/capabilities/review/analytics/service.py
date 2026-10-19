"""One analytics read over already-authorized repositories (shared by the local and hosted servers).

The host resolves access before calling :func:`analyze`: it passes only
repositories the caller may read, inside the caller's organization, each with
its own (tenant-scoped) Review store. This module applies the remaining scope
-- period, repositories, teams and paths -- reads facts through the read-only
store guard, and builds the report and its drill index.

Bounds: at most :data:`~.scope.MAX_REVIEWS` Reviews are analyzed (newest
created first). The report states how many candidates existed and whether it
was truncated. Production-outcome ledgers are scanned at most
``outcome_feeds`` ``SCAN_BATCH`` rows per kind per store; past that the report
says ``escapes_truncated``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from lemoncrow.pro.capabilities.review.analytics.facts import (
    CostClass,
    HostUsage,
    ReadOnlyStore,
    ReviewFacts,
    first_party_missing,
    review_facts,
    unavailable_sources,
)
from lemoncrow.pro.capabilities.review.analytics.load import reviewer_load
from lemoncrow.pro.capabilities.review.analytics.report import AnalyticsReport, DrillIndex, build_report, select_facts
from lemoncrow.pro.capabilities.review.analytics.scope import MAX_REVIEWS, AnalyticsScope
from lemoncrow.pro.capabilities.review.freshness import parse_timestamp

#: Sessions read per page while looking for a period's Reviews.
SESSION_PAGE: Final[int] = 500
# lc-debt: candidates are found by paging one repository's sessions newest-updated
# first until the period starts, at most MAX_SESSION_SCAN rows (then the report says
# ``scan_truncated``); upgrade path: a created_at-indexed store query.
MAX_SESSION_SCAN: Final[int] = 5_000


def sessions_in_period(
    store: Any, scope: AnalyticsScope, *, page: int = SESSION_PAGE, max_scan: int = MAX_SESSION_SCAN
) -> tuple[tuple[Any, ...], bool]:
    """``(sessions created in the period, scan_truncated)`` of one Review store, read-only and bounded.

    Pages run newest-updated first and stop at the first page that reaches a
    session last updated before the period: every older session was also
    created before it. Past ``max_scan`` rows the scan stops and says so.
    """

    reader = ReadOnlyStore(store)
    found: list[Any] = []
    scanned = 0
    after: tuple[str, str] | None = None
    while scanned < max_scan:
        want = min(page, max_scan - scanned)
        rows = reader.list_sessions(status="", limit=want, after=after)
        scanned += len(rows)
        found.extend(session for session in rows if scope.in_period(str(session.created_at or "")))
        if len(rows) < want:
            return tuple(found), False
        last = rows[-1]
        updated = parse_timestamp(str(last.updated_at or ""))
        if updated is not None and updated < scope.since:
            return tuple(found), False
        after = (str(last.updated_at), str(last.id))
    return tuple(found), True


@dataclass(frozen=True)
class RepositorySource:
    """One repository the caller may read, with its Reviews (already access-filtered by the host)."""

    repo_id: str
    store: Any
    sessions: Sequence[Any]
    repo_name: str = ""
    #: The session scan stopped at :data:`MAX_SESSION_SCAN` (see :func:`sessions_in_period`).
    scan_truncated: bool = False
    #: Requested teams per Review id (hosted); empty locally (no teams).
    teams: Callable[[Sequence[Any]], Mapping[str, frozenset[str]]] | None = None
    #: The viewer's Inbox rows for these Reviews (P9-INBOX projection).
    inbox_rows: Callable[[Sequence[Any]], Sequence[Any]] | None = None
    #: Host cost attribution for these Reviews (job-ledger rows, review-host cost class).
    usage: Callable[[Sequence[Any]], HostUsage] | None = None


@dataclass(frozen=True, slots=True)
class Viewer:
    teams: frozenset[str] = frozenset()
    all_teams_visible: bool = False
    #: Who pays for work on the process holding the Snapshot: ``customer`` locally, ``managed`` hosted.
    review_host_cost_class: CostClass = "customer"


@dataclass(frozen=True, slots=True)
class _Candidate:
    source: RepositorySource
    session: Any
    created: datetime


@dataclass(slots=True)
class _Escapes:
    review_ids: set[str] = field(default_factory=set)
    truncated: bool = False


def _escape_candidates(store: Any) -> _Escapes:
    from lemoncrow.pro.capabilities.review.outcomes.deployment_outcomes import DEPLOYMENT_ROW_KIND
    from lemoncrow.pro.capabilities.review.outcomes.observation_outcomes import OBSERVATION_ROW_KIND, SCAN_BATCH

    # lc-debt: escape candidates come from at most SCAN_BATCH ledger rows per kind (one
    # more is read only to tell a full window from an overflowing one); upgrade path: a
    # review-indexed outcome query.
    found = _Escapes()
    reader = ReadOnlyStore(store)
    for kind in (OBSERVATION_ROW_KIND, DEPLOYMENT_ROW_KIND):
        rows = reader.derived_results_by_kind(kind, limit=SCAN_BATCH + 1)
        found.review_ids.update(row.review_id for row in rows[:SCAN_BATCH])
        found.truncated = found.truncated or len(rows) > SCAN_BATCH
    return found


def _escapes(store: Any, session: Any) -> Sequence[Any]:
    from lemoncrow.pro.capabilities.review.outcomes.outcome_feeds import escaped_defects

    return escaped_defects(store, session)


def candidates_in_scope(sources: Sequence[RepositorySource], scope: AnalyticsScope) -> list[_Candidate]:
    """Reviews of the selected repositories created in the period, newest first."""

    out: list[_Candidate] = []
    for source in sources:
        if scope.repos and source.repo_id not in scope.repos:
            continue
        for session in source.sessions:
            created = parse_timestamp(getattr(session, "created_at", ""))
            if created is not None and scope.since <= created < scope.until:
                out.append(_Candidate(source, session, created))
    out.sort(key=lambda item: (item.created, item.source.repo_id, str(item.session.id)), reverse=True)
    return out


def analyze(
    sources: Sequence[RepositorySource],
    *,
    scope: AnalyticsScope,
    now: datetime,
    viewer: Viewer | None = None,
    org_matches: bool = True,
    notes: Sequence[str] = (),
    max_reviews: int = MAX_REVIEWS,
    unavailable: frozenset[str] | None = None,
) -> AnalyticsReport:
    """The analytics report and its drill index for one scope.

    ``org_matches`` is the host's answer to the ``org`` filter: a filter naming
    another organization selects nothing (it never widens access). ``unavailable``
    (default: the optional sources this engine does not ship) names sources that
    are not read and are reported unavailable.
    """

    viewer = viewer or Viewer()
    missing = unavailable_sources() if unavailable is None else unavailable
    candidates = candidates_in_scope(sources, scope) if org_matches else []
    chosen = candidates[:max_reviews]
    by_source: dict[str, list[Any]] = {}
    sources_by_id = {source.repo_id: source for source in sources}
    for candidate in chosen:
        by_source.setdefault(candidate.source.repo_id, []).append(candidate.session)
    facts: list[ReviewFacts] = []
    rows: list[Any] = []
    escapes_truncated = False
    for repo_id, sessions in by_source.items():
        source = sources_by_id[repo_id]
        teams = source.teams(sessions) if source.teams is not None else {}
        usage = (
            source.usage(sessions)
            if source.usage is not None
            else HostUsage(review_host_cost_class=viewer.review_host_cost_class)
        )
        escapes = _Escapes() if "escaped_defects" in missing else _escape_candidates(source.store)
        escapes_truncated = escapes_truncated or escapes.truncated
        repo_facts = [
            review_facts(
                source.store,
                session,
                repo_id=repo_id,
                repo_name=source.repo_name,
                teams=teams.get(str(session.id), frozenset()),
                want_paths=bool(scope.paths),
                host=usage,
                escapes_for=_escapes if str(session.id) in escapes.review_ids else None,
                unavailable=missing,
            )
            for session in sessions
        ]
        selected = select_facts(repo_facts, scope)
        facts.extend(selected)
        wanted = {item.review_id for item in selected if item.status == "open"}
        if source.inbox_rows is not None and wanted and "reviewer_load" not in missing:
            try:
                rows.extend(source.inbox_rows([session for session in sessions if str(session.id) in wanted]))
            except ModuleNotFoundError as exc:
                if not first_party_missing(exc):
                    raise
                # The host's Inbox projection needs code this engine does not ship.
                missing = missing | {"reviewer_load"}
                rows.clear()
    index = DrillIndex()
    waiting, load = reviewer_load(
        rows,
        index=index,
        now=now,
        viewer_teams=viewer.teams,
        all_teams_visible=viewer.all_teams_visible,
    )
    return build_report(
        facts,
        scope=scope,
        now=now,
        candidates=len(candidates),
        analyzed=len(chosen),
        waiting_by_owner=waiting,
        reviewer_load=load,
        load_index=index,
        escapes_truncated=escapes_truncated,
        scan_truncated=org_matches
        and any(source.scan_truncated for source in sources if source.repo_id in scope.repos or not scope.repos),
        notes=notes,
        unavailable=missing,
    )


__all__ = [
    "MAX_SESSION_SCAN",
    "SESSION_PAGE",
    "RepositorySource",
    "Viewer",
    "analyze",
    "candidates_in_scope",
    "sessions_in_period",
]

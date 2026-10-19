"""The local Review Inbox: views, workflow state, personal attention, saved filters (P9-INBOX).

The local server has one user. What that changes -- and what it never does:

* Every local Review is reviewed by the local reviewer, so every view is that
  user's. There are no teams and no review requests: ``assigned_to_my_teams``
  is always empty and a filter naming teams is refused.
* The viewer's turn comes from stored local facts: the review is not complete
  on the current Snapshot (``review_pending``), addressed comments are back
  (``thread_returned``), comments are not sent yet (``feedback_unsent``), or
  the workflow needs a decision (``decision_needed``: the one user decides).
  Comments handed to the author or a coding agent, and pending checks, are
  someone else's turn.
* ``authored_by_me`` is a Change captured with ``actor_type == "human"``;
  agent, mixed and unknown captures are not claimed. ``participated`` is review
  evidence (a mark or a comment) on a Change the user did not author.
* Priority, readiness (the reviewer's own record) and post-merge activity are
  the same stored records the hosted Inbox reads (:func:`load_stored_inbox_facts`).

Reads per repository: the bundled list read, stored target progress, the
feedback rows, two side facts and the stored-record reads. A packet, unit or
source is opened only to re-derive a stale progress row -- the same cache the
Reviews list keeps. Wire rows (titles, locations; git reads) are built only for
the page, or for every candidate when a search needs their text.

Saved filters are per local user in ``<review root>/.inbox/saved_filters.db``,
inside the Review state root that removing local Review state deletes.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from aiohttp import web

from lemoncrow.pro.capabilities.review import fix_lines
from lemoncrow.pro.capabilities.review.archive_safety import owed_cancellation
from lemoncrow.pro.capabilities.review.handoff import ReviewHandoff, handoff_row_fields, review_handoff
from lemoncrow.pro.capabilities.review.inbox_inputs import SnapshotRef, load_stored_inbox_facts, triage_wire
from lemoncrow.pro.capabilities.review.inbox_views import (
    INBOX_VIEWS,
    VIEW_LABELS,
    InboxCursor,
    InboxPage,
    InboxQueryError,
    InboxRow,
    InboxSelection,
    ViewFacts,
    decode_cursor,
    inbox_as_of,
    inbox_views,
    narrow,
    parse_limit,
    select_page,
    updated_sort_key,
)
from lemoncrow.pro.capabilities.review.personal_attention import (
    PersonalAttentionInputs,
    ViewerReason,
    personal_attention,
)
from lemoncrow.pro.capabilities.review.readiness import SnapshotProgress
from lemoncrow.pro.capabilities.review.saved_filters import (
    MAX_SAVED_FILTERS,
    SavedFilter,
    SavedFilterConflict,
    SavedFilterInvalid,
    SavedFilterLimit,
    SqliteSavedFilterStore,
    request_selection,
    selection_view,
    validate_document,
)
from lemoncrow.pro.capabilities.review.store import ReviewListInputs, ReviewStore, ShortReferenceIndex
from lemoncrow.pro.capabilities.review.targets import DerivedRefresh
from lemoncrow.pro.capabilities.review.workflow_state import (
    WORKFLOW_LABELS,
    WORKFLOW_STATES,
    ReadinessFact,
    WorkflowFacts,
    derive_workflow_state,
    scm_axis,
)

from .context import validate_opaque_id
from .errors import AgentAction, ErrorCode, ServerError
from .http.wire import json_response

if TYPE_CHECKING:
    from .local_review_service import LocalReviewService

__all__ = ["SEARCH_KEYS", "LocalInbox", "local_inbox_rows", "local_review_workflow"]

#: Wire-row fields a Reviews search matches (the legacy list and the view mode alike).
SEARCH_KEYS: Final[tuple[str, ...]] = (
    "id",
    "repo_id",
    "title",
    "source_ref",
    "directory",
    "project",
    "worktree",
    "branch",
    "worktree_root",
)
_ACTIONABLE: Final[tuple[str, ...]] = ("unreviewed", "changed_since_review", "unknown")
_REVIEWER_TURN: Final[frozenset[str]] = frozenset({"review_pending", "thread_returned"})
_NONE: Final[str] = "\0none"
FILTERS_PATH: Final[str] = "/api/review-inbox/filters"


# --------------------------------------------------------------------------- #
# projection
# --------------------------------------------------------------------------- #


def local_inbox_rows(
    store: ReviewStore,
    repo_id: str,
    items: Sequence[tuple[Any, ReviewListInputs]],
    progress: Mapping[str, Mapping[str, int]],
    *,
    as_of: datetime,
    haystacks: Mapping[str, str] | None = None,
) -> list[InboxRow]:
    """Inbox rows for one repository's Reviews, from bulk reads only."""

    if not items:
        return []
    from lemoncrow.pro.capabilities.review.sources.github import GITHUB_SOURCE_PREFIX

    ids = [str(session.id) for session, _item in items]
    versions, deliveries = store.feedback_rows_for_reviews(ids)
    side = store.inbox_side_facts(ids)
    # Readiness is evaluated as a reviewer: each Review reads its own reviewer's record.
    # lc-debt: five stored-record reads per distinct reviewer id (locally one); upgrade path:
    # one read keyed by (revision, evaluator) pairs if local Reviews ever carry many reviewers.
    by_reviewer: dict[str, list[SnapshotRef]] = {}
    for session, item in items:
        revision_id = "" if item.revision is None else str(item.revision.id)
        by_reviewer.setdefault(str(session.reviewer_id or "local"), []).append(
            SnapshotRef(review_id=str(session.id), revision_id=revision_id)
        )
    triage: dict[str, Any] = {}
    readiness: dict[str, ReadinessFact] = {}
    running: dict[str, int] = {}
    for reviewer, refs in sorted(by_reviewer.items()):
        stored = load_stored_inbox_facts(store, refs, viewer=reviewer, now=as_of)
        triage.update(stored.triage)
        readiness.update(stored.readiness)
        running.update(stored.post_merge_running)
    rows: list[InboxRow] = []
    for session, item in items:
        review_id = str(session.id)
        facts = side.get(review_id, {})
        pull_request = str(session.source_ref).startswith(GITHUB_SOURCE_PREFIX) or (
            facts.get("destination") == "github_pr"
        )
        handoff = (
            ReviewHandoff()
            if item.revision is None
            else review_handoff(item.revision.id, item.annotations, versions, deliveries, pull_request=pull_request)
        )
        rows.append(
            _row(
                session,
                item,
                progress.get(review_id) or {},
                handoff,
                repo_id=repo_id,
                pull_request=pull_request,
                landing=facts.get("landing", ""),
                triage=triage[review_id],
                readiness=readiness[review_id],
                post_merge_running=running.get(review_id, 0),
                as_of=as_of,
                haystack=(haystacks or {}).get(review_id, ""),
            )
        )
    return rows


def local_review_workflow(
    store: ReviewStore,
    repo_id: str,
    session: Any,
    *,
    progress_of: Callable[[ReviewStore, Sequence[tuple[Any, ReviewListInputs]]], Mapping[str, Mapping[str, int]]],
    as_of: datetime,
    progress: SnapshotProgress | None = None,
) -> dict[str, Any] | None:
    """One Review's Inbox ``workflow`` and ``next_action`` (Trust T2).

    The same row derivation the Inbox list runs, over this one Review, so the
    trust section on Review Status repeats the Inbox's answer instead of
    computing its own. ``progress_of`` is the stored target progress the list reads.
    ``progress`` is the target progress the calling Status read already derived
    from the same Snapshot, reviewer marks and annotations. Target state depends
    on those alone, so the counts this row reads (targets, reviewed, unreviewed,
    changed since review, unknown) equal what ``progress_of`` would return, and
    the 2,000-target re-derivation after each mark is not paid twice. It is used
    only while it names this Review's current Snapshot.
    """

    item = store.review_list_inputs([session]).get(str(session.id))
    if item is None:
        return None
    items = [(session, item)]
    # lc-debt: builds the whole Inbox row (views, attention, axes) to read two fields (~1 ms at 2,000
    # targets once progress is supplied); upgrade path: a workflow-only projection over the same inputs.
    current = "" if item.revision is None else str(item.revision.id)
    counts: Mapping[str, Mapping[str, int]]
    if progress is not None and current and progress.revision_id == current:
        counts = {str(session.id): progress.counts}
    else:
        counts = progress_of(store, items)
    rows = local_inbox_rows(store, repo_id, items, counts, as_of=as_of)
    if not rows:
        return None
    wire = rows[0].wire
    return {"workflow": wire["workflow"], "next_action": wire["next_action"]}


def _row(
    session: Any,
    item: ReviewListInputs,
    counts: Mapping[str, int],
    handoff: ReviewHandoff,
    *,
    repo_id: str,
    pull_request: bool,
    landing: str,
    triage: Any,
    readiness: ReadinessFact,
    post_merge_running: int,
    as_of: datetime,
    haystack: str,
) -> InboxRow:
    review_id = str(session.id)
    status = str(session.status)
    is_open = status == "open"
    revision = item.revision
    reviewer = str(session.reviewer_id or "local")
    actor = str(session.actor_type or "unknown")
    targets = int(counts.get("target_count", 0))
    remaining = sum(int(counts.get(key, 0)) for key in _ACTIONABLE)
    # Positive evidence only: the reviewer's fresh readiness record says reviewed, or every
    # current target is judged. No targets at all is not evidence of a finished review.
    review_complete = readiness.reviewed or (revision is not None and targets > 0 and remaining == 0)
    evidence = (
        bool(item.marks)
        or int(counts.get("reviewed", 0)) > 0
        or any(annotation.source == "human" for annotation in item.annotations)
    )
    reasons: list[ViewerReason] = []
    if is_open and revision is not None:
        if handoff.returned:
            reasons.append(
                ViewerReason("thread_returned", fact=f"{handoff.returned} addressed comment(s) back with you")
            )
        if not review_complete:
            reasons.append(
                ViewerReason(
                    "review_pending",
                    since=str(revision.created_at),
                    fact=(
                        f"{remaining} target(s) left to review"
                        if remaining
                        else "Review is not complete on the current Snapshot"
                    ),
                )
            )
        if handoff.unsent:
            reasons.append(ViewerReason("feedback_unsent", fact=f"{handoff.unsent} comment(s) not sent yet"))
    decision = derive_workflow_state(
        WorkflowFacts(
            status=status,
            merged=landing == "merged",
            post_merge_running=post_merge_running,
            check_failures=readiness.failed_checks if readiness.fresh else 0,
            threads_returned=handoff.returned,
            threads_waiting_on_author=handoff.with_author,
            feedback_with_agent=handoff.with_agent,
            review_evidence=evidence,
            review_complete=review_complete,
            author_is_agent=actor == "agent",
            readiness=readiness,
        )
    )
    if is_open and decision.state == "needs_decision":
        # One local user: a decision the facts call for is that user's.
        reasons.append(ViewerReason("decision_needed", fact=decision.explanation))
    authored = actor == "human"
    participated = not authored and evidence
    views = inbox_views(
        ViewFacts(
            open=is_open,
            reviewer_turn_for_me=any(reason.kind in _REVIEWER_TURN for reason in reasons),
            authored=authored,
            participated=participated,
            attention_for_me=bool(reasons),
            workflow_state=decision.state,
        )
    )
    attention = personal_attention(
        PersonalAttentionInputs(
            open=is_open,
            reasons=tuple(reasons),
            updated_at=str(session.updated_at),
            engaged=participated,
            priority=triage.priority,
            criticality=triage.criticality,
            effort=remaining if revision is not None and targets else None,
            effort_unit="targets",
        ),
        now=as_of,
    )
    mine = [{"principal_id": reviewer, "kind": r.kind, "fact": r.fact, "since": r.since} for r in reasons]
    theirs: list[dict[str, Any]] = []
    if is_open and handoff.with_author:
        theirs.append(
            {
                "principal_id": "author",
                "kind": "feedback_with_author",
                "fact": f"{handoff.with_author} comment(s) with the author",
                "since": "",
            }
        )
    if is_open and handoff.with_agent:
        theirs.append(
            {
                "principal_id": "agent",
                "kind": "feedback_with_agent",
                "fact": f"{handoff.with_agent} comment(s) with a coding agent",
                "since": "",
            }
        )
    wire: dict[str, Any] = {
        "progress": dict(counts),
        **handoff_row_fields(handoff, status=status, actionable_targets=remaining),
        "views": list(views),
        "workflow": decision.to_wire(),
        "priority": triage_wire(triage),
        "next_action": {
            "owner_ids": sorted({row["principal_id"] for row in (*mine, *theirs)}),
            "for_me": bool(mine),
            "my_reasons": mine,
            "reasons": [*mine, *theirs],
        },
        "readiness": readiness.to_wire(),
        "axes": {
            "your_review": {
                "state": "complete" if review_complete else "in_progress" if evidence else "not_started",
                "target_count": targets,
                "reviewed": int(counts.get("reviewed", 0)),
                "remaining": remaining,
            },
            "team_review": {"state": "not_applicable", "reason": "A local Review has one reviewer"},
            "scm": scm_axis(readiness, connected=pull_request, provider="github" if pull_request else ""),
        },
        "attention": attention.to_wire(),
        "teams": [],
        "team_requests": 0,
    }
    return InboxRow(
        repo_id=repo_id,
        review_id=review_id,
        status=status,
        views=views,
        workflow_state=decision.state,
        priority=triage.priority,
        sort_key=updated_sort_key(str(session.updated_at), repo_id=repo_id, review_id=review_id),
        haystack=haystack,
        wire=wire,
    )


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def _invalid(field: str, message: str, *, limit: int | None = None) -> ServerError:
    details: dict[str, Any] = {"field": field}
    if limit is not None:
        details["limit"] = limit
    return ServerError(ErrorCode.PAYLOAD_INVALID, message, details=details, action=AgentAction.FIX_REQUEST)


def _not_found() -> ServerError:
    return ServerError(
        ErrorCode.REVIEW_UNKNOWN,
        "saved Inbox filter not found",
        details={"field": "filter"},
        action=AgentAction.FIX_REQUEST,
    )


def _conflict(exc: Exception, details: Mapping[str, Any]) -> ServerError:
    return ServerError(ErrorCode.REVIEW_STATE_CONFLICT, str(exc), details=dict(details), action=AgentAction.FIX_REQUEST)


class LocalInbox:
    """View mode of ``GET /v1/reviews`` and the local user's saved filters."""

    def __init__(self, service: LocalReviewService) -> None:
        self._service = service
        self._filters: SqliteSavedFilterStore | None = None

    @property
    def filters(self) -> SqliteSavedFilterStore:
        # Lazy: the file appears only once the user saves or lists filters.
        if self._filters is None:
            self._filters = SqliteSavedFilterStore(self._service.review_repository.root / ".inbox" / "saved_filters.db")
        return self._filters

    def routes(self) -> tuple[tuple[str, str, Callable[[web.Request], Awaitable[web.StreamResponse]]], ...]:
        return (
            ("GET", FILTERS_PATH, self.list_filters),
            ("POST", FILTERS_PATH, self.create_filter),
            ("PUT", FILTERS_PATH + "/{filter_id}", self.update_filter),
            ("DELETE", FILTERS_PATH + "/{filter_id}", self.delete_filter),
        )

    async def views(self, request: web.Request) -> web.StreamResponse:
        """``GET /v1/reviews?view=...``: the local user's views over every local Review."""

        service = self._service
        principal = service._principal(request)
        try:
            selection = request_selection(lambda name: request.query.getall(name, []), teams_supported=False)
            limit = parse_limit(request.query.get("limit"))
            cursor = decode_cursor(request.query.get("cursor", ""))
        except InboxQueryError as exc:
            raise _invalid(exc.field, str(exc), limit=exc.limit) from exc
        except SavedFilterInvalid as exc:
            raise _invalid(exc.field, str(exc)) from exc
        saved: SavedFilter | None = None
        filter_id = request.query.get("filter", "").strip()
        if filter_id:
            saved = await service._in_worker(self.filters.get, principal.org_id, principal.subject, filter_id)
            if saved is None:
                raise _not_found()
            selection = narrow(selection, saved.selection)
        as_of = inbox_as_of(cursor, datetime.now(UTC))
        durations: dict[str, float] = {}
        started = time.perf_counter()
        page, reviews, repositories = await service._in_worker(self._page, selection, limit, cursor, as_of, durations)
        applied = selection_view(selection)
        response = json_response(
            {
                "mode": "views",
                "reviews": reviews,
                "view": applied["view"],
                "status": applied["status"],
                "query": selection.query,
                "selection": applied,
                "filter": None if saved is None else saved.to_wire(),
                "counts": page.counts,
                "total": page.total,
                "limit": limit,
                "next_cursor": page.next_cursor,
                "as_of": as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "repositories": repositories,
                "views": [{"id": view, "label": VIEW_LABELS[view]} for view in ("all", *INBOX_VIEWS)],
                "workflow_states": [{"id": state, "label": WORKFLOW_LABELS[state]} for state in WORKFLOW_STATES],
                "teams_supported": False,
            }
        )
        durations["inbox_total"] = (time.perf_counter() - started) * 1000
        response.headers["Server-Timing"] = ", ".join(
            f"{name};dur={milliseconds:.1f}" for name, milliseconds in durations.items()
        )
        return response

    def _page(
        self,
        selection: InboxSelection,
        limit: int,
        cursor: InboxCursor | None,
        as_of: datetime,
        durations: dict[str, float] | None = None,
    ) -> tuple[InboxPage, list[dict[str, Any]], int]:
        started = time.perf_counter()
        service = self._service
        repository = service.review_repository
        listed = {
            repo_id: repository.list_reviews(repo_id, status="", limit=None)
            for repo_id in repository.list_repository_ids()
        }
        enumerated = time.perf_counter()
        if durations is not None:
            durations["inbox_enumerate"] = (enumerated - started) * 1000
        # Short references are unique across every Review, whatever the selection.
        references = ShortReferenceIndex(
            (session.id for sessions in listed.values() for session in sessions), revision=False
        )
        status = "" if selection.status in {"all", _NONE} else selection.status
        refresh = DerivedRefresh()  # one re-derivation budget for the whole request
        rows: list[InboxRow] = []
        built: dict[tuple[str, str], dict[str, Any]] = {}
        sessions_by_key: dict[tuple[str, str], Any] = {}
        checkout_metadata: dict[Path, dict[str, Any]] = {}
        # lc-debt: scores every local Review per request (bulk reads, stored progress) before
        # paging; upgrade path: refresh a stored per-Review inbox row on Review writes.
        for repo_id, sessions in listed.items():
            if selection.repos and repo_id not in selection.repos:
                continue
            store = repository.store(repo_id)
            hidden = fix_lines.fix_child_review_ids(store)
            wanted = [s for s in sessions if s.id not in hidden and (not status or s.status == status)]
            if not wanted:
                continue
            inputs = store.review_list_inputs(wanted)
            items = [(session, inputs[str(session.id)]) for session in wanted]
            progress = service._review_progress(store, items, refresh)
            haystacks: dict[str, str] = {}
            if selection.query:
                # A search matches wire text (title, location), so it needs every candidate's row.
                for session, _item, wire in service._review_wire_rows(
                    repo_id, wanted, references=references, location_metadata_cache=checkout_metadata
                ):
                    built[(repo_id, str(session.id))] = wire
                    haystacks[str(session.id)] = " ".join(str(wire.get(key) or "") for key in SEARCH_KEYS).lower()
            for session in wanted:
                sessions_by_key[(repo_id, str(session.id))] = session
            rows.extend(local_inbox_rows(store, repo_id, items, progress, as_of=as_of, haystacks=haystacks))
        scored = time.perf_counter()
        if durations is not None:
            durations["inbox_score"] = (scored - enumerated) * 1000
        page = select_page(rows, selection, limit=limit, cursor=cursor, as_of=as_of)
        paged = time.perf_counter()
        if durations is not None:
            durations["inbox_select"] = (paged - scored) * 1000
        missing: dict[str, list[Any]] = {}
        for row in page.rows:
            if (row.repo_id, row.review_id) not in built:
                missing.setdefault(row.repo_id, []).append(sessions_by_key[(row.repo_id, row.review_id)])
        for repo_id, on_page in missing.items():
            for session, _item, wire in service._review_wire_rows(
                repo_id, on_page, references=references, location_metadata_cache=checkout_metadata
            ):
                built[(repo_id, str(session.id))] = wire
        reviews: list[dict[str, Any]] = []
        for row in page.rows:
            wire = {**built[(row.repo_id, row.review_id)], **row.wire}
            if row.status == "archived":
                # A provider cancellation owed by a discard stays visible until confirmed.
                owed = owed_cancellation(repository.store(row.repo_id), row.review_id)
                if owed is not None:
                    wire["owed_cancellation"] = owed
            reviews.append(wire)
        if durations is not None:
            durations["inbox_hydrate"] = (time.perf_counter() - paged) * 1000
        return page, reviews, len(listed)

    # -- saved filters ------------------------------------------------- #

    async def list_filters(self, request: web.Request) -> web.StreamResponse:
        principal = self._service._principal(request)
        rows = await self._service._in_worker(self.filters.list, principal.org_id, principal.subject)
        return json_response(
            {"filters": [row.to_wire() for row in rows], "limit": MAX_SAVED_FILTERS, "teams_supported": False}
        )

    async def create_filter(self, request: web.Request) -> web.StreamResponse:
        principal = self._service._principal(request)
        name, selection = self._validated(await self._service._body(request))
        try:
            created = await self._service._in_worker(
                self.filters.create, principal.org_id, principal.subject, name, selection
            )
        except SavedFilterLimit as exc:
            raise _conflict(exc, {"field": "filters", "limit": MAX_SAVED_FILTERS}) from exc
        except SavedFilterConflict as exc:
            raise _conflict(exc, {"field": "name"}) from exc
        return json_response({"filter": created.to_wire()}, status=201)

    async def update_filter(self, request: web.Request) -> web.StreamResponse:
        principal = self._service._principal(request)
        filter_id = self._filter_id(request)
        name, selection = self._validated(await self._service._body(request))
        try:
            updated = await self._service._in_worker(
                self.filters.update, principal.org_id, principal.subject, filter_id, name, selection
            )
        except SavedFilterConflict as exc:
            raise _conflict(exc, {"field": "name"}) from exc
        if updated is None:
            raise _not_found()
        return json_response({"filter": updated.to_wire()})

    async def delete_filter(self, request: web.Request) -> web.StreamResponse:
        principal = self._service._principal(request)
        filter_id = self._filter_id(request)
        if not await self._service._in_worker(self.filters.delete, principal.org_id, principal.subject, filter_id):
            raise _not_found()
        return json_response({"deleted": True, "id": filter_id})

    @staticmethod
    def _validated(document: Mapping[str, Any]) -> tuple[str, InboxSelection]:
        try:
            return validate_document(document, teams_supported=False)
        except SavedFilterInvalid as exc:
            raise _invalid(exc.field, str(exc)) from exc

    @staticmethod
    def _filter_id(request: web.Request) -> str:
        try:
            return validate_opaque_id(request.match_info.get("filter_id", ""), "filter_id")
        except ServerError:
            raise _not_found() from None

"""Local Review analytics (P13-ANALYTICS): ``GET /api/review-analytics`` and its drill-down.

The local server has one user, so every local Review is that user's to read,
there are no teams (a team filter is refused, never ignored) and work on this
machine is the customer's own compute. The routes live under ``/api`` because
the browser session may only call ``/v1/reviews`` and ``/api/*``.

Analytics are advisory: nothing here is stored and nothing feeds readiness.
Reviewer load reuses the Inbox projection, which may refresh the Inbox's own
progress cache exactly as opening the Inbox does.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from aiohttp import web

from lemoncrow.pro.capabilities.review import fix_lines
from lemoncrow.pro.capabilities.review.analytics import (
    AnalyticsQueryError,
    AnalyticsReport,
    AnalyticsScope,
    RepositorySource,
    Viewer,
    analyze,
    parse_drill_key,
    parse_scope,
)
from lemoncrow.pro.capabilities.review.analytics.report import DRILL_LIMIT
from lemoncrow.pro.capabilities.review.analytics.service import sessions_in_period
from lemoncrow.pro.capabilities.review.inbox_views import InboxRow
from lemoncrow.pro.capabilities.review.targets import DerivedRefresh

from .errors import AgentAction, ErrorCode, ServerError
from .http.wire import json_response
from .local_inbox import local_inbox_rows

if TYPE_CHECKING:
    from .local_review_service import LocalReviewService

__all__ = ["ANALYTICS_PATH", "LocalReviewAnalytics"]

ANALYTICS_PATH: Final[str] = "/api/review-analytics"


def _invalid(exc: AnalyticsQueryError) -> ServerError:
    details: dict[str, Any] = {"field": exc.field}
    if exc.limit is not None:
        details["limit"] = exc.limit
    return ServerError(ErrorCode.PAYLOAD_INVALID, str(exc), details=details, action=AgentAction.FIX_REQUEST)


def _drill_limit(raw: str) -> int:
    text = raw.strip()
    if not text:
        return DRILL_LIMIT
    try:
        value = int(text)
    except ValueError as exc:
        raise _invalid(AnalyticsQueryError("limit", "limit must be an integer")) from exc
    if value < 1 or value > DRILL_LIMIT:
        raise _invalid(AnalyticsQueryError("limit", f"limit must be between 1 and {DRILL_LIMIT}", limit=DRILL_LIMIT))
    return value


class LocalReviewAnalytics:
    """Analytics over every local Review (one user, no teams)."""

    def __init__(self, service: LocalReviewService) -> None:
        self._service = service

    def routes(self) -> tuple[tuple[str, str, Callable[[web.Request], Awaitable[web.StreamResponse]]], ...]:
        return (
            ("GET", ANALYTICS_PATH, self.report),
            ("GET", ANALYTICS_PATH + "/drill", self.drill),
        )

    async def report(self, request: web.Request) -> web.StreamResponse:
        principal = self._service._principal(request)
        scope = self._scope(request)
        report = await self._service._in_worker(self._analyze, scope, str(principal.org_id))
        return self._respond({**report.wire, "teams_supported": False})

    async def drill(self, request: web.Request) -> web.StreamResponse:
        principal = self._service._principal(request)
        scope = self._scope(request)
        try:
            key = parse_drill_key(request.query.get("key", ""))
        except AnalyticsQueryError as exc:
            raise _invalid(exc) from exc
        limit = _drill_limit(request.query.get("limit", ""))
        report = await self._service._in_worker(self._analyze, scope, str(principal.org_id))
        return self._respond({**report.drill(key, limit=limit), "scope": scope.to_wire()})

    @staticmethod
    def _respond(payload: dict[str, Any]) -> web.StreamResponse:
        response = json_response(payload)
        response.headers["Cache-Control"] = "no-store"
        return response

    @staticmethod
    def _scope(request: web.Request) -> AnalyticsScope:
        try:
            return parse_scope(
                lambda name: request.query.getall(name, []),
                now=datetime.now(UTC),
                teams_supported=False,
            )
        except AnalyticsQueryError as exc:
            raise _invalid(exc) from exc

    def _analyze(self, scope: AnalyticsScope, org_id: str) -> AnalyticsReport:
        repository = self._service.review_repository
        sources: list[RepositorySource] = []
        for repo_id in repository.list_repository_ids():
            if scope.repos and repo_id not in scope.repos:
                continue
            if not repository.store_root(repo_id).exists():
                continue
            store = repository.store(repo_id)
            # Fix lines' child Reviews are hidden from the Inbox; analytics count the same Reviews.
            hidden = fix_lines.fix_child_review_ids(store)
            found, truncated = sessions_in_period(store, scope)
            sessions = tuple(session for session in found if session.id not in hidden)
            checkout = repository.checkout_root(repo_id)
            name = checkout.name if checkout is not None else (Path(sessions[0].repo_root).name if sessions else "")
            sources.append(
                RepositorySource(
                    repo_id=repo_id,
                    store=store,
                    sessions=sessions,
                    repo_name=name or repo_id,
                    scan_truncated=truncated,
                    inbox_rows=partial(self._inbox_rows, store, repo_id),
                )
            )
        now = datetime.now(UTC)
        return analyze(
            sources,
            scope=scope,
            now=now,
            viewer=Viewer(review_host_cost_class="customer"),
            # One local organization: naming another one selects nothing.
            org_matches=not scope.org or scope.org == org_id,
        )

    def _inbox_rows(self, store: Any, repo_id: str, sessions: Sequence[Any]) -> list[InboxRow]:
        inputs = store.review_list_inputs(list(sessions))
        items = [(session, inputs[str(session.id)]) for session in sessions]
        progress = self._service._review_progress(store, items, DerivedRefresh())
        return local_inbox_rows(store, repo_id, items, progress, as_of=datetime.now(UTC))

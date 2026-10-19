"""The Project Knowledge surface over the local server (UX0b): ``/api/project-knowledge``.

One user, one LemonCrow root: the PK0 index ``lc knowledge project sync``
writes under that root, its narratives (PK1), and the record of where each
repository was synced from. Reads never call a model and never create the
store. Hide supersedes one AI narrative; regenerate queues one BI2 job on the
local worker; a correction answers with a patch and writes nothing.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from aiohttp import web

from lemoncrow.pro.capabilities.review.project_knowledge_surface import (
    KnowledgeSurfaceError,
    LocalKnowledgeSources,
    hide_narrative,
    knowledge_listing,
    knowledge_overview,
    propose_correction,
    regenerate_narrative,
)

from .errors import AgentAction, ErrorCode, ServerError
from .http.wire import json_response

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.project_knowledge import ProjectKnowledgeStore

    from .local_review_service import LocalReviewService

__all__ = ["PROJECT_KNOWLEDGE_PATH", "LocalProjectKnowledge"]

PROJECT_KNOWLEDGE_PATH: Final[str] = "/api/project-knowledge"


def _default_root() -> Path:
    from lemoncrow.pro.capabilities.review.fix_agent import settings_root

    return settings_root()


def _refusal(exc: KnowledgeSurfaceError) -> ServerError:
    details = {"reason": exc.reason}
    if exc.status == 404:
        return ServerError(ErrorCode.PAYLOAD_INVALID, str(exc), details=details, status=404, action=AgentAction.ABANDON)
    if exc.status == 409:
        return ServerError(ErrorCode.REVIEW_STATE_CONFLICT, str(exc), details=details, retryable=False)
    return ServerError(ErrorCode.PAYLOAD_INVALID, str(exc), details=details)


class LocalProjectKnowledge:
    """The Project Knowledge routes of the local Review service."""

    def __init__(
        self,
        service: LocalReviewService,
        *,
        root: Callable[[], Path] = _default_root,
        scheduler: Callable[[Path], Any] | None = None,
    ) -> None:
        self._service = service
        self._root = root
        self._scheduler_factory = scheduler
        self._lock = threading.Lock()
        self._stores: dict[Path, ProjectKnowledgeStore] = {}
        self._schedulers: dict[Path, Any] = {}

    def routes(self) -> tuple[tuple[str, str, Callable[[web.Request], Awaitable[web.StreamResponse]]], ...]:
        base = PROJECT_KNOWLEDGE_PATH
        return (
            ("GET", base, self.listing),
            ("GET", base + "/{repository_id}", self.overview),
            ("POST", base + "/{repository_id}/narratives/{entry_id}/hide", self.hide),
            ("POST", base + "/{repository_id}/narratives/{entry_id}/regenerate", self.regenerate),
            ("POST", base + "/{repository_id}/corrections", self.correct),
        )

    # -- state ---------------------------------------------------------- #

    def _store(self, root: Path) -> ProjectKnowledgeStore | None:
        """The PK0 store under *root*, or ``None`` when nothing was ever synced (never created here)."""

        from lemoncrow.pro.capabilities.review.project_knowledge import ProjectKnowledgeStore
        from lemoncrow.pro.capabilities.review.store import DB_NAME, ReviewStore

        with self._lock:
            store = self._stores.get(root)
            if store is None:
                if not (root / DB_NAME).is_file():
                    return None
                store = self._stores[root] = ProjectKnowledgeStore(ReviewStore(root))
            return store

    def _require_store(self, root: Path) -> ProjectKnowledgeStore:
        store = self._store(root)
        if store is None:
            raise _refusal(KnowledgeSurfaceError("no Project Knowledge synced yet", status=404, reason="not_found"))
        return store

    def _scheduler(self, root: Path) -> Any:
        with self._lock:
            scheduler = self._schedulers.get(root)
            if scheduler is None:
                if self._scheduler_factory is not None:
                    scheduler = self._scheduler_factory(root)
                else:
                    from lemoncrow.infra.storage.factory import create_store
                    from lemoncrow.pro.capabilities.review.background_local import LocalBackgroundScheduler

                    jobs = create_store(root)
                    jobs.init()
                    scheduler = LocalBackgroundScheduler.for_root(jobs.jobs, root)
                self._schedulers[root] = scheduler
            return scheduler

    async def _run(self, fn: Callable[[], dict[str, Any]]) -> web.StreamResponse:
        try:
            payload = await self._service._in_worker(fn)
        except KnowledgeSurfaceError as exc:
            raise _refusal(exc) from exc
        response = json_response(payload)
        response.headers["Cache-Control"] = "no-store"
        return response

    # -- routes --------------------------------------------------------- #

    async def listing(self, request: web.Request) -> web.StreamResponse:
        self._service._principal(request)
        root = self._root()

        def read() -> dict[str, Any]:
            store = self._store(root)
            sources = LocalKnowledgeSources(root)
            if store is None:
                from lemoncrow.pro.capabilities.review.project_knowledge_surface import (
                    LEGACY_OVERLAY_NOTE,
                    SYNC_COMMAND,
                )

                return {
                    "available": True,
                    "scope": "local",
                    "repositories": [],
                    "truncated": False,
                    "sync_command": SYNC_COMMAND,
                    "legacy_overlay_note": LEGACY_OVERLAY_NOTE,
                }
            return knowledge_listing(store, sources)

        return await self._run(read)

    async def overview(self, request: web.Request) -> web.StreamResponse:
        self._service._principal(request)
        root = self._root()
        term = request.query.get("q", "")
        raw_offset = request.query.get("offset", "0").strip() or "0"
        # isdigit() alone admits non-ASCII digits ("²") that int() rejects.
        if not (raw_offset.isascii() and raw_offset.isdigit()):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "offset must be a non-negative integer")
        repository_id = request.match_info["repository_id"]

        def read() -> dict[str, Any]:
            return knowledge_overview(
                self._require_store(root),
                LocalKnowledgeSources(root),
                repository_id,
                term=term,
                offset=int(raw_offset),
            )

        return await self._run(read)

    async def hide(self, request: web.Request) -> web.StreamResponse:
        self._service._principal(request)
        root = self._root()
        info = request.match_info
        return await self._run(
            lambda: hide_narrative(self._require_store(root), info["repository_id"], info["entry_id"])
        )

    async def regenerate(self, request: web.Request) -> web.StreamResponse:
        self._service._principal(request)
        root = self._root()
        info = request.match_info
        return await self._run(
            lambda: regenerate_narrative(
                self._require_store(root),
                LocalKnowledgeSources(root),
                partial(self._scheduler, root),
                info["repository_id"],
                info["entry_id"],
            )
        )

    async def correct(self, request: web.Request) -> web.StreamResponse:
        self._service._principal(request)
        root = self._root()
        body = await self._service._body(request)
        text, entry_id, subject = body.get("text"), body.get("entry_id", ""), body.get("subject")
        if (
            not isinstance(text, str)
            or not isinstance(entry_id, str)
            or not (subject is None or isinstance(subject, dict))
        ):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "a correction is {text, entry_id} or {text, subject}")
        repository_id = request.match_info["repository_id"]
        return await self._run(
            lambda: propose_correction(
                self._require_store(root), repository_id, text=text, entry_id=entry_id, subject=subject
            )
        )

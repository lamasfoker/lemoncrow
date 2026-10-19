"""The Dashboard service: the legacy dashboard API behind the loopback origin.

Its summaries decode the whole trace history, which holds the GIL for seconds;
run in the core it froze every coding-agent request with it. It is hosted by
the core only when services are not isolated (tests, debugging) and otherwise
in its own worker process (``service_host``), reached through the core.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Final

from aiohttp import web
from multidict import CIMultiDict

__all__ = ["BasicDashboardApp", "DashboardService", "basic_dashboard_allows", "default_dashboard_app"]

_LOG: Final[logging.Logger] = logging.getLogger("lemoncrow.server.dashboard")

#: Request headers the legacy app never sees. Browser authentication ends at
#: the outer server; accept-encoding because on loopback the legacy app's level-9
#: gzip cost more than it saved (a 9 MB /traces page: 301 ms identity vs 634 ms).
_DROPPED_HEADERS: Final[frozenset[bytes]] = frozenset(
    {
        b"authorization",
        b"x-lemoncrow-browser-session",
        b"x-lemoncrow-service-token",
        b"x-lemoncrow-principal",
        b"host",
        b"content-length",
        b"connection",
        b"accept-encoding",
    }
)

#: The basic local Dashboard reads sessions, usage, savings, settings and
#: system state. Knowledge, swarms, workflow, memory and rubrics stay outside
#: this lightweight surface. Each entry matches itself and its subpaths.
_BASIC_READ_PATHS: Final[tuple[str, ...]] = (
    # sessions
    "/traces",
    "/v1/traces",
    "/v1/sessions",
    "/ledgers",
    "/v1/outcomes",
    "/v1/insights",
    "/raw-artifacts",
    "/v1/files",
    # usage
    "/overview",
    "/analytics",
    "/calls",
    "/pricing",
    "/v1/reports",
    # savings
    "/savings",
    "/v1/savings",
    "/v1/optimizations",
    # settings
    "/config",
    "/telemetry",
    "/watchdogs/config",
    # system
    "/health",
    "/ready",
    "/hosts",
    "/mcp/status",
    "/skills",
    "/agents",
)
#: The lightweight Code surface: deterministic Runtime Lite reads plus local
#: machine actions that do not require the full/private index. Precise references,
#: generated module purpose and agent handoff remain outside this surface.
_BASIC_CODE_MAP_READS: Final[frozenset[str]] = frozenset(
    "/v1/code-map/" + name
    for name in (
        "projects",
        "modules",
        "relationships",
        "focus",
        "files",
        "file-symbols",
        "file-content",
        "search",
        "symbol",
        "changes",
        "file-changes",
        "hotspots",
        "activity",
        "editor",
        "ask/options",
    )
)
#: Local writes are explicit and bounded: telemetry consent plus actions whose
#: authority comes from the developer machine/current worktree.
_BASIC_WRITES: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("POST", "/telemetry/config"),
        ("POST", "/telemetry/ack"),
        ("POST", "/v1/code-map/editor"),
        ("POST", "/v1/code-map/ask/stream"),
        ("POST", "/v1/code-map/review"),
    }
)
_HOSTED_ONLY_BODY: Final[bytes] = json.dumps(
    {"detail": "this Dashboard capability is available on LemonCrow hosted and is not exposed by the local Dashboard"}
).encode("utf-8")


def basic_dashboard_allows(method: str, path: str) -> bool:
    """Whether the basic local Dashboard serves ``method path``."""

    segments = path.split("/")
    if ".." in segments or "." in segments:
        return False
    if (method, path) in _BASIC_WRITES:
        return True
    if method not in {"GET", "HEAD"}:
        return False
    if path in _BASIC_CODE_MAP_READS:
        return True
    return any(path == prefix or path.startswith(prefix + "/") for prefix in _BASIC_READ_PATHS)


class BasicDashboardApp:
    """ASGI wrapper serving only the basic local Dashboard subset."""

    __slots__ = ("_app",)

    def __init__(self, app: Any) -> None:
        self._app = app

    @property
    def state(self) -> Any:
        return self._app.state

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and not basic_dashboard_allows(
            str(scope.get("method") or ""), str(scope.get("path") or "")
        ):
            await send(
                {
                    "type": "http.response.start",
                    "status": 404,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(_HOSTED_ONLY_BODY)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": _HOSTED_ONLY_BODY})
            return
        await self._app(scope, receive, send)


def default_dashboard_app(code_intelligence_for_root: Callable[[Path], Any | None] | None) -> BasicDashboardApp:
    from lemoncrow.core.service.api import create_app

    app = create_app()
    # `/code` reads relationships from the same native index Review does.
    app.state.code_intelligence_for_root = code_intelligence_for_root
    return BasicDashboardApp(app)


class DashboardService:
    """Serve ``/api/{tail}`` from the legacy ASGI app, invoked in-process."""

    __slots__ = ("_app_provider",)

    def __init__(self, app_provider: Callable[[], Any]) -> None:
        self._app_provider = app_provider

    async def handle(self, request: web.Request) -> web.StreamResponse:
        app = self._app_provider()
        body = await request.read()
        tail = str(request.match_info.get("tail") or "")
        path = "/" + tail.lstrip("/")

        headers = [(key.lower(), value) for key, value in request.raw_headers if key.lower() not in _DROPPED_HEADERS]

        from lemoncrow.core.service.config import cfg as legacy_cfg

        if legacy_cfg.require_auth and legacy_cfg.api_key:
            headers.append((b"authorization", f"Bearer {legacy_cfg.api_key}".encode()))
        headers.append((b"host", request.host.encode("latin-1", errors="ignore")))

        transport = request.transport
        peer = transport.get_extra_info("peername") if transport is not None else None
        sock = transport.get_extra_info("sockname") if transport is not None else None
        client = (str(peer[0]), int(peer[1])) if isinstance(peer, tuple) and len(peer) >= 2 else None
        server = (str(sock[0]), int(sock[1])) if isinstance(sock, tuple) and len(sock) >= 2 else None

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": f"{request.version.major}.{request.version.minor}",
            "method": request.method,
            "scheme": request.scheme,
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": request.query_string.encode("latin-1"),
            "root_path": "",
            "headers": headers,
            "client": client,
            "server": server,
            "state": {},
        }
        received = False
        finished = asyncio.Event()

        async def receive() -> dict[str, Any]:
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": body, "more_body": False}
            # ASGI reports a disconnect only once the exchange is over. Answering
            # it at once made Starlette's StreamingResponse, which listens for
            # it, cancel every stream after its first chunk.
            await finished.wait()
            return {"type": "http.disconnect"}

        status = 500
        outgoing = CIMultiDict[str]()
        chunks: list[bytes] = []
        stream: web.StreamResponse | None = None

        async def send(message: dict[str, Any]) -> None:
            nonlocal status, stream
            message_type = message.get("type")
            if message_type == "http.response.start":
                status = int(message.get("status", 500))
                for key, value in message.get("headers") or []:
                    name = key.decode("latin-1")
                    if name.lower() not in {"content-length", "connection", "transfer-encoding"}:
                        # ASGI headers are an ordered multimap. Keep every value
                        # (notably repeated Set-Cookie/Link headers) when bridging
                        # the legacy app into aiohttp.
                        outgoing.add(name, value.decode("latin-1"))
                return
            if message_type != "http.response.body":
                return
            chunk = bytes(message.get("body") or b"")
            if stream is None and not message.get("more_body", False):
                # A one-shot body stays a plain, length-framed response.
                chunks.append(chunk)
                return
            if stream is None:
                # A streamed body (Code Ask's SSE) is relayed chunk by chunk
                # instead of being held until the answer completes.
                stream = web.StreamResponse(status=status, headers=outgoing)
                await stream.prepare(request)
            if chunk:
                await stream.write(chunk)

        try:
            await app(scope, receive, send)
        except Exception:
            if stream is None:
                raise
            # Headers are already on the wire: end this stream rather than hand
            # the error middleware a second response to send.
            _LOG.debug("legacy dashboard stream ended early: %s %s", request.method, path, exc_info=True)
        finally:
            finished.set()
        if stream is not None:
            with suppress(ConnectionError):
                await stream.write_eof()
            return stream
        return web.Response(status=status, body=b"".join(chunks), headers=outgoing)

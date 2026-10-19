"""The MCP stdio entry point. One process, one namespace, no daemon.

It is a child of the host session: it reads JSON-RPC from stdin, writes to
stdout, and exits when stdin closes. There is no port, no pid file, no socket,
no unit, no ``atexit`` respawn and no idle reaper -- the list from the design's
daemon disposition, in the negative.

The advertised surface is the enterprise-safe subset of the public LemonCrow
registry. ``mcp``, ``agent`` and ``workflow`` are intentionally absent because
their legacy local implementations start another MCP/LemonCrow process. Where
an advertised tool *runs* is decided by :mod:`lemoncrow_client.routing` and is
invisible from here.

Bootstrap belongs to this process and is fail-open. MCP ``initialize`` starts opening
the remote session/view in the background and returns immediately. If bootstrap fails, the
reason is recorded and client-side tools keep working; a later server-dependent
tool retries bootstrap after a short cooldown, so a local server that finishes
starting does not leave indexing disabled for the lifetime of the host session.
A stdio process that refused to initialize because a remote service was down
would strand the developer -- which is the failure mode the whole design exists
to remove.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Final, TextIO

from . import CLIENT_VERSION, toolbroker
from .autosync import AutoSync
from .config import ClientConfig, load_config
from .dispatcher import Dispatcher
from .errors import ClientError, ErrorCode
from .localtools.shell import cancel_active_bash_commands
from .routing import Site, route_for
from .search_dedup import SearchResultDedup
from .session import BootstrapReport, RemoteSession
from .surface import ToolSpec, tool_list

__all__ = ["McpServer", "serve"]

#: The MCP protocol version this client speaks to the host.
MCP_PROTOCOL_VERSION: Final[str] = "2025-06-18"

SERVER_NAME: Final[str] = "lc"
_MAX_LINE_BYTES: Final[int] = 32 * 1024 * 1024
_OFFLINE_RETRY_INTERVAL_S: Final[float] = 1.0
#: Client-side tools that can rewrite the tree without pushing what they wrote
#: (``edit`` and ``codemod`` push their own changes).
_MAY_REWRITE_TREE: Final[frozenset[str]] = frozenset({"bash"})
#: Tools answered beside the request loop instead of on it. ``bash`` runs as
#: long as its caller asks (up to an hour); answered inline, one hung command
#: held every later request -- an ``edit``, a ``read`` -- until the host timed
#: each out in turn. Everything else stays on the loop: server-routed calls
#: share one session and View, and hosts expect them answered in order.
_BESIDE_THE_LOOP: Final[frozenset[str]] = frozenset({"bash"})
_MAX_BESIDE_THE_LOOP: Final[int] = 8

_INSTRUCTIONS: Final[str] = (
    "LemonCrow thin client. This one stdio process owns the LemonCrow session for its lifetime; "
    "it starts no LemonCrow daemon, local server, MCP child server or second LemonCrow executable. "
    "Lead with code_search for code discovery; read known paths, edit files, run commands with bash, "
    "and use web_fetch for public pages. Indexed tools run on the configured LemonCrow server. "
    "A degraded result says why, and a refusal carries the next step."
)


class McpServer:
    """One JSON-RPC-over-stdio session."""

    _bootstrap_report: BootstrapReport | None
    __slots__ = (
        "_autosync",
        "_bootstrap_executor",
        "_bootstrap_future",
        "_bootstrap_lock",
        "_bootstrap_report",
        "_config",
        "_dispatcher",
        "_initialized",
        "_last_bootstrap_attempt",
        "_search_dedup",
        "_session",
        "_tools",
    )

    def __init__(self, config: ClientConfig, session: RemoteSession | None = None) -> None:
        self._config = config
        self._session = session if session is not None else RemoteSession(config)
        self._dispatcher = Dispatcher(config, self._session)
        self._search_dedup = SearchResultDedup(config.repo_root)
        self._tools = _advertised_tools()
        self._initialized = False
        self._bootstrap_report = None
        self._last_bootstrap_attempt = 0.0
        # The gateway runs stateless client-site tools concurrently with the
        # serial lane; this keeps a cold session from bootstrapping twice.
        self._bootstrap_lock = threading.Lock()
        self._bootstrap_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lc-bootstrap")
        self._bootstrap_future: Future[None] | None = None
        # Pushes host-hook and editor changes ahead of the next call; freshness
        # itself never depends on it. ``LEMONCROW_AUTOSYNC=0`` turns it off.
        self._autosync: AutoSync | None = (
            None
            if os.environ.get("LEMONCROW_AUTOSYNC", "1").strip() == "0"
            else AutoSync(self._session.sync_if_changed, config.repo_root, config.runtime_state_dir)
        )

    @property
    def dispatcher(self) -> Dispatcher:
        return self._dispatcher

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        """Answer one request, or ``None`` for a notification."""
        method = str(request.get("method") or "")
        request_id = request.get("id")
        params = request.get("params")
        arguments: Mapping[str, Any] = params if isinstance(params, Mapping) else {}

        if method == "initialize":
            self._search_dedup.clear()
            self._initialized = True
            # Tool discovery must never wait for repository bytes or indexing.
            # The session owns and joins this worker before closing HTTP state.
            if self._bootstrap_future is None:
                self._bootstrap_future = self._bootstrap_executor.submit(self._ensure_session, retry=True)
            return _ok(
                request_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "title": "LemonCrow",
                        "version": CLIENT_VERSION,
                        "description": (
                            "Indexed code search, bounded file tools, editing and verification "
                            "for coding agents, served by the LemonCrow server."
                        ),
                    },
                    "capabilities": {"tools": {}},
                    "instructions": f"{_INSTRUCTIONS} {self._session_status()}",
                },
            )
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if method == "ping":
            return _ok(request_id, {})
        if method == "tools/list":
            return _ok(request_id, {"tools": [dict(spec) for spec in self._tools]})
        if method == "tools/call":
            return self._call(request_id, arguments)
        if request_id is None:
            return None
        return _err(request_id, -32601, f"unknown method: {method}")

    def _call(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        raw_arguments = params.get("arguments")
        if isinstance(raw_arguments, str):
            # Some hosts deliver the whole arguments object as a JSON string.
            try:
                raw_arguments = json.loads(raw_arguments)
            except (ValueError, RecursionError):
                raw_arguments = {}
        arguments: Mapping[str, Any] = raw_arguments if isinstance(raw_arguments, Mapping) else {}
        if name == toolbroker.BROKER_TOOL_NAME and toolbroker.broker_enabled():
            # The broker's own target may be server-sited; always worth a retry
            # since these calls are rare and the bootstrap check is cheap once
            # already bootstrapped.
            self._ensure_session(retry=True)
            outcome = toolbroker.handle(arguments, dispatcher=self._dispatcher)
            return _ok(request_id, outcome.to_mcp())
        retry_bootstrap = False
        try:
            retry_bootstrap = route_for(name).site is not Site.CLIENT
        except ClientError:
            pass
        if retry_bootstrap:
            self._ensure_session(retry=True)
        if self._autosync is not None and self._session.bootstrapped and self._session.indexed:
            self._autosync.start()
            self._autosync.touch()
        outcome = self._dispatcher.call(name, arguments)
        if name in _MAY_REWRITE_TREE:
            self._sync_after_rewrite()
        if name == "code_search":
            outcome = self._search_dedup.compact(
                outcome,
                arguments=arguments,
                session_id=outcome.session_id or self._session.state.session_id,
                view_id=outcome.view_id or self._session.state.view_id,
            )
        # stdio MCP is the model-facing surface. ToolOutcome keeps structured
        # data internally for caches/hydration/programmatic callers, but this
        # boundary deliberately emits only the compact rendered content.
        return _ok(request_id, outcome.to_mcp())

    def _ensure_session(self, *, retry: bool = False) -> None:
        """Open this process's remote session, retrying after startup races."""
        with self._bootstrap_lock:
            if self._session.bootstrapped:
                return
            now = time.monotonic()
            if self._session.state.reason:
                if not retry or now - self._last_bootstrap_attempt < _OFFLINE_RETRY_INTERVAL_S:
                    return
            self._last_bootstrap_attempt = now
            try:
                budget = self._config.startup_budget_s
                if retry:
                    budget = max(budget, self._config.request_timeout_s)
                report = self._session.bootstrap(deadline_s=budget)
            except ClientError as exc:  # pragma: no cover - bootstrap is fail-open
                self._session.state.go_offline(f"{exc.server_code}: {exc.message}")
                return
            self._bootstrap_report = report
            if not report.ok:
                self._session.state.go_offline(report.reason)

    def _session_status(self) -> str:
        report = self._bootstrap_report
        if report is not None:
            return report.one_line(self._config.url)
        reason = self._session.state.reason
        if reason:
            return f"LemonCrow: server-side tools unavailable ({reason}); client-side tools still work"
        return "LemonCrow: session bootstrap pending"

    def answers_beside_the_loop(self, request: Mapping[str, Any]) -> bool:
        """Whether ``serve`` may answer this request concurrently with later ones."""
        if request.get("method") != "tools/call" or request.get("id") is None:
            return False
        params = request.get("params")
        return isinstance(params, Mapping) and params.get("name") in _BESIDE_THE_LOOP

    def background_running(self) -> bool:
        """A live ``bash bg=true`` run keeps this session from being evicted as idle."""
        return self._dispatcher.background_running()

    def _sync_after_rewrite(self) -> None:
        """Push what a command changed before answering it, only if it changed.

        The next index call then finds the View current instead of paying for
        the sync itself. Synchronous on purpose after bootstrap. Best effort -- freshness never depends on
        it, because every View-reading call still checks the tree first -- so it
        can never fail the command it follows, and it skips rather than waits
        when another call holds the session, or when this same tree already
        failed to push (a hung server would otherwise hold every later command).
        """
        if not (self._session.bootstrapped and self._session.indexed):
            return
        try:
            self._session.sync_if_changed(retry_failed=False)
        except Exception:  # never fail the command this follows
            return

    def close(self) -> None:
        self._bootstrap_executor.shutdown(wait=True, cancel_futures=True)
        if self._autosync is not None:
            self._autosync.close()  # joins its threads before the session goes away
        self._dispatcher.close_background()
        self._dispatcher.close_worktrees()
        self._search_dedup.clear()
        self._session.close()


def _advertised_tools() -> list[ToolSpec]:
    """The visible ``tools/list`` payload; hidden exact-name calls remain available."""
    tools = tool_list()
    if toolbroker.broker_advertised():
        tools = sorted([*tools, ToolSpec(toolbroker.broker_spec())], key=lambda spec: str(spec["name"]))
    return tools


def serve(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    config: ClientConfig | None = None,
) -> int:
    """Read requests until stdin closes, then exit. That is the whole lifecycle."""
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    try:
        resolved = config if config is not None else load_config(product="runtime")
    except ClientError as exc:
        # A misconfiguration is fatal and must be visible. Writing a JSON-RPC
        # error would be answering a request nobody made.
        print(f"lemoncrow-client: {exc.message}", file=sys.stderr)
        return 2
    server = McpServer(resolved)
    write_lock = threading.Lock()

    def emit(payload: Mapping[str, Any]) -> None:
        with write_lock:
            _write(sink, payload)

    def answer(request: Mapping[str, Any]) -> None:
        try:
            response = server.handle(request)
        except Exception as exc:  # a worker answers; it never dies silently
            response = _err(request.get("id"), -32603, f"internal error: {type(exc).__name__}")
        if response is not None:
            try:
                emit(response)
            except BrokenPipeError:  # the host is gone; the loop sees it too
                return

    # Threads start only when a ``bash`` call arrives, and all are joined below.
    pool = ThreadPoolExecutor(max_workers=_MAX_BESIDE_THE_LOOP, thread_name_prefix="lc-call")
    abandoned = False
    try:
        for line in source:
            if not line.strip():
                continue
            if len(line) > _MAX_LINE_BYTES:
                emit(_err(None, -32600, "request exceeds the client's line limit"))
                continue
            try:
                request = json.loads(line)
            except (ValueError, RecursionError):
                emit(_err(None, -32700, "parse error"))
                continue
            if not isinstance(request, Mapping):
                emit(_err(None, -32600, "request must be a JSON object"))
                continue
            if server.answers_beside_the_loop(request):
                pool.submit(answer, request)
                continue
            response = server.handle(request)
            if response is not None:
                emit(response)
    except (BrokenPipeError, KeyboardInterrupt):
        abandoned = True
        return 0
    finally:
        if abandoned:
            # Nobody will read the answers: end the commands instead of waiting.
            cancel_active_bash_commands()
        # Otherwise every request already read is answered before exit, exactly
        # as when the loop answered all of them itself.
        pool.shutdown(wait=True, cancel_futures=abandoned)
        server.close()
    return 0


def _write(sink: TextIO, payload: Mapping[str, Any]) -> None:
    sink.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sink.flush()


def _ok(request_id: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def _err(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message, "data": {"client_code": ErrorCode.INTERNAL.value}},
    }

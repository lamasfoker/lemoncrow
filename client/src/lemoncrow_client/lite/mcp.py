"""Session-scoped stdio MCP server for Runtime Lite.

This is a real execution boundary, not a thin-client configuration mode. It
does not load endpoint settings, credentials, RemoteSession, Views, upload code,
or network transport. Its only lifecycle is stdin/stdout: EOF ends the process.

``initialize`` answers immediately. The repository index is brought current on
one background thread owned by this process (a cold build of a large repository
can take minutes, far past a host's connect timeout). Index-backed tools wait
briefly for it and otherwise report build progress; file reads, grep, edit and
bash never wait.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TextIO

from ..errors import ClientError, error_block
from ..localtools import LocalContext, LocalResult, SessionMemory
from ..localtools import astgrep as local_astgrep
from ..localtools import editing as local_editing
from ..localtools import search as local_search
from ..localtools import shell as local_shell
from ..localtools import vcs as local_vcs
from ..localtools import web as local_web
from ..localtools.shell import close_background
from ..trace import ClientTraceWriter, new_trace_id
from ..worktrees import LinkedWorktree, WorktreeCatalog
from .index import LiteIndex, LiteIndexProgress, LiteIndexUpgradeRequired, ProgressCallback
from .policy import LITE_VISIBLE_TOOLS, LOCAL_LITE_TOOLS
from .semantic import LiteSemanticClient, fuse_ranked_paths, is_semantic_intent_query

SERVER_NAME: Final[str] = "lemoncrow-runtime-lite"
SERVER_VERSION: Final[str] = "1"
_PROTOCOL_VERSION: Final[str] = "2025-06-18"
_MUTATING_OR_UNKNOWN_TOOLS: Final[frozenset[str]] = frozenset({"edit", "codemod", "bash"})
_SERIAL_TOOLS: Final[frozenset[str]] = frozenset({"edit", "codemod"})
_MAX_CONCURRENT_CALLS: Final[int] = 8
_MAX_CONCURRENT_INDEXED_CALLS: Final[int] = 2
_MAX_WORKTREE_INDEXES: Final[int] = 8
#: Tools answered from the index; ``read`` joins them only for ``symbol=``.
_INDEX_TOOLS: Final[frozenset[str]] = frozenset({"code_search", "relations", "graph"})
#: How long an index-backed call waits for the background build before
#: reporting progress instead (small repositories finish inside it).
_INDEX_WAIT_S: Final[float] = 20.0
#: How long EOF waits for a cancelled background build to roll back.
_PREPARE_STOP_WAIT_S: Final[float] = 5.0
_LOCAL_EXECUTORS: Final[dict[str, Any]] = {
    "bash": local_shell.run_bash,
    "edit": local_editing.run_edit,
    "grep": local_search.run_grep,
    "scan": local_astgrep.run_scan,
    "codemod": local_astgrep.run_codemod,
    "blame": local_vcs.run_blame,
    "web_fetch": local_web.run_web_fetch,
}


def _workspace_root(environment: Mapping[str, str]) -> Path:
    explicit = environment.get("LEMONCROW_WORKSPACE_ROOT", "").strip()
    if explicit:
        root = Path(explicit).expanduser().resolve()
    else:
        try:
            completed = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=Path.cwd(),
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            completed = None
        root = (
            Path(completed.stdout.strip()).expanduser().resolve()
            if completed is not None and completed.returncode == 0 and completed.stdout.strip()
            else Path.cwd().resolve()
        )
    if not root.is_dir():
        raise RuntimeError(f"Runtime Lite workspace is not a directory: {root}")
    return root


def _state_root(environment: Mapping[str, str]) -> Path:
    explicit = environment.get("LEMONCROW_HOME", "").strip()
    return Path(explicit).expanduser().resolve() if explicit else (Path.home() / ".lemoncrow").resolve()


@dataclass(frozen=True, slots=True)
class _LiteConfig:
    state_dir: Path

    @property
    def runtime_state_dir(self) -> Path:
        return self.state_dir / "runtime"


def _local_config(_repo_root: Path, environment: Mapping[str, str]) -> _LiteConfig:
    """Construct local executor context without any endpoint/account fields."""

    return _LiteConfig(state_dir=_state_root(environment))


@dataclass(slots=True)
class _LiteWorkspace:
    root: Path
    environment: dict[str, str]
    index: LiteIndex
    memory: SessionMemory
    context: LocalContext


class _PrepareStopped(Exception):
    """Raised from the progress callback to abandon a background build at EOF."""


_LITE_SCHEMA_OVERRIDES: Final[dict[str, dict[str, Any]]] = {
    "bash": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "command": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ]
            },
            "cwd": {"type": "string"},
            "timeout": {"type": "integer", "default": 120},
            "bg": {"type": "boolean", "default": False},
            "id": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ]
            },
            "action": {
                "type": "string",
                "enum": ["poll", "status", "kill", "update"],
            },
        },
    },
    "edit": {
        "type": "object",
        "required": ["edits"],
        "additionalProperties": False,
        "properties": {
            "edits": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["path"],
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string"},
                        "old": {"type": "string"},
                        "new": {"type": "string"},
                        "replace": {"type": "boolean"},
                    },
                },
            }
        },
    },
    "read": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "files": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "object", "additionalProperties": True},
                    ]
                },
            },
            "symbol": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ]
            },
        },
    },
    "code_search": {
        "type": "object",
        "required": ["query"],
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
            "paths": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ]
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 32},
        },
    },
    "relations": {
        "type": "object",
        "required": ["symbol"],
        "additionalProperties": False,
        "properties": {
            "symbol": {"type": "string"},
            "kind": {
                "type": "string",
                "enum": ["self", "usages", "imports", "callers", "callees"],
                "default": "usages",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        },
    },
    "graph": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {"type": "string", "enum": ["map", "imports"], "default": "map"},
            "path": {"type": "string", "default": ""},
            "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
        },
    },
    "blame": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "path": {"type": "string"},
            "include_churn": {"type": "boolean", "default": True},
        },
    },
}


def _surface() -> list[dict[str, Any]]:
    """Return the explicit Lite allowlist using shared schemas where appropriate."""

    surface_path = Path(__file__).resolve().parents[1] / "toolsurface.json"
    payload = json.loads(surface_path.read_text(encoding="utf-8"))
    source = payload.get("tools") if isinstance(payload, dict) else None
    if not isinstance(source, dict):
        raise RuntimeError("Runtime tool surface is not a name-keyed object")

    tools: list[dict[str, Any]] = []
    for name in LOCAL_LITE_TOOLS:
        if name not in LITE_VISIBLE_TOOLS:
            continue
        raw = source.get(name)
        if not isinstance(raw, dict):
            raise RuntimeError(f"Runtime Lite tool missing shared schema: {name}")
        schema = _LITE_SCHEMA_OVERRIDES.get(name, raw.get("inputSchema"))
        if not isinstance(schema, dict):
            raise RuntimeError(f"Runtime Lite tool has invalid schema: {name}")
        description = str(raw.get("description") or "")
        if name == "relations":
            description = (
                "Basic local symbol relationships from the Runtime Lite index. "
                "kind=self|usages|imports is exact/basic; callers/callees are bounded lexical approximations."
            )
        elif name == "graph":
            description = (
                "Basic local repository map. kind=map summarizes files by symbol/import counts; "
                "kind=imports lists parsed imports. Advanced graph/lineage analytics are hosted."
            )
        tools.append({"name": name, "description": description, "inputSchema": schema})
    return tools


class LiteMcp:
    def __init__(self, *, environment: Mapping[str, str] | None = None) -> None:
        self.environment = dict(environment or os.environ)
        self.repo_root = _workspace_root(self.environment)
        self._git_workspace = (self.repo_root / ".git").exists()
        self._catalog = WorktreeCatalog(self.repo_root)
        self._children: OrderedDict[Path, _LiteWorkspace] = OrderedDict()
        self._children_lock = threading.Lock()
        # One session-owned refresh lane validates/updates warm WAL generations.
        # ThreadPoolExecutor is deliberate: unlike a detached raw thread, close()
        # shuts it down and joins all work before the MCP session exits.
        self._refresh_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lc-lite-refresh")
        self._refresh_futures: dict[Path, Future[Any]] = {}
        self._refresh_futures_lock = threading.Lock()
        self._main = self._new_workspace(self.repo_root)
        self._semantic = LiteSemanticClient(self.environment)
        # Keep the original public attributes for compatibility with tests and
        # callers that inspect the primary checkout directly.
        self.index = self._main.index
        self.memory = self._main.memory
        self.context = self._main.context
        self.tools = _surface()
        # One stdio process serves one host session; its calls are the local
        # usage record (same schema as the full client's trace, site "lite").
        self.session_id = f"lite_{secrets.token_hex(8)}"
        self._traces = ClientTraceWriter(_state_root(self.environment) / "runtime", env=self.environment)
        self._prepare_thread: threading.Thread | None = None
        self._prepare_error: Exception | None = None
        self._prepare_progress: LiteIndexProgress | None = None
        self._prepare_stop = threading.Event()
        self._semantic_prepare_thread: threading.Thread | None = None
        self._semantic_prepare_root: Path | None = None

    def _new_workspace(self, root: Path) -> _LiteWorkspace:
        environment = dict(self.environment)
        environment["LEMONCROW_WORKSPACE_ROOT"] = str(root)
        memory = SessionMemory()
        context = LocalContext(
            config=_local_config(root, environment),
            repo_root=root,
            sync=None,
            environment=environment,
            memory=memory,
        )
        return _LiteWorkspace(
            root=root,
            environment=environment,
            index=LiteIndex(root, stale_while_refresh=True),
            memory=memory,
            context=context,
        )

    def _schedule_refresh(self, workspace: _LiteWorkspace) -> None:
        if not self._git_workspace or not workspace.index.refresh_needed():
            return
        root = workspace.root.resolve()
        with self._refresh_futures_lock:
            existing = self._refresh_futures.get(root)
            if existing is not None and not existing.done():
                return
            if existing is not None:
                try:
                    existing.result()
                except Exception:
                    pass
            self._refresh_futures[root] = self._refresh_executor.submit(workspace.index.refresh_if_needed)

    def _worktree_workspace(self, worktree: LinkedWorktree) -> _LiteWorkspace:
        root = worktree.root.resolve()
        with self._children_lock:
            workspace = self._children.pop(root, None)
            if workspace is not None:
                self._children[root] = workspace
                return workspace

        # Opening/adopting an index may need Git and SQLite work. Keep it out of
        # the global child-worktree lock so one subagent entering a new checkout
        # cannot stall unrelated routed calls. Concurrent creators are harmless:
        # SQLite serializes a cold writer and the loser is discarded below.
        candidate = self._new_workspace(root)
        candidate.index.adopt_prepared()

        evicted: _LiteWorkspace | None = None
        discard: _LiteWorkspace | None = None
        with self._children_lock:
            workspace = self._children.pop(root, None)
            if workspace is None:
                workspace = candidate
            else:
                discard = candidate
            self._children[root] = workspace
            while len(self._children) > _MAX_WORKTREE_INDEXES:
                _old_root, evicted = self._children.popitem(last=False)

        if discard is not None:
            close_background(discard.memory)
        if evicted is not None:
            close_background(evicted.memory)
        self._start_semantic_prepare(root)
        return workspace

    def prepare(self, *, progress: ProgressCallback | None = None) -> dict[str, object]:
        if not self._git_workspace:
            return {"ok": True, "mode": "grep", "reason": "not a Git repository"}
        # Initialize only adopts the last durable WAL generation: proving it fresh
        # (git status) or refreshing it here would make a short-lived MCP session
        # pay for repository freshness even when it never used an indexed tool.
        # The first real tool call serves this generation and then schedules the
        # single session-owned refresh lane. With no generation yet, this builds
        # one, which start_prepare runs off the request path.
        return self.index.adopt_prepared(progress=progress).to_dict()

    def start_prepare(self) -> None:
        """Bring the main index current off the request path (once per process)."""

        if not self._git_workspace or self._prepare_thread is not None:
            return
        thread = threading.Thread(target=self._prepare_in_background, name="lite-index-prepare", daemon=True)
        self._prepare_thread = thread
        thread.start()

    def _prepare_in_background(self) -> None:
        try:
            self.prepare(progress=self._observe_prepare)
            self._start_semantic_prepare(self.repo_root)
        except _PrepareStopped:
            return
        except Exception as exc:  # surfaced by the next index-backed call
            self._prepare_error = exc

    def _start_semantic_prepare(self, root: Path, *, force: bool = False) -> None:
        if not self._semantic.active or self._semantic.ready(root):
            return
        thread = self._semantic_prepare_thread
        if thread is not None and thread.is_alive():
            return
        adopted = not force and self._semantic.adopt_prepared(root)
        self._semantic_prepare_root = root.resolve()
        thread = threading.Thread(
            target=self._prepare_semantic_in_background,
            args=(self._semantic_prepare_root, force, adopted),
            name="lite-semantic-prepare",
            daemon=True,
        )
        self._semantic_prepare_thread = thread
        thread.start()

    def _prepare_semantic_in_background(self, root: Path, force: bool, adopted: bool) -> None:
        try:
            if adopted:
                self._semantic.warm(root)
            else:
                self._semantic.prepare(root, force=force)
        finally:
            self._semantic_prepare_root = None

    def _observe_prepare(self, observed: LiteIndexProgress) -> None:
        self._prepare_progress = observed
        if self._prepare_stop.is_set():
            raise _PrepareStopped()

    def _needs_index(self, name: str, arguments: Mapping[str, Any]) -> bool:
        return name in _INDEX_TOOLS or (name == "read" and bool(arguments.get("symbol")))

    def _await_index(self) -> dict[str, Any] | None:
        """Wait briefly for the background build; report its progress rather than stall the host."""

        thread = self._prepare_thread
        if thread is not None:
            thread.join(_INDEX_WAIT_S)
            if thread.is_alive():
                return self._success(({"type": "text", "text": self._building_message()},), is_error=True)
        error, self._prepare_error = self._prepare_error, None
        if error is not None:
            raise error
        return None

    def _building_message(self) -> str:
        seen = self._prepare_progress
        if seen is None or seen.phase == "seed":
            detail = "starting"
        elif seen.phase == "lock":
            detail = "waiting for another lc process that is indexing this repository"
        elif seen.phase == "scan" and seen.total:
            detail = (
                f"{seen.done:,}/{seen.total:,} files ({seen.done * 100 // seen.total}%), {seen.elapsed_s:.0f}s elapsed"
            )
        elif seen.phase == "scan":
            detail = f"{seen.done:,} files walked, {seen.elapsed_s:.0f}s elapsed"
        else:
            detail = f"finishing, {seen.elapsed_s:.0f}s elapsed"
        return (
            f"[lc:index_building] Runtime Lite is building this repository's index in the background ({detail}). "
            "code_search, relations, graph and read symbol= need it: retry shortly. "
            "read files, grep, edit and bash work now.\n"
            "retryable=true action=retry"
        )

    def close(self) -> None:
        thread = self._prepare_thread
        if thread is not None and thread.is_alive():
            self._prepare_stop.set()
            thread.join(_PREPARE_STOP_WAIT_S)
        self._refresh_executor.shutdown(wait=True, cancel_futures=False)
        self._semantic.close()
        semantic_thread = self._semantic_prepare_thread
        if semantic_thread is not None and semantic_thread.is_alive():
            semantic_thread.join(_PREPARE_STOP_WAIT_S)
        close_background(self.memory)
        with self._children_lock:
            children = list(self._children.values())
            self._children.clear()
        for workspace in children:
            close_background(workspace.memory)

    @staticmethod
    def _success(
        content: Sequence[Mapping[str, Any]],
        *,
        structured: Mapping[str, Any] | None = None,
        is_error: bool = False,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"content": list(content), "isError": is_error}
        if structured is not None:
            result["structuredContent"] = dict(structured)
        return result

    @staticmethod
    def _with_worktree_header(result: dict[str, Any], worktree: LinkedWorktree) -> dict[str, Any]:
        rendered = dict(result)
        blocks = [dict(block) for block in rendered.get("content", []) if isinstance(block, Mapping)]
        prefix = f"[worktree {worktree.name}]"
        for index, block in enumerate(blocks):
            value = block.get("text")
            if block.get("type") == "text" and isinstance(value, str):
                block["text"] = f"{prefix}\n{value}" if value else prefix
                blocks[index] = block
                rendered["content"] = blocks
                return rendered
        blocks.insert(0, {"type": "text", "text": prefix})
        rendered["content"] = blocks
        return rendered

    def _run_local(self, workspace: _LiteWorkspace, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        executor = _LOCAL_EXECUTORS[name]
        result: LocalResult = executor(workspace.context, arguments)
        if name in _MUTATING_OR_UNKNOWN_TOOLS:
            workspace.index.mark_dirty()
        return self._success(result.content, structured=result.structured, is_error=result.is_error)

    def _confine_read_files(self, workspace: _LiteWorkspace, files: object) -> None:
        if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
            raise ValueError("read files must be an array")
        from ..kit.read_path import split_file_opts

        for raw in files:
            if isinstance(raw, Mapping):
                spelled = str(raw.get("path") or raw.get("file_path") or raw.get("filePath") or "")
            else:
                spelled = str(raw)
            path, _line_range, _full, _head, _tail, _summary, _outline = split_file_opts(spelled)
            workspace.context.resolve(path)

    def _read(self, workspace: _LiteWorkspace, arguments: Mapping[str, Any]) -> dict[str, Any]:
        blocks: list[Mapping[str, Any]] = []
        structured: dict[str, Any] = {}
        symbol = arguments.get("symbol")
        if symbol:
            names = [symbol] if isinstance(symbol, str) else [str(item) for item in symbol]
            definitions = workspace.index.exact_symbols(names)
            structured["symbols"] = definitions
            for definition in definitions:
                path = str(definition["path"])
                line = int(definition["line"])
                start = max(1, line - 3)
                end = line + 24
                local = local_search.read_from_disk(
                    workspace.context,
                    {"files": [f"{path}:L{start}-L{end}"]},
                )
                blocks.extend(local.content)
        files = arguments.get("files")
        if files:
            self._confine_read_files(workspace, files)
            local = local_search.read_from_disk(workspace.context, {"files": files})
            blocks.extend(local.content)
        if not blocks and not structured:
            local = local_search.read_from_disk(workspace.context, arguments)
            blocks.extend(local.content)
        return self._success(blocks, structured=structured or None)

    @staticmethod
    def _search_markdown(payload: Mapping[str, Any]) -> str:
        sections: list[str] = []
        for item in payload.get("files", []) or []:
            if not isinstance(item, Mapping):
                continue
            path = str(item.get("path") or "")
            start = int(item.get("line") or 1)
            end = int(item.get("end_line") or start)
            sections.append(f"## {path}:L{start}-L{end}")
            source = str(item.get("source") or "")
            if source:
                sections.append(source)
            else:
                sections.append(f"→ read {path}:L{start}-L{end}")
        related = payload.get("related_symbols") or []
        if related:
            sections.append("related_symbols:")
            for item in related:
                if isinstance(item, Mapping):
                    sections.append(f"- {item.get('path')}:L{item.get('line')} {item.get('kind')} {item.get('name')}")
        candidates = payload.get("candidate_files") or []
        if candidates:
            sections.append("candidate_files: " + ", ".join(str(path) for path in candidates))
        rendered = "\n".join(sections) if sections else "No indexed matches."
        # The model should see the ranked evidence, not an unbounded dump.
        # Full source remains addressable through read path:Lx-Ly.
        max_chars = 12_000
        if len(rendered) > max_chars:
            rendered = (
                rendered[:max_chars].rsplit("\n", 1)[0]
                + "\n[lc:output_trimmed] More matches omitted; read the cited paths or narrow the query."
            )
        return rendered

    def _call_without_git(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        notice = "[no git: literal grep]"
        if name == "code_search":
            import re

            query = str(arguments.get("query") or "")
            if not query.strip():
                raise ValueError("code_search requires a non-empty query")
            paths = arguments.get("paths") or ["."]
            scopes = [paths] if isinstance(paths, str) else list(paths)
            requested = arguments.get("limit", 8)
            limit = max(1, min(32, requested)) if isinstance(requested, int) and not isinstance(requested, bool) else 8
            blocks: list[Mapping[str, Any]] = [{"type": "text", "text": notice}]
            for scope in scopes:
                self.context.resolve(str(scope))
                local = local_search.run_grep(
                    self.context,
                    {"regex": re.escape(query), "path": str(scope), "file_limit": limit, "i": True},
                )
                blocks.extend(local.content)
            return self._success(blocks, structured={"mode": "grep", "reason": "not a Git repository"})
        if name in {"relations", "graph", "blame"} or (name == "read" and arguments.get("symbol")):
            return self._success(
                (
                    {
                        "type": "text",
                        "text": (
                            f"[lc:git_required] {name} requires a Git repository and its index. "
                            "This directory is in grep fallback mode; use code_search and read files instead."
                        ),
                    },
                ),
                is_error=True,
            )
        return self._call_workspace(self._main, name, arguments)

    def _call_workspace(
        self,
        workspace: _LiteWorkspace,
        name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        if name in _LOCAL_EXECUTORS:
            return self._run_local(workspace, name, arguments)
        if name == "read":
            return self._read(workspace, arguments)
        if name == "code_search":
            query = str(arguments.get("query") or "")
            paths = arguments.get("paths")
            requested_limit = arguments.get("limit", arguments.get("max_files", 8))
            limit = (
                int(requested_limit)
                if isinstance(requested_limit, int) and not isinstance(requested_limit, bool)
                else 8
            )
            scoped_paths = paths if isinstance(paths, (str, list, tuple)) else None
            payload = workspace.index.search(query, paths=scoped_paths, limit=limit)
            if self._semantic.active and is_semantic_intent_query(query):
                semantic_matches = []
                semantic_thread = self._semantic_prepare_thread
                preparing = semantic_thread is not None and semantic_thread.is_alive()
                if self._semantic.ready(workspace.root):
                    semantic_matches = self._semantic.search(
                        workspace.root,
                        query,
                        paths=scoped_paths,
                        limit=100,
                    )
                elif not preparing:
                    if self._semantic.adopt_prepared(workspace.root):
                        semantic_matches = self._semantic.search(
                            workspace.root,
                            query,
                            paths=scoped_paths,
                            limit=100,
                        )
                    else:
                        self._start_semantic_prepare(workspace.root)
                if semantic_matches:
                    semantic_by_path = {match.path: match for match in semantic_matches}
                    semantic_order = [match.path for match in semantic_matches]
                    lexical_items = [dict(item) for item in payload.get("files", []) if isinstance(item, Mapping)]
                    lexical_by_path = {str(item.get("path") or ""): item for item in lexical_items}
                    lexical_order = [str(item.get("path") or "") for item in lexical_items]
                    lexical_order.extend(str(path) for path in payload.get("candidate_files", []) or [])
                    lexical_order = [path for path in lexical_order if path]
                    fused = fuse_ranked_paths(lexical_order, semantic_order)
                    rendered: list[dict[str, Any]] = []
                    remaining_chars = 12_000
                    for rank, path in enumerate(fused[:limit], start=1):
                        item = dict(lexical_by_path.get(path, {}))
                        item.setdefault("path", path)
                        channels = set(item.get("channels") or ())
                        semantic_match = semantic_by_path.get(path)
                        if semantic_match is not None:
                            channels.add("semantic")
                        item["channels"] = sorted(channels)
                        item.setdefault("score", 0.0)
                        if semantic_match is not None and path not in lexical_by_path:
                            item["line"] = semantic_match.line
                            item["end_line"] = semantic_match.end_line
                        else:
                            item.setdefault("line", 1)
                            item.setdefault("end_line", int(item.get("line") or 1))
                        if "source" not in item and rank <= 3 and remaining_chars > 0:
                            snippet, start_line, end_line = workspace.index._snippet(
                                path,
                                query,
                                int(item.get("line") or 1),
                            )
                            if snippet:
                                snippet = snippet[:remaining_chars]
                                remaining_chars -= len(snippet)
                                item["source"] = snippet
                                item["line"] = start_line
                                item["end_line"] = end_line
                        rendered.append(item)
                    payload = dict(payload)
                    payload["files"] = rendered
                    payload["candidate_files"] = fused[limit:32]
                    payload["semantic"] = {
                        "enabled": True,
                        "provider": "potion-code-16M-v2-int8",
                        "fused": True,
                    }
            result = self._success(
                ({"type": "text", "text": self._search_markdown(payload)},),
                structured=payload,
            )
            result["_lc_metrics"] = workspace.index.timing_snapshot()
            return result
        if name == "relations":
            symbol = str(arguments.get("symbol") or "").strip()
            if not symbol:
                raise ValueError("relations requires symbol")
            kind = str(arguments.get("kind") or "usages")
            limit = arguments.get("limit", 20)
            payload = workspace.index.relations(
                symbol,
                kind=kind,
                limit=int(limit) if isinstance(limit, int) and not isinstance(limit, bool) else 20,
            )
            return self._success(
                ({"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)},),
                structured=payload,
            )
        if name == "graph":
            kind = str(arguments.get("kind") or "map")
            path = str(arguments.get("path") or "")
            limit = arguments.get("limit", 50)
            payload = workspace.index.repo_map(
                kind=kind,
                path=path,
                limit=int(limit) if isinstance(limit, int) and not isinstance(limit, bool) else 50,
            )
            return self._success(
                ({"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)},),
                structured=payload,
            )
        raise AssertionError(f"unhandled Runtime Lite tool: {name}")

    def call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        queue_wait_ms: float = 0.0,
    ) -> dict[str, Any]:
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        result = dict(self._call(name, arguments))
        raw_metrics = result.pop("_lc_metrics", {})
        metrics = dict(raw_metrics) if isinstance(raw_metrics, Mapping) else {}
        metrics["queue_wait_ms"] = max(0.0, queue_wait_ms)
        self._record(name, arguments, started_at, started, result, metrics=metrics)
        return result

    def _record(
        self,
        name: str,
        arguments: Mapping[str, Any],
        started_at: datetime,
        started: float,
        result: Mapping[str, Any],
        *,
        metrics: Mapping[str, int | float],
    ) -> None:
        if not self._traces.enabled:
            return
        try:
            self._traces.record(
                trace_id=new_trace_id(),
                tool=name,
                arguments=arguments,
                started_at=started_at,
                duration_ms=round((time.perf_counter() - started) * 1000),
                site="lite",
                is_error=bool(result.get("isError")),
                degraded=False,
                degraded_reason="",
                output_chars=sum(
                    len(str(block.get("text") or ""))
                    for block in result.get("content") or ()
                    if isinstance(block, Mapping)
                ),
                session_id=self.session_id,
                view_revision=None,
                execution_metrics=metrics,
            )
        except Exception:  # a usage record must never fail a tool call
            return

    def _call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name not in LOCAL_LITE_TOOLS:
            return {
                "content": [{"type": "text", "text": f"[lc:tool_unknown] Runtime Lite does not expose {name!r}"}],
                "isError": True,
            }
        try:
            if not self._git_workspace:
                return self._call_without_git(name, arguments)
            # There is deliberately no process-global "active worktree": several
            # subagents can share this MCP process, so sticky routing would let one
            # caller silently redirect another caller's relative/pathless request.
            routed = self._catalog.route(arguments, active=None)
            if routed.worktree is None and self._needs_index(name, routed.arguments):
                pending = self._await_index()
                if pending is not None:
                    return pending
            workspace = self._main if routed.worktree is None else self._worktree_workspace(routed.worktree)
            result = self._call_workspace(workspace, name, routed.arguments)
            self._schedule_refresh(workspace)
            if routed.worktree is not None:
                result = self._with_worktree_header(result, routed.worktree)
            return result
        except ClientError as exc:
            return self._success((error_block(exc),), is_error=True)
        except LiteIndexUpgradeRequired as exc:
            return self._success(
                ({"type": "text", "text": f"[lc:upgrade_required] {exc}\nretryable=false action=restart_host"},),
                is_error=True,
            )
        except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
            return {
                "content": [{"type": "text", "text": f"[lc:lite_failed] {exc}"}],
                "isError": True,
            }

    def answers_concurrently(self, message: Mapping[str, Any]) -> bool:
        if message.get("method") != "tools/call" or message.get("id") is None:
            return False
        params = message.get("params")
        if not isinstance(params, Mapping):
            return False
        name = str(params.get("name") or "")
        return name not in _SERIAL_TOOLS

    def handle(
        self,
        message: Mapping[str, Any],
        *,
        queue_wait_ms: float = 0.0,
    ) -> dict[str, Any] | None:
        method = str(message.get("method") or "")
        request_id = message.get("id")
        if request_id is None:
            return None
        if method == "initialize":
            self.start_prepare()
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": (
                        (
                            "Local-only Runtime Lite for this Git worktree. "
                            if self._git_workspace
                            else "Local-only Runtime Lite. No Git: code_search uses literal grep; Git/index tools unavailable. "
                        )
                        + "No hosted connection, account, upload, daemon, or listening port is used."
                    ),
                },
            }
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": self.tools}}
        if method == "tools/call":
            params = message.get("params")
            params = params if isinstance(params, Mapping) else {}
            name = str(params.get("name") or "")
            arguments = params.get("arguments")
            arguments = arguments if isinstance(arguments, Mapping) else {}
            result = self.call(name, arguments, queue_wait_ms=queue_wait_ms)
            # Model-facing boundary: ``content`` already carries the curated text.
            # Hosts such as Claude Code show ``structuredContent`` instead when it
            # is present, so the raw payload (scores, channels, generation) would
            # replace the Markdown. Programmatic callers keep it via ``call()``.
            result.pop("structuredContent", None)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }


def serve(*, source: TextIO | None = None, sink: TextIO | None = None) -> int:
    """Serve JSON-RPC over stdio with bounded concurrent tool dispatch."""

    source = source or sys.stdin
    sink = sink or sys.stdout
    server = LiteMcp()
    write_lock = threading.Lock()
    pool = ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_CALLS, thread_name_prefix="lc-lite-call")
    indexed_pool = ThreadPoolExecutor(
        max_workers=_MAX_CONCURRENT_INDEXED_CALLS,
        thread_name_prefix="lc-lite-index",
    )
    pending: set[Future[None]] = set()

    def emit(response: Mapping[str, Any]) -> None:
        with write_lock:
            sink.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sink.flush()

    def answer(message: Mapping[str, Any], enqueued_at: float) -> None:
        try:
            response = server.handle(
                message,
                queue_wait_ms=(time.perf_counter() - enqueued_at) * 1000.0,
            )
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32603, "message": f"internal error: {type(exc).__name__}"},
            }
        if response is not None:
            emit(response)

    def drain() -> None:
        if not pending:
            return
        for future in tuple(pending):
            future.result()
        pending.clear()

    try:
        for raw in source:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
                if not isinstance(message, Mapping):
                    raise ValueError("JSON-RPC message must be an object")
            except (ValueError, json.JSONDecodeError) as exc:
                emit(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": str(exc)},
                    }
                )
                continue

            # Indexed tools use their own small pool: SQLite/FTS throughput
            # improves with a little overlap but falls sharply when every
            # subagent runs a CPU/SQLite-heavy search at once. Cheap local tools
            # keep the wider pool so reads and long bash calls do not block them.
            if server.answers_concurrently(message):
                pending = {future for future in pending if not future.done()}
                params = message.get("params")
                name = str(params.get("name") or "") if isinstance(params, Mapping) else ""
                executor = indexed_pool if name in _INDEX_TOOLS else pool
                pending.add(executor.submit(answer, message, time.perf_counter()))
                continue

            if message.get("method") == "tools/call":
                drain()
            response = server.handle(message)
            if response is not None:
                emit(response)
        drain()
    finally:
        indexed_pool.shutdown(wait=True, cancel_futures=False)
        pool.shutdown(wait=True, cancel_futures=False)
        server.close()
    return 0


__all__ = ["SERVER_NAME", "LiteMcp", "serve"]

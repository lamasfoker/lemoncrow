"""Read-only code-map queries for the local product UI.

Listings expose indexed symbol metadata and tracked file metadata; file links
count only uniquely resolved calls and parser-resolved imports. No
repository-scale graph is ever returned. Exact source crosses the API boundary
only when a user explicitly opens one symbol. Live activity is projected from
the existing run ledger; raw tool output, diffs, stdout, and stderr stay local.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import re
import select
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.paths import default_store_root, is_recognized_workspace, resolve_workspace_store_dir

_MAX_SEARCH_RESULTS = 40
_MAX_GRAPH_NODES = 160
_MAX_GRAPH_DEPTH = 2
_MAX_SOURCE_CHARS = 20_000
_FILE_TYPE_COLORS = {
    "source": "#67e8f9",
    "test": "#a78bfa",
    "docs": "#fbbf24",
    "config": "#86efac",
    "data": "#fb7185",
    "asset": "#94a3b8",
    "other": "#737373",
}
_LANGUAGE_BY_SUFFIX = {
    ".astro": "Astro",
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cs": "C#",
    ".css": "CSS",
    ".go": "Go",
    ".h": "C/C++ header",
    ".hpp": "C++ header",
    ".html": "HTML",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript JSX",
    ".kt": "Kotlin",
    ".lua": "Lua",
    ".php": "PHP",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".sh": "Shell",
    ".sql": "SQL",
    ".svelte": "Svelte",
    ".swift": "Swift",
    ".ts": "TypeScript",
    ".tsx": "TypeScript JSX",
    ".vue": "Vue",
}
_DOC_SUFFIXES = {".md", ".mdx", ".rst", ".txt", ".adoc"}
_CONFIG_SUFFIXES = {".json", ".jsonc", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".xml", ".env", ".lock"}
_DATA_SUFFIXES = {".csv", ".tsv", ".parquet", ".sqlite", ".sqlite3", ".db", ".jsonl"}
_ASSET_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".pdf",
    ".zip",
    ".gz",
}
_VERIFY_COMMAND_RE = re.compile(
    r"(?:^|\s)(?:pytest|vitest|jest|ruff|mypy|pyright|eslint|cargo\s+test|go\s+test|npm\s+(?:run\s+)?(?:test|build|lint|typecheck)|pnpm\s+(?:test|build|lint|typecheck)|yarn\s+(?:test|build|lint|typecheck)|uv\s+run\s+pytest)(?:\s|$)",
    re.IGNORECASE,
)


def _git_root(candidate: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def _git_text(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _project_location_metadata(root: Path) -> dict[str, Any]:
    """Describe exactly which checkout/worktree the Code UI is reading."""

    branch = _git_text(root, "branch", "--show-current")
    revision = _git_text(root, "rev-parse", "--short", "HEAD")
    common_dir_raw = _git_text(root, "rev-parse", "--git-common-dir")
    if common_dir_raw:
        common_dir = Path(common_dir_raw)
        if not common_dir.is_absolute():
            common_dir = (root / common_dir).resolve()
        project_root = common_dir.parent if common_dir.name == ".git" else root
    else:
        project_root = root

    return {
        "directory": str(root.parent),
        "project": project_root.name,
        "project_root": str(project_root),
        "worktree": root.name,
        "worktree_root": str(root),
        "branch": branch or (f"detached@{revision}" if revision else "unknown"),
        "revision": revision,
        "detached": not bool(branch),
    }


def resolve_project_root(requested: str | Path | None = None) -> Path:
    """Resolve one exact, recognized workspace root for map queries."""

    if requested:
        raw_requested = str(requested).strip()
        if raw_requested.startswith("proj_"):
            from lemoncrow.core.service.code_warm import discover_workspaces
            from lemoncrow.core.service.project_registry import (
                project_id_for_root,
                resolve_registered_project,
            )

            registered = resolve_registered_project(raw_requested)
            if registered is not None:
                candidate = registered.root
            else:
                candidates = [path for path in discover_workspaces() if not _is_ephemeral(path)]
                configured = os.environ.get("LEMONCROW_WORKSPACE_ROOT", "").strip()
                if configured:
                    with contextlib.suppress(OSError):
                        candidates.append(Path(configured).expanduser().resolve())
                for root in tuple(candidates):
                    candidates.extend(_git_worktrees(root))
                match = next(
                    (path.resolve() for path in candidates if project_id_for_root(path) == raw_requested),
                    None,
                )
                if match is None:
                    raise ValueError(f"unknown project id: {raw_requested}")
                candidate = match
        else:
            candidate = Path(raw_requested).expanduser().resolve()
    else:
        configured = os.environ.get("LEMONCROW_WORKSPACE_ROOT", "").strip()
        if configured:
            candidate = Path(configured).expanduser().resolve()
        else:
            from lemoncrow.core.service.code_warm import discover_workspaces
            from lemoncrow.core.service.project_registry import registered_projects

            active = [path for path in discover_workspaces() if _is_user_project(path)]
            registered_roots = [project.root for project in registered_projects() if _is_user_project(project.root)]
            candidate = (
                active[0]
                if active
                else registered_roots[0] if registered_roots else (_git_root(Path.cwd()) or Path.cwd().resolve())
            )

    if not candidate.is_dir():
        raise ValueError(f"project root does not exist: {candidate}")
    git_root = _git_root(candidate)
    marker_root = candidate if (candidate / ".lemoncrow").is_dir() else None
    if git_root != candidate and marker_root != candidate:
        if not is_recognized_workspace(candidate):
            raise ValueError(f"project root is not a registered workspace: {candidate}")
        raise ValueError(f"project root must point at the repository root: {candidate}")
    return candidate


def _is_ephemeral(path: Path) -> bool:
    resolved = path.resolve()
    return any(
        root == resolved or root in resolved.parents for root in (Path("/tmp"), Path("/var/tmp"), Path("/dev/shm"))
    )


def _is_internal_server_workspace(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    server_root = (default_store_root() / "server").resolve()
    return resolved == server_root or server_root in resolved.parents


def _is_user_project(path: Path) -> bool:
    return not _is_ephemeral(path) and not _is_internal_server_workspace(path)


def _git_worktrees(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "worktree", "list", "--porcelain", "-z"],
            capture_output=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [
        Path(_decode_path(record.removeprefix(b"worktree "))).resolve()
        for record in result.stdout.split(b"\0")
        if record.startswith(b"worktree ")
    ]


def list_projects() -> list[dict[str, Any]]:
    """List active and registered projects with their available Git worktrees."""

    candidates: list[Path] = []
    configured = os.environ.get("LEMONCROW_WORKSPACE_ROOT", "").strip()
    if configured:
        with contextlib.suppress(OSError):
            candidates.append(resolve_project_root(configured))
    from lemoncrow.core.service.code_warm import discover_workspaces
    from lemoncrow.core.service.project_registry import registered_projects

    active_workspaces = discover_workspaces()
    active_set = {path.resolve() for path in active_workspaces}
    candidates.extend(path for path in active_workspaces if _is_user_project(path))
    candidates.extend(project.root for project in registered_projects() if _is_user_project(project.root))
    for candidate in tuple(candidates):
        candidates.extend(root for root in _git_worktrees(candidate) if root.is_dir() and _is_user_project(root))
    if not candidates:
        with contextlib.suppress(ValueError):
            candidates.append(resolve_project_root())

    from lemoncrow.core.service.project_registry import project_id_for_root

    seen: set[Path] = set()
    projects: list[dict[str, Any]] = []
    for candidate in candidates:
        root = candidate.resolve()
        if root in seen:
            continue
        seen.add(root)
        db_path = resolve_workspace_store_dir(workspace_root=root) / "code_context.sqlite"
        projects.append(
            {
                "project_id": project_id_for_root(root),
                "root": str(root),
                "label": root.name,
                "indexed": db_path.is_file(),
                "active": root in active_set,
                **_project_location_metadata(root),
            }
        )
    return projects


@functools.lru_cache(maxsize=8)
def get_engine(project_root: str) -> Any:
    """Keep a small read-only engine set warm for the UI process."""

    from lemoncrow.core.capabilities.code_intelligence_provider import (
        create_code_intelligence_provider,
    )

    return create_code_intelligence_provider(project_root, autosync_enabled=False)


def get_map_engine(project_root: str) -> Any:
    """The engine `/code`'s deterministic views read.

    The private code-intelligence engine when it is installed; otherwise the
    open-source Runtime Lite index of the same worktree, whose relationships
    are import-level and labelled so (:mod:`code_map_lite`).
    """

    from lemoncrow.core.capabilities.code_intelligence_provider import ProEngineUnavailable

    try:
        return get_engine(project_root)
    except ProEngineUnavailable:
        from lemoncrow.core.service.code_map_lite import lite_engine

        return lite_engine(project_root)


def list_map_projects() -> list[dict[str, Any]]:
    """`/code`'s project list: ``indexed`` names the index the map will read."""

    from lemoncrow.core.capabilities.code_intelligence_provider import private_code_intelligence_available

    projects = list_projects()
    if private_code_intelligence_available():
        return projects
    from lemoncrow.core.service.code_map_lite import LITE_BACKEND, lite_index_present

    for project in projects:
        project["index"] = LITE_BACKEND
        project["indexed"] = lite_index_present(Path(str(project["root"])))
    return projects


def _record_node(record: Any) -> dict[str, Any]:
    return {
        "id": str(record.symbol_id),
        "label": str(record.symbol_name),
        "qualified_name": str(record.qualified_name),
        "path": str(record.file_path),
        "kind": str(record.kind),
        "language": str(record.language),
        "line": int(record.start_line),
        "end_line": int(record.end_line),
        "score": round(float(record.score), 4) if record.score is not None else None,
    }


def _payload_node(payload: dict[str, Any], *, focus: bool = False) -> dict[str, Any]:
    return {
        "id": str(payload.get("id") or payload.get("symbol_id") or ""),
        "label": str(payload.get("name") or payload.get("symbol_name") or "unknown"),
        "qualified_name": str(payload.get("qualified_name") or payload.get("name") or "unknown"),
        "path": str(payload.get("path") or payload.get("file_path") or ""),
        "kind": str(payload.get("kind") or "symbol"),
        "language": str(payload.get("language") or ""),
        "line": int(payload.get("line") or payload.get("start_line") or 0),
        "end_line": int(payload.get("end_line") or payload.get("line") or payload.get("start_line") or 0),
        "provenance": str(payload.get("provenance") or "local"),
        "focus": focus,
    }


def _file_node_id(path: str) -> str:
    return "file::" + hashlib.sha1(path.encode()).hexdigest()[:20]


def _module_folder(path: str) -> str:
    parent = Path(path).parent.as_posix()
    return "" if parent == "." else parent


def _file_metadata(path: str) -> tuple[str, str]:
    normalized = path.lower()
    suffix = Path(normalized).suffix
    name = Path(normalized).name
    language = _LANGUAGE_BY_SUFFIX.get(suffix, "")
    if suffix in _DOC_SUFFIXES:
        return "docs", "Markdown" if suffix in {".md", ".mdx"} else "Documentation"
    if (
        {"test", "tests"} & set(Path(normalized).parts)
        or name.startswith("test_")
        or ".test." in name
        or ".spec." in name
    ):
        return "test", language or "Test"
    if language:
        return "source", language
    if suffix in _CONFIG_SUFFIXES or name in {"dockerfile", "makefile", "procfile"}:
        return "config", suffix.removeprefix(".").upper() or "Configuration"
    if suffix in _DATA_SUFFIXES:
        return "data", suffix.removeprefix(".").upper() or "Data"
    if suffix in _ASSET_SUFFIXES:
        return "asset", suffix.removeprefix(".").upper() or "Asset"
    return "other", suffix.removeprefix(".").upper() or "Other"


def _tracked_files(project_root: Path) -> list[str]:
    # ``--recurse-submodules`` surfaces tracked files inside submodules, but git
    # rejects it combined with ``--others``, so untracked files come from a
    # separate top-level-only call.
    paths: list[str] = []
    for extra_args in (
        ("--cached", "--recurse-submodules"),
        ("--others", "--exclude-standard"),
    ):
        try:
            result = subprocess.run(
                ["git", "-C", str(project_root), "ls-files", "-z", *extra_args],
                capture_output=True,
                check=False,
                timeout=8,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if result.returncode != 0:
            return []
        paths.extend(
            path
            for raw in result.stdout.split(b"\0")
            if raw and (path := raw.decode("utf-8", errors="surrogateescape"))
        )
    return sorted(paths)


def _ignored_index_paths(project_root: Path, paths: list[str]) -> set[str]:
    if not paths:
        return set()
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "check-ignore", "-z", "--stdin"],
            input=b"\0".join(path.encode("utf-8", errors="surrogateescape") for path in paths) + b"\0",
            capture_output=True,
            check=False,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    return {_decode_path(raw) for raw in result.stdout.split(b"\0") if raw}


def _decode_path(raw: bytes) -> str:
    return raw.decode("utf-8", errors="surrogateescape")


def _ignored_worktree_paths(project_root: Path, *, limit: int = 2000) -> tuple[set[str], bool]:
    """Enumerate opt-in ignored files without reading unbounded Git output."""
    try:
        process = subprocess.Popen(
            ["git", "-C", str(project_root), "ls-files", "-z", "--others", "--ignored", "--exclude-standard"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return set(), False
    paths: set[str] = set()
    pending = b""
    truncated = False
    complete = False
    deadline = time.monotonic() + 8
    try:
        assert process.stdout is not None
        while len(paths) <= limit and time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                truncated = True
                break
            chunk = os.read(process.stdout.fileno(), 65_536)
            if not chunk:
                complete = True
                break
            *records, pending = (pending + chunk).split(b"\0")
            for raw in records:
                if raw:
                    paths.add(_decode_path(raw))
                if len(paths) > limit:
                    truncated = True
                    break
        if len(paths) > limit:
            truncated = True
    finally:
        if process.poll() is None:
            truncated = truncated or not complete
            process.kill()
        process.wait()
        if process.stdout is not None:
            process.stdout.close()
    return set(sorted(paths)[:limit]), truncated


def list_file_nodes(engine: Any, project_root: Path, *, include_ignored: bool = False) -> dict[str, Any]:
    """Return the complete repository file list for source browsing.

    The relationship graph is intentionally bounded for interactive rendering,
    but the source browser must not silently inherit that cap. Merge git-visible
    files with indexed symbol paths so stale or non-git indexes remain browsable.
    """

    tracked_files = _tracked_files(project_root)
    max_end_by_file: dict[str, int] = {}
    with contextlib.suppress(sqlite3.Error):
        rows = engine.connection().execute("""
            SELECT file_path, MAX(end_line) AS end_line
            FROM symbols
            GROUP BY file_path
            ORDER BY file_path
            """).fetchall()
        for row in rows:
            path = str(row["file_path"] or "")
            if path:
                max_end_by_file[path] = max(1, int(row["end_line"] or 1))

    indexed_ignored = _ignored_index_paths(project_root, list(max_end_by_file))
    ignored_files, ignored_truncated = _ignored_worktree_paths(project_root) if include_ignored else (set(), False)
    all_files = sorted(
        (set(tracked_files) | set(max_end_by_file) | ignored_files) - (set() if include_ignored else indexed_ignored)
    )
    files: list[dict[str, Any]] = []
    for path in all_files:
        file_type, language = _file_metadata(path)
        files.append(
            {
                "id": _file_node_id(path),
                "label": Path(path).name,
                "qualified_name": path,
                "path": path,
                "kind": file_type,
                "language": language,
                "file_type": file_type,
                "ignored": path in indexed_ignored or path in ignored_files,
                "node_type": "file",
                "line": 1,
                "end_line": max_end_by_file.get(path, 1),
                "module": _module_folder(path),
                "color": _FILE_TYPE_COLORS[file_type],
                "degree": 0,
            }
        )

    return {
        "project": {"root": str(project_root), "label": project_root.name},
        "total_files": len(files),
        "ignored_truncated": ignored_truncated,
        "files": files,
    }


def list_file_symbols(engine: Any, file_path: str, *, limit: int = 2000) -> dict[str, Any]:
    """Return indexed symbols for one file without constructing the repository graph."""

    clean_path = file_path.strip()
    if not clean_path:
        return {"path": "", "total_symbols": 0, "symbols": [], "truncated": False}
    bounded_limit = max(1, min(limit, 2000))
    conn = engine.connection()
    total = int(
        conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE file_path = ?",
            (clean_path,),
        ).fetchone()[0]
    )
    rows = conn.execute(
        """
        SELECT symbol_id, file_path, language, symbol_name, qualified_name,
               kind, start_line, end_line
        FROM symbols
        WHERE file_path = ?
        ORDER BY start_line, end_line, symbol_name
        LIMIT ?
        """,
        (clean_path, bounded_limit),
    ).fetchall()
    file_type, inferred_language = _file_metadata(clean_path)
    module = _module_folder(clean_path)
    symbols = [
        {
            "id": str(row["symbol_id"]),
            "label": str(row["symbol_name"]),
            "qualified_name": str(row["qualified_name"]),
            "path": str(row["file_path"]),
            "kind": str(row["kind"]),
            "language": str(inferred_language or row["language"] or "Other"),
            "file_type": file_type,
            "node_type": "symbol",
            "line": int(row["start_line"]),
            "end_line": int(row["end_line"]),
            "module": module,
            "degree": 0,
        }
        for row in rows
    ]
    return {
        "path": clean_path,
        "total_symbols": total,
        "symbols": symbols,
        "truncated": total > len(symbols),
    }


def symbol_row(engine: Any, symbol_id: str) -> dict[str, Any]:
    """One indexed symbol by id, as the Source outline lists it."""

    from lemoncrow.core.service.code_map_lite import LiteCodeMapEngine

    if isinstance(engine, LiteCodeMapEngine):
        # Lite ids carry the indexed name and line; never scan for them.
        return engine.lite_symbol_row(symbol_id)
    row = (
        engine.connection()
        .execute(
            """
            SELECT symbol_id, file_path, language, symbol_name, qualified_name,
                   kind, start_line, end_line
            FROM symbols
            WHERE symbol_id = ?
            LIMIT 1
            """,
            (symbol_id,),
        )
        .fetchone()
    )
    if row is None:
        raise LookupError(f"symbol not found: {symbol_id}")
    return {
        "id": str(row["symbol_id"]),
        "label": str(row["symbol_name"]),
        "qualified_name": str(row["qualified_name"]),
        "path": str(row["file_path"]),
        "kind": str(row["kind"]),
        "line": int(row["start_line"]),
        "end_line": int(row["end_line"]),
    }


def search_symbols(engine: Any, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    clean_query = query.strip()
    if not clean_query:
        return []
    bounded_limit = max(1, min(limit, _MAX_SEARCH_RESULTS))
    records = engine.search_symbols(
        clean_query,
        limit=bounded_limit,
        mode="lexical",
        snippet="none",
        auto_index=False,
    )
    return [_record_node(record) for record in records]


def search_text(engine: Any, query: str, *, limit: int = 12) -> list[dict[str, Any]]:
    clean_query = query.strip()
    if len(clean_query) < 2:
        return []
    bounded_limit = max(1, min(limit, 40))
    matches = engine.search_text(
        clean_query,
        path=".",
        limit=bounded_limit,
        ignore_case=True,
    )
    payload: list[dict[str, Any]] = []
    for match in matches:
        path = str(match.file_path)
        file_type, language = _file_metadata(path)
        payload.append(
            {
                "id": f"text::{path}:{match.line}:{match.column}",
                "path": path,
                "line": int(match.line),
                "column": int(match.column),
                "text": str(match.text).strip(),
                "language": language,
                "file_type": file_type,
            }
        )
    return payload


def find_references(engine: Any, symbol_id: str, *, limit: int = 60) -> dict[str, Any]:
    bounded_limit = max(1, min(limit, 120))
    payload = engine.find_references(
        symbol_id=symbol_id,
        group_by="none",
        snippet_lines=1,
        limit=bounded_limit,
        auto_index=False,
        budget_tokens=12_000,
    )
    if payload.get("error"):
        raise LookupError(str(payload.get("message") or f"symbol not found: {symbol_id}"))

    raw_references = payload.get("references")
    references: list[dict[str, Any]] = []
    if isinstance(raw_references, list):
        for raw in raw_references:
            if not isinstance(raw, dict):
                continue
            path = str(raw.get("file_path") or "")
            line = int(raw.get("line") or 0)
            if not path or line <= 0:
                continue
            references.append(
                {
                    "path": path,
                    "line": line,
                    "column": int(raw.get("column") or 1),
                    "end_line": int(raw.get("end_line") or line),
                    "snippet": str(raw.get("snippet") or ""),
                    "caller": str(raw.get("caller") or ""),
                    "edge_kind": str(raw.get("edge_kind") or "reference"),
                    "provenance": str(raw.get("provenance") or ""),
                    "confidence": raw.get("confidence"),
                }
            )

    return {
        "symbol_id": symbol_id,
        "reference_count": int(payload.get("reference_count") or len(references)),
        "references": references,
        "truncated": bool(payload.get("truncated")),
    }


def build_neighborhood(engine: Any, symbol_id: str, *, depth: int = 1, limit: int = 80) -> dict[str, Any]:
    """Merge bounded caller and callee traversals around one symbol."""

    bounded_depth = max(1, min(depth, _MAX_GRAPH_DEPTH))
    bounded_limit = max(4, min(limit, _MAX_GRAPH_NODES))
    per_direction = max(2, bounded_limit // 2)
    callers = engine.tool_callers(
        symbol_id=symbol_id,
        depth=bounded_depth,
        limit=per_direction,
        auto_index=False,
        budget_tokens=16_000,
    )
    callees = engine.tool_callees(
        symbol_id=symbol_id,
        depth=bounded_depth,
        limit=per_direction,
        auto_index=False,
        budget_tokens=16_000,
    )
    target = callers.get("target") or callees.get("target")
    if not isinstance(target, dict) or not target.get("id"):
        raise LookupError(f"symbol not found: {symbol_id}")

    nodes: dict[str, dict[str, Any]] = {}
    focus_node = _payload_node(target, focus=True)
    nodes[focus_node["id"]] = focus_node
    for payload in [*(callers.get("related") or []), *(callees.get("related") or [])]:
        if not isinstance(payload, dict):
            continue
        node = _payload_node(payload)
        if node["id"] and node["id"] not in nodes and len(nodes) < bounded_limit:
            nodes[node["id"]] = node

    edges: dict[tuple[str, str], dict[str, Any]] = {}
    for payload in [*(callers.get("edges") or []), *(callees.get("edges") or [])]:
        if not isinstance(payload, dict):
            continue
        source = str(payload.get("caller_symbol_id") or "")
        target_id = str(payload.get("callee_symbol_id") or "")
        if not source or not target_id or source not in nodes or target_id not in nodes:
            continue
        edges[(source, target_id)] = {
            "id": f"{source}->{target_id}",
            "source": source,
            "target": target_id,
            "kind": "calls",
            "depth": int(payload.get("depth") or 1),
        }

    degrees: dict[str, int] = {node_id: 0 for node_id in nodes}
    for edge in edges.values():
        degrees[edge["source"]] += 1
        degrees[edge["target"]] += 1
    for node_id, node in nodes.items():
        node["degree"] = degrees[node_id]

    return {
        "repo_id": str(getattr(engine, "repo_id", "")),
        "focus": focus_node["id"],
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "truncated": bool(callers.get("truncated") or callees.get("truncated")),
        "depth": bounded_depth,
    }


_LEGACY_LINKS_CACHE: dict[tuple[str, int], list[dict[str, Any]]] = {}


def legacy_file_links(engine: Any) -> list[dict[str, Any]]:
    """File-to-file links from the legacy index, aggregated by (source, target, kind).

    Imports are path-resolved by the parser. Calls are resolved by name and
    kept only when the name identifies exactly one definition, so an ambiguous
    ``run()`` never becomes a link. Cached per index database write.
    """

    db_path = Path(str(getattr(engine, "db_path", "")))
    stamp = db_path.stat().st_mtime_ns if db_path.is_file() else 0
    key = (str(db_path), stamp)
    cached = _LEGACY_LINKS_CACHE.get(key)
    if cached is not None:
        return cached

    aggregate: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add(source: str, target: str, kind: str, symbol: str = "") -> None:
        if not source or not target or source == target:
            return
        row = aggregate.get((source, target, kind))
        if row is None:
            row = {"source_path": source, "target_path": target, "kind": kind, "weight": 0, "symbols": {}}
            aggregate[(source, target, kind)] = row
        row["weight"] += 1
        if symbol:
            row["symbols"][symbol] = int(row["symbols"].get(symbol, 0)) + 1

    conn = engine.connection()
    with contextlib.suppress(sqlite3.Error):
        for row in conn.execute(
            "SELECT source_file, target_file FROM imports WHERE target_file IS NOT NULL AND target_file != ''"
        ):
            add(str(row["source_file"] or ""), str(row["target_file"] or ""), "imports")

    with contextlib.suppress(sqlite3.Error):
        by_qualified: dict[str, set[str]] = {}
        by_name: dict[str, set[str]] = {}
        by_file_name: set[tuple[str, str]] = set()
        for row in conn.execute("SELECT file_path, symbol_name, qualified_name FROM symbols"):
            path = str(row["file_path"] or "")
            name = str(row["symbol_name"] or "")
            by_qualified.setdefault(str(row["qualified_name"] or ""), set()).add(path)
            by_name.setdefault(name, set()).add(path)
            by_file_name.add((path, name))
        for row in conn.execute("SELECT caller_file_path, callee_name, callee_short_name FROM call_edges"):
            caller = str(row["caller_file_path"] or "")
            callee = str(row["callee_name"] or "")
            short = str(row["callee_short_name"] or callee.rsplit(".", 1)[-1])
            if (caller, short) in by_file_name:
                continue  # Resolves inside the caller's own file.
            targets = by_qualified.get(callee) or set()
            if len(targets) != 1:
                targets = by_name.get(short) or set()
            if len(targets) == 1:
                add(caller, next(iter(targets)), "calls", short)

    rows = list(aggregate.values())
    for row in rows:
        named = row["symbols"]
        row["symbols"] = [
            {"name": name, "count": count}
            for name, count in sorted(named.items(), key=lambda item: (-item[1], item[0]))[:4]
        ]
    _LEGACY_LINKS_CACHE.clear()
    _LEGACY_LINKS_CACHE[key] = rows
    return rows


def indexed_content_hash(engine: Any, path: str) -> str:
    """The content hash the legacy index recorded for *path*, or ``""``."""

    with contextlib.suppress(sqlite3.Error):
        row = (
            engine.connection()
            .execute("SELECT content_hash FROM files WHERE file_path = ? LIMIT 1", (path,))
            .fetchone()
        )
        if row is not None:
            return str(row["content_hash"] or "")
    return ""


def symbol_detail(engine: Any, symbol_id: str) -> dict[str, Any]:
    payload = engine.get_symbol(symbol_id=symbol_id, auto_index=False)
    source = str(payload.pop("source", ""))
    truncated = len(source) > _MAX_SOURCE_CHARS
    if truncated:
        source = source[:_MAX_SOURCE_CHARS] + "\n… source truncated"
    node = _payload_node(payload)
    return {**node, "signature": str(payload.get("signature") or ""), "source": source, "source_truncated": truncated}


def _parse_at(value: Any) -> datetime | None:
    if not value:
        return None
    with contextlib.suppress(TypeError, ValueError):
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _event_path(payload: dict[str, Any]) -> str | None:
    args_value = payload.get("args")
    args: dict[str, Any] = args_value if isinstance(args_value, dict) else {}
    for key in ("path", "file_path", "target_path"):
        value = payload.get(key) or args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("files", "edits"):
        values = args.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            candidate = value.get("path") if isinstance(value, dict) else value
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _event_path_line(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r":L?(\d+)(?:-L?\d+)?$", value)
    return int(match.group(1)) if match else None


def _strip_event_range(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r":(?:L?\d+(?:-L?\d+)?|full|outline|summary|head=\d+|tail=\d+)$", "", value)


def _relative_event_path(value: str | None, project_root: Path) -> str | None:
    if not value:
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        return candidate.as_posix()
    with contextlib.suppress(OSError, ValueError):
        return candidate.resolve().relative_to(project_root).as_posix()
    return None


def classify_activity_event(
    event: dict[str, Any],
    *,
    session_id: str,
    sequence: int,
    project_root: Path | None = None,
) -> dict[str, Any] | None:
    """Project a ledger event into the privacy-bounded map activity schema."""

    event_kind = str(event.get("kind") or "")
    payload_value = event.get("payload")
    payload: dict[str, Any] = payload_value if isinstance(payload_value, dict) else {}
    args_value = payload.get("args")
    args: dict[str, Any] = args_value if isinstance(args_value, dict) else {}
    tool = str(payload.get("tool") or "").lower()
    summary = str(event.get("summary") or "")
    activity_kind: str | None = None
    query: str | None = None
    raw_path = _event_path(payload)
    line = _event_path_line(raw_path)
    path = _strip_event_range(raw_path)
    status: str | None = None
    label = ""

    if event_kind == "file_edit" or "edit" in tool or "write" in tool:
        activity_kind = "edit"
        label = f"Edited {Path(path).name}" if path else "Edited code"
    elif event_kind == "test_result":
        activity_kind = "verify"
        passed = bool(payload.get("passed"))
        status = "passed" if passed else "failed"
        label = f"Verification {status}"
    elif event_kind == "command_result" or tool.endswith("bash") or tool.endswith("exec_command"):
        command = str(payload.get("command") or args.get("command") or summary)
        if _VERIFY_COMMAND_RE.search(command):
            activity_kind = "verify"
            ok = bool(payload.get("ok", payload.get("return_code") in (None, 0)))
            status = "passed" if ok else "failed"
            label = f"Verification {status}"
    elif any(marker in tool for marker in ("code_search", "explore", "callers", "callees", "usages")):
        activity_kind = "search"
        raw_query = args.get("query") or args.get("symbol_name") or args.get("symbol_id")
        query = str(raw_query).strip()[:160] if raw_query else None
        label = f"Searched {query}" if query else "Searched the code graph"
    elif tool.endswith("read") or event_kind == "file_read":
        activity_kind = "read"
        label = f"Read {Path(path).name}" if path else "Read exact source"

    if activity_kind is None:
        return None
    if project_root is not None:
        path = _relative_event_path(path, project_root)
    event_id = hashlib.sha1(f"{session_id}:{event.get('at')}:{sequence}:{activity_kind}".encode()).hexdigest()[:16]
    result: dict[str, Any] = {
        "id": event_id,
        "session_id": session_id,
        "kind": activity_kind,
        "at": str(event.get("at") or ""),
        "label": label,
    }
    if path:
        result["path"] = path
    if query:
        result["query"] = query
    if line is not None:
        result["line"] = line
    if status:
        result["status"] = status
    return result


def _session_relates_to_project(snapshot: dict[str, Any], project_root: Path) -> bool:
    root_text = str(project_root)
    workspace_path = str(snapshot.get("workspace_path") or "").strip()
    if workspace_path:
        with contextlib.suppress(OSError):
            if Path(workspace_path).expanduser().resolve() == project_root.resolve():
                return True
    for value in snapshot.get("files_touched") or []:
        if isinstance(value, str) and (value == root_text or value.startswith(root_text + os.sep)):
            return True
    encoded_root = root_text.replace(os.sep, "-")
    if any(encoded_root in str(value) for value in snapshot.get("raw_artifact_ids") or []):
        return True
    for event in reversed(snapshot.get("events") or []):
        if not isinstance(event, dict):
            continue
        payload_value = event.get("payload")
        payload: dict[str, Any] = payload_value if isinstance(payload_value, dict) else {}
        path = _event_path(payload)
        if path and (path == root_text or path.startswith(root_text + os.sep)):
            return True
    return False


def _attach_activity_targets(engine: Any, events: list[dict[str, Any]]) -> None:
    """Resolve small event hints to exact graph node IDs without exposing outputs."""

    for event in events:
        targets: list[str] = []
        path = str(event.get("path") or "")
        line = int(event.get("line") or 0)
        if path and line:
            with contextlib.suppress(OSError, ValueError, sqlite3.Error):
                rows = (
                    engine.connection()
                    .execute(
                        """
                    SELECT symbol_id
                    FROM symbols
                    WHERE file_path = ? AND start_line <= ? AND end_line >= ?
                    ORDER BY (end_line - start_line) ASC
                    LIMIT 3
                    """,
                        (path, line, line),
                    )
                    .fetchall()
                )
                targets.extend(str(row["symbol_id"]) for row in rows)
        if path:
            targets.append(_file_node_id(path))
        query = str(event.get("query") or "").strip()
        if query:
            with contextlib.suppress(OSError, LookupError, ValueError, sqlite3.Error):
                targets.extend(match["id"] for match in search_symbols(engine, query, limit=5))
        if targets:
            event["symbol_ids"] = list(dict.fromkeys(targets))[:6]


_RUN_SUMMARY_CACHE: dict[tuple[str, int, int, str], dict[str, Any] | None] = {}
_LIVE_WINDOW_S = 600


def _run_summary(path: Path, project_root: Path) -> dict[str, Any] | None:
    """Who a run ledger belongs to, if it relates to *project_root*.

    Ledgers reach several MB and the activity feed polls every few seconds, so
    the verdict is cached per file write; only a ledger that changed is parsed
    again.
    """

    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size, str(project_root))
    if key in _RUN_SUMMARY_CACHE:
        return _RUN_SUMMARY_CACHE[key]
    summary: dict[str, Any] | None = None
    try:
        candidate = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        candidate = None
    if isinstance(candidate, dict) and _session_relates_to_project(candidate, project_root):
        host = str(candidate.get("host") or path.parent.parent.name or candidate.get("agent") or "").strip().lower()
        session_id = str(candidate.get("session_id") or path.parent.name)
        summary = {
            "id": f"{host}:{session_id}" if host else session_id,
            "host": host,
            "session_id": session_id,
            "status": str(candidate.get("status") or "running"),
            "last_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            "live": False,
            "path": str(path),
        }
    if len(_RUN_SUMMARY_CACHE) > 400:
        _RUN_SUMMARY_CACHE.clear()
    _RUN_SUMMARY_CACHE[key] = summary
    return summary


def activity_sessions(runtime_root: Path, project_root: Path, *, limit: int = 8) -> list[dict[str, Any]]:
    """Recent agent sessions that relate to *project_root*, newest first."""

    run_files = sorted(
        (runtime_root / "sessions").glob("*/*/*/*/*/run.json"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    now = datetime.now(UTC)
    out: list[dict[str, Any]] = []
    for path in run_files[:40]:
        summary = _run_summary(path, project_root)
        if summary is None:
            continue
        row = dict(summary)
        last = _parse_at(row["last_at"])
        row["live"] = bool(last and (now - last).total_seconds() <= _LIVE_WINDOW_S and row["status"] != "completed")
        out.append(row)
        if len(out) >= limit:
            break
    return out


def recent_activity(
    runtime_root: Path,
    project_root: Path,
    *,
    after: str | None = None,
    limit: int = 60,
    engine: Any | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    """Return one relevant session's small map activity projection.

    *session* (``host:session_id``) picks the session to follow; otherwise the
    newest relevant one. ``sessions`` lists the recent relevant sessions.
    """

    sessions = activity_sessions(runtime_root, project_root)
    chosen = next((row for row in sessions if session and row["id"] == session), None) or (
        sessions[0] if sessions else None
    )
    snapshot: dict[str, Any] | None = None
    snapshot_host = ""
    if chosen is not None:
        with contextlib.suppress(OSError, json.JSONDecodeError):
            candidate = json.loads(Path(chosen["path"]).read_text(encoding="utf-8"))
            if isinstance(candidate, dict):
                snapshot = candidate
                snapshot_host = str(chosen["host"])
    public_sessions = [{key: value for key, value in row.items() if key != "path"} for row in sessions]
    if snapshot is None:
        return {
            "session_id": None,
            "host": None,
            "status": "idle",
            "events": [],
            "cursor": after,
            "sessions": public_sessions,
            "selected_session": None,
        }

    session_id = str(snapshot.get("session_id") or "")
    after_dt = _parse_at(after)
    events: list[dict[str, Any]] = []
    for sequence, raw in enumerate(snapshot.get("events") or []):
        if not isinstance(raw, dict):
            continue
        event_dt = _parse_at(raw.get("at"))
        if after_dt is not None and (event_dt is None or event_dt <= after_dt):
            continue
        projected = classify_activity_event(
            raw,
            session_id=session_id,
            sequence=sequence,
            project_root=project_root,
        )
        if projected is not None:
            events.append(projected)
    bounded_limit = max(1, min(limit, 200))
    events = events[-bounded_limit:]
    if engine is not None:
        _attach_activity_targets(engine, events)
    cursor = events[-1]["at"] if events else after
    return {
        "session_id": session_id,
        "host": snapshot_host or None,
        "status": str(snapshot.get("status") or "running"),
        "events": events,
        "cursor": cursor,
        "sessions": public_sessions,
        "selected_session": chosen["id"] if chosen is not None else None,
    }


__all__ = [
    "activity_sessions",
    "build_neighborhood",
    "classify_activity_event",
    "get_engine",
    "get_map_engine",
    "list_map_projects",
    "list_projects",
    "recent_activity",
    "resolve_project_root",
    "search_symbols",
    "search_text",
    "symbol_detail",
]

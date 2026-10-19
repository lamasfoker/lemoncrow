"""`/code` projections over the shared code-intelligence provider.

`/code` is the present tense: the working tree as it is now, read through the
current index. Everything here is bounded per request -- relationships for one
file or symbol, one module overview, one focus neighborhood -- so no
repository-scale graph ever crosses the API boundary.

Relationships come from the same provider Review uses (the native index link
layer, the legacy engine as fallback) and pass the same plausibility filter,
so the two surfaces cannot disagree about one relationship.
"""

from __future__ import annotations

import codecs
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import tomllib
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from lemoncrow.core.service.code_relations import file_category, implausible_relation

_MAX_RELATION_ROWS = 40
_MAX_FOCUS_SUBJECT_FILES = 40
_MAX_FOCUS_SIDE_FILES = 24
_MAX_FOCUS_SYMBOLS = 60
_MODULE_SPLIT_FILES = 150
_MODULE_MERGE_FILES = 3
_TINY_REPOSITORY_FILES = 20
_PACKAGE_MARKERS = ("package.json", "pyproject.toml", "Cargo.toml", "go.mod")
_PACKAGE_ROOT_MAX_DEPTH = 3
_MAX_MODULE_EDGES = 400
_COLLAPSED_FOLDERS = frozenset(
    {
        "node_modules",
        "vendor",
        "third_party",
        "dist",
        "build",
        "out",
        ".next",
        "target",
        "coverage",
        "generated",
        "__generated__",
        "__snapshots__",
    }
)
_KIND_PRIORITY = {"calls": 0, "imports": 1, "references": 2}
_KIND_NAME = {"call": "calls", "import": "imports", "reference": "references", "caller": "calls"}

# lc-debt: process-local TTL caches; move onto the index generation stamp once
# the provider exposes one for both backends.
_CACHE_LOCK = threading.Lock()
_MODULES_CACHE: dict[tuple[str, str], tuple[float, tuple[dict[str, Any], dict[str, str]]]] = {}
_CHANGES_CACHE: dict[str, tuple[float, str, dict[str, Any]]] = {}
_MODULES_TTL_S = 30.0
_CHANGES_TTL_S = 10.0


# --------------------------------------------------------------------------- #
# Provider readiness                                                          #
# --------------------------------------------------------------------------- #


def provider_context(provider: Any) -> dict[str, Any]:
    """Which index answered and how ready it is. Never Snapshot-exact."""

    try:
        readiness = provider.readiness().to_wire()
    except Exception:
        readiness = {"backend": "unavailable", "reason": "readiness_unavailable"}
    backend = str(readiness.get("backend") or "")
    context: dict[str, Any] = {
        "source": "current_index",
        "backend": backend,
        "exactness": "current_index",
        "readiness": readiness,
        "fallback": backend == "legacy",
    }
    basis = getattr(provider, "relationship_basis", "")
    if isinstance(basis, str) and basis:
        # Runtime Lite: every relationship is an import, never a precise call or reference.
        context["relationship_basis"] = basis
        context["degraded"] = True
        context["degraded_reason"] = str(readiness.get("reason") or "")
    return context


def _kind(value: object) -> str:
    text = str(value or "").strip().lower()
    return _KIND_NAME.get(text, text or "references")


# --------------------------------------------------------------------------- #
# Relationships for the inspector                                             #
# --------------------------------------------------------------------------- #


def _merge_row(
    rows: dict[str, dict[str, Any]], other: str, kind: str, weight: int, symbols: Iterable[Any], line: int
) -> None:
    row = rows.get(other)
    if row is None:
        row = {"path": other, "kind": kind, "kinds": {}, "weight": 0, "symbols": Counter(), "lines": []}
        rows[other] = row
    row["kinds"][kind] = int(row["kinds"].get(kind, 0)) + max(1, weight)
    row["weight"] += max(1, weight)
    if _KIND_PRIORITY.get(kind, 3) < _KIND_PRIORITY.get(row["kind"], 3):
        row["kind"] = kind
    for item in symbols:
        if isinstance(item, Mapping) and item.get("name"):
            row["symbols"][str(item["name"])] += int(item.get("count") or 1)
        elif isinstance(item, str) and item:
            row["symbols"][item] += 1
    if line > 0 and line not in row["lines"] and len(row["lines"]) < 8:
        row["lines"].append(line)


def _finish_rows(rows: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    ordered = sorted(
        rows.values(),
        key=lambda row: (_KIND_PRIORITY.get(row["kind"], 3), -int(row["weight"]), str(row["path"])),
    )
    out: list[dict[str, Any]] = []
    for row in ordered[:_MAX_RELATION_ROWS]:
        out.append(
            {
                "path": row["path"],
                "kind": row["kind"],
                "kinds": dict(row["kinds"]),
                "weight": int(row["weight"]),
                "symbols": [name for name, _count in row["symbols"].most_common(4)],
                "lines": sorted(row["lines"]),
            }
        )
    return out, len(ordered)


def file_relationships(provider: Any, path: str) -> dict[str, Any]:
    """Used by / Uses for one file, grouped by the other file."""

    clean = path.strip()
    raw = provider.file_relationships((clean,), limit=320)
    used_by: dict[str, dict[str, Any]] = {}
    uses: dict[str, dict[str, Any]] = {}
    filtered: Counter[str] = Counter()
    for edge in raw.get("edges") or ():
        if not isinstance(edge, Mapping):
            continue
        source = str(edge.get("source_path") or "")
        target = str(edge.get("target_path") or "")
        kind = _kind(edge.get("kind"))
        if not source or not target or source == target or clean not in {source, target}:
            continue
        reason = implausible_relation(source, target, kind)
        if reason:
            filtered[reason] += 1
            continue
        evidence_line = 0
        for item in edge.get("evidence") or ():
            if isinstance(item, Mapping) and int(item.get("line") or 0) > 0:
                evidence_line = int(item["line"])
                break
        weight = int(edge.get("weight") or 1)
        symbols = edge.get("symbols") or ()
        if target == clean:
            _merge_row(used_by, source, kind, weight, symbols, evidence_line)
        else:
            _merge_row(uses, target, kind, weight, symbols, 0)
    used_rows, used_total = _finish_rows(used_by)
    uses_rows, uses_total = _finish_rows(uses)
    return {
        "subject": {"kind": "file", "path": clean},
        "used_by": used_rows,
        "uses": uses_rows,
        "used_by_total": used_total,
        "uses_total": uses_total,
        "filtered": dict(filtered),
        "truncated": bool(raw.get("truncated")) or used_total > len(used_rows) or uses_total > len(uses_rows),
        "degraded_reason": str(raw.get("degraded_reason") or ""),
        "uses_attributed": True,
        "context": provider_context(provider),
    }


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _plain_digest(value: str) -> str:
    return value.split(":", 1)[1] if value.startswith("sha256:") else value


def symbol_span_is_current(provider: Any, engine: Any, project_root: Path, path: str) -> bool:
    """Whether the legacy index's line geometry for *path* describes the file the
    provider's links were built from.

    Calls out of a symbol are attributed by line. That is exact only when the
    index that measured the symbol's span and the index that recorded the call
    lines both hold the file exactly as it is on disk.
    """

    from lemoncrow.core.service import code_map

    disk = _sha256_file(project_root / path)
    if not disk or code_map.indexed_content_hash(engine, path) != disk:
        return False
    digest_fn = getattr(provider, "path_digest", None)
    if not callable(digest_fn):
        return False
    return _plain_digest(str(digest_fn(path) or "")) == disk


def symbol_relationships(
    provider: Any,
    engine: Any,
    project_root: Path,
    symbol: Mapping[str, Any],
) -> dict[str, Any]:
    """Callers and calls for one symbol, grouped by the other file."""

    path = str(symbol.get("path") or "")
    label = str(symbol.get("label") or "")
    line = int(symbol.get("line") or 0)
    end_line = int(symbol.get("end_line") or line)
    request = dict(symbol)
    native = callable(getattr(provider, "path_digest", None))
    if native and end_line > line > 0 and symbol_span_is_current(provider, engine, project_root, path):
        request["span_start"] = line
        request["span_end"] = end_line
    raw = provider.neighborhood(request, depth=1, limit=40)
    nodes = {str(node.get("id") or ""): node for node in raw.get("nodes") or () if isinstance(node, Mapping)}
    focus_id = str(symbol.get("id") or "")
    if focus_id not in nodes:
        focus_id = next(
            (node_id for node_id, node in nodes.items() if node.get("path") == path and node.get("label") == label),
            focus_id,
        )
    used_by: dict[str, dict[str, Any]] = {}
    uses: dict[str, dict[str, Any]] = {}
    filtered: Counter[str] = Counter()
    for edge in raw.get("edges") or ():
        if not isinstance(edge, Mapping):
            continue
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        kind = _kind(edge.get("kind"))
        weight = int(edge.get("weight") or 1)
        if target == focus_id and source in nodes:
            other = nodes[source]
            other_path = str(other.get("path") or "")
            reason = implausible_relation(other_path, path, kind)
            if reason:
                filtered[reason] += 1
                continue
            caller = str(other.get("label") or "")
            names = [caller] if caller and caller not in {label, other_path} else []
            _merge_row(used_by, other_path, kind, weight, names, int(other.get("line") or 0))
        elif source == focus_id and target in nodes:
            other = nodes[target]
            other_path = str(other.get("path") or "")
            reason = implausible_relation(path, other_path, kind)
            if reason:
                filtered[reason] += 1
                continue
            _merge_row(uses, other_path, kind, weight, [str(other.get("label") or "")], int(other.get("line") or 0))
    used_rows, used_total = _finish_rows(used_by)
    uses_rows, uses_total = _finish_rows(uses)
    attributed = bool(raw.get("outgoing_attributed")) if native else True
    return {
        "subject": {"kind": "symbol", "path": path, "symbol": label, "line": line, "end_line": end_line},
        "used_by": used_rows,
        "uses": uses_rows,
        "used_by_total": used_total,
        "uses_total": uses_total,
        "filtered": dict(filtered),
        "truncated": bool(raw.get("truncated")),
        "degraded_reason": str(raw.get("degraded_reason") or ""),
        "uses_attributed": attributed,
        "context": provider_context(provider),
    }


# --------------------------------------------------------------------------- #
# Modules                                                                     #
# --------------------------------------------------------------------------- #


def _parent(folder: str) -> str:
    parent = PurePosixPath(folder).parent.as_posix()
    return "" if parent == "." else parent


def _folder(path: str) -> str:
    return _parent(path)


def _ancestors(folder: str) -> list[str]:
    out: list[str] = []
    while folder:
        out.append(folder)
        folder = _parent(folder)
    return out


def _collapsed_root(path: str) -> str:
    """The outermost generated/vendor/build folder containing *path*, or ``""``."""

    parts = PurePosixPath(path).parts[:-1]
    for index, part in enumerate(parts):
        if part in _COLLAPSED_FOLDERS:
            return "/".join(parts[: index + 1])
    return ""


def package_roots(paths: Sequence[str]) -> set[str]:
    """Folders that declare a package, excluding the repository root.

    Only manifests within the first few folder levels count: deeper ones are
    almost always fixtures or sample projects (a benchmark task's own
    ``pyproject.toml``), not how the repository is organized.
    """

    roots: set[str] = set()
    for path in paths:
        parts = PurePosixPath(path).parts
        if (
            parts[-1] in _PACKAGE_MARKERS
            and 1 < len(parts) <= _PACKAGE_ROOT_MAX_DEPTH + 1
            and not _collapsed_root(path)
        ):
            roots.add(_folder(path))
    return roots


def module_partition(paths: Sequence[str]) -> dict[str, str]:
    """Map every file to its module: a folder at an adaptive depth.

    1. Declared package roots are always module boundaries.
    2. From the top-level folders down, a folder above ~150 files (or holding a
       nested package root) splits into its children; its own files stay in it.
    3. Modules below ~3 files merge into their parent folder.
    4. Generated, vendor and build-output folders collapse into one module each.

    A repository below ~20 files has no module level: every file is its own.
    """

    files = sorted({path for path in paths if path})
    if len(files) < _TINY_REPOSITORY_FILES:
        return {path: path for path in files}
    roots = package_roots(files)
    counts: Counter[str] = Counter()
    for path in files:
        for folder in _ancestors(_folder(path)):
            counts[folder] += 1
    nested_roots: set[str] = set()
    for root in roots:
        nested_roots.update(_ancestors(_parent(root)))

    def module_for(path: str) -> str:
        collapsed = _collapsed_root(path)
        if collapsed:
            return collapsed
        folder = _folder(path)
        if not folder:
            return ""
        chain = list(reversed(_ancestors(folder)))  # outermost first
        for index, candidate in enumerate(chain):
            deeper = chain[index + 1] if index + 1 < len(chain) else None
            must_split = counts[candidate] > _MODULE_SPLIT_FILES or candidate in nested_roots
            if deeper is None or not must_split:
                return candidate
        return folder

    assignment = {path: module_for(path) for path in files}
    sizes = Counter(assignment.values())
    # Merge tiny modules one level up, deepest first. A parent that absorbed a
    # child is a real module now and does not cascade further toward the root.
    # A package root or collapsed folder stays whole.
    absorbing: set[str] = set()
    parent_of: dict[str, str] = {}
    for module in sorted(sizes, key=lambda item: (-item.count("/"), item)):
        if not module or sizes[module] >= _MODULE_MERGE_FILES or module in roots or module in absorbing:
            continue
        if PurePosixPath(module).name in _COLLAPSED_FOLDERS:
            continue
        parent = _parent(module)
        parent_of[module] = parent
        sizes[parent] += sizes.pop(module)
        absorbing.add(parent)
    if parent_of:

        def merged(module: str) -> str:
            while module in parent_of:
                module = parent_of[module]
            return module

        assignment = {path: merged(module) for path, module in assignment.items()}
    return assignment


_README_NAMES = ("README.md", "README.rst", "README.txt", "README")
_SENTENCE = re.compile(r"^(.{12,220}?[.!?])(?:\s|$)")


def _first_sentence(text: str) -> str:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "<", "!", "[", "|", "```", "---", "===", "..", "-", "*")):
            continue
        match = _SENTENCE.match(line)
        return (match.group(1) if match else line)[:220]
    return ""


def _declared_purpose(project_root: Path, module: str) -> tuple[str, str]:
    """The module's own one-line statement and where it came from.

    Only self-declared text: README first sentence, package manifest
    description, or package docstring. Nothing is generated.
    """

    folder = project_root / module if module else project_root
    with contextlib.suppress(OSError, ValueError):
        for name in _README_NAMES:
            candidate = folder / name
            if candidate.is_file():
                sentence = _first_sentence(candidate.read_text(encoding="utf-8", errors="replace")[:8000])
                if sentence:
                    return sentence, f"{module + '/' if module else ''}{name}"
        package_json = folder / "package.json"
        if package_json.is_file():
            data = json.loads(package_json.read_text(encoding="utf-8"))
            description = str(data.get("description") or "").strip() if isinstance(data, dict) else ""
            if description:
                return description[:220], f"{module + '/' if module else ''}package.json"
        for manifest, table in (("pyproject.toml", "project"), ("Cargo.toml", "package")):
            candidate = folder / manifest
            if candidate.is_file():
                data = tomllib.loads(candidate.read_text(encoding="utf-8"))
                section = data.get(table) if isinstance(data, dict) else None
                description = str(section.get("description") or "").strip() if isinstance(section, dict) else ""
                if description:
                    return description[:220], f"{module + '/' if module else ''}{manifest}"
        init = folder / "__init__.py"
        if init.is_file():
            text = init.read_text(encoding="utf-8", errors="replace")[:4000]
            match = re.match(r'\s*(?:#[^\n]*\n\s*)*(?:"""|\'\'\')(.*?)(?:"""|\'\'\')', text, re.DOTALL)
            sentence = _first_sentence(match.group(1)) if match else ""
            if sentence:
                return sentence, f"{module + '/' if module else ''}__init__.py"
    return "", ""


_PY_DOCSTRING = re.compile(r'\s*(?:#[^\n]*\n\s*)*[rRuU]?(?:"""|\'\'\')(.*?)(?:"""|\'\'\')', re.DOTALL)
_HEADER_COMMENT = re.compile(r"(?:#![^\n]*\n)?\s*/\*\*?(.*?)\*/", re.DOTALL)
_GO_PACKAGE_COMMENT = re.compile(r"((?:^//[^\n]*\n)+)package\s", re.MULTILINE)
_JS_SUFFIXES = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"})
_STATEMENT_NOISE = re.compile(r"copyright|licen[cs]e|spdx|eslint|prettier|@ts-|auto-?generated|do not edit", re.I)


def _file_statement(project_root: Path, path: str) -> str:
    """A file's own leading statement, first sentence only.

    The module docstring (Python), a header doc comment (JavaScript,
    TypeScript), ``//!`` crate docs (Rust), or the package comment (Go).
    License and tooling headers are not statements.
    """

    suffix = PurePosixPath(path).suffix.lower()
    try:
        text = (project_root / path).read_text(encoding="utf-8", errors="replace")[:4000]
    except (OSError, ValueError):
        return ""
    body = ""
    if suffix in {".py", ".pyi"}:
        match = _PY_DOCSTRING.match(text)
        body = match.group(1) if match else ""
    elif suffix in _JS_SUFFIXES:
        match = _HEADER_COMMENT.match(text)
        if match:
            lines = (re.sub(r"^\s*\*\s?", "", line) for line in match.group(1).splitlines())
            lines = (re.sub(r"^@(?:file|fileoverview|overview|module)\b\s*", "", line) for line in lines)
            body = "\n".join(line for line in lines if not line.startswith("@"))
    elif suffix == ".rs":
        docs: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("//!"):
                docs.append(stripped[3:].strip())
            elif docs or stripped:
                break
        body = "\n".join(docs)
    elif suffix == ".go":
        match = _GO_PACKAGE_COMMENT.search(text)
        body = "\n".join(line[2:].strip() for line in match.group(1).splitlines()) if match else ""
    sentence = _first_sentence(body)
    return "" if not sentence or _STATEMENT_NOISE.search(sentence) else sentence


def _symbol_counts(engine: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    with contextlib.suppress(sqlite3.Error, OSError, AttributeError):
        for row in engine.connection().execute("SELECT file_path, COUNT(*) AS n FROM symbols GROUP BY file_path"):
            counts[str(row["file_path"] or "")] = int(row["n"] or 0)
    return counts


def modules_overview(
    provider: Any,
    engine: Any,
    project_root: Path,
    paths: Sequence[str],
    changed: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Modules with counts and aggregated module-to-module dependencies."""

    changed_rows = changed or {}
    stamp = hashlib.sha1("\0".join(sorted(paths)).encode("utf-8", errors="surrogateescape")).hexdigest()
    key = (str(project_root), stamp)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _MODULES_CACHE.get(key)
    if cached is not None and now - cached[0] < _MODULES_TTL_S:
        base, assignment = cached[1]
    else:
        base, assignment = _modules_base(provider, engine, project_root, paths)
        with _CACHE_LOCK:
            _MODULES_CACHE.clear()
            _MODULES_CACHE[key] = (now, (base, assignment))
    counts = Counter(assignment[path] for path in changed_rows if path in assignment)
    generated = _generated_overlay(project_root, assignment) if base["level"] == "module" else {}
    modules = []
    for module in base["modules"]:
        row = {**module, "changed": counts.get(module["id"], 0)}
        overlay = generated.get(module["id"])
        if overlay is not None and row["purpose_kind"] != "declared":
            row.update(overlay)
        modules.append(row)
    return {**base, "modules": modules, "purpose_generation": _purpose_generation(base["level"])}


def _modules_base(
    provider: Any,
    engine: Any,
    project_root: Path,
    paths: Sequence[str],
) -> tuple[dict[str, Any], dict[str, str]]:
    assignment = module_partition(paths)
    symbol_counts = _symbol_counts(engine)
    members: dict[str, list[str]] = {}
    for path, module in assignment.items():
        members.setdefault(module, []).append(path)
    tiny = len(assignment) < _TINY_REPOSITORY_FILES

    raw = provider.repository_file_links() if callable(getattr(provider, "repository_file_links", None)) else {}
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    filtered: Counter[str] = Counter()
    degree: Counter[str] = Counter()
    for row in raw.get("edges") or ():
        if not isinstance(row, Mapping):
            continue
        source_path = str(row.get("source_path") or "")
        target_path = str(row.get("target_path") or "")
        source = assignment.get(source_path)
        target = assignment.get(target_path)
        if source is None or target is None or source == target:
            continue
        kind = _kind(row.get("kind"))
        reason = implausible_relation(source_path, target_path, kind)
        if reason:
            filtered[reason] += 1
            continue
        weight = int(row.get("weight") or 1)
        edge = edges.get((source, target))
        if edge is None:
            edge = {"source": source, "target": target, "weight": 0, "kinds": Counter(), "examples": []}
            edges[(source, target)] = edge
        edge["weight"] += weight
        edge["kinds"][kind] += weight
        names = [str(item.get("name")) for item in row.get("symbols") or () if isinstance(item, Mapping)]
        if not edge["examples"]:
            edge["examples"].append(
                {
                    "source_path": source_path,
                    "target_path": target_path,
                    "kind": kind,
                    "symbol": names[0] if names else "",
                }
            )
        degree[source_path] += weight
        degree[target_path] += weight

    declared_roots = package_roots(paths)
    modules: list[dict[str, Any]] = []
    for module in sorted(members):
        files = sorted(members[module])
        top_files = sorted(files, key=lambda path: (-degree[path], path))[:3]
        collapsed = bool(module) and PurePosixPath(module).name in _COLLAPSED_FOLDERS
        purpose, purpose_source = ("", "") if tiny else _declared_purpose(project_root, module)
        purpose_kind = "declared" if purpose else ""
        if not purpose and not tiny and not collapsed:
            # The module states nothing itself; its most connected file may.
            for path in top_files:
                statement = _file_statement(project_root, path)
                if statement:
                    purpose, purpose_source, purpose_kind = statement, path, "file"
                    break
        modules.append(
            {
                "id": module,
                "path": module,
                "label": PurePosixPath(module).name if module else "(repository root)",
                "parent": _parent(module) if module else "",
                "kind": "file" if tiny else "module",
                "files": len(files),
                "symbols": sum(symbol_counts.get(path, 0) for path in files),
                "changed": 0,
                "purpose": purpose,
                "purpose_source": purpose_source,
                "purpose_kind": purpose_kind,
                "collapsed": collapsed,
                "package": module in declared_roots,
                "top_files": top_files,
            }
        )
    payload = {
        "project": {"root": str(project_root), "label": project_root.name},
        "level": "file" if tiny else "module",
        "total_files": len(assignment),
        "modules": modules,
        # The strongest module links; weak ones would only add noise to layout.
        "edges": [
            {
                "source": edge["source"],
                "target": edge["target"],
                "weight": int(edge["weight"]),
                "kinds": dict(edge["kinds"]),
                "examples": edge["examples"],
            }
            for edge in sorted(edges.values(), key=lambda item: (-int(item["weight"]), item["source"], item["target"]))[
                :_MAX_MODULE_EDGES
            ]
        ],
        "edges_total": len(edges),
        "links_truncated": bool(raw.get("truncated")),
        "filtered": dict(filtered),
        "degraded_reason": str(raw.get("degraded_reason") or ""),
        "context": provider_context(provider),
    }
    return payload, assignment


# --------------------------------------------------------------------------- #
# Generated module purposes                                                   #
# --------------------------------------------------------------------------- #


class PurposeUnavailable(RuntimeError):
    """No internal model is configured, or the configured one failed."""


_PURPOSE_EVIDENCE_FILES = 12
_PURPOSE_NAMES_PER_FILE = 8
_MAX_GENERATED_PURPOSES = 256
# lc-debt: generated purposes live in process memory, so a restart asks the
# model again; persist them per index generation if that cost starts to matter.
_GENERATED_PURPOSES: dict[tuple[str, str], dict[str, Any]] = {}
_PURPOSE_PROMPT = (
    "You state what one code module is for, for a reader skimming a map of the repository. "
    "Use only the evidence given: file paths, the files' own statements, and the names they define. "
    "Reply with one plain sentence of at most 25 words, with no preamble, quotes, or markdown. "
    "If the evidence does not show a purpose, reply exactly UNKNOWN."
)


def _members_stamp(paths: Iterable[str]) -> str:
    return hashlib.sha1("\0".join(sorted(paths)).encode("utf-8", errors="surrogateescape")).hexdigest()


def _purpose_generation(level: str) -> dict[str, Any]:
    configured: tuple[str, str] | None = None
    if level == "module":
        with contextlib.suppress(Exception):
            from lemoncrow.infra.internal_llm import configured_model

            configured = configured_model()
    return {"available": configured is not None, "model": configured[1] if configured else ""}


def _generated_overlay(project_root: Path, assignment: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """Generated purposes still describing the module's current file set."""

    root = str(project_root)
    with _CACHE_LOCK:
        entries = {module: dict(entry) for (owner, module), entry in _GENERATED_PURPOSES.items() if owner == root}
    if not entries:
        return {}
    members: dict[str, list[str]] = {}
    for path, module in assignment.items():
        if module in entries:
            members.setdefault(module, []).append(path)
    out: dict[str, dict[str, Any]] = {}
    for module, entry in entries.items():
        if entry.pop("members_stamp") == _members_stamp(members.get(module, ())):
            entry.pop("module", None)
            out[module] = entry
    return out


def _defined_names(engine: Any, path: str) -> list[str]:
    """The file's largest symbols by span: usually what it exists to define."""

    with contextlib.suppress(sqlite3.Error, OSError, AttributeError, LookupError, TypeError):
        rows = (
            engine.connection()
            .execute(
                "SELECT symbol_name FROM symbols WHERE file_path = ? "
                "ORDER BY (end_line - start_line) DESC, start_line LIMIT ?",
                (path, _PURPOSE_NAMES_PER_FILE * 2),
            )
            .fetchall()
        )
        names = [str(row["symbol_name"]) for row in rows if row["symbol_name"]]
        return list(dict.fromkeys(names))[:_PURPOSE_NAMES_PER_FILE]
    return []


def _purpose_evidence(engine: Any, project_root: Path, module: str, members: Sequence[str]) -> tuple[list[str], str]:
    counts = _symbol_counts(engine)
    chosen = sorted(members, key=lambda path: (-counts.get(path, 0), path))[:_PURPOSE_EVIDENCE_FILES]
    lines = [f"Module {module or '(repository root)'}: {len(members)} files."]
    for path in chosen:
        parts = [f"- {path}"]
        statement = _file_statement(project_root, path)
        if statement:
            parts.append(f'states: "{statement}"')
        names = _defined_names(engine, path)
        if names:
            parts.append("defines " + ", ".join(names))
        lines.append("; ".join(parts))
    rest = [PurePosixPath(path).name for path in members if path not in chosen]
    if rest:
        lines.append(f"- {len(rest)} more files: {', '.join(rest)}"[:600])
    return chosen, "\n".join(lines)


def _clean_generated(text: str) -> str:
    line = next((raw.strip() for raw in text.splitlines() if raw.strip()), "")
    line = re.sub(r"^(?:purpose|module purpose)\s*:\s*", "", line, flags=re.I)
    line = line.strip("\"'`* ").strip()
    return "" if line.upper().rstrip(".") == "UNKNOWN" else line[:220]


def generate_module_purpose(
    engine: Any,
    project_root: Path,
    module: str,
    paths: Sequence[str],
    *,
    chat: Callable[[list[dict[str, Any]]], Any] | None = None,
    configured: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """One generated purpose line for *module*, labelled with what made it.

    Asked for by the reader, never generated in the background. The model sees
    only paths, the files' own statements, and defined names; the result
    carries the model, the time, and the files it was shown, and is dropped
    from the overview once the module's file set changes.
    """

    assignment = module_partition(paths)
    if len(assignment) < _TINY_REPOSITORY_FILES:
        raise LookupError("This repository is small enough to show files directly; it has no modules.")
    members = sorted(path for path, owner in assignment.items() if owner == module)
    if not members:
        raise LookupError(f"No module {module or '(repository root)'} in this project.")
    if configured is None:
        from lemoncrow.infra.internal_llm import configured_model

        configured = configured_model()
    if configured is None:
        raise PurposeUnavailable("No internal model is configured. Set LEMONCROW_LLM_BACKEND to describe modules.")
    if chat is None:
        from lemoncrow.infra.internal_llm import chat as internal_chat

        chat = internal_chat
    chosen, evidence = _purpose_evidence(engine, project_root, module, members)
    try:
        raw = chat([{"role": "system", "content": _PURPOSE_PROMPT}, {"role": "user", "content": evidence}])
    except Exception as exc:
        raise PurposeUnavailable(f"The model could not be reached: {exc}") from exc
    purpose = _clean_generated(raw if isinstance(raw, str) else str((raw or {}).get("purpose") or ""))
    if not purpose:
        raise ValueError("The model could not state a purpose from this module's files.")
    entry = {
        "module": module,
        "purpose": purpose,
        "purpose_kind": "generated",
        "purpose_source": configured[1],
        "purpose_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "purpose_evidence": chosen,
    }
    with _CACHE_LOCK:
        _GENERATED_PURPOSES.pop((str(project_root), module), None)
        _GENERATED_PURPOSES[(str(project_root), module)] = {**entry, "members_stamp": _members_stamp(members)}
        while len(_GENERATED_PURPOSES) > _MAX_GENERATED_PURPOSES:
            _GENERATED_PURPOSES.pop(next(iter(_GENERATED_PURPOSES)))
    return entry


# --------------------------------------------------------------------------- #
# Focus neighborhoods (MapPayload for the lane engine)                         #
# --------------------------------------------------------------------------- #


def _file_node_id(path: str) -> str:
    return "file:" + path


def _map_file_node(path: str, *, lane: str, changed: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    change = changed.get(path)
    status = "context"
    if lane == "":
        status = "subject"
    if change:
        status = "new" if change.get("status") == "A" else "changed"
    return {
        "id": _file_node_id(path),
        "label": PurePosixPath(path).name,
        "kind": "file",
        "module": _folder(path),
        "lane": lane,
        "path": path,
        "line": 0,
        "end_line": 0,
        "status": status,
        "change": (
            {"A": "added", "D": "deleted", "R": "renamed"}.get(str(change.get("status")), "modified") if change else ""
        ),
        "additions": int(change.get("additions") or 0) if change else 0,
        "deletions": int(change.get("deletions") or 0) if change else 0,
        "category": file_category(path),
        "exactness": "current_index",
        "target_ids": [],
        "boundaries": [],
        "important_public_contract": False,
    }


def _map_edge(source_path: str, target_path: str, row: Mapping[str, Any], kind: str) -> dict[str, Any]:
    evidence = [
        {
            "kind": kind,
            "path": str(item.get("path") or source_path),
            "line": int(item.get("line") or 0),
            "uncertainty": "",
            "symbol": str(item.get("symbol") or ""),
        }
        for item in row.get("evidence") or ()
        if isinstance(item, Mapping)
    ][:4]
    return {
        "id": f"{kind}:{source_path}->{target_path}",
        "source": _file_node_id(source_path),
        "target": _file_node_id(target_path),
        "kind": kind,
        "exactness": "current_index",
        "weight": int(row.get("weight") or 1),
        "uncertain": False,
        "evidence": evidence,
        "symbols": [dict(item) for item in row.get("symbols") or () if isinstance(item, Mapping)][:12],
    }


def _file_graph(
    provider: Any,
    subject_paths: Sequence[str],
    changed: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter[str], bool]:
    seeds = set(subject_paths)
    raw = provider.file_relationships(tuple(subject_paths), limit=320)
    edges: list[dict[str, Any]] = []
    filtered: Counter[str] = Counter()
    side_weight: dict[str, dict[str, int]] = {}
    for row in raw.get("edges") or ():
        if not isinstance(row, Mapping):
            continue
        source = str(row.get("source_path") or "")
        target = str(row.get("target_path") or "")
        kind = _kind(row.get("kind"))
        if not source or not target or source == target or not (source in seeds or target in seeds):
            continue
        reason = implausible_relation(source, target, kind)
        if reason:
            filtered[reason] += 1
            continue
        edges.append(_map_edge(source, target, row, kind))
        weight = int(row.get("weight") or 1) * (3 if kind == "calls" else 2 if kind == "imports" else 1)
        if source in seeds and target not in seeds:
            side_weight.setdefault(target, {"used_by": 0, "uses": 0})["uses"] += weight
        elif target in seeds and source not in seeds:
            side_weight.setdefault(source, {"used_by": 0, "uses": 0})["used_by"] += weight
    lanes = {
        path: ("uses" if weights["uses"] > weights["used_by"] else "used_by") for path, weights in side_weight.items()
    }
    kept: set[str] = set(seeds)
    for lane in ("used_by", "uses"):
        ranked = sorted(
            (path for path, value in lanes.items() if value == lane),
            key=lambda path: (-sum(side_weight[path].values()), path),
        )
        kept.update(ranked[:_MAX_FOCUS_SIDE_FILES])
    nodes = [_map_file_node(path, lane="", changed=changed) for path in subject_paths]
    nodes.extend(_map_file_node(path, lane=lanes[path], changed=changed) for path in sorted(kept - seeds))
    kept_ids = {_file_node_id(path) for path in kept}
    edges = [edge for edge in edges if edge["source"] in kept_ids and edge["target"] in kept_ids]
    truncated = bool(raw.get("truncated")) or len(kept) - len(seeds) < len(lanes)
    return nodes, edges, filtered, truncated


def _payload(
    level: str, focus: str | None, nodes: list[dict[str, Any]], edges: list[dict[str, Any]], **extra: Any
) -> dict[str, Any]:
    stats = extra.pop("stats", {})
    return {
        "level": level,
        "graph": {
            "level": level,
            "focus": focus,
            "nodes": nodes,
            "edges": edges,
            "truncated": bool(extra.pop("truncated", False)),
            "omitted_node_count": int(extra.pop("omitted", 0)),
            "stats": stats,
            "reason": str(extra.pop("reason", "")),
        },
        **extra,
    }


def module_focus(
    provider: Any,
    project_root: Path,
    module: str,
    paths: Sequence[str],
    changed: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    assignment = module_partition(paths)
    member_paths = sorted(path for path, owner in assignment.items() if owner == module)
    if not member_paths:
        raise LookupError(f"unknown module: {module or '(repository root)'}")
    # Rank members by how connected they are so a large module opens on the
    # files that explain it; uncommitted files always make the cut.
    raw_links = provider.repository_file_links() if callable(getattr(provider, "repository_file_links", None)) else {}
    degree: Counter[str] = Counter()
    for row in raw_links.get("edges") or ():
        if isinstance(row, Mapping):
            degree[str(row.get("source_path") or "")] += int(row.get("weight") or 1)
            degree[str(row.get("target_path") or "")] += int(row.get("weight") or 1)
    ranked = sorted(member_paths, key=lambda path: (path not in changed, -degree[path], path))
    subject = ranked[:_MAX_FOCUS_SUBJECT_FILES]
    nodes, edges, filtered, truncated = _file_graph(provider, subject, changed)
    return _payload(
        "architecture",
        None,
        nodes,
        edges,
        truncated=truncated or len(member_paths) > len(subject),
        omitted=max(0, len(member_paths) - len(subject)),
        stats={
            "subject_files": len(member_paths),
            "shown_subject": len(subject),
            "filtered_relationships": sum(filtered.values()),
            "filtered_reasons": dict(filtered),
        },
        subject={
            "kind": "module",
            "id": module,
            "path": module,
            "label": PurePosixPath(module).name or "(repository root)",
        },
        context=provider_context(provider),
    )


def file_focus(
    provider: Any,
    engine: Any,
    project_root: Path,
    path: str,
    changed: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    from lemoncrow.core.service import code_map

    nodes, edges, filtered, truncated = _file_graph(provider, [path], changed)
    listing = code_map.list_file_symbols(engine, path, limit=_MAX_FOCUS_SYMBOLS)
    focus = nodes[0]
    focus["symbols"] = [
        {
            "id": str(symbol["id"]),
            "label": str(symbol["label"]),
            "qualified_name": str(symbol["qualified_name"]),
            "kind": str(symbol["kind"]),
            "path": path,
            "line": int(symbol["line"]),
            "end_line": int(symbol["end_line"]),
            "change": "",
            "caller_count": -1,
            "usage_count": -1,
            "centrality_rank": None,
            "centrality_percentile": None,
        }
        for symbol in listing.get("symbols") or ()
    ]
    focus["symbol_count"] = int(listing.get("total_symbols") or len(focus["symbols"]))
    focus["symbols_truncated"] = bool(listing.get("truncated"))
    return _payload(
        "file",
        focus["id"],
        nodes,
        edges,
        truncated=truncated,
        stats={"filtered_relationships": sum(filtered.values()), "filtered_reasons": dict(filtered)},
        subject={"kind": "file", "id": path, "path": path, "label": PurePosixPath(path).name},
        context=provider_context(provider),
    )


def symbol_focus(
    provider: Any,
    engine: Any,
    project_root: Path,
    symbol: Mapping[str, Any],
    changed: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    relations = symbol_relationships(provider, engine, project_root, symbol)
    path = str(symbol.get("path") or "")
    label = str(symbol.get("label") or "")
    focus_id = "symbol:" + str(symbol.get("id") or f"{path}:{symbol.get('line')}:{label}")
    focus_node = {
        "id": focus_id,
        "label": label,
        "qualified_name": str(symbol.get("qualified_name") or label),
        "kind": str(symbol.get("kind") or "symbol"),
        "module": _folder(path),
        "lane": "",
        "path": path,
        "line": int(symbol.get("line") or 0),
        "end_line": int(symbol.get("end_line") or 0),
        "status": "changed" if path in changed else "subject",
        "change": "modified" if path in changed else "",
        "exactness": "current_index",
        "target_ids": [],
        "boundaries": [],
        "important_public_contract": False,
    }
    nodes = [focus_node]
    edges: list[dict[str, Any]] = []
    for lane, rows in (("used_by", relations["used_by"]), ("uses", relations["uses"])):
        for row in rows[:_MAX_FOCUS_SIDE_FILES]:
            other_path = str(row["path"])
            names = row["symbols"] or []
            node_label = names[0] if lane == "uses" and names else PurePosixPath(other_path).name
            node_id = f"{lane}:{other_path}"
            nodes.append(
                {
                    **_map_file_node(other_path, lane=lane, changed=changed),
                    "id": node_id,
                    "label": node_label,
                    "kind": "symbol" if lane == "uses" and names else "file",
                    "line": int(row["lines"][0]) if row["lines"] else 0,
                }
            )
            source, target = (node_id, focus_id) if lane == "used_by" else (focus_id, node_id)
            edges.append(
                {
                    "id": f"{row['kind']}:{source}->{target}",
                    "source": source,
                    "target": target,
                    "kind": row["kind"],
                    "exactness": "current_index",
                    "weight": int(row["weight"]),
                    "uncertain": False,
                    "evidence": [
                        {"kind": row["kind"], "path": other_path, "line": line, "uncertainty": ""}
                        for line in row["lines"][:4]
                    ],
                    "symbols": [{"name": name, "count": 1} for name in names],
                }
            )
    return _payload(
        "symbol",
        focus_id,
        nodes,
        edges,
        truncated=bool(relations["truncated"]),
        stats={
            "filtered_relationships": sum(relations["filtered"].values()),
            "filtered_reasons": relations["filtered"],
            "outgoing_attributed": bool(relations["uses_attributed"]),
        },
        subject={"kind": "symbol", "id": str(symbol.get("id") or ""), "path": path, "label": label},
        context=provider_context(provider),
    )


# --------------------------------------------------------------------------- #
# Uncommitted changes, attributed to sessions                                  #
# --------------------------------------------------------------------------- #


def _git(project_root: Path, *args: str, timeout: float = 8.0) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), *args],
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="surrogateescape")


def uncommitted_files(project_root: Path) -> dict[str, dict[str, Any]]:
    """Every uncommitted path with its status letter and line counts."""

    status = _git(project_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if status is None:
        return {}
    rows: dict[str, dict[str, Any]] = {}
    items = status.split(b"\0")
    index = 0
    while index < len(items):
        item = items[index]
        index += 1
        if len(item) < 4:
            continue
        code = _decode(item[:2])
        path = _decode(item[3:])
        old_path = ""
        if "R" in code or "C" in code:
            old_path = _decode(items[index]) if index < len(items) else ""
            index += 1
        if code == "??" or "A" in code:
            letter = "A"
        elif "D" in code:
            letter = "D"
        elif "R" in code:
            letter = "R"
        else:
            letter = "M"
        rows[path] = {"path": path, "status": letter, "old_path": old_path, "additions": 0, "deletions": 0}
    # Renames stay renames: a moved file counts only the lines it changed.
    numstat = _git(project_root, "diff", "HEAD", "--numstat", "-z", "-M")
    if numstat is not None:
        records = numstat.split(b"\0")
        position = 0
        while position < len(records):
            parts = records[position].split(b"\t", 2)
            position += 1
            if len(parts) != 3:
                continue
            if parts[2]:
                path = _decode(parts[2])
            else:
                # A rename: its old and new paths follow as their own records.
                path = _decode(records[position + 1]) if position + 1 < len(records) else ""
                position += 2
            row = rows.get(path)
            if row is None:
                continue
            with contextlib.suppress(ValueError):
                row["additions"] = int(parts[0])
                row["deletions"] = int(parts[1])
    for row in rows.values():
        if row["status"] == "A" and not row["additions"]:
            candidate = project_root / row["path"]
            with contextlib.suppress(OSError):
                if candidate.is_file() and candidate.stat().st_size <= 1_000_000:
                    data = candidate.read_bytes()
                    # A last line without a newline is still a line.
                    row["additions"] = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    return rows


_HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _patch_path(raw: bytes) -> str:
    """The new-side path a ``+++`` patch header names, unquoted; "" for /dev/null."""

    if raw.endswith(b"\t"):
        raw = raw[:-1]  # git ends the header of a path holding a space with a tab
    if raw.startswith(b'"') and raw.endswith(b'"'):
        raw = codecs.escape_decode(raw[1:-1])[0]
    return _decode(raw[2:]) if raw.startswith(b"b/") else ""


def changed_line_ranges(project_root: Path, path: str) -> dict[str, Any]:
    """Uncommitted line ranges of one file, for the Source gutter.

    ``added`` and ``modified`` are new-side line ranges; ``deleted`` lists the
    new-side line a removal sits before. An untracked file is added whole.
    """

    rows = uncommitted_files(project_root)
    change = rows.get(path)
    empty: dict[str, Any] = {"path": path, "status": "", "added": [], "modified": [], "deleted": []}
    if change is None:
        return empty
    if change["status"] == "A":
        count = int(change.get("additions") or 0)
        return {**empty, "status": "A", "added": [[1, max(1, count)]]}
    if change["status"] == "D":
        return {**empty, "status": "D"}
    paths = [path]
    if change["status"] == "R" and change.get("old_path"):
        # Diffed alone, the new path of a rename is a new file: every line added.
        paths.append(str(change["old_path"]))
    diff = _git(
        project_root,
        "diff",
        "HEAD",
        "-M",
        "-U0",
        "--no-color",
        "--no-ext-diff",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "--",
        *paths,
    )
    added: list[list[int]] = []
    modified: list[list[int]] = []
    deleted: list[int] = []
    current, in_header = "", False
    for line in (diff or b"").splitlines():
        # Only a file section's header names its path; hunk lines never start
        # with "diff --git" or "@@", so neither can be mistaken for content.
        if line.startswith(b"diff --git "):
            current, in_header = "", True
            continue
        if in_header and line.startswith(b"+++ "):
            current = _patch_path(line[4:])
            continue
        if not line.startswith(b"@@"):
            continue
        in_header = False
        match = _HUNK.match(line)
        if not match or current != path:
            continue
        old_count = int(match.group(2)) if match.group(2) is not None else 1
        new_start = int(match.group(3))
        new_count = int(match.group(4)) if match.group(4) is not None else 1
        if new_count == 0:
            deleted.append(max(1, new_start + 1))
        elif old_count == 0:
            added.append([new_start, new_start + new_count - 1])
        else:
            modified.append([new_start, new_start + new_count - 1])
    return {**empty, "status": str(change["status"]), "added": added, "modified": modified, "deleted": deleted}


def _session_titles(store_root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    from datetime import UTC, datetime, timedelta

    out: dict[tuple[str, str], dict[str, Any]] = {}
    with contextlib.suppress(Exception):
        from lemoncrow.core.foundation.history_store import HistoryStore

        store = HistoryStore(store_root)
        if not store.db_path.exists():
            return out
        for trace in store.list_traces(since=datetime.now(UTC) - timedelta(days=7), limit=200):
            session_id = (trace.session_id or "").strip()
            host = (trace.host or trace.agent or "").strip().lower()
            if not session_id:
                continue
            title = (trace.session_title or trace.task or "").strip().splitlines()
            out.setdefault(
                (host, session_id),
                {
                    "title": title[0][:140] if title else "",
                    "started_at": trace.created_at.isoformat() if trace.created_at else "",
                    "model": trace.model or "",
                },
            )
    return out


def changes_by_session(
    store_root: Path,
    project_root: Path,
    *,
    attribute: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Uncommitted files grouped by the agent session that edited them.

    Authorship needs an exact edit record for the path (the provenance rule:
    workspace path and timing are presence, not authorship). A file edited by
    several sessions appears under each, marked shared. Files with no exact
    edit record are unattributed; nothing is guessed.
    """

    files = uncommitted_files(project_root)
    fingerprint = hashlib.sha1(
        json.dumps(
            sorted((path, row["status"], row["additions"], row["deletions"]) for path, row in files.items())
        ).encode()
    ).hexdigest()
    cache_key = str(project_root)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CHANGES_CACHE.get(cache_key)
    if cached is not None and cached[1] == fingerprint and now - cached[0] < _CHANGES_TTL_S:
        return cached[2]

    if attribute is None:
        from lemoncrow.pro.capabilities.review.provenance import collect_exact_file_attributions

        attribute = collect_exact_file_attributions
    try:
        attributions = tuple(attribute(store_root, project_root, sorted(files), head_commit_time=None)) if files else ()
    except Exception:
        attributions = ()
    titles = _session_titles(store_root) if attributions else {}
    sessions: dict[tuple[str, str], dict[str, Any]] = {}
    authors: dict[str, list[str]] = {}
    for row in attributions:
        path = str(getattr(row, "scope_key", "") or "")
        host = str(getattr(row, "host", "") or "").strip().lower()
        session_id = str(getattr(row, "session_id", "") or "").strip()
        if path not in files or not session_id:
            continue
        key = (host, session_id)
        session = sessions.get(key)
        if session is None:
            meta = titles.get(key) or next((value for (_, sid), value in titles.items() if sid == session_id), {})
            session = {
                "id": f"{host}:{session_id}" if host else session_id,
                "host": host,
                "session_id": session_id,
                "model": str(getattr(row, "model", "") or meta.get("model") or ""),
                "title": str(meta.get("title") or ""),
                "started_at": str(meta.get("started_at") or ""),
                "files": [],
            }
            sessions[key] = session
        if path not in session["files"]:
            session["files"].append(path)
            authors.setdefault(path, []).append(session["id"])

    file_rows = []
    for path in sorted(files):
        row = dict(files[path])
        row["sessions"] = authors.get(path, [])
        row["shared"] = len(row["sessions"]) > 1
        file_rows.append(row)
    ordered_sessions = sorted(sessions.values(), key=lambda session: (-len(session["files"]), session["id"]))
    for session in ordered_sessions:
        session["files"].sort()
        session["shared_files"] = [path for path in session["files"] if len(authors.get(path, [])) > 1]
    payload = {
        "project": {"root": str(project_root), "label": project_root.name},
        "files": file_rows,
        "sessions": ordered_sessions,
        "unattributed": [path for path in sorted(files) if not authors.get(path)],
        "total": len(files),
        "rule": "author = exact edit record for the path; presence and timing are not authorship",
    }
    with _CACHE_LOCK:
        _CHANGES_CACHE[cache_key] = (now, fingerprint, payload)
        while len(_CHANGES_CACHE) > 8:
            _CHANGES_CACHE.pop(next(iter(_CHANGES_CACHE)))
    return payload


# --------------------------------------------------------------------------- #
# Hotspots: files agents keep re-editing                                      #
# --------------------------------------------------------------------------- #


_HOTSPOT_DAYS = 7
_HOTSPOT_MAX_LEDGERS = 400
_HOTSPOT_MAX_FILES = 50
_HOTSPOT_TTL_S = 30.0
# lc-debt: each request walks sessions/**/run.json and stats every ledger;
# parsed edits are cached per ledger write. Move onto an edit index if the
# session store grows past a few thousand runs.
_LEDGER_EDITS: dict[str, tuple[int, int, dict[str, Any]]] = {}
_HOTSPOT_CACHE: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_EDIT_RANGE = re.compile(r":(?:L?\d+(?:-L?\d+)?|full|outline|summary)$")


def _parse_time(value: str) -> datetime | None:
    with contextlib.suppress(TypeError, ValueError):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _ledger_edits(run_file: Path, stat: os.stat_result) -> dict[str, Any]:
    """The ``file_edit`` events one run ledger recorded, cached per write."""

    key = str(run_file)
    with _CACHE_LOCK:
        cached = _LEDGER_EDITS.get(key)
    if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    info: dict[str, Any] = {
        "host": run_file.parent.parent.name.strip().lower(),
        "session_id": run_file.parent.name,
        "workspace": "",
        "edits": (),
    }
    try:
        raw = run_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raw = ""
    # Ledgers reach several MB; one without an edit is not worth parsing.
    if '"file_edit"' in raw:
        with contextlib.suppress(json.JSONDecodeError, ValueError):
            payload = json.loads(raw)
            if isinstance(payload, dict):
                info["host"] = str(payload.get("host") or info["host"]).strip().lower()
                info["session_id"] = str(payload.get("session_id") or info["session_id"])
                info["workspace"] = str(payload.get("workspace_path") or "")
                edits: list[tuple[str, str]] = []
                for event in payload.get("events") or ():
                    if not isinstance(event, dict) or event.get("kind") != "file_edit":
                        continue
                    body = event.get("payload")
                    recorded = body.get("path") if isinstance(body, dict) else None
                    if isinstance(recorded, str) and recorded.strip():
                        edits.append((recorded.strip(), str(event.get("at") or "")))
                info["edits"] = tuple(edits)
    with _CACHE_LOCK:
        if len(_LEDGER_EDITS) > 2000:
            _LEDGER_EDITS.clear()
        _LEDGER_EDITS[key] = (stat.st_mtime_ns, stat.st_size, info)
    return info


def _same_root(workspace: str, resolved_root: Path) -> bool:
    if not workspace.strip():
        return False
    try:
        return Path(workspace).expanduser().resolve() == resolved_root
    except (OSError, RuntimeError):
        return False


def _edited_path(recorded: str, roots: Sequence[str], workspace_is_root: bool) -> str | None:
    """The repository-relative path an edit record names, if it names one here.

    An absolute record names its tree. A relative one names no tree, so it
    counts only when the session's workspace is this project (the provenance
    rule for exact edit evidence).
    """

    clean = _EDIT_RANGE.sub("", recorded.strip())
    if not clean:
        return None
    norm = os.path.normpath(clean)
    if os.path.isabs(norm):
        for base in roots:
            if norm.startswith(base + os.sep):
                return Path(norm[len(base) + 1 :]).as_posix()
        return None
    if not workspace_is_root or norm == ".." or norm.startswith(".." + os.sep):
        return None
    return Path(norm).as_posix()


def edit_hotspots(
    store_root: Path,
    project_root: Path,
    paths: Iterable[str],
    *,
    days: int = _HOTSPOT_DAYS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Files agents re-edited most in the last *days*, from recorded edits only.

    Counts the ``file_edit`` events agent sessions' run ledgers recorded at
    edit time for files in this project. A file is a hotspot when it was
    edited more than once. Reads, timing, and git history are not counted, and
    no score is derived: the reader sees edits, sessions, and the last edit.
    """

    cache_key = (str(project_root), days)
    started = time.monotonic()
    if now is None:
        with _CACHE_LOCK:
            cached = _HOTSPOT_CACHE.get(cache_key)
        if cached is not None and started - cached[0] < _HOTSPOT_TTL_S:
            return cached[1]
    moment = now or datetime.now(UTC)
    since = moment - timedelta(days=days)
    cutoff = since.timestamp()
    ledgers: list[tuple[float, Path, os.stat_result]] = []
    with contextlib.suppress(OSError):
        for run_file in (store_root / "sessions").glob("*/*/*/*/*/run.json"):
            with contextlib.suppress(OSError):
                stat = run_file.stat()
                if stat.st_mtime >= cutoff:
                    ledgers.append((stat.st_mtime, run_file, stat))
    ledgers.sort(key=lambda item: item[0], reverse=True)
    truncated = len(ledgers) > _HOTSPOT_MAX_LEDGERS
    ledgers = ledgers[:_HOTSPOT_MAX_LEDGERS]

    known = set(paths)
    resolved_root = project_root.resolve()
    roots = tuple(dict.fromkeys(os.path.normpath(str(root)) for root in (project_root, resolved_root)))
    rows: dict[str, dict[str, Any]] = {}
    sessions: set[str] = set()
    for _mtime, run_file, stat in ledgers:
        info = _ledger_edits(run_file, stat)
        if not info["edits"]:
            continue
        workspace_is_root = _same_root(info["workspace"], resolved_root)
        session = f"{info['host']}:{info['session_id']}" if info["host"] else str(info["session_id"])
        for recorded, at in info["edits"]:
            when = _parse_time(at)
            if when is None or when < since:
                continue
            path = _edited_path(recorded, roots, workspace_is_root)
            if path is None or path not in known:
                continue
            row = rows.setdefault(path, {"path": path, "edits": 0, "sessions": [], "last_at": ""})
            row["edits"] += 1
            if session not in row["sessions"]:
                row["sessions"].append(session)
            stamp = when.astimezone(UTC).isoformat(timespec="seconds")
            row["last_at"] = max(row["last_at"], stamp)
            sessions.add(session)

    for row in rows.values():
        row["sessions"].sort()
    reedited = sorted((row for row in rows.values() if row["edits"] > 1), key=lambda row: row["last_at"], reverse=True)
    reedited.sort(key=lambda row: (-row["edits"], -len(row["sessions"])))
    payload = {
        "project": {"root": str(project_root), "label": project_root.name},
        "days": days,
        "since": since.astimezone(UTC).isoformat(timespec="seconds"),
        "files": reedited[:_HOTSPOT_MAX_FILES],
        "reedited_total": len(reedited),
        "edited_total": len(rows),
        "sessions": len(sessions),
        "ledgers_scanned": len(ledgers),
        "truncated": truncated,
        "rule": "edits = file_edit events agent sessions recorded at edit time; reads, timing, and git history are not counted",
    }
    if now is None:
        with _CACHE_LOCK:
            _HOTSPOT_CACHE[cache_key] = (started, payload)
            while len(_HOTSPOT_CACHE) > 8:
                _HOTSPOT_CACHE.pop(next(iter(_HOTSPOT_CACHE)))
    return payload


__all__ = [
    "PurposeUnavailable",
    "changed_line_ranges",
    "changes_by_session",
    "edit_hotspots",
    "file_focus",
    "file_relationships",
    "generate_module_purpose",
    "module_focus",
    "module_partition",
    "modules_overview",
    "package_roots",
    "provider_context",
    "symbol_focus",
    "symbol_relationships",
    "uncommitted_files",
]

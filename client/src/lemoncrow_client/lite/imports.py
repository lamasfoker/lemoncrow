"""Import statements and the repository files they name, for Runtime Lite.

Two halves, both deterministic, offline and bounded:

* :func:`extract_imports` parses one file's import statements into the rows
  the Lite index stores, ``(path, module, imported, line)``.
* :class:`ImportResolver` resolves a stored row to the indexed files it names,
  using only the repository's file list and its configuration files.

Stored rows, by language (``line`` is where the statement starts):

* Python (``.py``, ``.pyi``): ``module`` is the dotted module as written, with
  the leading dots of a relative import (``.``, ``.mod``, ``..pkg.mod``);
  ``imported`` is the names of a ``from`` import (``a, b as c``, ``*``) and
  empty for ``import a.b as c``. ``import a, b`` stores one row per module.
* JavaScript/TypeScript (``.js .jsx .ts .tsx .mjs .cjs .mts .cts`` and the
  scripts in ``.vue .svelte .astro``): ``module`` is the specifier as written
  (``./view``, ``react``, ``@scope/pkg/sub``, ``#alias``); ``imported`` is
  the imported binding names (``React, useState``, ``*``) and empty for
  side-effect imports, dynamic ``import("x")`` and ``require``.
* Go: ``module`` is the import path; ``imported`` is empty.
* Rust: ``module`` is the ``use`` path up to a group (``crate::a::B``, or
  ``crate::a::`` with ``imported`` ``B, c::D``); ``mod x;`` is stored as
  ``self::x`` with ``imported`` ``mod``. A ``super::`` that only leaves an
  inline ``mod name { ... }`` block is stored as ``self::``.
* Java/Kotlin: ``module`` is the imported name (``a.b.C``, ``a.b.*``);
  ``imported`` is ``static`` for a static import.
* Any other file keeps the original line patterns.

Resolution reads ``tsconfig.json``/``jsconfig.json`` (``baseUrl``, ``paths``,
``extends`` and ``references`` inside the repository), ``package.json``
(``name``, ``exports``, ``source``/``module``/``main``/``types``, ``imports``),
``go.mod`` (``module``), ``Cargo.toml`` (``[package] name``, ``[lib] path``)
and the Python project markers (``pyproject.toml``, ``setup.py``,
``setup.cfg``). Each configuration file is parsed at most once per resolver
and cached across resolvers by its ``(mtime, size)``. A package outside the
repository resolves to ``external`` and is never counted as missing.
"""

from __future__ import annotations

import bisect
import json
import posixpath
import re
import threading
import tomllib
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final

__all__ = [
    "IMPORT_STATUSES",
    "ImportResolution",
    "ImportResolver",
    "extract_imports",
    "imported_names",
]

PYTHON_SUFFIXES: Final[frozenset[str]] = frozenset({".py", ".pyi"})
JS_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte", ".astro"}
)
GO_SUFFIXES: Final[frozenset[str]] = frozenset({".go"})
RUST_SUFFIXES: Final[frozenset[str]] = frozenset({".rs"})
JVM_SUFFIXES: Final[frozenset[str]] = frozenset({".java", ".kt", ".kts"})

#: ``resolved``: ``targets`` names at least one other indexed file. ``local``:
#: the import stays inside this repository but names no other file (a namespace
#: package, or the importing file itself). ``external``: outside this
#: repository, never missing. ``missing``: it names this repository but no
#: indexed file matches. ``skipped``: a file type Lite does not resolve.
IMPORT_STATUSES: Final[tuple[str, ...]] = ("resolved", "local", "external", "missing", "skipped")

_MAX_IMPORTED_CHARS: Final[int] = 300
_MAX_IMPORTED_NAMES: Final[int] = 32
# lc-debt: a Python statement continued with brackets or backslashes is read
# across at most this many lines; names past it are not recorded (the module
# still is). Upgrade: a tokenizer-based reader.
_MAX_STATEMENT_LINES: Final[int] = 200
# lc-debt: a JavaScript import/export clause or a Rust ``use`` body longer than
# this many characters is not recognised at all. Upgrade: a real tokenizer.
_MAX_CLAUSE_CHARS: Final[int] = 20_000
_MAX_SPECIFIER_CHARS: Final[int] = 512
# lc-debt: one resolver parses at most this many configuration files
# (package.json, tsconfig/jsconfig, go.mod, Cargo.toml), shallowest first; past
# it every resolution says ``truncated``. Upgrade: per-package lazy loading.
_MAX_CONFIG_FILES: Final[int] = 2_000
# A configuration file larger than this is not parsed; resolution says ``truncated``.
_MAX_CONFIG_BYTES: Final[int] = 1 << 20
# lc-debt: tsconfig ``extends`` chains and ``references`` lists are followed
# this far; past it the chain is cut and resolution says ``truncated``.
_MAX_TSCONFIG_DEPTH: Final[int] = 8
# lc-debt: one Go package or Java/Kotlin wildcard import links at most this
# many files, in path order; past it the resolution says ``truncated``.
_MAX_PACKAGE_FILES: Final[int] = 64
# Parsed configuration kept across resolvers, keyed by path and (mtime, size).
_MAX_CONFIG_CACHE: Final[int] = 4_096

_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_JS_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_$][\w$]*")
_NEWLINE: Final[re.Pattern[str]] = re.compile(r"\n")
_NOT_NEWLINE: Final[re.Pattern[str]] = re.compile(r"[^\n]")

# --------------------------------------------------------------------------- #
# Extraction                                                                  #
# --------------------------------------------------------------------------- #


def _bounded(pattern: str, flags: int = 0) -> re.Pattern[str]:
    """Compile ``pattern`` with its ``@CLAUSE@``/``@SPEC@`` length bounds filled in."""

    return re.compile(
        pattern.replace("@CLAUSE@", str(_MAX_CLAUSE_CHARS)).replace("@SPEC@", str(_MAX_SPECIFIER_CHARS)), flags
    )


# The original line patterns, kept for files no language reader owns.
_LINE_PY_FROM = re.compile(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+(.+)")
_LINE_PY_IMPORT = re.compile(r"^\s*import\s+([A-Za-z_][\w.]*)")
_LINE_JS_IMPORT = re.compile(r"""^\s*import\b.*?\bfrom\s+["']([^"']+)["']""")
_LINE_JS_REQUIRE = re.compile(r"""\brequire\(\s*["']([^"']+)["']\s*\)""")
_LINE_RUST_USE = re.compile(r"^\s*(?:pub\s+)?use\s+([A-Za-z_][\w:]*)")
_LINE_GO_IMPORT = re.compile(r'^\s*(?:[A-Za-z_]\w*\s+)?["]([^"]+)["]')

_PY_FROM = re.compile(r"^\s*from\s+(\.+(?:[A-Za-z_][\w.]*)?|[A-Za-z_][\w.]*)\s+import\b\s*(.*)$")
_PY_IMPORT = re.compile(r"^\s*import\s+([A-Za-z_].*)$")
_PY_MODULE = re.compile(r"[A-Za-z_][\w.]*")

_JS_LEX = re.compile(
    r"""//[^\n]*|/\*.*?\*/|'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`""",
    re.S,
)
_JS_STATIC = _bounded(
    r"""(?<![\w$.])import\s+(?:type\s+)?(?P<clause>[\w$*{}\s,]{0,@CLAUSE@}?)(?<![\w$])from\s*"""
    r"""(?P<q>["'])(?P<spec>[^"'\n]{1,@SPEC@})(?P=q)"""
)
_JS_SIDE_EFFECT = _bounded(r"""(?<![\w$.])import\s*(?P<q>["'])(?P<spec>[^"'\n]{1,@SPEC@})(?P=q)""")
_JS_DYNAMIC = _bounded(r"""(?<![\w$.])import\s*\(\s*(?P<q>["'`])(?P<spec>[^"'`\n$]{1,@SPEC@})(?P=q)\s*[,)]""")
_JS_REQUIRE = _bounded(r"""(?<![\w$.])require\s*\(\s*(?P<q>["'`])(?P<spec>[^"'`\n$]{1,@SPEC@})(?P=q)\s*\)""")
_JS_EXPORT_FROM = _bounded(
    r"""(?<![\w$.])export\s+(?:type\s+)?(?P<clause>\*(?:\s+as\s+[\w$]+)?|\{[^}]{0,@CLAUSE@}\})\s*from\s*"""
    r"""(?P<q>["'])(?P<spec>[^"'\n]{1,@SPEC@})(?P=q)"""
)
_JS_PATTERNS: Final[tuple[tuple[re.Pattern[str], bool], ...]] = (
    (_JS_STATIC, True),
    (_JS_EXPORT_FROM, True),
    (_JS_SIDE_EFFECT, False),
    (_JS_DYNAMIC, False),
    (_JS_REQUIRE, False),
)

_GO_LEX = re.compile(r"""//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|`[^`]*`""", re.S)
_GO_DECL = re.compile(r"^(?:func|type|var|const)\b", re.M)
_GO_BLOCK = re.compile(r"(?<![\w.])import\s*\((?P<block>[^)]*)\)")
_GO_SINGLE = re.compile(r'(?<![\w.])import\s+(?:[\w.]+\s+)?"(?P<spec>[^"\n]+)"')
_GO_ENTRY = re.compile(r'(?:[\w.]+\s+)?"(?P<spec>[^"\n]+)"')

_RUST_LEX = re.compile(
    r"//[^\n]*|/\*.*?\*/" r'|"(?:\\.|[^"\\])*"' r"|'(?:\\(?:x[0-9a-fA-F]{2}|u\{[0-9a-fA-F]{1,6}\}|.)|[^'\\\n])'",
    re.S,
)
_RUST_VISIBILITY = r"(?<![\w:])(?:pub(?:\s*\([^)]*\))?\s+)?"
_RUST_USE = _bounded(_RUST_VISIBILITY + r"use\s+(?P<body>[^;{}]*(?:\{[^;]{0,@CLAUSE@})?);")
_RUST_MOD = re.compile(_RUST_VISIBILITY + r"mod\s+(?P<name>[A-Za-z_]\w*)\s*;")
_RUST_INLINE_MOD = re.compile(_RUST_VISIBILITY + r"mod\s+[A-Za-z_]\w*\s*\{")
_RUST_PATH = re.compile(r"[A-Za-z_][\w:]*")
_BRACE = re.compile(r"[{}]")

_JVM_IMPORT = re.compile(
    r"^[ \t]*import\s+(?P<static>static\s+)?(?P<name>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\.\*)?)", re.M
)


def _suffix(path: str) -> str:
    return PurePosixPath(path).suffix.lower()


class _Lines:
    """Line numbers for character offsets, built only when first asked."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._offsets: list[int] | None = None

    def at(self, position: int) -> int:
        if self._offsets is None:
            self._offsets = [match.start() for match in _NEWLINE.finditer(self._text)]
        return bisect.bisect_left(self._offsets, position) + 1


def _line_imports(path: str, text: str) -> list[tuple[str, str, str, int]]:
    """The original Lite line patterns, unchanged, for files no reader owns."""

    result: list[tuple[str, str, str, int]] = []
    in_go_block = False
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped == "import (":
            in_go_block = True
            continue
        if in_go_block and stripped == ")":
            in_go_block = False
            continue
        match = _LINE_PY_FROM.match(line)
        if match:
            names = match.group(2).split("#", 1)[0].strip()
            result.append((path, match.group(1), names[:_MAX_IMPORTED_CHARS], number))
            continue
        match = _LINE_PY_IMPORT.match(line)
        if match:
            result.append((path, match.group(1), "", number))
            continue
        match = _LINE_JS_IMPORT.match(line)
        if match:
            result.append((path, match.group(1), "", number))
            continue
        match = _LINE_JS_REQUIRE.search(line)
        if match:
            result.append((path, match.group(1), "", number))
            continue
        match = _LINE_RUST_USE.match(line)
        if match:
            result.append((path, match.group(1), "", number))
            continue
        if in_go_block or stripped.startswith("import "):
            match = _LINE_GO_IMPORT.search(stripped.removeprefix("import ").strip())
            if match:
                result.append((path, match.group(1), "", number))
    return result


def _clean_names(raw: str) -> str:
    text = raw.replace("\\", " ").replace("(", " ").replace(")", " ")
    parts = (" ".join(part.split()) for part in text.split(","))
    return ", ".join(part for part in parts if part)[:_MAX_IMPORTED_CHARS]


def _python_imports(path: str, text: str) -> list[tuple[str, str, str, int]]:
    result: list[tuple[str, str, str, int]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        number = index + 1
        index += 1
        if "import" not in line:
            continue
        match = _PY_FROM.match(line)
        if match:
            module = match.group(1)
            rest = match.group(2).split("#", 1)[0].strip()
            bracketed = rest.startswith("(")
            if bracketed or rest.endswith("\\"):
                # A bracketed or continued name list: read on to its end, bounded.
                collected = [rest]
                while index < len(lines) and len(collected) < _MAX_STATEMENT_LINES:
                    if (")" in collected[-1]) if bracketed else not collected[-1].endswith("\\"):
                        break
                    collected.append(lines[index].split("#", 1)[0].strip())
                    index += 1
                joined = " ".join(collected)
                if bracketed:
                    joined = joined.split(")", 1)[0]
                result.append((path, module, _clean_names(joined), number))
            elif rest:
                result.append((path, module, rest[:_MAX_IMPORTED_CHARS], number))
            continue
        match = _PY_IMPORT.match(line)
        if match:
            body = match.group(1).split("#", 1)[0].strip()
            read = 1
            while body.endswith("\\") and index < len(lines) and read < _MAX_STATEMENT_LINES:
                body = body[:-1] + " " + lines[index].split("#", 1)[0].strip()
                index += 1
                read += 1
            for part in body.rstrip("\\").split(","):
                module_match = _PY_MODULE.match(part.strip())
                if module_match:
                    result.append((path, module_match.group(0), "", number))
    return result


def _lex(text: str, lexer: re.Pattern[str], *, blank_strings: bool) -> tuple[str, list[int], list[int]]:
    """Blank comments (and strings when asked) keeping offsets; return kept string spans."""

    pieces: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    last = 0
    for match in lexer.finditer(text):
        token = match.group(0)
        start, end = match.span()
        pieces.append(text[last:start])
        if blank_strings or token.startswith(("//", "/*")):
            pieces.append(_NOT_NEWLINE.sub(" ", token))
        else:
            pieces.append(token)
            starts.append(start)
            ends.append(end)
        last = end
    pieces.append(text[last:])
    return "".join(pieces), starts, ends


def _in_string(position: int, starts: Sequence[int], ends: Sequence[int]) -> bool:
    slot = bisect.bisect_right(starts, position) - 1
    return slot >= 0 and position < ends[slot]


def _js_names(clause: str) -> str:
    names: list[str] = []
    braces = re.search(r"\{([^}]*)\}", clause)
    outer = clause if braces is None else clause[: braces.start()] + "," + clause[braces.end() :]
    for part in outer.split(","):
        part = part.strip()
        if part.startswith("*"):
            names.append("*")
        elif _JS_IDENTIFIER.fullmatch(part):
            names.append(part)
    if braces is not None:
        for part in braces.group(1).split(","):
            words = part.split()
            if len(words) > 1 and words[0] == "type":
                words = words[1:]
            if words and _JS_IDENTIFIER.fullmatch(words[0]):
                names.append(words[0])
    return ", ".join(dict.fromkeys(names))[:_MAX_IMPORTED_CHARS]


def _js_imports(path: str, text: str) -> list[tuple[str, str, str, int]]:
    if "import" not in text and "require" not in text and "export" not in text:
        return []
    clean, starts, ends = _lex(text, _JS_LEX, blank_strings=False)
    lines = _Lines(clean)
    found: dict[tuple[int, str], tuple[str, str, str, int]] = {}
    for pattern, has_clause in _JS_PATTERNS:
        for match in pattern.finditer(clean):
            start = match.start()
            if _in_string(start, starts, ends):
                continue
            spec = match.group("spec").strip()
            if not spec:
                continue
            names = _js_names(match.group("clause")) if has_clause else ""
            found.setdefault((start, spec), (path, spec, names, lines.at(start)))
    return [found[key] for key in sorted(found)]


def _go_imports(path: str, text: str) -> list[tuple[str, str, str, int]]:
    if "import" not in text:
        return []
    declaration = _GO_DECL.search(text)
    header = text[: declaration.start()] if declaration else text
    clean, _starts, _ends = _lex(header, _GO_LEX, blank_strings=False)
    lines = _Lines(clean)
    found: dict[int, tuple[str, str, str, int]] = {}
    for block in _GO_BLOCK.finditer(clean):
        offset = block.start("block")
        for entry in _GO_ENTRY.finditer(block.group("block")):
            position = offset + entry.start("spec")
            found[position] = (path, entry.group("spec"), "", lines.at(position))
    for single in _GO_SINGLE.finditer(clean):
        found.setdefault(single.start("spec"), (path, single.group("spec"), "", lines.at(single.start())))
    return [found[key] for key in sorted(found)]


def _rust_split(group: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for char in group:
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        depth += (char == "{") - (char == "}")
        current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _rust_flatten(group: str, prefix: str = "") -> list[str]:
    """``b,c::{d,e as f},self`` -> ``['b', 'c::d', 'c::e', 'self']``."""

    items: list[str] = []
    for part in _rust_split(group):
        if "{" in part and part.endswith("}"):
            head = part[: part.index("{")].rstrip(":")
            items.extend(_rust_flatten(part[part.index("{") + 1 : -1], f"{prefix}{head}::" if head else prefix))
            continue
        name = part.split(" as ", 1)[0].strip()
        if name:
            items.append(prefix + name)
    return items


def _rust_inline_mods(clean: str) -> list[tuple[int, int]]:
    """Character spans of inline ``mod name { ... }`` bodies."""

    opening = {match.end() - 1 for match in _RUST_INLINE_MOD.finditer(clean)}
    if not opening:
        return []
    spans: list[tuple[int, int]] = []
    stack: list[int] = []
    for brace in _BRACE.finditer(clean):
        if brace.group(0) == "{":
            stack.append(brace.start())
        elif stack:
            start = stack.pop()
            if start in opening:
                spans.append((start, brace.start()))
    spans.extend((start, len(clean)) for start in stack if start in opening)
    return spans


def _rust_relative(module: str, depth: int) -> str:
    """Drop the ``super::`` segments that only leave inline modules of this file."""

    if depth <= 0 or not module.startswith("super::"):
        return module
    segments = module.split("::")
    supers = 0
    while supers < len(segments) and segments[supers] == "super":
        supers += 1
    if supers <= depth:
        return "::".join(["self", *segments[supers:]])
    return "::".join(segments[depth:])


def _rust_imports(path: str, text: str) -> list[tuple[str, str, str, int]]:
    if "use" not in text and "mod" not in text:
        return []
    clean, _starts, _ends = _lex(text, _RUST_LEX, blank_strings=True)
    lines = _Lines(clean)
    inline = _rust_inline_mods(clean)
    found: dict[int, tuple[str, str, str, int]] = {}
    for match in _RUST_USE.finditer(clean):
        start = match.start()
        body = re.sub(r"\s*(::|\{|\}|,)\s*", r"\1", " ".join(match.group("body").split()))
        depth = sum(1 for low, high in inline if low < start < high)
        if "{" in body:
            module = body[: body.index("{")]
            if not _RUST_PATH.fullmatch(module):
                continue
            group = body[body.index("{") + 1 : body.rindex("}")] if "}" in body else ""
            imported = ", ".join(_rust_flatten(group))[:_MAX_IMPORTED_CHARS]
            found[start] = (path, _rust_relative(module, depth), imported, lines.at(start))
            continue
        head = _RUST_PATH.match(body)
        if head:
            found[start] = (path, _rust_relative(head.group(0), depth), "", lines.at(start))
    for match in _RUST_MOD.finditer(clean):
        if not any(low < match.start() < high for low, high in inline):
            found[match.start()] = (path, f"self::{match.group('name')}", "mod", lines.at(match.start()))
    return [found[key] for key in sorted(found)]


def _jvm_imports(path: str, text: str) -> list[tuple[str, str, str, int]]:
    if "import" not in text:
        return []
    lines = _Lines(text)
    return [
        (path, match.group("name"), "static" if match.group("static") else "", lines.at(match.start("name")))
        for match in _JVM_IMPORT.finditer(text)
    ]


def extract_imports(path: str, text: str) -> list[tuple[str, str, str, int]]:
    """The import rows ``(path, module, imported, line)`` of one file, in source order.

    The language comes from the file's suffix; the module documentation lists
    what each language stores. Deterministic: the same text gives the same rows.
    """

    suffix = _suffix(path)
    if suffix in PYTHON_SUFFIXES:
        return _python_imports(path, text)
    if suffix in JS_SUFFIXES:
        return _js_imports(path, text)
    if suffix in GO_SUFFIXES:
        return _go_imports(path, text)
    if suffix in RUST_SUFFIXES:
        return _rust_imports(path, text)
    if suffix in JVM_SUFFIXES:
        return _jvm_imports(path, text)
    return _line_imports(path, text)


def imported_names(imported: str) -> list[str]:
    """The plain names one stored ``imported`` value lists, aliases and paths dropped."""

    names: list[str] = []
    for part in imported.strip().strip("()").split(","):
        name = part.strip().strip("()").split(" as ", 1)[0].strip().rsplit("::", 1)[-1]
        if _IDENTIFIER.fullmatch(name) and name not in {"self", "mod", "static"}:
            names.append(name)
    return names[:_MAX_IMPORTED_NAMES]


# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

_CONFIG_CACHE: OrderedDict[str, tuple[int, int, Any]] = OrderedDict()
_CONFIG_LOCK = threading.Lock()
_TOO_LARGE: Final[object] = object()

_JSON_COMMENT = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.S)
_JSON_TRAILING_COMMA = re.compile(r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])', re.S)


def _keep_strings(match: re.Match[str]) -> str:
    token = match.group(0)
    return token if token.startswith('"') else ""


def _parse_jsonc(text: str) -> Any:
    """JSON with comments and trailing commas, as tsconfig and jsconfig allow."""

    return json.loads(_JSON_TRAILING_COMMA.sub(_keep_strings, _JSON_COMMENT.sub(_keep_strings, text)))


def _parse_config(name: str, text: str) -> Any:
    if name.endswith(".json"):
        return _parse_jsonc(text)
    if name == "Cargo.toml":
        return tomllib.loads(text)
    return text


def _load_config(absolute: Path) -> Any:
    """One parsed configuration file, or None; cached by path, mtime and size."""

    try:
        info = absolute.stat()
    except OSError:
        return None
    key = str(absolute)
    stamp = (int(info.st_mtime_ns), int(info.st_size))
    with _CONFIG_LOCK:
        cached = _CONFIG_CACHE.get(key)
        if cached is not None and (cached[0], cached[1]) == stamp:
            _CONFIG_CACHE.move_to_end(key)
            return cached[2]
    parsed: Any
    if stamp[1] > _MAX_CONFIG_BYTES:
        parsed = _TOO_LARGE
    else:
        try:
            parsed = _parse_config(absolute.name, absolute.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError, tomllib.TOMLDecodeError, RecursionError):
            parsed = None
    with _CONFIG_LOCK:
        _CONFIG_CACHE[key] = (stamp[0], stamp[1], parsed)
        _CONFIG_CACHE.move_to_end(key)
        while len(_CONFIG_CACHE) > _MAX_CONFIG_CACHE:
            _CONFIG_CACHE.popitem(last=False)
    return parsed


def _join(*parts: str) -> str:
    present = [part for part in parts if part]
    if not present:
        return ""
    joined = posixpath.normpath(posixpath.join(*present))
    return "" if joined == "." else joined


def _inside(path: str) -> bool:
    return path != ".." and not path.startswith(("../", "/"))


def _parent(path: str) -> str:
    return path.rpartition("/")[0]


def _tail(path: str) -> str:
    return "/".join(path.rsplit("/", 2)[-2:])


def _package_name(spec: str) -> tuple[str, str]:
    """``@scope/pkg/sub/x`` -> (``@scope/pkg``, ``sub/x``); ``pkg/x`` -> (``pkg``, ``x``)."""

    parts = spec.split("/")
    count = 2 if spec.startswith("@") and len(parts) >= 2 else 1
    return "/".join(parts[:count]), "/".join(parts[count:])


@dataclass(frozen=True, slots=True)
class ImportResolution:
    """Where one stored import row points.

    ``status`` is one of :data:`IMPORT_STATUSES`. ``targets`` maps each other
    indexed file the row names to the imported names that file provides (empty
    when the row names the whole module). ``truncated`` says a bound cut the
    resolution short: a package with more files than are linked, or partial
    configuration.
    """

    status: str
    targets: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    truncated: bool = False


_EXTERNAL: Final[ImportResolution] = ImportResolution("external")
_MISSING: Final[ImportResolution] = ImportResolution("missing")
_LOCAL: Final[ImportResolution] = ImportResolution("local")
_SKIPPED: Final[ImportResolution] = ImportResolution("skipped")
_NAMESPACE: Final[str] = "\0namespace"

_JS_EXTENSIONS: Final[tuple[str, ...]] = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
# TypeScript ESM sources import their compiled names: ``./x.js`` is ``./x.ts``.
_ESM_SOURCES: Final[dict[str, tuple[str, ...]]] = {
    ".js": (".ts", ".tsx"),
    ".jsx": (".tsx",),
    ".mjs": (".mts",),
    ".cjs": (".cts",),
}
_SCRIPT_SUFFIXES: Final[frozenset[str]] = JS_SUFFIXES | {".json"}
_BUILD_DIRS: Final[frozenset[str]] = frozenset({"dist", "lib", "build", "out", "esm", "cjs", "es", "types"})
_CONDITIONS: Final[tuple[str, ...]] = (
    "source",
    "development",
    "import",
    "module",
    "browser",
    "node",
    "default",
    "require",
    "types",
)
_PACKAGE_ENTRY_FIELDS: Final[tuple[str, ...]] = ("source", "module", "main", "types", "typings")
_PYTHON_MARKERS: Final[frozenset[str]] = frozenset({"pyproject.toml", "setup.py", "setup.cfg"})
_CONFIG_NAMES: Final[frozenset[str]] = frozenset(
    {"package.json", "tsconfig.json", "jsconfig.json", "go.mod", "Cargo.toml"}
)
_GO_MODULE = re.compile(r'^\s*module\s+"?([^\s"]+)"?', re.M)
# A specifier an npm package could answer: an optional ``@scope/``, then a name.
_NPM_NAME = re.compile(r"(?:@[a-z0-9][\w.-]*/)?[a-z0-9][\w.-]*(?:/|$)", re.I)


def _pick_target(value: Any, depth: int = 0) -> str | None:
    """One path from a package.json ``exports``/``imports`` value, by condition order."""

    if depth > _MAX_TSCONFIG_DEPTH:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for item in value:
            picked = _pick_target(item, depth + 1)
            if picked:
                return picked
        return None
    if isinstance(value, dict):
        keys = [key for key in _CONDITIONS if key in value] + [key for key in value if key not in _CONDITIONS]
        for key in keys:
            picked = _pick_target(value[key], depth + 1)
            if picked:
                return picked
    return None


def _match_pattern(table: Mapping[str, Any], key: str) -> tuple[Any, str] | None:
    """Node/TypeScript subpath matching: the exact key, else the longest ``*`` prefix."""

    if key in table:
        return table[key], ""
    best: tuple[int, str, str] | None = None
    for pattern in table:
        if pattern.count("*") != 1:
            continue
        prefix, suffix = pattern.split("*", 1)
        if len(key) >= len(prefix) + len(suffix) and key.startswith(prefix) and key.endswith(suffix):
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), pattern, key[len(prefix) : len(key) - len(suffix)])
    if best is None:
        return None
    return table[best[1]], best[2]


@dataclass(frozen=True, slots=True)
class _TsOptions:
    base_url: str | None
    paths: Mapping[str, tuple[str, ...]]
    paths_base: str


# --------------------------------------------------------------------------- #
# Resolution                                                                  #
# --------------------------------------------------------------------------- #


class ImportResolver:
    """Resolve stored Lite import rows to the indexed files they name.

    Built from one index generation's file list (``paths``: repository-relative
    POSIX paths) and the configuration files among them. Resolution is a pure
    function of those: no network, no subprocess, and nothing outside
    ``repo_root`` is read. A listed path that leaves the repository is ignored.
    Keep one resolver per index generation.
    """

    def __init__(self, repo_root: Path | str, paths: Iterable[str]) -> None:
        self.repo_root = Path(repo_root)
        # Every probe and configuration read is a lookup in this set, so it must
        # hold only paths inside the repository.
        files = sorted({clean for clean in (_join(str(path)) for path in paths) if clean and _inside(clean)})
        self._paths: frozenset[str] = frozenset(files)
        self._dirs: dict[str, list[str]] = {}
        self._all_dirs: set[str] = {""}
        self._dirs_by_name: dict[str, list[str]] = {}
        self._by_tail: dict[str, list[str]] = {}
        #: True once a bound cut configuration short; every resolution is then partial.
        self.truncated = False
        self._config: dict[str, Any] = {}
        config_paths: list[str] = []
        markers: list[str] = []
        python_files: list[str] = []
        for path in files:
            parent, _, name = path.rpartition("/")
            self._dirs.setdefault(parent, []).append(path)
            directory = parent
            while directory not in self._all_dirs:
                self._all_dirs.add(directory)
                self._dirs_by_name.setdefault(directory.rpartition("/")[2], []).append(directory)
                directory = _parent(directory)
            suffix = _suffix(name)
            if suffix in PYTHON_SUFFIXES or suffix in JVM_SUFFIXES or suffix in RUST_SUFFIXES:
                self._by_tail.setdefault(_tail(path), []).append(path)
            if suffix in PYTHON_SUFFIXES:
                python_files.append(path)
            if name in _CONFIG_NAMES and "node_modules" not in path.split("/"):
                config_paths.append(path)
            elif name in _PYTHON_MARKERS:
                markers.append(path)
        self._config_paths = frozenset(config_paths)
        self._ts_options: dict[str, _TsOptions | None] = {}
        self._nearest: dict[tuple[str, str], str | None] = {}
        eager = sorted(
            (path for path in config_paths if not path.endswith(("tsconfig.json", "jsconfig.json"))),
            key=lambda path: (path.count("/"), path),
        )
        self._packages: dict[str, str] = {}
        self._go_modules: list[tuple[str, str]] = []
        self._crates: dict[str, str] = {}
        self._crate_roots: dict[str, str] = {}
        for path in eager:
            data = self._read(path)
            directory = _parent(path)
            if path.endswith("package.json"):
                package_name = data.get("name") if isinstance(data, dict) else None
                if isinstance(package_name, str) and package_name:
                    self._packages.setdefault(package_name, directory)
            elif path.endswith("go.mod"):
                match = _GO_MODULE.search(data) if isinstance(data, str) else None
                if match:
                    self._go_modules.append((match.group(1), directory))
            elif isinstance(data, dict):
                self._add_crate(directory, data)
        self._go_modules.sort(key=lambda item: (-len(item[0]), item[1]))
        roots = {""}
        for marker in markers:
            roots.add(_parent(marker))
        roots |= {_join(root, "src") for root in roots if _join(root, "src") in self._all_dirs}
        roots |= {directory for directory in self._all_dirs if directory.rpartition("/")[2] == "src"}
        #: Top-level Python names -> the import roots that hold them.
        self._python_tops: dict[str, list[str]] = {}
        for path in python_files:
            parts = path.split("/")
            for position in range(len(parts)):
                root = "/".join(parts[:position])
                if root not in roots:
                    continue
                top = parts[position] if position < len(parts) - 1 else parts[position].rsplit(".", 1)[0]
                holders = self._python_tops.setdefault(top, [])
                if root not in holders:
                    holders.append(root)

    # -- shared ---------------------------------------------------------------

    def _read(self, path: str) -> Any:
        if path in self._config:
            return self._config[path]
        if path not in self._paths:
            return None
        if len(self._config) >= _MAX_CONFIG_FILES:
            self.truncated = True
            return None
        parsed = _load_config(self.repo_root / path)
        if parsed is _TOO_LARGE:
            self.truncated = True
            parsed = None
        self._config[path] = parsed
        return parsed

    def _nearest_config(self, directory: str, names: Sequence[str]) -> str | None:
        key = (directory, "|".join(names))
        if key in self._nearest:
            return self._nearest[key]
        found: str | None = None
        current = directory
        while found is None:
            found = next((c for c in (_join(current, n) for n in names) if c in self._config_paths), None)
            if not current:
                break
            current = _parent(current)
        self._nearest[key] = found
        return found

    def _unique_suffix(self, candidate: str) -> str | None:
        if "/" not in candidate:
            return None
        matches = [p for p in self._by_tail.get(_tail(candidate), ()) if p == candidate or p.endswith("/" + candidate)]
        return matches[0] if len(matches) == 1 else None

    def resolve(self, source_path: str, module: str, imported: str = "") -> ImportResolution:
        """Resolve one stored row ``(source_path, module, imported)``.

        ``source_path`` is the importing file, repository-relative; ``module``
        and ``imported`` are the stored values. Targets never include
        ``source_path`` itself.
        """

        module = module.strip()
        suffix = _suffix(source_path)
        if suffix in PYTHON_SUFFIXES:
            result = self._resolve_python(source_path, module, imported)
        elif suffix in JS_SUFFIXES:
            result = self._resolve_js(source_path, module)
            if result.targets and imported:
                names = tuple(imported_names(imported))
                result = ImportResolution(result.status, {target: names for target in result.targets})
        elif suffix in GO_SUFFIXES:
            result = self._resolve_go(module)
        elif suffix in RUST_SUFFIXES:
            result = self._resolve_rust(source_path, module, imported)
        elif suffix in JVM_SUFFIXES:
            result = self._resolve_jvm(module)
        else:
            return _SKIPPED
        targets = {target: names for target, names in result.targets.items() if target != source_path}
        truncated = result.truncated or self.truncated
        if result.status == "resolved" and not targets:
            return ImportResolution("local", truncated=truncated)
        if len(targets) != len(result.targets) or truncated != result.truncated:
            return ImportResolution(result.status, targets, truncated)
        return result

    # -- Python ---------------------------------------------------------------

    def _python_file(self, base: str) -> str | None:
        if not base:
            return None
        for candidate in (base + ".py", base + ".pyi", base + "/__init__.py", base + "/__init__.pyi"):
            if candidate in self._paths:
                return candidate
        return _NAMESPACE if base in self._all_dirs else None

    def _python_absolute(self, source_path: str, segments: Sequence[str]) -> str | None:
        if not segments:
            return None
        source_dir = _parent(source_path)
        relative = "/".join(segments)
        if len(segments) == 1 and source_dir and _join(source_dir, "__init__.py") not in self._paths:
            # A script directory imports its siblings first; a package does not.
            sibling = self._python_file(_join(source_dir, relative))
            if sibling not in (None, _NAMESPACE):
                return sibling

        def order(root: str) -> tuple[int, int, str]:
            if not root or source_path.startswith(root + "/"):
                return 0, -len(root), root
            return 1, root.count("/"), root

        namespace = False
        for root in sorted(self._python_tops.get(segments[0], ()), key=order):
            found = self._python_file(_join(root, relative))
            if found == _NAMESPACE:
                namespace = True
            elif found is not None:
                return found
        if len(segments) >= 2:
            for candidate in (relative + ".py", relative + ".pyi", relative + "/__init__.py"):
                unique = self._unique_suffix(candidate)
                if unique is not None:
                    return unique
        return _NAMESPACE if namespace else None

    def _python_find(self, source_path: str, level: int, segments: Sequence[str]) -> str | None:
        if not level:
            return self._python_absolute(source_path, segments)
        parts = [part for part in _parent(source_path).split("/") if part]
        package = "/".join(parts[: len(parts) - (level - 1)])
        if segments:
            return self._python_file(_join(package, "/".join(segments)))
        return self._python_file(package)

    def _resolve_python(self, source_path: str, module: str, imported: str) -> ImportResolution:
        level = len(module) - len(module.lstrip("."))
        segments = [part for part in module[level:].split(".") if part]
        if not level and not segments:
            return _MISSING
        if level and level - 1 > _parent(source_path).count("/") + (1 if _parent(source_path) else 0):
            return _MISSING  # Climbs above the repository root.
        targets: dict[str, list[str]] = {}
        remaining: list[str] = []
        local = False
        names = imported_names(imported)
        for name in names:
            submodule = self._python_find(source_path, level, [*segments, name])
            if submodule == _NAMESPACE:
                local = True
            elif submodule is not None:
                targets.setdefault(submodule, []).append(name)
            else:
                remaining.append(name)
        if remaining or not names:
            found = self._python_find(source_path, level, segments)
            if found == _NAMESPACE:
                local = True
            elif found is not None:
                targets.setdefault(found, []).extend(remaining)
            elif not targets and not local:
                if level:
                    return _MISSING
                # Missing only when the package that should hold it is here;
                # otherwise a distribution sharing a top-level name is external.
                parent = self._python_find(source_path, 0, segments[:-1])
                return _MISSING if parent not in (None, _NAMESPACE) else _EXTERNAL
        if not targets:
            return _LOCAL
        return ImportResolution("resolved", {target: tuple(found) for target, found in targets.items()})

    # -- JavaScript / TypeScript ----------------------------------------------

    def _probe(self, stem: str) -> str | None:
        stem = _join(stem)
        if not stem or not _inside(stem):
            return None
        if stem in self._paths:
            return stem
        suffix = PurePosixPath(stem).suffix
        for replacement in _ESM_SOURCES.get(suffix, ()):
            candidate = stem[: -len(suffix)] + replacement
            if candidate in self._paths:
                return candidate
        for extension in _JS_EXTENSIONS:
            if stem + extension in self._paths:
                return stem + extension
        if stem in self._all_dirs:
            for extension in _JS_EXTENSIONS:
                if f"{stem}/index{extension}" in self._paths:
                    return f"{stem}/index{extension}"
        return None

    def _found(self, found: str | None, spec: str) -> ImportResolution:
        if found is not None:
            return ImportResolution("resolved", {found: ()})
        suffix = PurePosixPath(spec).suffix.lower()
        # An asset Lite does not index (an image, a stylesheet) is not a missing module.
        return _SKIPPED if suffix and suffix not in _SCRIPT_SUFFIXES else _MISSING

    def _probe_package_path(self, package_dir: str, target: str) -> str | None:
        found = self._probe(_join(package_dir, target))
        if found is not None:
            return found
        parts = [part for part in _join(target).split("/") if part]
        stripped = list(parts)
        while len(stripped) > 1 and stripped[0] in _BUILD_DIRS:
            stripped.pop(0)
        if stripped == parts:
            return None
        # A built entry (dist/index.js, types/index.d.ts) maps back to its source.
        source = "/".join(stripped)
        source = source[: -len(".d.ts")] if source.endswith(".d.ts") else source
        return self._probe(_join(package_dir, "src", source))

    def _package_entry(self, package_dir: str, subpath: str) -> str | None:
        data = self._read(_join(package_dir, "package.json"))
        data = data if isinstance(data, dict) else {}
        exports = data.get("exports")
        candidates: list[Any] = []
        if isinstance(exports, dict) and any(str(key).startswith(".") for key in exports):
            matched = _match_pattern(exports, "./" + subpath if subpath else ".")
            if matched is not None:
                picked = _pick_target(matched[0])
                candidates.append(picked.replace("*", matched[1]) if picked else None)
        elif exports is not None and not subpath:
            candidates.append(_pick_target(exports))
        if subpath:
            candidates.extend([subpath, "src/" + subpath])
        else:
            candidates.extend(data.get(name) for name in _PACKAGE_ENTRY_FIELDS)
            candidates.extend(["src/index", "index"])
        for candidate in candidates:
            if isinstance(candidate, str) and candidate:
                found = self._probe_package_path(package_dir, candidate)
                if found is not None:
                    return found
        return None

    def _ts_reference(self, config: str, reference: str) -> str | None:
        """The repository file an ``extends``/``references`` entry names, if any."""

        if reference.startswith("."):
            candidate = _join(_parent(config), reference)
            if not _inside(candidate):
                return None
            options: tuple[str, ...] = (candidate, candidate + ".json", _join(candidate, "tsconfig.json"))
        else:
            name, rest = _package_name(reference)
            package_dir = self._packages.get(name)
            if package_dir is None:
                return None  # A published base config (@tsconfig/node20): outside the repository.
            candidate = _join(package_dir, rest or "tsconfig.json")
            options = (candidate, candidate + ".json")
        return next((option for option in options if option in self._paths), None)

    def _ts_chain(self, config: str, depth: int = 0) -> tuple[str | None, Any, str | None]:
        """(baseUrl directory, paths, directory of the config defining paths) through ``extends``."""

        data = self._read(config)
        if not isinstance(data, dict):
            return None, None, None
        base_url: str | None = None
        paths: Any = None
        paths_dir: str | None = None
        extends = data.get("extends")
        parents = [extends] if isinstance(extends, str) else [item for item in extends or () if isinstance(item, str)]
        for parent in parents:
            if depth >= _MAX_TSCONFIG_DEPTH:
                self.truncated = True
                break
            parent_config = self._ts_reference(config, parent)
            if parent_config is None:
                continue
            parent_base, parent_paths, parent_paths_dir = self._ts_chain(parent_config, depth + 1)
            base_url = parent_base if parent_base is not None else base_url
            if parent_paths is not None:
                paths, paths_dir = parent_paths, parent_paths_dir
        options = data.get("compilerOptions")
        if isinstance(options, dict):
            if isinstance(options.get("baseUrl"), str):
                base_url = _join(_parent(config), options["baseUrl"])
            if isinstance(options.get("paths"), dict):
                paths, paths_dir = options["paths"], _parent(config)
        return base_url, paths, paths_dir

    def _ts_options_for(self, source_dir: str) -> _TsOptions | None:
        config = self._nearest_config(source_dir, ("tsconfig.json", "jsconfig.json"))
        if config is None:
            return None
        if config in self._ts_options:
            return self._ts_options[config]
        base_url, paths, paths_dir = self._ts_chain(config)
        if base_url is None and paths is None:
            # A solution config: the first referenced project with aliases speaks for it.
            data = self._read(config)
            references = data.get("references") if isinstance(data, dict) else None
            for count, reference in enumerate(references if isinstance(references, list) else []):
                if count >= _MAX_TSCONFIG_DEPTH:
                    self.truncated = True
                    break
                path = reference.get("path") if isinstance(reference, dict) else None
                referenced = self._ts_reference(config, path) if isinstance(path, str) else None
                if referenced is not None:
                    base_url, paths, paths_dir = self._ts_chain(referenced)
                    if base_url is not None or paths is not None:
                        break
        options: _TsOptions | None = None
        if base_url is not None or isinstance(paths, dict):
            table = {
                str(pattern): tuple(item for item in substitutions if isinstance(item, str))
                for pattern, substitutions in (paths if isinstance(paths, dict) else {}).items()
                if isinstance(substitutions, list)
            }
            options = _TsOptions(
                base_url=base_url,
                paths=table,
                paths_base=base_url if base_url is not None else (paths_dir or _parent(config)),
            )
        self._ts_options[config] = options
        return options

    def _resolve_js(self, source_path: str, specifier: str) -> ImportResolution:
        source_dir = _parent(source_path)
        spec = specifier if specifier.startswith("#") else specifier.split("?", 1)[0].split("#", 1)[0]
        if not spec:
            return _MISSING
        if spec.startswith(("./", "../")) or spec in {".", ".."}:
            target = _join(source_dir, spec)
            if "node_modules" in target.split("/"):
                return _EXTERNAL  # An installed package reached by path.
            return self._found(self._probe(target), spec) if _inside(target) else _MISSING
        if spec.startswith("#"):
            package_json = self._nearest_config(source_dir, ("package.json",))
            data = self._read(package_json) if package_json else None
            table = data.get("imports") if isinstance(data, dict) else None
            matched = _match_pattern(table, spec) if isinstance(table, dict) else None
            picked = _pick_target(matched[0]) if matched is not None else None
            if package_json is None or matched is None or not picked:
                return _MISSING
            picked = picked.replace("*", matched[1])
            if not picked.startswith("."):
                return self._resolve_bare(source_dir, picked)
            return self._found(self._probe_package_path(_parent(package_json), picked), picked)
        if spec.startswith("/"):
            package_json = self._nearest_config(source_dir, ("package.json",))
            root = _parent(package_json) if package_json else ""
            return self._found(self._probe(_join(root, spec.lstrip("/"))), spec)
        return self._resolve_bare(source_dir, spec)

    def _resolve_bare(self, source_dir: str, spec: str) -> ImportResolution:
        options = self._ts_options_for(source_dir)
        aliased = False
        if options is not None:
            matched = _match_pattern(options.paths, spec) if options.paths else None
            if matched is not None:
                aliased = True
                for substitution in matched[0]:
                    found = self._probe(_join(options.paths_base, substitution.replace("*", matched[1])))
                    if found is not None:
                        return ImportResolution("resolved", {found: ()})
            if options.base_url is not None:
                found = self._probe(_join(options.base_url, spec))
                if found is not None:
                    return ImportResolution("resolved", {found: ()})
        name, subpath = _package_name(spec)
        package_dir = self._packages.get(name)
        if package_dir is not None:
            return self._found(self._package_entry(package_dir, subpath), spec)
        # A path alias that names no file falls back to node_modules, as TypeScript does;
        # it is missing only when no package could carry that name (``@/x``, ``~x``).
        return self._found(None, spec) if aliased and not _NPM_NAME.match(spec) else _EXTERNAL

    # -- Go -------------------------------------------------------------------

    def _resolve_go(self, spec: str) -> ImportResolution:
        for module_path, module_dir in self._go_modules:
            if spec != module_path and not spec.startswith(module_path + "/"):
                continue
            package_dir = _join(module_dir, spec[len(module_path) :].lstrip("/"))
            files = [p for p in self._dirs.get(package_dir, ()) if p.endswith(".go") and not p.endswith("_test.go")]
            if not files:
                return _MISSING
            return ImportResolution(
                "resolved",
                {path: () for path in files[:_MAX_PACKAGE_FILES]},
                truncated=len(files) > _MAX_PACKAGE_FILES,
            )
        return _EXTERNAL

    # -- Rust -----------------------------------------------------------------

    def _add_crate(self, crate_dir: str, data: Mapping[str, Any]) -> None:
        package = data.get("package")
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            return
        lib = data.get("lib")
        lib_path = lib.get("path") if isinstance(lib, dict) else None
        candidates = [_join(crate_dir, lib_path)] if isinstance(lib_path, str) else []
        candidates += [_join(crate_dir, "src/lib.rs"), _join(crate_dir, "src/main.rs")]
        self._crate_roots[crate_dir] = next((c for c in candidates if c in self._paths), "")
        self._crates.setdefault(package["name"].replace("-", "_"), crate_dir)

    def _crate_of(self, source_path: str) -> str | None:
        directory = _parent(source_path)
        while directory not in self._crate_roots:
            if not directory:
                return None
            directory = _parent(directory)
        return directory

    def _rust_children(self, crate_dir: str, source_path: str) -> str:
        """The directory holding the files of ``source_path``'s child modules."""

        name = source_path.rpartition("/")[2]
        source_dir = _parent(source_path)
        src = _join(crate_dir, "src")
        if (
            name in {"lib.rs", "main.rs", "mod.rs"}
            or source_path == self._crate_roots.get(crate_dir)
            or not source_path.startswith(src + "/")
            or source_dir == _join(src, "bin")
        ):
            return source_dir
        return _join(source_dir, name[: -len(".rs")])

    def _rust_module_file(self, crate_dir: str, children: str) -> str | None:
        """The file of the module whose child modules live in ``children``."""

        if children == _join(crate_dir, "src"):
            return self._crate_roots.get(crate_dir) or None
        return next((c for c in (children + ".rs", children + "/mod.rs") if c in self._paths), None)

    def _rust_file(self, base: str, segments: Sequence[str]) -> str | None:
        """The deepest module file along ``segments`` under ``base``."""

        for count in range(len(segments), 0, -1):
            stem = _join(base, "/".join(segments[:count]))
            for candidate in (stem + ".rs", stem + "/mod.rs"):
                if candidate in self._paths:
                    return candidate
        return None

    def _rust_path(self, source_path: str, crate_dir: str | None, path: str) -> tuple[str, str | None]:
        segments = [part for part in path.split("::") if part and part != "*"]
        if not segments:
            return "missing", None
        head = segments[0]
        if head in {"crate", "self", "super"}:
            if crate_dir is None:
                return "local", None  # A file outside any Cargo package: its crate is unknown.
            src = _join(crate_dir, "src")
            if head == "crate":
                base, rest = src, segments[1:]
            else:
                base = self._rust_children(crate_dir, source_path)
                rest = segments[1:] if head == "self" else segments
                while rest and rest[0] == "super":
                    if base == src or not base.startswith(src + "/"):
                        return "missing", None  # Climbs above the crate root.
                    base, rest = _parent(base), rest[1:]
            found = self._rust_file(base, rest) or self._rust_module_file(crate_dir, base)
            return ("resolved", found) if found else ("local", None)
        crate = self._crates.get(head)
        if crate is None:
            return "external", None  # std, core, alloc and dependencies.
        found = self._rust_file(_join(crate, "src"), segments[1:]) or self._crate_roots.get(crate) or None
        return ("resolved", found) if found else ("missing", None)

    def _resolve_rust(self, source_path: str, module: str, imported: str) -> ImportResolution:
        crate_dir = self._crate_of(source_path)
        prefix = module.rstrip(":")
        grouped = module.endswith("::") and imported not in {"", "mod"}
        items = [item.strip() for item in imported.split(",") if item.strip()] if grouped else [""]
        targets: dict[str, list[str]] = {}
        statuses: set[str] = set()
        for item in items:
            full = prefix if item in {"", "self"} else f"{prefix}::{item}"
            status, found = self._rust_path(source_path, crate_dir, full)
            statuses.add(status)
            if found is not None:
                names = targets.setdefault(found, [])
                name = item.rsplit("::", 1)[-1]
                if _IDENTIFIER.fullmatch(name) and name != "self":
                    names.append(name)
        if targets:
            return ImportResolution("resolved", {target: tuple(names) for target, names in targets.items()})
        for status in ("missing", "local"):
            if status in statuses:
                return ImportResolution(status)
        return _EXTERNAL

    # -- Java / Kotlin --------------------------------------------------------

    def _resolve_jvm(self, name: str) -> ImportResolution:
        segments = [part for part in name.split(".") if part and part != "*"]
        if not segments:
            return _MISSING
        if name.endswith(".*"):
            package = "/".join(segments)
            files = sorted(
                path
                for directory in self._dirs_by_name.get(segments[-1], ())
                if directory == package or directory.endswith("/" + package)
                for path in self._dirs.get(directory, ())
                if _suffix(path) in JVM_SUFFIXES
            )
            if not files:
                return _EXTERNAL
            return ImportResolution(
                "resolved",
                {path: () for path in files[:_MAX_PACKAGE_FILES]},
                truncated=len(files) > _MAX_PACKAGE_FILES,
            )
        # ``a.b.C``; a static or nested import names a member, so try its owners too.
        for drop in range(min(3, len(segments) - 1)):
            owner = segments[: len(segments) - drop]
            for extension in (".java", ".kt"):
                candidate = "/".join(owner) + extension
                found = candidate if candidate in self._paths else self._unique_suffix(candidate)
                if found is not None:
                    return ImportResolution("resolved", {found: (owner[-1],)})
        return _EXTERNAL

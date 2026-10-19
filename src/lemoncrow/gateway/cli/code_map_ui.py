"""``lc rt map``: this repository as the Runtime Lite index sees it, in the terminal.

Runtime Lite has no web dashboard, so its code map lives here: folders and
files with their symbol counts, each file's symbols (nested by line range),
what it imports and what imports it, symbol source, and symbol search. Links
are import-level, the same basis the dashboard's Lite map labels them with.

:class:`CodeMap` is one read of the index; :class:`MapSource` gives the shared
picker (:mod:`lemoncrow.gateway.cli.picker`) its pages; :func:`overview_lines`
prints the same overview off a terminal.
"""

from __future__ import annotations

import ast
import os
import shlex
import sqlite3
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Final

from lemoncrow.gateway.cli import style
from lemoncrow.gateway.cli.picker import (
    GAP,
    POINTER,
    Fragment,
    Key,
    Line,
    Outcome,
    Page,
    Row,
    describe_lines,
    heading,
    note,
)

_MOST_IMPORTED: Final[int] = 8
_MAX_SOURCE_LINES: Final[int] = 400
_SEARCH_LIMIT: Final[int] = 40
_MENTION_LIMIT: Final[int] = 40
_CHANGES_SHOWN: Final[int] = 8
_CONTEXT_LINES: Final[int] = 12
_GIT_TIMEOUT_S: Final[float] = 2.0
_KIND_LABELS: Final[dict[str, str]] = {"binding": "variable"}


@dataclass(frozen=True)
class FileInfo:
    path: str
    symbols: int
    language: str
    kind: str
    """source, test, docs, config, data, asset or other."""


@dataclass(frozen=True)
class Mention:
    """A line that names a symbol: a text match, not a resolved reference."""

    path: str
    line: int
    text: str


@dataclass(frozen=True)
class Change:
    """A path ``git status`` reports: modified, new, deleted, renamed or conflicted."""

    path: str
    status: str


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    path: str
    line: int
    end_line: int
    signature: str


class CodeMap:
    """One read of a worktree's Runtime Lite index: files, folders, symbols, import links."""

    def __init__(self, engine: Any, connection: sqlite3.Connection) -> None:
        from lemoncrow.core.service.code_map import _file_metadata

        self.engine = engine
        self.root: Path = engine.repo_root
        self.db = connection
        counts = {
            str(path): int(count)
            for path, count in connection.execute("SELECT path, COUNT(*) FROM main.symbols GROUP BY path")
        }
        self.files: dict[str, FileInfo] = {}
        for (path,) in connection.execute("SELECT path FROM main.files ORDER BY path"):
            kind, language = _file_metadata(str(path))
            self.files[str(path)] = FileInfo(str(path), counts.get(str(path), 0), language, kind)
        links = engine.import_links(connection)
        self.links_truncated = bool(links.truncated)
        self.imports: dict[str, set[str]] = defaultdict(set)
        self.importers: dict[str, set[str]] = defaultdict(set)
        for row in links.rows:
            source, target = str(row["source_path"]), str(row["target_path"])
            if source != target:
                self.imports[source].add(target)
                self.importers[target].add(source)

    @property
    def symbol_count(self) -> int:
        return sum(info.symbols for info in self.files.values())

    def languages(self, top: int = 3) -> list[tuple[str, int]]:
        """The main languages of source and test files, as whole percentages."""
        counts = Counter(info.language for info in self.files.values() if info.kind in ("source", "test"))
        total = sum(counts.values())
        if not total:
            return []
        return [(language, round(100 * n / total)) for language, n in counts.most_common(top) if language]

    def folder(self, folder: str) -> tuple[dict[str, list[FileInfo]], list[FileInfo]]:
        """The subfolders (name → every file below it) and the files directly in ``folder``."""
        # lc-debt: scans every indexed path per folder opened; fine to ~10^5 files. Upgrade: a prefix tree built once.
        prefix = f"{folder}/" if folder else ""
        subfolders: dict[str, list[FileInfo]] = defaultdict(list)
        files: list[FileInfo] = []
        for path, info in self.files.items():
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix) :]
            head, sep, _ = rest.partition("/")
            if sep:
                subfolders[head].append(info)
            else:
                files.append(info)
        return dict(sorted(subfolders.items())), files

    def most_imported(self, top: int = _MOST_IMPORTED) -> list[tuple[str, int]]:
        ranked = sorted(((len(sources), path) for path, sources in self.importers.items()), reverse=True)
        return [(path, count) for count, path in ranked[:top] if count > 1]

    def symbols(self, path: str) -> list[tuple[int, Symbol]]:
        """A file's symbols in line order with their extents, each with its nesting depth."""
        rows = self.db.execute(
            "SELECT name, kind, line, signature FROM main.symbols WHERE path = ? ORDER BY line, name", (path,)
        ).fetchall()
        extents = _Extents(path, self._read(path))
        nested: list[tuple[int, Symbol]] = []
        open_ends: list[int] = []
        for row in rows:
            name, kind, line = str(row["name"]), str(row["kind"]), int(row["line"])
            symbol = Symbol(name, kind, path, line, extents.end(name, line), str(row["signature"] or ""))
            while open_ends and symbol.line > open_ends[-1]:
                open_ends.pop()
            nested.append((len(open_ends), symbol))
            if symbol.end_line > symbol.line:
                open_ends.append(symbol.end_line)
        return nested

    def search(self, query: str) -> list[Symbol]:
        records = self.engine.search_symbols(query, limit=_SEARCH_LIMIT)
        return [Symbol(r.symbol_name, r.kind, r.file_path, int(r.start_line), int(r.start_line), "") for r in records]

    def source(self, symbol: Symbol) -> tuple[int, list[str]]:
        """Where the symbol ends, and its source lines (at most a few hundred)."""
        extents = _Extents(symbol.path, self._read(symbol.path))
        if not extents.lines:
            return symbol.line, []
        end = extents.end(symbol.name, symbol.line)
        return end, extents.lines[symbol.line - 1 : min(end, symbol.line + _MAX_SOURCE_LINES - 1)]

    def mentions(self, symbol: Symbol) -> tuple[list[Mention], bool]:
        """Other lines that name ``symbol`` as a whole word, and whether the search stopped at its limit."""
        found = self.engine.search_text(symbol.name, limit=_MENTION_LIMIT, ignore_case=False, whole_word=True)
        mentions = [
            Mention(str(match.file_path), int(match.line), str(match.text).strip())
            for match in found
            if (str(match.file_path), int(match.line)) != (symbol.path, symbol.line)
        ]
        return mentions, len(found) >= _MENTION_LIMIT

    def lines(self, path: str) -> list[str]:
        return self._read(path).splitlines()

    def changes(self) -> list[Change]:
        """What ``git status`` lists for this worktree, in its order; empty when git can't answer."""
        try:
            result = subprocess.run(
                ["git", "-c", "core.quotepath=false", "status", "--porcelain=v1", "-z"],
                cwd=self.root,
                capture_output=True,
                timeout=_GIT_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if result.returncode != 0:
            return []
        entries = result.stdout.decode("utf-8", errors="replace").split("\0")
        changes: list[Change] = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if len(entry) < 4:
                continue
            code, path = entry[:2], entry[3:]
            if "R" in code or "C" in code:
                index += 1  # the original path follows a rename or copy
            changes.append(Change(path, _change_status(code)))
        return changes

    def _read(self, path: str) -> str:
        try:
            return (self.root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


def _change_status(code: str) -> str:
    if code == "??" or "A" in code:
        return "new"
    for letter, status in (("U", "conflict"), ("D", "deleted"), ("R", "renamed"), ("C", "copied")):
        if letter in code:
            return status
    return "modified"


_OPEN: Final[str] = "([{"
_CLOSE: Final[str] = ")]}"
_SCAN_LINES: Final[int] = 5000


class _Extents:
    """Where each symbol of one file ends. The Lite index stores only where it starts.

    Python definitions take their AST extent. Elsewhere a symbol runs until the
    brackets its first lines open balance again (``{`` on the next line counts,
    for Allman-style braces); failing that, it runs over the lines indented
    deeper than it, plus a closing ``end``. Strings and comments are not
    parsed, so a stray bracket in one can shift an end.
    """

    def __init__(self, path: str, text: str) -> None:
        self.lines = text.splitlines()
        self._python: dict[tuple[str, int], int] = {}
        if PurePosixPath(path).suffix in (".py", ".pyi"):
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                tree = None
            for node in ast.walk(tree) if tree is not None else ():
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and node.end_lineno:
                    self._python[(node.name, node.lineno)] = node.end_lineno

    def end(self, name: str, line: int) -> int:
        lines = self.lines
        if not 1 <= line <= len(lines):
            return line
        if (name, line) in self._python:
            return max(line, self._python[(name, line)])
        depth = 0
        for index in range(line - 1, min(len(lines), line - 1 + _SCAN_LINES)):
            text = lines[index]
            depth += sum(text.count(c) for c in _OPEN) - sum(text.count(c) for c in _CLOSE)
            if depth <= 0:
                following = lines[index + 1].lstrip() if index + 1 < len(lines) else ""
                if not following.startswith("{"):
                    break
        else:
            return line
        if index + 1 > line:
            return index + 1
        return self._indented_end(line)

    def _indented_end(self, line: int) -> int:
        lines = self.lines
        indent = _indent(lines[line - 1])
        end = line
        for index in range(line, min(len(lines), line + _SCAN_LINES)):
            text = lines[index]
            if not text.strip():
                continue
            if _indent(text) > indent:
                end = index + 1
                continue
            if end > line and text.strip() in ("end", "end;", "End", "END"):
                end = index + 1
            break
        return end


def _indent(text: str) -> int:
    return len(text.expandtabs(4)) - len(text.expandtabs(4).lstrip())


# ── the picker ──────────────────────────────────────────────────────────────────────


def _summary(code_map: CodeMap) -> str:
    parts = [style.plural(len(code_map.files), "file"), style.plural(code_map.symbol_count, "symbol")]
    parts += [f"{language} {share}%" for language, share in code_map.languages()]
    return " · ".join(parts)


def _parent(path: str) -> str:
    parent = str(PurePosixPath(path).parent)
    return "" if parent == "." else parent


def _right(rows: list[Row]) -> list[Row]:
    """Rows with their values right-aligned to one another: counts read down a column."""
    width = max((len(row.value) for row in rows if row.selectable), default=0)
    return [replace(row, value=row.value.rjust(width)) if row.selectable and row.value else row for row in rows]


def _signature_tail(symbol: Symbol) -> str:
    """What a signature says after the name: ``(key: str) -> str`` for ``def get(self, key: str) -> str:``.

    The index keeps a signature's first line, so one cut off mid-list ends in ``…``.
    """
    signature = " ".join(symbol.signature.split())
    at = signature.find(symbol.name)
    if at < 0:
        return ""
    tail = signature[at + len(symbol.name) :].rstrip(" :{=;").strip()
    for receiver in ("self", "cls"):
        if tail.startswith(f"({receiver}, ") or tail.startswith(f"({receiver})"):
            tail = "(" + tail[len(receiver) + 1 :].removeprefix(", ")
    return f"{tail}…" if tail.endswith(("(", ",")) else tail


def highlight(path: str, lines: list[str]) -> list[tuple[Fragment, ...]]:
    """``lines`` as styled pieces, coloured by the pygments lexer for ``path``; plain text when none fits."""
    plain: list[tuple[Fragment, ...]] = [(("", line),) for line in lines]
    try:
        from prompt_toolkit.styles.pygments import pygments_token_to_classname
        from pygments.lexers import get_lexer_for_filename
        from pygments.util import ClassNotFound
    except ImportError:
        return plain
    try:
        lexer = get_lexer_for_filename(path, stripnl=False, stripall=False, ensurenl=False)
    except ClassNotFound:
        return plain
    out: list[list[Fragment]] = [[]]
    for token, value in lexer.get_tokens("\n".join(lines)):
        css = f"class:{pygments_token_to_classname(token)}"
        for index, part in enumerate(value.split("\n")):
            if index:
                out.append([])
            if part:
                out[-1].append((css, part))
    return [tuple(spans) for spans in (out + [[]] * len(lines))[: len(lines)]]


class MapSource:
    def __init__(self, code_map: CodeMap) -> None:
        self.map = code_map
        self.title = f"Code map  {code_map.root.name}"
        on_symbol = _on_symbol
        self._open = Key(
            "o",
            "o open in editor",
            self._edit,
            when=lambda row: bool(row.ref) and row.ref[0] in ("file", "unindexed", "symbol", "mention"),
        )
        self._mentions = Key("u", "u mentions", self._mentions_of, when=on_symbol)
        self._file = Key("f", "f file", self._file_of, when=on_symbol)

    # ── pages ──

    def home(self) -> Page:
        return Page("", self._home_rows, keys=(self._open,), empty="This index has no files yet.")

    def search(self) -> Page:
        keys = (self._open, self._mentions, self._file)
        return Page("Search", self._search_rows, searchable=True, keys=keys, empty="No symbol matches that.")

    def _home_rows(self, _query: str) -> list[Row]:
        code_map = self.map
        subfolders, top_files = code_map.folder("")
        rows = [note(_summary(code_map)), GAP]
        changes = code_map.changes()
        if changes:
            rows.append(heading(f"Changed here ({len(changes)})"))
            for change in changes[:_CHANGES_SHOWN]:
                rows.append(self._change_row(change))
            if len(changes) > _CHANGES_SHOWN:
                rows.append(note(f"{len(changes) - _CHANGES_SHOWN} more: git status lists them all"))
            rows.append(GAP)
        rows.append(heading("Folders"))
        folders = [
            self._folder_row(name, name, files)
            for name, files in sorted(subfolders.items(), key=lambda item: (-len(item[1]), item[0]))
        ]
        if top_files:
            folders.append(Row("Top-level files", style.plural(len(top_files), "file"), ref=("folder", "")))
        rows += _right(folders)
        imported = code_map.most_imported()
        if imported:
            rows += [GAP, heading("Most imported")]
            rows += _right(
                [Row(PurePosixPath(p).name, f"{count} importers", _parent(p), ref=("file", p)) for p, count in imported]
            )
        rows += [GAP, Row("Search symbols", "/", "Find a function, class or method by name", ref=("search",))]
        return rows

    def _change_row(self, change: Change) -> Row:
        path = change.path.rstrip("/")
        if change.path.endswith("/"):
            ref: tuple[str, ...] = ("folder", path)
            label = f"{PurePosixPath(path).name}/"
        else:
            if change.status == "deleted":
                ref = ("gone", path)
            else:
                ref = ("file", path) if path in self.map.files else ("unindexed", path)
            label = PurePosixPath(path).name
        return Row(label, change.status, _parent(path), ref=ref)

    def _folder_row(self, label: str, path: str, files: list[FileInfo]) -> Row:
        # A folder holding one folder and nothing else reads as one step: src/lemoncrow/.
        while True:
            subfolders, direct = self.map.folder(path)
            if direct or len(subfolders) != 1:
                break
            only = next(iter(subfolders))
            label, path = f"{label}/{only}", f"{path}/{only}"
        symbols = sum(info.symbols for info in files)
        languages = Counter(info.language for info in files if info.kind in ("source", "test") and info.language)
        main = languages.most_common(1)[0][0] if languages else ""
        note_text = " · ".join(part for part in (style.plural(symbols, "symbol") if symbols else "", main) if part)
        return Row(f"{label}/", style.plural(len(files), "file"), note_text, ref=("folder", path))

    def _folder_rows(self, folder: str) -> list[Row]:
        subfolders, files = self.map.folder(folder)
        prefix = f"{folder}/" if folder else ""
        rows = _right([self._folder_row(name, prefix + name, below) for name, below in subfolders.items()])
        if rows and files:
            rows.append(GAP)
        file_rows = []
        for info in files:
            importers = len(self.map.importers.get(info.path, ()))
            language = info.language if info.kind != "other" else ""
            note_text = " · ".join(part for part in (language, f"imported by {importers}" if importers else "") if part)
            value = style.plural(info.symbols, "symbol") if info.symbols else ""
            file_rows.append(Row(PurePosixPath(info.path).name, value, note_text, ref=("file", info.path)))
        return rows + _right(file_rows)

    def _file_rows(self, path: str) -> list[Row]:
        info = self.map.files.get(path)
        facts = [info.language] if info and info.language else []
        imports = sorted(self.map.imports.get(path, ()))
        importers = sorted(self.map.importers.get(path, ()))
        rows = [note(" · ".join([path, *facts])), GAP]
        symbols = self.map.symbols(path)
        rows.append(heading(f"Symbols ({len(symbols)})" if symbols else "No symbols indexed"))
        containers: list[str] = []  # the kinds of the symbols each row sits inside, outermost first
        for depth, symbol in symbols:
            del containers[depth:]
            inside_class = bool(containers) and containers[-1] == "class"
            kind = (
                "method" if symbol.kind == "function" and inside_class else _KIND_LABELS.get(symbol.kind, symbol.kind)
            )
            rows.append(Row("  " * depth + symbol.name, kind, _signature_tail(symbol), ref=("symbol", symbol)))
            if symbol.end_line > symbol.line:
                containers.append(symbol.kind)
        for title, paths in (("Imports", imports), ("Imported by", importers)):
            if paths:
                rows += [GAP, heading(f"{title} ({len(paths)})")]
                rows += [Row(PurePosixPath(p).name, "", _parent(p), ref=("file", p)) for p in paths]
        return rows

    def _code_rows(self, path: str, first: int, lines: list[str], target: int = 0) -> list[Row]:
        """Numbered, highlighted lines from ``first``; ``target`` is marked with the pointer."""
        lines = [line.expandtabs(4) for line in lines]
        width = len(str(first + max(len(lines) - 1, 0)))
        rows = []
        for offset, spans in enumerate(highlight(path, lines)):
            number = first + offset
            gutter: Fragment = (
                ("class:target", f"{POINTER} {number:>{width}}  ")
                if number == target
                else ("class:gutter", f"  {number:>{width}}  ")
            )
            rows.append(Row(f"{number:>{width}}  {lines[offset]}", spans=(gutter, *spans)))
        return rows

    def _source_rows(self, symbol: Symbol) -> list[Row]:
        end, lines = self.map.source(symbol)
        if not lines:
            return [Row("The file can't be read.", style="class:dim")]
        span = f"{symbol.line}-{end}" if end > symbol.line else str(symbol.line)
        rows = [Row(f"{symbol.path}:{span}", style="class:dim"), Row("")]
        rows += self._code_rows(symbol.path, symbol.line, lines)
        hidden = end - symbol.line + 1 - len(lines)
        if hidden > 0:
            rows += [Row(""), Row(f"{hidden:,} more lines: o opens the whole symbol in your editor", style="class:dim")]
        return rows

    def _mention_rows(self, symbol: Symbol) -> list[Row]:
        mentions, cut = self.map.mentions(symbol)
        rows = [note(f"Lines naming {symbol.name}: a text search, so a different thing with that name shows too."), GAP]
        by_file: dict[str, list[Mention]] = defaultdict(list)
        for mention in mentions:
            by_file[mention.path].append(mention)
        width = max((len(str(mention.line)) for mention in mentions), default=1)
        for path, found in by_file.items():
            rows.append(heading(path))
            rows += [Row(str(m.line).rjust(width), "", m.text, ref=("mention", m)) for m in found]
        if cut:
            rows += [GAP, note(f"The first {_MENTION_LIMIT} matches.")]
        return rows

    def _context_rows(self, mention: Mention) -> list[Row]:
        lines = self.map.lines(mention.path)
        if not lines:
            return [Row("The file can't be read.", style="class:dim")]
        first = max(mention.line - _CONTEXT_LINES, 1)
        last = min(mention.line + _CONTEXT_LINES, len(lines))
        rows = [Row(f"{mention.path}:{mention.line}", style="class:dim"), Row("")]
        return rows + self._code_rows(mention.path, first, lines[first - 1 : last], target=mention.line)

    def _search_rows(self, query: str) -> list[Row]:
        if not query.strip():
            return [note("Type part of a name: a function, class or method.")]
        return [
            Row(
                symbol.name,
                _KIND_LABELS.get(symbol.kind, symbol.kind),
                f"{symbol.path}:{symbol.line}",
                ref=("symbol", symbol),
            )
            for symbol in self.map.search(query)
        ]

    # ── actions ──

    def open(self, page: Page, row: Row) -> Outcome | None:
        kind = row.ref[0]
        if kind == "folder":
            folder = row.ref[1]
            title = row.label.rstrip("/") if folder else "Top level"
            return Outcome(page=Page(title, lambda _q: self._folder_rows(folder), keys=(self._open,)))
        if kind == "file":
            return Outcome(page=self._file_page(row.ref[1]))
        if kind == "symbol":
            symbol: Symbol = row.ref[1]
            keys = (self._open, self._mentions, self._file)
            return Outcome(
                page=Page(symbol.name, lambda _q: self._source_rows(symbol), text=True, keys=keys, ref=row.ref)
            )
        if kind == "mention":
            mention: Mention = row.ref[1]
            title = f"{PurePosixPath(mention.path).name}:{mention.line}"
            return Outcome(
                page=Page(title, lambda _q: self._context_rows(mention), text=True, keys=(self._open,), ref=row.ref)
            )
        if kind == "search":
            return Outcome(page=self.search())
        if kind == "gone":
            return Outcome(message=("off", "That file is deleted in this worktree."))
        if kind == "unindexed":
            return Outcome(message=("off", "Not indexed yet. o opens it in your editor."))
        return None

    def _file_page(self, path: str) -> Page:
        keys = (self._open, self._mentions)
        return Page(PurePosixPath(path).name, lambda _q: self._file_rows(path), keys=keys)

    def submit(self, edit: Any, text: str) -> Outcome:
        return Outcome()

    def _edit(self, page: Page, row: Row) -> Outcome | None:
        kind = row.ref[0]
        if kind in ("file", "unindexed"):
            path, line = row.ref[1], 1
        elif kind in ("symbol", "mention"):
            path, line = row.ref[1].path, row.ref[1].line
        else:
            return None
        command = editor_command(self.map.root / path, line)
        return Outcome(run=lambda: _run_editor(command))

    def _mentions_of(self, page: Page, row: Row) -> Outcome:
        symbol: Symbol = row.ref[1]
        return Outcome(
            page=Page(
                f"Mentions of {symbol.name}",
                lambda _q: self._mention_rows(symbol),
                keys=(self._open,),
                empty="Nothing else names it.",
            )
        )

    def _file_of(self, page: Page, row: Row) -> Outcome:
        return Outcome(page=self._file_page(row.ref[1].path))

    # ── footer ──

    def describe(self, page: Page, row: Row | None, width: int) -> list[Line]:
        if row is None:
            return []
        kind = row.ref[0]
        if kind == "folder":
            folder = row.ref[1]
            subfolders, files = self.map.folder(folder)
            total = len(files) + sum(len(below) for below in subfolders.values())
            return describe_lines("", [folder or "top level", style.plural(total, "file")], width)
        if kind in ("file", "gone", "unindexed"):
            path = row.ref[1]
            info = self.map.files.get(path)
            facts = [path]
            if info is not None:
                imports, importers = len(self.map.imports.get(path, ())), len(self.map.importers.get(path, ()))
                facts.append(info.language or info.kind)
                facts += [style.plural(info.symbols, "symbol")] if info.symbols else []
                facts += [f"imports {imports}"] if imports else []
                facts += [f"imported by {importers}"] if importers else []
            return describe_lines("", facts, width)
        if kind == "symbol":
            symbol = row.ref[1]
            span = f"{symbol.line}-{symbol.end_line}" if symbol.end_line > symbol.line else str(symbol.line)
            facts = [f"{symbol.path}:{span}", symbol.kind]
            return describe_lines(symbol.signature.strip(), facts, width, max_lines=1)
        if kind == "mention":
            mention = row.ref[1]
            return describe_lines(mention.text, [f"{mention.path}:{mention.line}"], width, max_lines=1)
        return []


def _on_symbol(row: Row) -> bool:
    return bool(row.ref) and row.ref[0] == "symbol"


def editor_command(path: Path, line: int) -> list[str]:
    """``$VISUAL`` / ``$EDITOR`` (else ``vi``) opened at ``path:line``, in the spelling each editor takes."""
    editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
    name = Path(editor[0]).name
    if name in {"code", "code-insiders", "codium", "cursor", "windsurf"}:
        return [*editor, "-g", f"{path}:{line}"]
    if name in {"zed", "subl", "hx", "helix"}:
        return [*editor, f"{path}:{line}"]
    return [*editor, f"+{line}", str(path)]


def _run_editor(command: list[str]) -> None:
    try:
        subprocess.run(command, check=False)
    except OSError as exc:
        print(f"Could not start {command[0]}: {exc}")


# ── plain output ─────────────────────────────────────────────────────────────────────


def overview_lines(code_map: CodeMap) -> list[str]:
    """The map's front page as plain lines: summary, biggest folders, most imported files."""
    lines = [style.title("Code map", code_map.root.name), "", f"  {_summary(code_map)}"]
    subfolders, top_files = code_map.folder("")
    folders = sorted(subfolders.items(), key=lambda item: (-len(item[1]), item[0]))
    if folders or top_files:
        lines += ["", "  " + style.heading("Folders")]
        rows = [(f"{name}/", style.plural(len(files), "file")) for name, files in folders]
        if top_files:
            rows.append(("(top level)", style.plural(len(top_files), "file")))
        width = max(len(label) for label, _ in rows)
        value_width = max(len(value) for _, value in rows)
        lines += [f"    {label.ljust(width)}  {value.rjust(value_width)}" for label, value in rows]
    imported = code_map.most_imported()
    if imported:
        lines += ["", "  " + style.heading("Most imported")]
        width = max(len(path) for path, _ in imported)
        lines += [f"    {path.ljust(width)}  {style.dim(f'{count} importers')}" for path, count in imported]
    lines += ["", style.next_step("Explore it:", "lc rt map") + style.dim("  (in a terminal)")]
    return lines


__all__ = [
    "Change",
    "CodeMap",
    "FileInfo",
    "MapSource",
    "Mention",
    "Symbol",
    "editor_command",
    "highlight",
    "overview_lines",
]

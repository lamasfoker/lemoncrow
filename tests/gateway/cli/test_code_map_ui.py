"""`lc rt map`: symbol extents, the map's pages, the plain overview, the editor hand-off and the command."""

from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.gateway.cli.code_map_ui import (
    CodeMap,
    MapSource,
    Symbol,
    _Extents,
    _signature_tail,
    editor_command,
    highlight,
    overview_lines,
)
from lemoncrow.gateway.cli.picker import POINTER, Picker, cells, fit_spans

PYTHON = '''import pkg.b


class Store:
    """Doc."""

    def get(self, key):
        return key

    def set(self, key, value):
        self.value = value


LIMIT = {
    "a": 1,
}
'''

TYPESCRIPT = """export function add(a: number,
                    b: number): number {
  return a + b;
}

export const ONE = 1;
"""

SYMBOLS = [
    ("Store", "class", "pkg/a.py", 4),
    ("get", "function", "pkg/a.py", 7),
    ("set", "function", "pkg/a.py", 10),
    ("LIMIT", "binding", "pkg/a.py", 14),
    ("add", "function", "pkg/b.ts", 1),
    ("ONE", "binding", "pkg/b.ts", 6),
]

SIGNATURES = {
    ("pkg/a.py", 4): "class Store:",
    ("pkg/a.py", 7): "def get(self, key):",
    ("pkg/a.py", 10): "def set(self, key, value):",
    ("pkg/a.py", 14): "LIMIT = {",
    ("pkg/b.ts", 1): "export function add(a: number,",
    ("pkg/b.ts", 6): "export const ONE = 1;",
}

USE = """from pkg.a import Store

store = Store()
Storeroom = None
"""


@pytest.fixture(autouse=True)
def _no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")


class _Engine:
    """The slice of LiteCodeMapEngine a CodeMap reads."""

    def __init__(self, root: Path) -> None:
        self.repo_root = root

    def import_links(self, connection: sqlite3.Connection) -> Any:
        return SimpleNamespace(rows=[{"source_path": "pkg/a.py", "target_path": "pkg/b.ts"}], truncated=False)

    def search_symbols(self, query: str, *, limit: int) -> list[Any]:
        return [
            SimpleNamespace(symbol_name=name, kind=kind, file_path=path, start_line=line, end_line=line)
            for name, kind, path, line in SYMBOLS
            if query.lower() in name.lower()
        ]

    def search_text(self, query: str, *, limit: int, ignore_case: bool, whole_word: bool) -> list[Any]:
        assert not ignore_case and whole_word
        word = re.compile(rf"\b{re.escape(query)}\b")
        return [
            SimpleNamespace(file_path=path.relative_to(self.repo_root).as_posix(), line=number, text=text)
            for path in sorted(self.repo_root.rglob("*.py"))
            for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if word.search(text)
        ][:limit]


@pytest.fixture
def code_map(tmp_path: Path) -> CodeMap:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text(PYTHON, encoding="utf-8")
    (tmp_path / "pkg" / "b.ts").write_text(TYPESCRIPT, encoding="utf-8")
    (tmp_path / "pkg" / "use.py").write_text(USE, encoding="utf-8")
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE files (path TEXT)")
    db.execute("CREATE TABLE symbols (name TEXT, kind TEXT, path TEXT, line INT, end_line INT, signature TEXT)")
    db.executemany("INSERT INTO files VALUES (?)", [("pkg/a.py",), ("pkg/b.ts",), ("README.md",)])
    # The Lite index stores where a symbol starts only: end_line == line.
    db.executemany(
        "INSERT INTO symbols VALUES (?, ?, ?, ?, ?, ?)",
        [(n, k, p, line, line, SIGNATURES[(p, line)]) for n, k, p, line in SYMBOLS],
    )
    return CodeMap(_Engine(tmp_path), db)


# ── extents ──


def test_python_definitions_take_their_ast_extent_and_bindings_their_brackets() -> None:
    extents = _Extents("pkg/a.py", PYTHON)
    assert extents.end("Store", 4) == 11
    assert extents.end("get", 7) == 8
    assert extents.end("set", 10) == 11
    assert extents.end("LIMIT", 14) == 16


def test_brace_languages_run_until_their_braces_balance() -> None:
    extents = _Extents("pkg/b.ts", TYPESCRIPT)
    assert extents.end("add", 1) == 4
    assert extents.end("ONE", 6) == 6
    allman = _Extents("main.c", "int main(void)\n{\n    return 0;\n}\n")
    assert allman.end("main", 1) == 4


def test_other_languages_fall_back_to_indentation_and_a_closing_end() -> None:
    extents = _Extents("lib.rb", "def greet(name)\n  puts name\nend\n\ndef other\nend\n")
    assert extents.end("greet", 1) == 3


def test_a_line_outside_the_file_is_its_own_extent() -> None:
    assert _Extents("x.py", "a = 1\n").end("gone", 40) == 40


# ── the map ──


def test_a_files_symbols_nest_by_their_extents(code_map: CodeMap) -> None:
    nested = [(depth, symbol.name, symbol.end_line) for depth, symbol in code_map.symbols("pkg/a.py")]
    assert nested == [(0, "Store", 11), (1, "get", 8), (1, "set", 11), (0, "LIMIT", 16)]


def test_the_picker_walks_folders_files_symbols_and_source(code_map: CodeMap) -> None:
    picker = Picker(MapSource(code_map))
    labels = [row.label for row in picker.page.rows]
    assert "pkg/" in labels
    assert "Top-level files" in labels

    picker.page.cursor = labels.index("pkg/")
    picker.activate()
    assert [row.label for row in picker.page.rows if row.selectable] == ["a.py", "b.ts"]

    picker.activate()  # a.py
    rows = [row.label for row in picker.page.rows]
    assert "  get" in rows and "Store" in rows
    assert "Imports (1)" in rows

    picker.page.cursor = rows.index("  get")
    picker.activate()
    text = [row.label for row in picker.page.rows]
    assert text[0] == "pkg/a.py:7-8"
    assert any("def get(self, key):" in line for line in text)
    assert any("return key" in line for line in text)
    assert not any("def set" in line for line in text)


def test_a_searched_class_shows_its_whole_body(code_map: CodeMap) -> None:
    picker = Picker(MapSource(code_map))
    picker.start_search()
    picker.set_query("stor")
    assert [row.label for row in picker.page.rows] == ["Store"]
    picker.activate()
    text = "\n".join(row.label for row in picker.page.rows)
    assert "pkg/a.py:4-11" in text
    assert "def set(self, key, value):" in text


def test_a_files_symbols_read_as_methods_and_variables_with_their_signatures(code_map: CodeMap) -> None:
    rows = {row.label: (row.value, row.note) for row in MapSource(code_map)._file_rows("pkg/a.py") if row.selectable}
    assert rows["Store"] == ("class", "")
    assert rows["  get"] == ("method", "(key)")
    assert rows["  set"] == ("method", "(key, value)")
    assert rows["LIMIT"] == ("variable", "")


def test_a_signature_reads_from_after_its_name() -> None:
    for name, signature, tail in [
        ("get", "def get(self, key: str) -> str:", "(key: str) -> str"),
        ("make", "def make(cls) -> Store:", "() -> Store"),
        ("config", "const config: Config = {", ": Config"),
        ("add", "export function add(a: number,", "(a: number,…"),
        ("selfish", "def selfish(selfish):", "(selfish)"),
        ("gone", "def other():", ""),
    ]:
        assert _signature_tail(Symbol(name, "function", "x", 1, 1, signature)) == tail


def test_source_is_highlighted_with_a_line_number_gutter(code_map: CodeMap) -> None:
    picker = Picker(MapSource(code_map))
    picker.start_search()
    picker.set_query("get")
    picker.activate()
    code = [row for row in picker.page.rows if row.spans]
    assert [row.label for row in code] == ["7      def get(self, key):", "8          return key"]
    first = code[0].spans
    assert first[0] == ("class:gutter", "  7  ")
    assert ("class:pygments.keyword", "def") in first
    assert "".join(text for _, text in first[1:]) == "    def get(self, key):"


def test_highlighting_keeps_every_line_and_falls_back_to_plain_text() -> None:
    lines = ['s = """a', 'b"""', "", "x = 1"]
    spans = highlight("m.py", lines)
    assert ["".join(text for _, text in line) for line in spans] == lines
    assert highlight("notes.unknown-extension", ["a", "b"]) == [(("", "a"),), (("", "b"),)]


def test_spans_fit_their_width_with_an_ellipsis() -> None:
    spans = [("class:a", "abcd"), ("class:b", "efgh")]
    assert fit_spans(spans, 8) == spans
    cut = fit_spans(spans, 6)
    assert "".join(text for _, text in cut) == "abcde…"
    assert sum(cells(text) for _, text in cut) == 6
    assert cut[0] == ("class:a", "abcd")


def test_mentions_list_other_lines_naming_a_symbol_and_open_in_context(code_map: CodeMap) -> None:
    picker = Picker(MapSource(code_map))
    picker.start_search()
    picker.set_query("stor")
    assert picker.press("u")
    assert picker.page.title == "Mentions of Store"
    rows = picker.page.rows
    assert [row.label for row in rows if row.kind == "heading"] == ["pkg/use.py"]
    assert [(row.label, row.note) for row in rows if row.selectable] == [
        ("1", "from pkg.a import Store"),
        ("3", "store = Store()"),
    ]  # not the definition, not Storeroom

    picker.page.cursor = next(i for i, row in enumerate(rows) if row.label == "3")
    picker.activate()
    assert picker.page.title == "use.py:3"
    marked = [row for row in picker.page.rows if row.spans and row.spans[0][0] == "class:target"]
    assert [row.label for row in marked] == ["3  store = Store()"]
    assert marked[0].spans[0][1].startswith(POINTER)


def test_a_symbols_source_page_opens_its_mentions_and_its_file(code_map: CodeMap) -> None:
    picker = Picker(MapSource(code_map))
    picker.start_search()
    picker.set_query("stor")
    picker.activate()
    assert "u mentions" in picker.hints(40) and "f file" in picker.hints(40)
    assert picker.press("f")
    assert picker.page.title == "a.py"
    picker.back()
    assert picker.press("u")
    assert picker.page.title == "Mentions of Store"


def test_home_lists_what_changed_in_the_worktree(code_map: CodeMap) -> None:
    root = code_map.root
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    (root / "pkg" / "a.py").write_text(PYTHON + "\nX = 1\n", encoding="utf-8")
    (root / "README.md").unlink()
    (root / "pkg" / "new.py").write_text("y = 2\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("# g\n", encoding="utf-8")
    subprocess.run([*git, "mv", "pkg/use.py", "pkg/used.py"], check=True)

    picker = Picker(MapSource(code_map))
    rows = picker.page.rows
    start = next(i for i, row in enumerate(rows) if row.label.startswith("Changed here"))
    assert rows[start].label == "Changed here (5)"
    changed = {row.label: (row.value, row.ref[0]) for row in rows[start + 1 : start + 6]}
    assert changed == {
        "README.md": ("deleted", "gone"),
        "a.py": ("modified", "file"),
        "used.py": ("renamed", "unindexed"),
        "docs/": ("new", "folder"),
        "new.py": ("new", "unindexed"),
    }

    picker.page.cursor = start + 1 + list(changed).index("README.md")
    picker.activate()
    assert picker.message == ("off", "That file is deleted in this worktree.")


def test_home_has_no_changes_section_outside_git(code_map: CodeMap) -> None:
    labels = [row.label for row in Picker(MapSource(code_map)).page.rows]
    assert not any(label.startswith("Changed here") for label in labels)


def test_the_plain_overview_lists_folders_and_most_imported(code_map: CodeMap) -> None:
    text = "\n".join(overview_lines(code_map))
    assert "3 files · 6 symbols" in text
    assert "pkg/" in text
    assert "lc rt map" in text


@pytest.mark.parametrize(
    ("editor", "expected"),
    [
        ("code --wait", ["code", "--wait", "-g", "/r/a.py:7"]),
        ("hx", ["hx", "/r/a.py:7"]),
        ("vim", ["vim", "+7", "/r/a.py"]),
    ],
)
def test_the_editor_opens_at_the_line_in_its_own_spelling(
    monkeypatch: pytest.MonkeyPatch, editor: str, expected: list[str]
) -> None:
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", editor)
    assert editor_command(Path("/r/a.py"), 7) == expected


# ── the command ──


def test_map_off_a_terminal_prints_the_overview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    monkeypatch.setenv("LEMONCROW_WORKSPACE_ROOT", str(repo))

    result = CliRunner().invoke(cli, ["--root", str(tmp_path / "state"), "runtime", "map"])

    assert result.exit_code == 0, result.output
    assert "Code map" in result.output
    assert "src/" in result.output


def test_map_outside_a_repository_says_where_to_run_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.core.foundation import paths

    def nowhere() -> Path:
        raise paths.WorkspaceNotRegisteredError("no")

    monkeypatch.setattr(paths, "resolve_workspace_root", nowhere)

    result = CliRunner().invoke(cli, ["--root", str(tmp_path), "runtime", "map"])

    assert result.exit_code == 2
    assert "Not inside a git repository" in result.output
    assert "lc rt map" in result.output

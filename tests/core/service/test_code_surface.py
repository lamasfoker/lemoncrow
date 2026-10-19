from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lemoncrow.core.service import code_map, code_surface


class _Provider:
    """Minimal provider double: file links, one neighborhood, readiness."""

    def __init__(
        self,
        edges: list[dict[str, Any]],
        neighborhood: Mapping[str, Any] | None = None,
        digests: Mapping[str, str] | None = None,
    ) -> None:
        self.edges = edges
        self._neighborhood = neighborhood or {"nodes": [], "edges": []}
        self._digests = digests
        self.neighborhood_requests: list[dict[str, Any]] = []

    def readiness(self) -> Any:
        return SimpleNamespace(to_wire=lambda: {"backend": "native", "graph": {"state": "ready"}})

    def file_relationships(self, paths: tuple[str, ...], *, limit: int = 320) -> Mapping[str, Any]:
        seeds = set(paths)
        rows = [row for row in self.edges if row["source_path"] in seeds or row["target_path"] in seeds]
        return {"edges": rows[:limit], "degraded": False, "degraded_reason": "", "truncated": False}

    def repository_file_links(self) -> Mapping[str, Any]:
        return {"edges": self.edges, "degraded": False, "degraded_reason": "", "truncated": False}

    def neighborhood(self, symbol: Mapping[str, Any], *, depth: int = 1, limit: int = 40) -> Mapping[str, Any]:
        self.neighborhood_requests.append(dict(symbol))
        return {**self._neighborhood, "outgoing_attributed": "span_start" in symbol}

    def __getattr__(self, name: str) -> Any:
        if name == "path_digest" and self._digests is not None:
            digests = self._digests
            return lambda path: digests.get(path, "")
        raise AttributeError(name)


def _row(source: str, target: str, kind: str = "calls", weight: int = 1, symbol: str = "") -> dict[str, Any]:
    return {
        "source_path": source,
        "target_path": target,
        "kind": kind,
        "weight": weight,
        "symbols": [{"name": symbol, "count": weight}] if symbol else [],
        "evidence": [{"path": source, "line": 7, "symbol": symbol}] if symbol else [],
    }


# --------------------------------------------------------------------------- #
# Modules                                                                     #
# --------------------------------------------------------------------------- #


def test_module_partition_splits_large_folders_keeps_packages_and_merges_tiny_ones() -> None:
    paths = [f"src/app/core/file{index}.py" for index in range(120)]
    paths += [f"src/app/api/route{index}.py" for index in range(60)]
    paths += ["src/app/__init__.py", "src/app/lonely/one.py"]
    paths += ["web/package.json"] + [f"web/src/view{index}.ts" for index in range(10)]
    paths += [f"vendor/lib/deep/x{index}.js" for index in range(30)]
    paths += ["README.md", "pyproject.toml"]
    # A fixture's own manifest deep in the tree is not how the repo is organized.
    paths += [f"tests/fixtures/sample/project/pkg/f{index}.py" for index in range(4)]
    paths += ["tests/fixtures/sample/project/pkg/pyproject.toml"]

    modules = code_surface.module_partition(paths)

    # src/app (182 files) splits into its children; its own file stays with it.
    assert modules["src/app/core/file0.py"] == "src/app/core"
    assert modules["src/app/api/route3.py"] == "src/app/api"
    assert modules["src/app/__init__.py"] == "src/app"
    # A one-file folder merges into its parent.
    assert modules["src/app/lonely/one.py"] == "src/app"
    # A declared package is one module even when small.
    assert modules["web/src/view1.ts"] == "web"
    # Vendored code collapses into one card.
    assert modules["vendor/lib/deep/x1.js"] == "vendor"
    assert modules["README.md"] == ""
    assert modules["tests/fixtures/sample/project/pkg/f0.py"] == "tests"
    assert "tests/fixtures/sample/project/pkg" not in code_surface.package_roots(paths)


def test_tiny_repository_has_no_module_level() -> None:
    paths = ["main.py", "util/helpers.py", "README.md"]
    assert code_surface.module_partition(paths) == {path: path for path in paths}


def test_modules_overview_aggregates_plausible_module_links_and_purposes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = [f"api/h{index}.py" for index in range(5)] + [f"db/m{index}.py" for index in range(5)]
    paths += [f"web/v{index}.ts" for index in range(5)] + [f"tests/t{index}.py" for index in range(5)]
    (tmp_path / "db").mkdir(parents=True)
    (tmp_path / "db" / "README.md").write_text("# DB\n\nPersistence for accounts and sessions.\n")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text('{"description": "Browser client."}')
    monkeypatch.setattr(code_surface, "_MODULES_CACHE", {})
    provider = _Provider(
        [
            _row("api/h0.py", "db/m0.py", "calls", 3, "save"),
            _row("api/h1.py", "db/m1.py", "imports", 1),
            _row("api/h0.py", "api/h1.py", "calls", 9),  # inside one module
            _row("api/h0.py", "web/v0.ts", "calls", 4, "save"),  # name collision
            _row("db/m0.py", "tests/t0.py", "calls", 2),  # production -> test
        ]
    )
    engine = SimpleNamespace(connection=lambda: (_ for _ in ()).throw(AttributeError()))

    overview = code_surface.modules_overview(provider, engine, tmp_path, paths, {"db/m0.py": {"status": "M"}})

    by_id = {module["id"]: module for module in overview["modules"]}
    assert by_id["db"]["purpose"] == "Persistence for accounts and sessions."
    assert by_id["db"]["purpose_source"] == "db/README.md"
    assert by_id["web"]["purpose"] == "Browser client."
    assert by_id["db"]["changed"] == 1
    assert overview["edges"] == [
        {
            "source": "api",
            "target": "db",
            "weight": 4,
            "kinds": {"calls": 3, "imports": 1},
            "examples": [{"source_path": "api/h0.py", "target_path": "db/m0.py", "kind": "calls", "symbol": "save"}],
        }
    ]
    assert overview["filtered"] == {"cross_language": 1, "production_to_test": 1}
    assert overview["context"]["backend"] == "native"

    # Changed counts are overlaid per request on the cached structure.
    again = code_surface.modules_overview(provider, engine, tmp_path, paths, {})
    assert {module["id"]: module["changed"] for module in again["modules"]}["db"] == 0


def _module_repo(tmp_path: Path) -> list[str]:
    paths = [f"api/h{index}.py" for index in range(5)] + [f"db/m{index}.py" for index in range(5)]
    paths += [f"web/v{index}.ts" for index in range(5)] + [f"lib/l{index}.rs" for index in range(5)]
    for folder in ("api", "db", "web", "lib"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    (tmp_path / "db" / "README.md").write_text("# DB\n\nPersistence for accounts and sessions.\n")
    (tmp_path / "db" / "m0.py").write_text('"""Row mappers for the accounts table."""\n')
    (tmp_path / "api" / "h0.py").write_text('"""Shared request helpers for the handlers."""\n')
    (tmp_path / "api" / "h1.py").write_text(
        '# -*- coding: utf-8 -*-\n"""HTTP handlers for account signup and login."""\n'
    )
    (tmp_path / "web" / "v0.ts").write_text(
        "/* Copyright 2024 Acme Inc. All rights reserved. */\nexport const a = 1;\n"
    )
    (tmp_path / "web" / "v1.ts").write_text(
        "/**\n * @file Renders the account settings page.\n */\nexport const b = 2;\n"
    )
    (tmp_path / "lib" / "l0.rs").write_text("//! Checksums for stored blobs.\n\nfn sum() {}\n")
    return paths


def test_module_without_its_own_statement_takes_its_most_connected_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _module_repo(tmp_path)
    monkeypatch.setattr(code_surface, "_MODULES_CACHE", {})
    monkeypatch.setattr(code_surface, "_GENERATED_PURPOSES", {})
    # api/h1.py is api's most connected file, so its statement outranks h0's.
    provider = _Provider([_row("api/h1.py", "db/m1.py", "calls", 3, "save")])
    engine = SimpleNamespace(connection=lambda: (_ for _ in ()).throw(AttributeError()))

    by_id = {row["id"]: row for row in code_surface.modules_overview(provider, engine, tmp_path, paths)["modules"]}

    purposes = {key: (row["purpose"], row["purpose_source"], row["purpose_kind"]) for key, row in by_id.items()}
    assert purposes == {
        "api": ("HTTP handlers for account signup and login.", "api/h1.py", "file"),
        "db": ("Persistence for accounts and sessions.", "db/README.md", "declared"),
        # A license header is not a statement; the next file's doc comment is.
        "web": ("Renders the account settings page.", "web/v1.ts", "file"),
        "lib": ("Checksums for stored blobs.", "lib/l0.rs", "file"),
    }


def test_generated_purpose_is_labelled_and_lasts_only_while_the_module_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _module_repo(tmp_path)
    monkeypatch.setattr(code_surface, "_MODULES_CACHE", {})
    monkeypatch.setattr(code_surface, "_GENERATED_PURPOSES", {})
    provider = _Provider([])
    engine = SimpleNamespace(connection=lambda: (_ for _ in ()).throw(AttributeError()))
    asked: list[list[dict[str, Any]]] = []

    def chat(messages: list[dict[str, Any]]) -> str:
        asked.append(messages)
        return '"Serves the account HTTP API."\n'

    entry = code_surface.generate_module_purpose(
        engine, tmp_path, "api", paths, chat=chat, configured=("openai", "gpt-test")
    )

    assert entry["purpose"] == "Serves the account HTTP API."
    assert (entry["purpose_kind"], entry["purpose_source"]) == ("generated", "gpt-test")
    assert entry["purpose_evidence"] == [f"api/h{index}.py" for index in range(5)]
    evidence = asked[0][1]["content"]
    assert evidence.startswith("Module api: 5 files.")
    assert '- api/h1.py; states: "HTTP handlers for account signup and login."' in evidence

    # A declared purpose outranks a generated one.
    code_surface.generate_module_purpose(engine, tmp_path, "db", paths, chat=chat, configured=("openai", "gpt-test"))
    overview = code_surface.modules_overview(provider, engine, tmp_path, paths)
    by_id = {row["id"]: row for row in overview["modules"]}
    assert (by_id["api"]["purpose"], by_id["api"]["purpose_kind"]) == ("Serves the account HTTP API.", "generated")
    assert by_id["api"]["purpose_at"]
    assert by_id["db"]["purpose_kind"] == "declared"

    # A new file in the module retires the generated line.
    grown = {
        row["id"]: row
        for row in code_surface.modules_overview(provider, engine, tmp_path, [*paths, "api/h5.py"])["modules"]
    }
    assert grown["api"]["purpose_kind"] == "file"

    def unknown(messages: list[dict[str, Any]]) -> str:
        return "UNKNOWN"

    def down(messages: list[dict[str, Any]]) -> str:
        raise RuntimeError("connection refused")

    with pytest.raises(ValueError):
        code_surface.generate_module_purpose(engine, tmp_path, "lib", paths, chat=unknown, configured=("o", "m"))
    with pytest.raises(code_surface.PurposeUnavailable, match="connection refused"):
        code_surface.generate_module_purpose(engine, tmp_path, "lib", paths, chat=down, configured=("o", "m"))
    with pytest.raises(LookupError):
        code_surface.generate_module_purpose(engine, tmp_path, "nope", paths, chat=chat, configured=("o", "m"))
    monkeypatch.setattr("lemoncrow.infra.internal_llm.configured_model", lambda: None)
    with pytest.raises(code_surface.PurposeUnavailable, match="LEMONCROW_LLM_BACKEND"):
        code_surface.generate_module_purpose(engine, tmp_path, "lib", paths, chat=chat)
    assert code_surface.modules_overview(provider, engine, tmp_path, paths)["purpose_generation"] == {
        "available": False,
        "model": "",
    }


# --------------------------------------------------------------------------- #
# Hotspots                                                                    #
# --------------------------------------------------------------------------- #


def _ledger(store: Path, host: str, session_id: str, workspace: str, edits: list[tuple[str, datetime]]) -> Path:
    folder = store / "sessions" / "2026" / "09" / "27" / host / session_id
    folder.mkdir(parents=True)
    events: list[dict[str, Any]] = [{"kind": "tool_call", "at": datetime.now(UTC).isoformat(), "summary": "read"}]
    events += [
        {"kind": "file_edit", "at": at.isoformat(), "summary": "edit", "payload": {"path": path}} for path, at in edits
    ]
    run_file = folder / "run.json"
    run_file.write_text(
        json.dumps({"host": host, "session_id": session_id, "workspace_path": workspace, "events": events})
    )
    return run_file


def test_hotspots_count_recorded_edits_to_this_project_in_the_window(tmp_path: Path) -> None:
    store = tmp_path / "store"
    project = tmp_path / "repo"
    project.mkdir()
    now = datetime.now(UTC)
    hour = timedelta(hours=1)
    _ledger(
        store,
        "claude",
        "s1",
        str(project),
        [
            (str(project / "a.py"), now - hour),
            ("a.py:L10-L12", now - 2 * hour),  # relative, and this session's workspace is the project
            (str(project / "b.py"), now - 3 * hour),
            (str(project / "a.py"), now - timedelta(days=10)),  # before the window
            (str(tmp_path / "other" / "a.py"), now - hour),  # another tree
        ],
    )
    _ledger(
        store,
        "codex",
        "s2",
        str(tmp_path / "elsewhere"),
        [
            ("a.py", now - hour),  # relative to a tree that is not this project
            (str(project / "a.py"), now - 4 * hour),
            (str(project / "c.py"), now - 5 * hour),
            (str(project / "c.py"), now - 6 * hour),
            (str(project / "gone.py"), now - hour),
            (str(project / "gone.py"), now - hour),  # not in the project any more
        ],
    )
    stale = _ledger(store, "claude", "old", str(project), [(str(project / "b.py"), now - hour)] * 3)
    old = (now - timedelta(days=9)).timestamp()
    os.utime(stale, (old, old))

    payload = code_surface.edit_hotspots(store, project, ["a.py", "b.py", "c.py"], now=now)

    assert [(row["path"], row["edits"], row["sessions"]) for row in payload["files"]] == [
        ("a.py", 3, ["claude:s1", "codex:s2"]),
        ("c.py", 2, ["codex:s2"]),
    ]
    assert payload["files"][0]["last_at"] == (now - hour).isoformat(timespec="seconds")
    assert (payload["reedited_total"], payload["edited_total"], payload["sessions"]) == (2, 3, 2)
    assert (payload["ledgers_scanned"], payload["truncated"], payload["days"]) == (2, False, 7)


# --------------------------------------------------------------------------- #
# Relationships                                                               #
# --------------------------------------------------------------------------- #


def test_file_relationships_group_by_other_file_and_drop_name_collisions() -> None:
    provider = _Provider(
        [
            _row("src/api.py", "src/core.py", "calls", 2, "run"),
            _row("src/api.py", "src/core.py", "imports", 1),
            _row("src/core.py", "src/util.py", "imports", 1),
            _row("src/core.py", "web/app.ts", "calls", 5, "run"),
            _row("src/core.py", "tests/test_core.py", "calls", 1),
            _row("tests/test_core.py", "src/core.py", "calls", 3, "run"),
        ]
    )

    payload = code_surface.file_relationships(provider, "src/core.py")

    used_by = {row["path"]: row for row in payload["used_by"]}
    assert set(used_by) == {"src/api.py", "tests/test_core.py"}
    assert used_by["src/api.py"]["kinds"] == {"calls": 2, "imports": 1}
    assert used_by["src/api.py"]["kind"] == "calls"
    assert used_by["src/api.py"]["symbols"] == ["run"]
    assert used_by["src/api.py"]["lines"] == [7]
    assert [row["path"] for row in payload["uses"]] == ["src/util.py"]
    assert payload["filtered"] == {"cross_language": 1, "production_to_test": 1}
    assert payload["context"]["exactness"] == "current_index"


def _write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode()).hexdigest()


def test_symbol_calls_are_attributed_only_when_both_indexes_hold_the_file_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _write(tmp_path / "src" / "core.py", "def run():\n    helper()\n    return 1\n")
    indexed = {"src/core.py": digest}
    monkeypatch.setattr(code_map, "indexed_content_hash", lambda engine, path: indexed.get(path, ""))
    neighborhood = {
        "nodes": [
            {"id": "focus", "path": "src/core.py", "label": "run", "line": 1},
            {"id": "caller", "path": "src/api.py", "label": "run", "line": 12},
            {"id": "callee", "path": "src/util.py", "label": "helper", "line": 0},
            {"id": "noise", "path": "web/app.ts", "label": "run", "line": 3},
        ],
        "edges": [
            {"source": "caller", "target": "focus", "kind": "call", "weight": 1},
            {"source": "noise", "target": "focus", "kind": "call", "weight": 1},
            {"source": "focus", "target": "callee", "kind": "call", "weight": 2},
        ],
    }
    symbol = {"id": "focus", "label": "run", "path": "src/core.py", "line": 1, "end_line": 3}

    exact = _Provider([], neighborhood, digests={"src/core.py": "sha256:" + digest})
    payload = code_surface.symbol_relationships(exact, object(), tmp_path, symbol)
    assert exact.neighborhood_requests[-1]["span_start"] == 1
    assert exact.neighborhood_requests[-1]["span_end"] == 3
    assert [row["path"] for row in payload["used_by"]] == ["src/api.py"]
    assert payload["used_by"][0]["lines"] == [12]
    assert [row["symbols"] for row in payload["uses"]] == [["helper"]]
    assert payload["uses_attributed"] is True
    assert payload["filtered"] == {"cross_language": 1}

    # The View holds another version of the file: no span, so no calls out.
    stale = _Provider([], neighborhood, digests={"src/core.py": "sha256:" + "0" * 64})
    code_surface.symbol_relationships(stale, object(), tmp_path, symbol)
    assert "span_start" not in stale.neighborhood_requests[-1]

    # The legacy index measured an older file: no span either.
    indexed["src/core.py"] = "1" * 64
    fresh_view = _Provider([], neighborhood, digests={"src/core.py": digest})
    result = code_surface.symbol_relationships(fresh_view, object(), tmp_path, symbol)
    assert "span_start" not in fresh_view.neighborhood_requests[-1]
    assert result["uses_attributed"] is False


# --------------------------------------------------------------------------- #
# Focus payloads                                                              #
# --------------------------------------------------------------------------- #


def test_module_focus_puts_members_in_the_subject_lane_and_neighbors_by_direction() -> None:
    paths = [f"api/h{index}.py" for index in range(4)] + [f"db/m{index}.py" for index in range(4)]
    paths += [f"app/main{index}.py" for index in range(4)] + [f"other/o{index}.py" for index in range(8)]
    provider = _Provider(
        [
            _row("app/main0.py", "api/h0.py", "calls", 2, "handle"),
            _row("api/h0.py", "db/m0.py", "calls", 3, "save"),
            _row("api/h1.py", "api/h0.py", "calls", 1, "handle"),
        ]
    )

    payload = code_surface.module_focus(provider, Path("/repo"), "api", paths, {"api/h1.py": {"status": "M"}})

    graph = payload["graph"]
    lanes = {node["path"]: (node["lane"], node["status"]) for node in graph["nodes"]}
    assert lanes["api/h0.py"] == ("", "subject")
    assert lanes["api/h1.py"] == ("", "changed")
    assert lanes["app/main0.py"] == ("used_by", "context")
    assert lanes["db/m0.py"] == ("uses", "context")
    assert payload["level"] == "architecture"
    assert payload["subject"]["kind"] == "module"
    assert {edge["id"] for edge in graph["edges"]} == {
        "calls:app/main0.py->api/h0.py",
        "calls:api/h0.py->db/m0.py",
        "calls:api/h1.py->api/h0.py",
    }
    with pytest.raises(LookupError):
        code_surface.module_focus(provider, Path("/repo"), "nowhere", paths, {})


def test_file_focus_lists_symbols_and_symbol_focus_places_callers_and_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        code_map,
        "list_file_symbols",
        lambda engine, path, limit: {
            "total_symbols": 1,
            "truncated": False,
            "symbols": [
                {"id": "s1", "label": "run", "qualified_name": "run", "kind": "function", "line": 1, "end_line": 3}
            ],
        },
    )
    provider = _Provider(
        [_row("src/api.py", "src/core.py", "calls", 2, "run")],
        {
            "nodes": [
                {"id": "s1", "path": "src/core.py", "label": "run", "line": 1},
                {"id": "c", "path": "src/api.py", "label": "run", "line": 9},
                {"id": "u", "path": "src/util.py", "label": "helper", "line": 0},
            ],
            "edges": [
                {"source": "c", "target": "s1", "kind": "call", "weight": 1},
                {"source": "s1", "target": "u", "kind": "call", "weight": 1},
            ],
        },
    )

    file_payload = code_surface.file_focus(provider, object(), tmp_path, "src/core.py", {})
    focus = file_payload["graph"]["nodes"][0]
    assert file_payload["graph"]["focus"] == focus["id"] == "file:src/core.py"
    assert [symbol["label"] for symbol in focus["symbols"]] == ["run"]
    assert file_payload["graph"]["edges"][0]["symbols"] == [{"name": "run", "count": 2}]

    monkeypatch.setattr(code_map, "indexed_content_hash", lambda engine, path: "")
    symbol_payload = code_surface.symbol_focus(
        provider,
        object(),
        tmp_path,
        {"id": "s1", "label": "run", "path": "src/core.py", "line": 1, "end_line": 3, "kind": "function"},
        {},
    )
    nodes = {node["id"]: node for node in symbol_payload["graph"]["nodes"]}
    assert nodes["symbol:s1"]["lane"] == ""
    assert nodes["used_by:src/api.py"]["lane"] == "used_by"
    assert nodes["uses:src/util.py"]["label"] == "helper"
    assert {(edge["source"], edge["target"]) for edge in symbol_payload["graph"]["edges"]} == {
        ("used_by:src/api.py", "symbol:s1"),
        ("symbol:s1", "uses:src/util.py"),
    }


# --------------------------------------------------------------------------- #
# Uncommitted changes by session                                              #
# --------------------------------------------------------------------------- #


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    for name in ("a.py", "b.py", "gone.py", "old.py"):
        (root / name).write_text(f"{name}\n" * 3, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    (root / "a.py").write_text("changed\n" * 5, encoding="utf-8")
    (root / "new file.py").write_text("x\ny\n", encoding="utf-8")
    (root / "gone.py").unlink()
    _git(root, "mv", "old.py", "renamed.py")
    return root


def test_uncommitted_files_report_status_and_line_counts(repo: Path) -> None:
    files = code_surface.uncommitted_files(repo)

    assert {path: row["status"] for path, row in files.items()} == {
        "a.py": "M",
        "new file.py": "A",
        "gone.py": "D",
        "renamed.py": "R",
    }
    assert files["renamed.py"]["old_path"] == "old.py"
    # A move changes no line; counting it as a new file would claim all three.
    assert (files["renamed.py"]["additions"], files["renamed.py"]["deletions"]) == (0, 0)
    assert (files["a.py"]["additions"], files["a.py"]["deletions"]) == (5, 3)
    assert files["new file.py"]["additions"] == 2


def test_changes_group_by_exact_authoring_session_and_mark_shared_files(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(code_surface, "_CHANGES_CACHE", {})
    monkeypatch.setattr(code_surface, "_session_titles", lambda store_root: {("claude", "s1"): {"title": "Fix a"}})
    seen: list[list[str]] = []

    def attribute(store_root: Path, root: Path, paths: list[str], *, head_commit_time: object) -> list[Any]:
        seen.append(list(paths))
        return [
            SimpleNamespace(scope_key="a.py", host="claude", session_id="s1", model="opus"),
            SimpleNamespace(scope_key="new file.py", host="claude", session_id="s1", model="opus"),
            SimpleNamespace(scope_key="a.py", host="codex", session_id="s2", model=""),
            SimpleNamespace(scope_key="not-changed.py", host="codex", session_id="s2", model=""),
        ]

    payload = code_surface.changes_by_session(repo / ".store", repo, attribute=attribute)

    assert seen == [["a.py", "gone.py", "new file.py", "renamed.py"]]
    sessions = {session["id"]: session for session in payload["sessions"]}
    assert sessions["claude:s1"]["files"] == ["a.py", "new file.py"]
    assert sessions["claude:s1"]["title"] == "Fix a"
    assert sessions["claude:s1"]["shared_files"] == ["a.py"]
    assert sessions["codex:s2"]["files"] == ["a.py"]
    assert payload["unattributed"] == ["gone.py", "renamed.py"]
    shared = next(row for row in payload["files"] if row["path"] == "a.py")
    assert shared["shared"] is True and shared["sessions"] == ["claude:s1", "codex:s2"]
    assert payload["total"] == 4


def test_changed_line_ranges_split_added_modified_and_deleted(repo: Path) -> None:
    (repo / "b.py").write_text("b.py\nnew line\nb.py\n", encoding="utf-8")  # modify line 2
    ranges = code_surface.changed_line_ranges(repo, "b.py")
    assert ranges["status"] == "M"
    assert ranges["modified"] == [[2, 2]]

    (repo / "b.py").write_text("b.py\nb.py\nb.py\nextra\nextra\n", encoding="utf-8")
    assert code_surface.changed_line_ranges(repo, "b.py")["added"] == [[4, 5]]

    (repo / "b.py").write_text("b.py\n", encoding="utf-8")
    assert code_surface.changed_line_ranges(repo, "b.py")["deleted"] == [2]

    assert code_surface.changed_line_ranges(repo, "new file.py") == {
        "path": "new file.py",
        "status": "A",
        "added": [[1, 2]],
        "modified": [],
        "deleted": [],
    }
    assert code_surface.changed_line_ranges(repo, "untouched.py")["status"] == ""


def test_changed_line_ranges_of_a_staged_rename_mark_only_its_edits(repo: Path) -> None:
    # Diffed by its new path alone, a staged rename read as a new file and the
    # gutter marked every line added.
    assert code_surface.changed_line_ranges(repo, "renamed.py") == {
        "path": "renamed.py",
        "status": "R",
        "added": [],
        "modified": [],
        "deleted": [],
    }
    (repo / "renamed.py").write_text("old.py\nedited\nold.py\n", encoding="utf-8")
    assert code_surface.changed_line_ranges(repo, "renamed.py")["modified"] == [[2, 2]]
    assert code_surface.changed_line_ranges(repo, "renamed.py")["added"] == []


def test_changed_line_ranges_read_paths_with_spaces_and_untracked_last_lines(repo: Path) -> None:
    (repo / "with space.py").write_text("one\ntwo\n", encoding="utf-8")
    _git(repo, "add", "with space.py")
    _git(repo, "commit", "-q", "-m", "space")
    (repo / "with space.py").write_text("one\nTWO\n", encoding="utf-8")
    assert code_surface.changed_line_ranges(repo, "with space.py")["modified"] == [[2, 2]]

    # A last line without a newline is still a line of the new file.
    (repo / "tail.py").write_text("x\ny", encoding="utf-8")
    assert code_surface.changed_line_ranges(repo, "tail.py")["added"] == [[1, 2]]

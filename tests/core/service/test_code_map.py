from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from lemoncrow.core.service.code_map import (
    _git_worktrees,
    _ignored_worktree_paths,
    build_neighborhood,
    classify_activity_event,
    find_references,
    legacy_file_links,
    list_file_nodes,
    list_file_symbols,
    recent_activity,
    resolve_project_root,
    search_symbols,
    search_text,
)


class _FakeEngine:
    repo_id = "repo-1"

    def search_symbols(self, query: str, **_: object) -> list[SimpleNamespace]:
        assert query == "chargeCard"
        return [
            SimpleNamespace(
                symbol_id="charge",
                symbol_name="chargeCard",
                qualified_name="PaymentGateway.chargeCard",
                file_path="src/payment.py",
                kind="method",
                language="python",
                start_line=12,
                end_line=24,
                score=42.5,
            )
        ]

    def search_text(self, query: str, **_: object) -> list[SimpleNamespace]:
        assert query == "retry"
        return [
            SimpleNamespace(
                file_path="src/payment.py",
                line=18,
                column=9,
                text="return retry(payment)",
            )
        ]

    def tool_callers(self, **_: object) -> dict[str, object]:
        return {
            "target": {
                "id": "charge",
                "name": "chargeCard",
                "qualified_name": "PaymentGateway.chargeCard",
                "path": "src/payment.py",
                "kind": "method",
                "language": "python",
                "line": 12,
                "end_line": 24,
            },
            "related": [
                {
                    "id": "checkout",
                    "name": "checkout",
                    "qualified_name": "checkout",
                    "path": "src/checkout.py",
                    "kind": "function",
                    "line": 4,
                    "end_line": 9,
                }
            ],
            "edges": [
                {
                    "caller_symbol_id": "checkout",
                    "callee_symbol_id": "charge",
                    "depth": 1,
                }
            ],
            "truncated": False,
        }

    def tool_callees(self, **_: object) -> dict[str, object]:
        return {
            "target": {
                "id": "charge",
                "name": "chargeCard",
                "qualified_name": "PaymentGateway.chargeCard",
                "path": "src/payment.py",
                "kind": "method",
                "language": "python",
                "line": 12,
                "end_line": 24,
            },
            "related": [
                {
                    "id": "gateway",
                    "name": "send",
                    "qualified_name": "PaymentGateway.send",
                    "path": "src/payment.py",
                    "kind": "method",
                    "line": 26,
                    "end_line": 31,
                }
            ],
            "edges": [
                {
                    "caller_symbol_id": "charge",
                    "callee_symbol_id": "gateway",
                    "depth": 1,
                }
            ],
            "truncated": True,
        }

    def find_references(self, **kwargs: object) -> dict[str, object]:
        assert kwargs["symbol_id"] == "charge"
        assert kwargs["group_by"] == "none"
        return {
            "reference_count": 1,
            "references": [
                {
                    "file_path": "src/checkout.py",
                    "line": 8,
                    "column": 12,
                    "end_line": 8,
                    "snippet": "return gateway.chargeCard(total)",
                    "caller": "checkout",
                    "edge_kind": "call",
                    "provenance": "ast",
                    "confidence": 1.0,
                }
            ],
            "truncated": False,
        }


def test_resolve_project_root_accepts_registered_project_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.core.service import project_registry

    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".lemoncrow").mkdir()
    project_id = project_registry.project_id_for_root(project_root)
    project = project_registry.RegisteredProject(
        project_id=project_id,
        root=project_root.resolve(),
    )
    monkeypatch.setattr(
        project_registry,
        "resolve_registered_project",
        lambda reference: project if reference == project_id else None,
    )

    assert resolve_project_root(project_id) == project_root.resolve()


def test_search_text_returns_exact_source_locations() -> None:
    matches = search_text(_FakeEngine(), "retry", limit=12)

    assert matches == [
        {
            "id": "text::src/payment.py:18:9",
            "path": "src/payment.py",
            "line": 18,
            "column": 9,
            "text": "return retry(payment)",
            "language": "Python",
            "file_type": "source",
        }
    ]


def test_search_symbols_returns_compact_ranked_records() -> None:
    payload = search_symbols(_FakeEngine(), "chargeCard", limit=10)

    assert payload[0] == {
        "id": "charge",
        "label": "chargeCard",
        "qualified_name": "PaymentGateway.chargeCard",
        "path": "src/payment.py",
        "kind": "method",
        "language": "python",
        "line": 12,
        "end_line": 24,
        "score": 42.5,
    }


def test_find_references_returns_compact_usage_locations() -> None:
    payload = find_references(_FakeEngine(), "charge", limit=20)

    assert payload == {
        "symbol_id": "charge",
        "reference_count": 1,
        "references": [
            {
                "path": "src/checkout.py",
                "line": 8,
                "column": 12,
                "end_line": 8,
                "snippet": "return gateway.chargeCard(total)",
                "caller": "checkout",
                "edge_kind": "call",
                "provenance": "ast",
                "confidence": 1.0,
            }
        ],
        "truncated": False,
    }


def test_build_neighborhood_merges_both_directions_and_marks_focus() -> None:
    payload = build_neighborhood(_FakeEngine(), "charge", depth=1, limit=20)

    assert payload["focus"] == "charge"
    assert {node["id"] for node in payload["nodes"]} == {"charge", "checkout", "gateway"}
    assert next(node for node in payload["nodes"] if node["id"] == "charge")["focus"] is True
    assert {(edge["source"], edge["target"]) for edge in payload["edges"]} == {
        ("checkout", "charge"),
        ("charge", "gateway"),
    }
    assert payload["truncated"] is True


def test_classify_activity_event_does_not_expose_raw_output() -> None:
    event = classify_activity_event(
        {
            "kind": "tool_call",
            "at": "2026-07-16T12:00:00Z",
            "summary": "code_search(query=chargeCard)",
            "payload": {
                "tool": "code_search",
                "args": {"query": "chargeCard"},
                "output": "private source that must not cross the API",
            },
        },
        session_id="session-1",
        sequence=3,
    )

    assert event is not None
    assert event["kind"] == "search"
    assert event["query"] == "chargeCard"
    assert "private source" not in json.dumps(event)


def test_classify_read_range_from_batched_files() -> None:
    event = classify_activity_event(
        {
            "kind": "tool_call",
            "at": "2026-07-16T12:00:00Z",
            "payload": {"tool": "mcp__lc__read", "args": {"files": ["src/payment.py:L12-L24"]}},
        },
        session_id="session-1",
        sequence=4,
        project_root=Path("/repo"),
    )

    assert event is not None
    assert event["kind"] == "read"
    assert event["path"] == "src/payment.py"
    assert event["line"] == 12


def test_recent_activity_filters_to_project_and_after_cursor(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    sessions = tmp_path / "runtime" / "sessions" / "2026" / "07" / "16" / "codex" / "session-1"
    sessions.mkdir(parents=True)
    (sessions / "run.json").write_text(
        json.dumps(
            {
                "session_id": "session-1",
                "agent": "lemoncrow:code",
                "status": "running",
                "files_touched": [str(project / "src" / "payment.py")],
                "events": [
                    {
                        "kind": "file_edit",
                        "at": "2026-07-16T12:00:00Z",
                        "summary": "edited payment.py",
                        "payload": {"path": str(project / "src" / "payment.py")},
                    },
                    {
                        "kind": "test_result",
                        "at": "2026-07-16T12:01:00Z",
                        "summary": "unit=pass",
                        "payload": {"test_id": "unit", "passed": True},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = recent_activity(
        tmp_path / "runtime",
        project,
        after="2026-07-16T12:00:30Z",
        limit=20,
    )

    assert payload["session_id"] == "session-1"
    assert payload["host"] == "codex"
    assert payload["status"] == "running"
    assert [event["kind"] for event in payload["events"]] == ["verify"]


def test_recent_activity_uses_workspace_and_resolves_exact_live_targets(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    sessions = tmp_path / "runtime" / "sessions" / "2026" / "07" / "16" / "codex" / "session-2"
    sessions.mkdir(parents=True)
    (sessions / "run.json").write_text(
        json.dumps(
            {
                "session_id": "session-2",
                "workspace_path": str(project),
                "status": "running",
                "files_touched": [],
                "events": [
                    {
                        "kind": "tool_call",
                        "at": "2026-07-16T12:00:00Z",
                        "summary": "code_search(query=chargeCard)",
                        "payload": {"tool": "code_search", "args": {"query": "chargeCard"}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = recent_activity(tmp_path / "runtime", project, engine=_FakeEngine())

    assert payload["events"][0]["symbol_ids"] == ["charge"]


@pytest.mark.private_engine
def test_legacy_file_links_keep_imports_and_uniquely_resolved_calls(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.code_context import CodeContextEngine

    source = tmp_path / "src" / "payments.py"
    source.parent.mkdir()
    source.write_text(
        "def send(amount: int) -> int:\n    return amount\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "checkout.py").write_text(
        "from src.payments import send\n\n\ndef charge_card(amount: int) -> int:\n    return send(amount)\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Payments\n", encoding="utf-8")
    engine = CodeContextEngine(
        tmp_path,
        db_path=tmp_path / "index" / "code_context.sqlite",
        autosync_enabled=False,
    )
    engine.index_repo(force=True)

    links = legacy_file_links(engine)

    calls = [row for row in links if row["kind"] == "calls"]
    assert any(
        row["source_path"] == "src/checkout.py"
        and row["target_path"] == "src/payments.py"
        and row["symbols"][0]["name"] == "send"
        for row in calls
    ), links
    assert all(row["source_path"] != row["target_path"] for row in links)

    file_symbols = list_file_symbols(engine, "src/payments.py")
    assert file_symbols["total_symbols"] == 1
    assert [symbol["label"] for symbol in file_symbols["symbols"]] == ["send"]
    assert file_symbols["symbols"][0]["module"] == "src"
    assert file_symbols["truncated"] is False


def test_git_worktrees_discovers_sibling_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    main = tmp_path / "main"
    feature = tmp_path / "feature"
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    (main / "main.py").write_text("pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(main), "add", "main.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(main),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-qb", "feature", str(feature)], check=True)

    assert set(_git_worktrees(main)) == {main.resolve(), feature.resolve()}
    from lemoncrow.core.service.project_registry import project_id_for_root

    monkeypatch.setenv("LEMONCROW_WORKSPACE_ROOT", str(main))
    assert resolve_project_root(project_id_for_root(feature)) == feature.resolve()

    from lemoncrow.core.service import code_map, code_warm, project_registry

    monkeypatch.setattr(code_map, "_is_user_project", lambda _path: True)
    monkeypatch.setattr(code_warm, "discover_workspaces", lambda: [main])
    monkeypatch.setattr(project_registry, "registered_projects", lambda: [])
    assert {row["root"] for row in code_map.list_projects()} == {str(main.resolve()), str(feature.resolve())}


def test_source_file_listing_hides_ignored_until_requested(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("pass\n", encoding="utf-8")
    ignored = tmp_path / "ignored"
    ignored.mkdir()
    for name in ("one.py", "two.py", "three.py"):
        (ignored / name).write_text("pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".gitignore", "main.py"], check=True)
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE symbols (file_path TEXT, end_line INTEGER)")
    connection.execute("INSERT INTO symbols VALUES ('ignored/one.py', 1)")
    engine = SimpleNamespace(connection=lambda: connection)

    normal = list_file_nodes(engine, tmp_path)
    assert "main.py" in {row["path"] for row in normal["files"]}
    assert not any(row["ignored"] for row in normal["files"])
    assert "ignored/one.py" not in {row["path"] for row in normal["files"]}

    expanded = list_file_nodes(engine, tmp_path, include_ignored=True)
    ignored_rows = {row["path"] for row in expanded["files"] if row["ignored"]}
    assert ignored_rows == {"ignored/one.py", "ignored/two.py", "ignored/three.py"}
    assert expanded["ignored_truncated"] is False
    complete, truncated = _ignored_worktree_paths(tmp_path, limit=3)
    assert len(complete) == 3
    assert truncated is False
    limited, truncated = _ignored_worktree_paths(tmp_path, limit=2)
    assert len(limited) == 2
    assert truncated is True


@pytest.mark.private_engine
def test_source_file_listing_lists_every_file(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.code_context import CodeContextEngine

    for name in ("alpha.py", "beta.py", "gamma.py"):
        (tmp_path / name).write_text(
            f"def {name.removesuffix('.py')}() -> None:\n    pass\n",
            encoding="utf-8",
        )
    engine = CodeContextEngine(
        tmp_path,
        db_path=tmp_path / "index" / "code_context.sqlite",
        autosync_enabled=False,
    )
    engine.index_repo(force=True)

    listing = list_file_nodes(engine, tmp_path)

    assert listing["total_files"] >= 3
    assert len(listing["files"]) == listing["total_files"]
    assert {"alpha.py", "beta.py", "gamma.py"} <= {file["path"] for file in listing["files"]}

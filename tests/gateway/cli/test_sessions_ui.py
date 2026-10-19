"""Sessions in the terminal: recent runs, `lc rt dashboard`, the `lc session` picker, reports, and the home screen."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.core.capabilities.savings_summary import plain_lines
from lemoncrow.gateway.cli import cli, style
from lemoncrow.gateway.cli.picker import Picker, cells
from lemoncrow.gateway.cli.sessions_ui import (
    Run,
    SessionSource,
    _day,
    overview_lines,
    recent_runs,
    report_text,
    short_ids,
)

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")


def _write(root: Path, sid: str, *, task: str = "", cost: float = 0.0, ago: timedelta = timedelta(0)) -> None:
    calls = [{"model": "claude-haiku-4-5", "input_tokens": 1000, "output_tokens": 200, "cost_usd": cost}]
    # Written relative to the clock now, not to import time: the picker labels "1m ago" from the real
    # clock, and a long suite runs this test minutes after collection.
    written = datetime.now(UTC)
    snapshot: dict[str, Any] = {
        "session_id": sid,
        "agent": "lemoncrow:code",
        "task": task,
        "status": "done",
        "created_at": (written - ago - timedelta(minutes=5)).isoformat(),
        "updated_at": (written - ago).isoformat(),
        "cost": {"calls": calls if cost else [], "total_cost_usd": cost},
        "events": [],
    }
    path = root / "sessions" / "2026" / "10" / "05" / "claude" / sid / "run.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(snapshot), encoding="utf-8")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    _write(tmp_path, "aaa111aaa", task="Fix the parser\nand more", cost=1.5, ago=timedelta(minutes=1))
    _write(tmp_path, "ses_abc1xx", task="Tidy docs", cost=0.25, ago=timedelta(hours=2))
    _write(tmp_path, "ses_abc2yy", task="Write tests", cost=0.5, ago=timedelta(hours=3))
    _write(tmp_path, "bbb222bbb", ago=timedelta(hours=4))  # opened and closed: no usage
    _write(tmp_path, "ccc333ccc", task="untitled", ago=timedelta(hours=5))  # a task but nothing spent
    return tmp_path


def test_recent_runs_skip_and_count_the_ones_without_usage(root: Path) -> None:
    runs, passed_over = recent_runs(root, limit=10, carry=False)
    assert [run.session_id for run in runs] == ["aaa111aaa", "ses_abc1xx", "ses_abc2yy"]
    assert runs[0].task == "Fix the parser"
    assert runs[0].cost == pytest.approx(1.5)
    assert passed_over == 2


def test_short_ids_grow_until_no_listed_run_shares_them(root: Path) -> None:
    runs, _ = recent_runs(root, limit=10, carry=False)
    assert short_ids(runs) == {"aaa111aaa": "aaa111", "ses_abc1xx": "ses_abc1", "ses_abc2yy": "ses_abc2"}


def _window(**overrides: Any) -> SimpleNamespace:
    window = {
        "since": NOW - timedelta(days=7),
        "until": NOW,
        "cost_by_day": {},
        "session_count": 3,
        "total_duration_seconds": 3 * 3600.0,
        "total_cost_usd": 10.0,
        "total_lemoncrow_savings_usd": 2.5,
        "cost_by_model": {"claude-opus-5-5": 9.0, "local-model": 0.0},
        "cost_by_tool": {"Read": 1.0},
    }
    return SimpleNamespace(**{**window, **overrides})


@pytest.mark.parametrize("width", [60, 80, 140])
def test_the_dashboard_fits_the_width_and_hides_runs_without_usage(
    root: Path, monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    monkeypatch.setattr("lemoncrow.pro.runtime.insights.build_insights", lambda *a, **k: _window())

    lines = overview_lines(root, width=width, browser=False)
    text = "\n".join(lines)

    assert lines[0] == "◆ LemonCrow  last 7 days"
    assert "avg 1h each" in text
    assert "25% of spend" in text
    assert "local-model" not in text  # nothing spent: no row
    assert "Fix the" in text
    assert "2 runs with no recorded usage hidden" in text
    assert "lc session report aaa111" in text
    assert "lc rt dashboard open" not in text
    assert max(cells(line) for line in lines) <= max(width, 60) + 2


def test_the_dashboard_charts_each_day_and_keeps_model_shares_whole(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = NOW.astimezone().date()
    by_day = {today.isoformat(): 6.0, (today - timedelta(days=1)).isoformat(): 4.0}
    monkeypatch.setattr("lemoncrow.pro.runtime.insights.build_insights", lambda *a, **k: _window(cost_by_day=by_day))

    lines = overview_lines(root, width=100, browser=False)

    start = lines.index("  Each day")
    days = lines[start + 1 : lines.index("", start)]
    assert len(days) == 7  # the partial eighth day spent nothing, so it is left off
    assert days[-1].split()[:2] == ["Today", "$6.00"]
    assert days[-2].split()[:2] == ["Yesterday", "$4.00"]
    assert days[-1].endswith("█" * 14)  # the busiest day fills its bar
    unrecorded = next(line for line in lines if line.strip().startswith("model not recorded"))
    assert unrecorded.split()[3:5] == ["$1.00", "10%"]  # $10 spent, $9 named a model
    assert not any(line.strip().startswith("other models") for line in lines)


def test_the_dashboard_offers_the_browser_only_when_it_runs(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lemoncrow.pro.runtime.insights.build_insights", lambda *a, **k: _window())
    assert "lc rt dashboard open" in "\n".join(overview_lines(root, width=80, browser=True))


def test_an_empty_dashboard_says_how_to_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = _window(session_count=0, total_cost_usd=0.0, total_lemoncrow_savings_usd=0.0, cost_by_model={})
    monkeypatch.setattr("lemoncrow.pro.runtime.insights.build_insights", lambda *a, **k: empty)
    text = "\n".join(overview_lines(tmp_path, width=80, browser=False))
    assert "No sessions yet." in text
    assert "lc with claude" in text


def test_sessions_group_by_day() -> None:
    today = datetime(2026, 10, 5, 12, tzinfo=UTC).astimezone()
    assert _day(today, today) == "Today"
    assert _day(today - timedelta(days=1), today) == "Yesterday"
    assert _day(today - timedelta(days=3), today) == "This week"
    assert _day(today - timedelta(days=30), today) == "Older"
    assert _day(None, today) == "Older"


def test_the_session_picker_lists_searches_and_opens_a_report(root: Path) -> None:
    runs, passed_over = recent_runs(root, limit=10, carry=False)
    picker = Picker(SessionSource(root, runs, passed_over))

    labels = [row.label for row in picker.page.rows if row.selectable]
    assert labels == ["Fix the parser", "Tidy docs", "Write tests"]
    assert any("2 runs with no recorded usage hidden" in row.label for row in picker.page.rows)
    headings = [row.label for row in picker.page.rows if row.kind == "heading"]
    assert sum(int(label.split(" · ")[1].split()[0]) for label in headings) == 3
    if len(headings) == 1:  # all three today (or all yesterday, just after midnight)
        assert headings[0].endswith(" · 3 runs · $2.25")
    first = picker.selected()
    assert first is not None and first.value.strip() == "$1.50"
    assert first.note == "1m ago · aaa111 · lemoncrow:code"

    picker.start_search()
    picker.set_query("abc2")
    assert [row.label for row in picker.page.rows] == ["Write tests"]
    picker.back()

    picker.activate()
    text = [row.label for row in picker.page.rows]
    assert text[0] == "Fix the parser"
    assert text[1].startswith("Session aaa111")
    assert any(line.strip().startswith("Started:") for line in text)
    assert "Cost" in text
    assert "Estimated LemonCrow savings" in text


def test_a_report_page_wraps_a_long_task(tmp_path: Path) -> None:
    task = "Refactor " + "the parser and its tests " * 8
    _write(tmp_path, "ddd444ddd", task=task, cost=1.0)
    runs, passed_over = recent_runs(tmp_path, limit=10, carry=False)
    picker = Picker(SessionSource(tmp_path, runs, passed_over))
    picker.activate()
    labels = [row.label for row in picker.page.rows]
    titles = labels[: next(i for i, label in enumerate(labels) if label.startswith("Session ddd444"))]
    assert len(titles) == 3
    assert " ".join(titles) == task.strip()


def test_session_report_prints_the_styled_report(root: Path) -> None:
    result = CliRunner().invoke(cli, ["--root", str(root), "session", "report", "aaa111"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("◆ Session aaa111")
    assert "Total:" in result.output


def test_bare_session_off_a_terminal_prints_its_help(root: Path) -> None:
    result = CliRunner().invoke(cli, ["--root", str(root), "session"])
    assert result.exit_code == 0, result.output
    assert "report" in result.output
    assert "Error" not in result.output


def test_report_text_is_the_plain_report_with_a_title(root: Path) -> None:
    from lemoncrow.infra.runtime.session_report import load_report, render_text

    report = load_report("aaa111aaa", root)
    assert report is not None
    styled = report_text(report)
    assert styled[0].startswith("◆ Session aaa111")
    assert styled[2:] == render_text(report).splitlines()[2:]


# ── lc rt dashboard open ──


@pytest.mark.parametrize(
    ("review", "expected", "next_command"),
    [
        (False, "comes with LemonCrow Review", "lc rt dashboard"),
        (True, "local server isn't running", "lc doctor"),
    ],
)
def test_dashboard_open_without_a_server_says_what_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, review: bool, expected: str, next_command: str
) -> None:
    monkeypatch.setattr("lemoncrow.infra.runtime.dashboard_url.discover_dashboard_url", lambda *a, **k: None)
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.product_status._review_installed", lambda root: review)

    result = CliRunner().invoke(cli, ["--root", str(tmp_path), "runtime", "dashboard", "open"])

    assert result.exit_code == 1
    assert expected in result.output
    assert next_command in result.output


# ── the root ──


def test_a_runtime_command_at_the_root_points_to_lc_rt(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["--root", str(tmp_path), "debt", "list"])
    assert result.exit_code == 2
    assert "No such command 'debt'" in result.output
    assert "lc rt debt list" in result.output


def test_home_shows_this_repositorys_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.gateway.cli.home import repository_lines

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    assert "not built yet" in "\n".join(repository_lines())

    index = repo / ".lemoncrow" / "workspace" / "runtime-lite-v3.sqlite3"
    index.parent.mkdir(parents=True)
    with sqlite3.connect(index) as db:
        db.execute("CREATE TABLE files (path TEXT)")
        db.executemany("INSERT INTO files VALUES (?)", [("a",), ("b",), ("c",)])
    text = "\n".join(repository_lines())
    assert "3 files" in text
    assert "updated just now" in text

    monkeypatch.chdir(tmp_path)
    assert repository_lines() == []


def test_tagged_lines_style_and_print_plain_alike() -> None:
    lines = [
        ("title", "Name\tdetail"),
        ("heading", "  Head"),
        ("warn", "careful"),
        ("next", "Do it:\tlc x"),
        ("", "  body"),
    ]
    assert style.tagged(lines) == ["◆ Name  detail", "  Head", "  ⚠ careful", "  → Do it: lc x", "  body"]
    assert plain_lines(lines).splitlines() == style.tagged(lines)


def test_ago_uses_the_largest_whole_unit() -> None:
    assert style.ago(5) == "just now"
    assert style.ago(125) == "2m ago"
    assert style.ago(3 * 3600 + 5) == "3h ago"
    assert style.ago(2 * 86400) == "2d ago"


def test_run_marks_failures() -> None:
    run = Run("x", "a", "t", "failed", None, 1.0, 0.0)
    assert run.failed

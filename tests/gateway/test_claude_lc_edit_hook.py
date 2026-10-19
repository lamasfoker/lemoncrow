"""The Claude PostToolUse hook records LemonCrow MCP edits as exact evidence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.foundation.paths import session_dir

HOOK = Path("integrations/claude/plugin/hooks/post_tool_use.py").resolve()
SRC = Path("src").resolve()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def checkouts(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "init")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))
    stale = time.time() - 3600
    for path in (repo / "a.py", worktree / "a.py"):
        os.utime(path, (stale, stale))
    return repo.resolve(), worktree.resolve()


def _run(tmp_path: Path, repo: Path, edits: list[dict[str, Any]], tool: str = "mcp__lc__edit") -> list[dict[str, Any]]:
    root = tmp_path / ".lemoncrow"
    env = os.environ.copy()
    env.update(
        {
            "LEMONCROW_ROOT": str(root),
            "CLAUDE_WORKSPACE_ROOT": str(repo),
            "PYTHONPATH": str(SRC),
            "LEMONCROW_LIVE_REVIEW_PREWARM": "0",
        }
    )
    payload = {"session_id": "sid1", "cwd": str(repo), "tool_name": tool, "tool_input": {"edits": edits}}
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        cwd=repo,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    run_file = session_dir(root, "claude", "sid1") / "run.json"
    if not run_file.exists():
        return []
    return [event for event in json.loads(run_file.read_text("utf-8"))["events"] if event["kind"] == "file_edit"]


def test_relative_edit_is_resolved_to_the_linked_worktree_it_landed_in(
    tmp_path: Path, checkouts: tuple[Path, Path]
) -> None:
    repo, worktree = checkouts
    (worktree / "a.py").write_text("x = 2\n", encoding="utf-8")

    events = _run(tmp_path, repo, [{"path": "a.py:L1-L1", "old": "x = 1", "new": "x = 2"}])

    assert [event["payload"]["path"] for event in events] == [str(worktree / "a.py")]
    payload = events[0]["payload"]
    assert "+x = 2" in payload["diff"]
    assert payload["host"] == "claude"
    assert payload["session_id"] == "sid1"


def test_a_created_file_records_its_new_text(tmp_path: Path, checkouts: tuple[Path, Path]) -> None:
    repo, _worktree = checkouts
    (repo / "new.py").write_text("print(1)\n", encoding="utf-8")

    events = _run(
        tmp_path,
        repo,
        [{"path": "new.py", "new": "print(1)\n", "replace": True}],
        tool="mcp__plugin_lemoncrow_lemoncrow__edit",
    )

    assert [event["payload"]["path"] for event in events] == [str(repo / "new.py")]
    assert "+print(1)" in events[0]["payload"]["diff"]


def test_unverifiable_or_ambiguous_edits_record_nothing(tmp_path: Path, checkouts: tuple[Path, Path]) -> None:
    repo, worktree = checkouts
    # The new text is nowhere on disk: the call did not write it.
    assert _run(tmp_path, repo, [{"path": "a.py", "old": "x = 1", "new": "x = 9"}]) == []

    # Both checkouts hold a fresh file with the new text: no single author file.
    for path in (repo / "a.py", worktree / "a.py"):
        path.write_text("x = 3\n", encoding="utf-8")
    assert _run(tmp_path, repo, [{"path": "a.py", "old": "x = 1", "new": "x = 3"}]) == []


def test_other_mcp_edit_tools_are_ignored(tmp_path: Path, checkouts: tuple[Path, Path]) -> None:
    repo, _worktree = checkouts
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert _run(tmp_path, repo, [{"path": "a.py", "new": "x = 2"}], tool="mcp__other__edit") == []


def test_hooks_json_routes_lemoncrow_edits_to_the_ledger_hook() -> None:
    import re

    config = json.loads(Path("integrations/claude/plugin/hooks/hooks.json").read_text("utf-8"))
    groups = [
        group
        for group in config["hooks"]["PostToolUse"]
        if any("post_tool_use.py" in hook.get("command", "") for hook in group["hooks"])
    ]
    assert len(groups) == 1
    matcher = re.compile(f"^(?:{groups[0]['matcher']})$")
    for tool in ("Edit", "Write", "MultiEdit", "mcp__lc__edit", "mcp__plugin_lemoncrow_lc__edit"):
        assert matcher.match(tool), tool
    assert not matcher.match("mcp__lc__read")

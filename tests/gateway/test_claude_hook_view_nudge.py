"""Host edits wake the LemonCrow client so they reach the index before the next call."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_client.autosync import AutoSync

HOOKS = Path("integrations/claude/plugin/hooks").resolve()
PYTHONPATH = os.pathsep.join((str(Path("src").resolve()), str(Path("client/src").resolve())))


@pytest.fixture
def state() -> Iterator[Path]:
    target = Path(tempfile.mkdtemp(prefix="lc", dir="/tmp"))  # short: Unix socket paths are capped
    yield target
    shutil.rmtree(target, ignore_errors=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    return root.resolve()


class _Listening:
    def __init__(self, repo: Path, state: Path) -> None:
        self.calls = 0
        self._lock = threading.Lock()
        self.auto = AutoSync(self._sync, repo, state, debounce_s=0.01, idle_check_s=3600)

    def _sync(self) -> bool:
        with self._lock:
            self.calls += 1
        return False


def _hook(name: str, payload: dict[str, Any], repo: Path, state: Path, tmp_path: Path) -> None:
    env = {
        **os.environ,
        "PYTHONPATH": PYTHONPATH,
        "LEMONCROW_HOME": str(state),
        "LEMONCROW_ROOT": str(tmp_path / ".lemoncrow"),
        "CLAUDE_WORKSPACE_ROOT": str(repo),
        "LEMONCROW_LIVE_REVIEW_PREWARM": "0",
    }
    result = subprocess.run(
        [sys.executable, str(HOOKS / name)],
        input=json.dumps({"session_id": "sid1", "cwd": str(repo), **payload}),
        text=True,
        capture_output=True,
        env=env,
        cwd=repo,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _eventually(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


@pytest.mark.parametrize(
    ("hook", "payload"),
    [
        ("post_tool_use.py", {"tool_name": "Write", "tool_input": {"file_path": "a.py", "content": "x = 2\n"}}),
        ("post_tool_use_bash.py", {"tool_name": "Bash", "tool_input": {"command": "ruff format ."}}),
    ],
)
def test_a_host_edit_wakes_the_client_serving_that_checkout(
    hook: str, payload: dict[str, Any], repo: Path, state: Path, tmp_path: Path
) -> None:
    if payload["tool_name"] == "Write":
        payload["tool_input"]["file_path"] = str(repo / "a.py")
    listening = _Listening(repo, state)
    listening.auto.start()
    try:
        _hook(hook, payload, repo, state, tmp_path)
        assert _eventually(lambda: listening.calls == 1)
    finally:
        listening.auto.close()


def test_lemoncrows_own_edit_does_not_wake_the_client(repo: Path, state: Path, tmp_path: Path) -> None:
    """``edit`` already pushed what it wrote; a nudge would only re-check."""
    listening = _Listening(repo, state)
    listening.auto.start()
    try:
        _hook("post_tool_use.py", {"tool_name": "mcp__lc__edit", "tool_input": {"edits": []}}, repo, state, tmp_path)
        assert not _eventually(lambda: listening.calls > 0, timeout=0.3)
    finally:
        listening.auto.close()

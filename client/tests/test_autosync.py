"""Background sync: host edits reach the View ahead of the next call, only when changed."""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from _stub import StubServer
from conftest import make_config
from lemoncrow_client.autosync import AutoSync, default_state_dir, nudge_workspace, socket_dir
from lemoncrow_client.config import load_config
from lemoncrow_client.mcpserver import McpServer
from lemoncrow_client.session import RemoteSession


def _eventually(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


@pytest.fixture
def state(tmp_path: Path) -> Iterator[Path]:
    """A short state directory: pytest's tmp_path alone can exceed the Unix socket limit."""
    target = Path(tempfile.mkdtemp(prefix="lc", dir="/tmp"))
    yield target
    shutil.rmtree(target, ignore_errors=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


class _Counter:
    def __init__(self, results: list[bool | None] | None = None) -> None:
        self.calls = 0
        self._results = list(results or [])
        self._lock = threading.Lock()

    def __call__(self) -> bool | None:
        with self._lock:
            self.calls += 1
            return self._results.pop(0) if self._results else False


def test_a_burst_of_nudges_is_one_check(repo: Path, state: Path) -> None:
    sync = _Counter()
    auto = AutoSync(sync, repo, state, debounce_s=0.05, idle_check_s=3600, listen=False)
    auto.start()
    try:
        for _ in range(5):
            auto.nudge()
        _eventually(lambda: sync.calls == 1)
        time.sleep(0.2)
        assert sync.calls == 1
    finally:
        auto.close()


def test_the_catch_all_runs_only_while_the_session_is_in_use(repo: Path, state: Path) -> None:
    sync = _Counter()
    auto = AutoSync(sync, repo, state, debounce_s=0.01, idle_check_s=0.05, active_window_s=0.3, listen=False)
    auto.start()
    try:
        time.sleep(0.2)
        assert sync.calls == 0  # never used: no polling at all
        auto.touch()
        _eventually(lambda: sync.calls >= 1)
        time.sleep(0.5)  # the active window closes
        settled = sync.calls
        time.sleep(0.3)
        assert sync.calls == settled
    finally:
        auto.close()


def test_a_busy_session_is_retried(repo: Path, state: Path) -> None:
    sync = _Counter([None, None, False])
    auto = AutoSync(sync, repo, state, debounce_s=0.01, idle_check_s=3600, listen=False)
    auto.start()
    try:
        auto.nudge()
        _eventually(lambda: sync.calls == 3)
    finally:
        auto.close()


def test_a_hook_nudge_reaches_the_listening_client_and_close_cleans_up(repo: Path, state: Path) -> None:
    sync = _Counter()
    auto = AutoSync(sync, repo, state, debounce_s=0.01, idle_check_s=3600)
    auto.start()
    path = auto.socket_path
    assert path is not None and path.parent == socket_dir(repo, state)
    assert oct(path.stat().st_mode & 0o777) == oct(0o600)
    assert oct(path.parent.stat().st_mode & 0o777) == oct(0o700)
    try:
        assert nudge_workspace(repo, state) == 1
        _eventually(lambda: sync.calls == 1)
    finally:
        auto.close()
    assert not path.exists() and not path.parent.exists()  # what it created, it removes
    assert not any(thread.name.startswith("lc-autosync") for thread in threading.enumerate())


def test_a_dead_clients_socket_is_removed_by_the_next_nudge(repo: Path, state: Path) -> None:
    directory = socket_dir(repo, state)
    directory.mkdir(parents=True, mode=0o700)
    stale = directory / "12345-dead.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as gone:
        gone.bind(str(stale))
    assert nudge_workspace(repo, state) == 0
    assert not stale.exists()


@pytest.mark.parametrize("planted", ["state", "sync"])
def test_a_directory_others_can_write_is_refused(repo: Path, state: Path, planted: str) -> None:
    if planted == "state":
        os.chmod(state, 0o777)  # anyone could swap sync/ out from under us
    else:
        (state / "sync").mkdir(mode=0o700)
        os.chmod(state / "sync", 0o770)
    sync = _Counter()
    auto = AutoSync(sync, repo, state, debounce_s=0.01, idle_check_s=3600)
    auto.start()
    try:
        assert auto.socket_path is None  # no listener; in-process checks still work
        auto.nudge()
        _eventually(lambda: sync.calls == 1)
    finally:
        auto.close()


def test_hooks_resolve_the_same_state_directory_as_the_client(tmp_path: Path) -> None:
    for env in ({"LEMONCROW_HOME": str(tmp_path / "custom")}, {"HOME": str(tmp_path / "home")}):
        full = {"LEMONCROW_URL": "http://127.0.0.1:9", "HOME": str(tmp_path / "home"), **env}
        assert default_state_dir(full) == load_config(full, cwd=tmp_path).state_dir


def test_hooks_follow_the_store_root_lc_mcp_hands_the_client(tmp_path: Path) -> None:
    env = {"LEMONCROW_URL": "http://127.0.0.1:9", "HOME": str(tmp_path / "home"), "LEMONCROW_ROOT": str(tmp_path / "r")}
    served = {**env, "LEMONCROW_HOME": env["LEMONCROW_ROOT"]}  # LEMONCROW_ROOT handed over as LEMONCROW_HOME
    assert default_state_dir(env) == load_config(served, cwd=tmp_path).state_dir


def test_a_host_edit_reaches_the_server_before_the_next_call(
    stub: StubServer, worktree: Path, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: host edit -> hook nudge -> socket -> background sync -> server."""
    monkeypatch.delenv("LEMONCROW_AUTOSYNC")
    config = make_config(url=stub.url, worktree=worktree, state_dir=state)
    session = RemoteSession(config)
    assert session.bootstrap(deadline_s=10.0).ok
    server = McpServer(config, session)
    try:
        params = {"name": "grep", "arguments": {"pattern": "alpha"}}
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
        edited = b"def alpha():\n    return 'FROM_HOST_EDIT'\n"
        (worktree / "pkg" / "alpha.py").write_bytes(edited)
        assert nudge_workspace(worktree, state) == 1
        _eventually(lambda: stub.state.content_for("pkg/alpha.py") == edited)
    finally:
        server.close()
    assert not any(thread.name.startswith("lc-autosync") for thread in threading.enumerate())

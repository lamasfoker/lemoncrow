"""Ordinary sync reads only Git-selected dirty files, and MCP never waits for it."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest
from conftest import make_config
from lemoncrow_client.config import is_index_candidate
from lemoncrow_client.errors import ClientError, ErrorCode
from lemoncrow_client.git_sync import committed_base, dirty_paths
from lemoncrow_client.manifest import manifest_root, walk_worktree
from lemoncrow_client.mcpserver import McpServer
from lemoncrow_client.session import RemoteSession
from lemoncrow_client.walkcache import WalkCache


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_stale_git_metadata_is_not_a_repository(tmp_path, stub, state_dir):
    root = tmp_path / "plain"
    (root / ".git" / "lemoncrow").mkdir(parents=True)
    (root / "large.txt").write_text("unrelated")
    assert not is_index_candidate(root)
    session = RemoteSession(make_config(url=stub.url, worktree=root, state_dir=state_dir))
    try:
        report = session.bootstrap()
        assert report.ok and report.not_indexed and report.files == 0
        assert stub.state.view_id == ""
    finally:
        session.close()


def test_explicit_subdirectory_is_not_a_repository_root(worktree):
    assert is_index_candidate(worktree)
    assert not is_index_candidate(worktree / "pkg")


def test_sync_never_reads_clean_worktree_files(worktree, stub, state_dir, monkeypatch):
    root = worktree
    config = make_config(url=stub.url, worktree=root, state_dir=state_dir)
    session = RemoteSession(config)
    real_open = Path.open
    reads = []

    def observe(path, *args, **kwargs):
        if path.is_relative_to(root) and ".git" not in path.relative_to(root).parts:
            reads.append(path.relative_to(root).as_posix())
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observe)
    try:
        assert session.bootstrap(deadline_s=10).ok
        assert reads == []
        (root / "pkg/alpha.py").write_text("def alpha(): return 'modified'\n")
        (root / "new file.py").write_text("NEW = 1\n")
        (root / "pkg/beta.py").unlink()
        (root / "ignored.log").write_text("ignored")
        reads.clear()
        session.call_tool("read", {"files": ["pkg/alpha.py"]})
        assert set(reads) <= {"pkg/alpha.py", "new file.py"}
        assert b"modified" in stub.state.content_for("pkg/alpha.py")
        assert stub.state.content_for("new file.py") == b"NEW = 1\n"
        assert "pkg/beta.py" not in stub.state.manifest
        assert "ignored.log" not in stub.state.manifest

        git(root, "restore", "pkg/alpha.py", "pkg/beta.py")
        (root / "new file.py").unlink()
        reads.clear()
        session.call_tool("read", {"files": ["pkg/alpha.py"]})
        assert reads == [], "restoring the base must use already-held committed rows"
        assert b"'alpha'" in stub.state.content_for("pkg/alpha.py")
        assert "pkg/beta.py" in stub.state.manifest
        assert "new file.py" not in stub.state.manifest
        assert session._git_overlay_paths == frozenset()

        (root / "pkg/alpha.py").write_text("COMMITTED = 2\n")
        git(root, "add", "pkg/alpha.py")
        git(root, "-c", "user.name=Tests", "-c", "user.email=tests@example.invalid", "commit", "-qm", "next")
        reads.clear()
        session.call_tool("read", {"files": ["pkg/alpha.py"]})
        assert reads == []
        assert stub.state.content_for("pkg/alpha.py") == b"COMMITTED = 2\n"
    finally:
        session.close()


def test_warm_dirty_session_reuses_committed_blob_digests(worktree, stub, state_dir, monkeypatch):
    from lemoncrow_client import git_sync

    (worktree / "pkg/alpha.py").write_text("DIRTY = True\n")
    config = make_config(url=stub.url, worktree=worktree, state_dir=state_dir)
    first = RemoteSession(config)
    assert first.bootstrap(deadline_s=10).ok
    first.close()

    def unexpected_objects(*args, **kwargs):
        raise AssertionError("a held base must not reread Git blob contents")

    monkeypatch.setattr(git_sync, "_objects", unexpected_objects)
    second = RemoteSession(config)
    try:
        report = second.bootstrap(deadline_s=10)
        assert report.ok, report.reason
        assert second._git_base.hashed == 0
        assert report.uploaded_bytes == 0
    finally:
        second.close()


def test_git_delta_matches_full_snapshot_for_changes_and_restoration(worktree):
    from lemoncrow_client.git_sync import overlay_entries

    cache = WalkCache(worktree)
    cache.begin()
    base = committed_base(worktree, cache, max_files=100)
    for index in range(8):
        changed = worktree / "pkg/alpha.py"
        changed.write_text(f"VALUE = {index}\n")
        git(worktree, "add", "pkg/alpha.py")
        changed.write_text(f"VALUE = {index + 1}\n")  # staged and unstaged differ
        new = worktree / f"new {index}.py"
        new.write_bytes(b"binary\x00\nbytes")
        if index % 2:
            (worktree / "pkg/beta.py").unlink()
        paths = dirty_paths(worktree, max_files=100)
        rows = overlay_entries(worktree, tuple(paths))
        effective = dict(base.entries)
        for path in paths:
            effective.pop(path, None)
        effective.update(rows)
        assert manifest_root(tuple(effective.values())) == walk_worktree(worktree, size_cap=1024**2).root()
        git(worktree, "restore", "--staged", "--worktree", ".")
        new.unlink()


def test_limits_refuse_instead_of_silently_truncating(worktree):
    cache = WalkCache(worktree)
    cache.begin()
    count = len(walk_worktree(worktree, size_cap=1024**2).files)
    assert len(committed_base(worktree, cache, max_files=count).entries) == count
    with pytest.raises(ClientError) as raised:
        committed_base(worktree, cache, max_files=count - 1)
    assert raised.value.code is ErrorCode.REQUEST_TOO_LARGE
    (worktree / "one.py").write_text("one")
    (worktree / "two.py").write_text("two")
    assert len(dirty_paths(worktree, max_files=2)) == 2
    with pytest.raises(ClientError):
        dirty_paths(worktree, max_files=1)


def test_initialize_and_bash_do_not_wait_for_sync(config, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = RemoteSession.bootstrap

    def held(session, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(session, **kwargs)

    monkeypatch.setattr(RemoteSession, "bootstrap", held)
    server = McpServer(config)
    try:
        started = time.monotonic()
        answer = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert time.monotonic() - started < 0.5
        assert "bootstrap pending" in answer["result"]["instructions"]
        assert entered.wait(2)
        listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert len(listed["result"]["tools"]) == 5
        bash = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "bash", "arguments": {"command": "true"}},
            }
        )
        assert not bash["result"]["isError"]
    finally:
        release.set()
        server.close()


def test_path_push_does_not_hide_other_editor_changes(worktree, stub, state_dir):
    session = RemoteSession(make_config(url=stub.url, worktree=worktree, state_dir=state_dir))
    try:
        assert session.bootstrap(deadline_s=10).ok
        (worktree / "pkg/alpha.py").write_text("ALPHA = 2\n")
        (worktree / "pkg/beta.py").write_text("BETA = 3\n")
        session.push_paths(("pkg/alpha.py",))
        session.call_tool("code_search", {"query": "BETA"})
        assert stub.state.content_for("pkg/beta.py") == b"BETA = 3\n"
    finally:
        session.close()


def test_restoring_after_path_push_cannot_reuse_old_workspace_proof(worktree, stub, state_dir):
    from lemoncrow_client.liveview import load_live_view_binding

    session = RemoteSession(make_config(url=stub.url, worktree=worktree, state_dir=state_dir))
    try:
        assert session.bootstrap(deadline_s=10).ok
        (worktree / "pkg/alpha.py").write_text("ALPHA = 2\n")
        session.push_paths(("pkg/alpha.py",))
        assert load_live_view_binding(state_dir, worktree, server_url=session._config.url) is None
        git(worktree, "restore", "pkg/alpha.py")
        session.call_tool("code_search", {"query": "alpha"})
        assert b"'alpha'" in stub.state.content_for("pkg/alpha.py")
        assert session._git_overlay_paths == frozenset()
    finally:
        session.close()


def test_git_timeout_is_a_reported_sync_failure(worktree, monkeypatch):
    from lemoncrow_client import git_sync

    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 60)

    monkeypatch.setattr(git_sync, "_generation_git", timed_out)
    with pytest.raises(ClientError) as caught:
        dirty_paths(worktree, max_files=100)
    assert caught.value.retryable


def test_symlink_overlay_never_reads_target_outside_repository(worktree, tmp_path):
    from lemoncrow_client.git_sync import overlay_entries

    outside = tmp_path / "outside"
    outside.write_text("private bytes")
    (worktree / "pkg/alpha.py").unlink()
    (worktree / "pkg/alpha.py").symlink_to(outside)
    assert overlay_entries(worktree, ("pkg/alpha.py",)) == {}

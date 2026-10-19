"""Recovery must tolerate view opens slower than the MCP initialization budget."""

from __future__ import annotations

import hashlib
import time
from dataclasses import replace

import pytest
from _stub import _error, _Handler
from lemoncrow_client.errors import ClientError, ErrorCode
from lemoncrow_client.mcpserver import McpServer
from lemoncrow_client.session import RemoteSession


@pytest.mark.parametrize("code", ["session_unknown", "session_expired", "view_unknown"])
def test_reopen_uses_request_budget(config, monkeypatch, code):
    config = replace(config, startup_budget_s=0.01, request_timeout_s=1.0)
    session = RemoteSession(config)
    try:
        assert session.bootstrap(deadline_s=2).ok
        original_tool = _Handler._tool
        original_view = _Handler._open_view
        attempts = 0

        def tool(handler, name, body):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                handler._send(404, _error(code, "session lost during restart"))
            else:
                original_tool(handler, name, body)

        def slow_view(handler, body):
            time.sleep(0.08)
            original_view(handler, body)

        monkeypatch.setattr(_Handler, "_tool", tool)
        monkeypatch.setattr(_Handler, "_open_view", slow_view)
        result = session._dispatch("read", {"files": ["pkg/alpha.py"]})
        assert not result["is_error"]
        assert attempts == 2
        assert session.state.online
    finally:
        session.close()


def test_explicit_mcp_retry_uses_request_budget(config, monkeypatch):
    config = replace(config, startup_budget_s=0.01, request_timeout_s=1.0)
    server = McpServer(config)
    original = _Handler._open_view

    def slow_view(handler, body):
        time.sleep(0.08)
        original(handler, body)

    monkeypatch.setattr(_Handler, "_open_view", slow_view)
    try:
        server._ensure_session(retry=True)
        assert server._session.bootstrapped
        assert server._session.state.online
    finally:
        server._session.close()


def test_an_edit_after_a_server_restart_reopens_and_lands(config, stub, worktree, monkeypatch):
    """The push after a local edit re-opens a lost session instead of leaving
    this and every later edit refused as ``session_unknown``."""
    session = RemoteSession(config)
    try:
        assert session.bootstrap(deadline_s=2).ok
        original_overlay = _Handler._overlay
        refused = 0

        def restarted_once(handler, body):
            nonlocal refused
            if refused == 0:
                refused += 1
                handler._send(404, _error("session_unknown", "session is not available", action="reopen_session"))
                return
            original_overlay(handler, body)

        monkeypatch.setattr(_Handler, "_overlay", restarted_once)
        (worktree / "pkg" / "alpha.py").write_bytes(b"def alpha():\n    return 'edited'\n")
        session.push_paths(("pkg/alpha.py",))
        assert refused == 1
        assert stub.state.content_for("pkg/alpha.py") == b"def alpha():\n    return 'edited'\n"
        events = [event for event in session.state.recovery_events if event.kind == "session_reopen"]
        assert [event.outcome for event in events[-2:]] == ["retrying", "recovered"]
        assert events[-1].tool == "edit"
    finally:
        session.close()


def test_a_write_refused_by_a_pinned_wal_is_sent_again(config, stub, worktree, monkeypatch):
    """A hosted server refuses at once, from its event loop, when a reader pins
    its WAL; the client waits that read out instead of failing the edit."""
    session = RemoteSession(config)
    try:
        assert session.bootstrap(deadline_s=2).ok
        original_overlay = _Handler._overlay
        refused = 0

        def pinned_once(handler, body):
            nonlocal refused
            if refused == 0:
                refused += 1
                handler._send(
                    507,
                    _error("storage_limit", "WAL pinned", retryable=True, action="retry_later", kind="wal"),
                )
                return
            original_overlay(handler, body)

        monkeypatch.setattr(_Handler, "_overlay", pinned_once)
        (worktree / "pkg" / "alpha.py").write_bytes(b"def alpha():\n    return 'wal'\n")
        session.push_paths(("pkg/alpha.py",))
        assert refused == 1
        assert stub.state.content_for("pkg/alpha.py") == b"def alpha():\n    return 'wal'\n"
    finally:
        session.close()


def test_a_wal_that_stays_pinned_still_refuses_the_write(config, stub, worktree, monkeypatch):
    from lemoncrow_client import blobs

    monkeypatch.setattr(blobs, "_WAL_BACKOFF_S", 0.2)
    session = RemoteSession(config)
    try:
        assert session.bootstrap(deadline_s=2).ok

        def always_pinned(handler, body):
            handler._send(
                507,
                _error("storage_limit", "WAL pinned", retryable=True, action="retry_later", kind="wal"),
            )

        monkeypatch.setattr(_Handler, "_overlay", always_pinned)
        (worktree / "pkg" / "alpha.py").write_bytes(b"def alpha():\n    return 'wal'\n")
        with pytest.raises(ClientError) as refusal:
            session.push_paths(("pkg/alpha.py",))
        assert refusal.value.code is ErrorCode.STORAGE_LIMIT
    finally:
        session.close()


def test_a_fill_resends_content_the_client_remembers_uploading(config, stub, worktree):
    """``need`` outranks the client's acknowledged-digest cache: content the
    server lost after acknowledging it (restart, collection) is sent again."""
    session = RemoteSession(config)
    try:
        assert session.bootstrap(deadline_s=2).ok
        digest = hashlib.sha256((worktree / "pkg" / "alpha.py").read_bytes()).hexdigest()
        session.state.remember_server_digests([digest])
        stub.state.blobs.pop(digest, None)
        stub.state.miss_once["read"] = ("pkg/alpha.py",)
        answer = session.call_tool("read", {"symbol": "alpha"})
        assert not answer.is_error
        assert digest in stub.state.blobs
    finally:
        session.close()


def test_timed_out_tool_is_never_replayed(config, monkeypatch):
    session = RemoteSession(config)
    attempts = 0

    def timeout(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise ClientError(ErrorCode.SERVER_UNREACHABLE, "timed out", details={"timed_out": True})

    monkeypatch.setattr(RemoteSession, "_post_tool", timeout)
    try:
        with pytest.raises(ClientError):
            session._dispatch("read", {"files": ["pkg/alpha.py"]})
        assert attempts == 1
    finally:
        session.close()

"""The degradation table, row by row.

The distinction the design draws, and the one this module exists to hold, is
between a server that was **never there** and one that **went away**:

* never there -> client-side tools work, ``read`` falls back to a bounded file
  read flagged degraded, and one visible line says why;
* went away -> server-side tools return the typed error the agent can act on,
  and client-side tools are unaffected.

And in neither case is a local index built. There is no code path in the
package that could build one, which is asserted here as well as claimed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _audit_source import code_only, iter_modules
from _stub import StubServer
from lemoncrow_client.config import ClientConfig
from lemoncrow_client.dispatcher import Dispatcher
from lemoncrow_client.errors import ErrorCode
from lemoncrow_client.mcpserver import McpServer
from lemoncrow_client.session import RemoteSession


@pytest.fixture
def unreachable_config(worktree: Path, state_dir: Path) -> ClientConfig:
    """A configuration pointing at a port nothing is listening on."""
    # Bind and immediately release, so the port is real and certainly closed.
    import socket

    from conftest import make_config

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return make_config(
        url=f"http://127.0.0.1:{port}",
        worktree=worktree,
        state_dir=state_dir,
        LEMONCROW_REQUEST_TIMEOUT_S="1",
    )


# --------------------------------------------------------------------------- #
# Server unreachable at MCP initialize                                       #
# --------------------------------------------------------------------------- #


def test_an_unreachable_server_leaves_client_tools_working(unreachable_config: ClientConfig) -> None:
    session = RemoteSession(unreachable_config)
    report = session.bootstrap(deadline_s=2.0)
    assert not report.ok
    assert not session.bootstrapped

    dispatcher = Dispatcher(unreachable_config, session)
    outcome = dispatcher.call("bash", {"command": "printf offline-ok"})
    assert not outcome.is_error
    assert "offline-ok" in outcome.content[0]["text"]
    assert dispatcher.call("grep", {"regex": "def alpha"}).content


def test_an_unreachable_server_produces_exactly_one_visible_line(
    unreachable_config: ClientConfig,
) -> None:
    report = RemoteSession(unreachable_config).bootstrap(deadline_s=2.0)
    line = report.one_line(unreachable_config.url)
    assert line.count("\n") == 0
    assert "client-side tools still work" in line
    assert "server_unreachable" in report.reason


def test_a_degraded_answer_carries_one_visible_line_saying_why() -> None:
    from lemoncrow_client.dispatcher import ToolOutcome

    rendered = ToolOutcome(
        content=({"type": "text", "text": "relations ok"},), degraded=True, degraded_reason="link:building"
    ).to_mcp()
    assert [block["text"] for block in rendered["content"]] == ["relations ok", "! degraded: link:building"]
    assert rendered["_meta"]["lemoncrow/degraded_reason"] == "link:building"

    already = ToolOutcome(
        content=({"type": "text", "text": "hits\n\n! degraded: semantic:building"},),
        degraded=True,
        degraded_reason="semantic:building",
    ).to_mcp()
    assert len(already["content"]) == 1  # code_search renders its own line; never twice
    assert "_meta" not in ToolOutcome(content=({"type": "text", "text": "ok"},)).to_mcp()


def test_ordinary_read_stays_local_when_server_is_unreachable(unreachable_config: ClientConfig) -> None:
    """Standard file reads execute locally from disk without degrading."""
    session = RemoteSession(unreachable_config)
    session.bootstrap(deadline_s=2.0)
    outcome = Dispatcher(unreachable_config, session).call("read", {"files": ["pkg/alpha.py"]})
    assert not outcome.is_error
    assert not outcome.degraded
    assert outcome.site == "client"
    assert "def alpha" in outcome.content[0]["text"]
    assert "_meta" not in outcome.to_mcp() or not outcome.to_mcp().get("_meta", {}).get("lemoncrow/degraded")


def test_online_absolute_read_stays_local(
    bootstrapped: RemoteSession,
    config: ClientConfig,
    worktree: Path,
) -> None:
    target = worktree.parent / "outside-skill.md"
    target.write_text("absolute local read\n", encoding="utf-8")
    outcome = Dispatcher(config, bootstrapped).call("read", {"files": [f"{target}:full"]})
    assert not outcome.is_error
    assert not outcome.degraded
    assert outcome.site == "client"
    assert "absolute local read" in outcome.content[0]["text"]


def test_online_absolute_read_entries_use_the_read_grammar(
    bootstrapped: RemoteSession,
    config: ClientConfig,
    worktree: Path,
) -> None:
    target = worktree.parent / "outside-notes.md"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    dispatcher = Dispatcher(config, bootstrapped)
    ranged = dispatcher.call("read", {"files": [f"{target}:2-3"]})
    assert ranged.site == "client"
    assert "2\ttwo\n3\tthree" in ranged.content[0]["text"]
    keyed = dispatcher.call("read", {"files": [{"file_path": str(target), "range": "L1"}]})
    assert keyed.site == "client"
    assert "1\tone" in keyed.content[0]["text"]


def test_every_other_server_tool_refuses_with_a_typed_error(
    unreachable_config: ClientConfig,
) -> None:
    session = RemoteSession(unreachable_config)
    session.bootstrap(deadline_s=2.0)
    outcome = Dispatcher(unreachable_config, session).call("code_search", {"query": "alpha"})
    assert outcome.is_error
    body = outcome.content[0]["text"]
    assert ErrorCode.SERVER_SESSION_UNAVAILABLE.value in body
    assert "action=retry_later" in body
    payload = json.loads(body.splitlines()[-1])
    assert payload["retryable"] is True


def test_a_directory_outside_git_is_never_indexed_and_says_so(
    stub: StubServer, tmp_path: Path, state_dir: Path
) -> None:
    """A plain folder (a home, a folder of projects) opens no view and uploads nothing."""
    from conftest import make_config

    plain = tmp_path / "projects"
    (plain / "nested").mkdir(parents=True)
    (plain / "nested" / "alpha.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    config = make_config(url=stub.url, worktree=plain, state_dir=state_dir)
    session = RemoteSession(config)
    report = session.bootstrap(deadline_s=10.0)
    assert report.ok and session.bootstrapped and not session.indexed
    assert stub.state.view_id == "" and not stub.state.blobs and not stub.state.manifest
    line = report.one_line(config.url)
    assert line.count("\n") == 0 and "is not a git repository" in line

    dispatcher = Dispatcher(config, session)
    for tool, arguments in (
        ("code_search", {"query": "alpha"}),
        ("search", {"query": "alpha"}),
        ("relations", {"path": "nested/alpha.py"}),
        ("index", {}),
    ):
        outcome = dispatcher.call(tool, arguments)
        assert outcome.is_error, tool
        payload = json.loads(outcome.content[0]["text"].splitlines()[-1])
        assert payload["code"] == ErrorCode.NOT_INDEXED.value, tool
        assert payload["retryable"] is False
        assert payload["action"] == "use_client_tool"
        assert "not a git repository" in payload["message"]
    read = dispatcher.call("read", {"files": ["nested/alpha.py"]})
    assert not read.is_error and not read.degraded and read.site == "client"
    assert "def alpha" in read.content[0]["text"]
    assert stub.state.tool_calls == []


def test_index_refuses_rather_than_building_one_locally(unreachable_config: ClientConfig) -> None:
    session = RemoteSession(unreachable_config)
    session.bootstrap(deadline_s=2.0)
    outcome = Dispatcher(unreachable_config, session).call("index", {})
    assert outcome.is_error
    assert "never builds a local index" in outcome.content[0]["text"]


#: The hosted thin-client execution path. It must never reach Runtime Lite,
#: whose repo-local index is a separate, local-only execution boundary.
_THIN_CLIENT_MODULES = frozenset({"mcpserver.py", "dispatcher.py", "session.py"})


def _imports(path: Path) -> tuple[set[str], set[str]]:
    """Top-level absolute imports, and relative/absolute package-local imports."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    absolute: set[str] = set()
    local: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            absolute |= {alias.name.split(".")[0] for alias in node.names}
            local |= {alias.name for alias in node.names if alias.name.startswith("lemoncrow_client.")}
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0:
                absolute.add(module.split(".")[0])
                if module.startswith("lemoncrow_client."):
                    local.add(module.removeprefix("lemoncrow_client."))
            else:
                local.add(module)
                local |= {alias.name for alias in node.names if not module}
    return absolute, local


def test_nothing_in_the_thin_client_can_build_a_local_index() -> None:
    """A structural check behind the behavioural one.

    An index needs somewhere to put itself. Outside Runtime Lite (``lite/``,
    whose repo-local index is its purpose and which never talks to a server),
    the package imports no persistence module and no numeric or model runtime
    except the ``sql`` tool's engine (``kit/sql.py``), which connects to the
    *developer's own* database and, on a read, creates nothing; and it issues no
    DDL at all. The thin-client execution path never imports Lite.
    """
    persistence = {"sqlite3", "dbm", "shelve", "pickle", "marshal", "numpy", "torch"}
    for path in iter_modules():
        if path.parent.name == "lite":
            continue
        absolute, local = _imports(path)
        offending = absolute & persistence
        if path.name == "sql.py" and path.parent.name == "kit":
            # The one exception, and it is narrow: the developer's database.
            assert offending == {"sqlite3"}, f"kit/sql.py imports {offending}"
            continue
        assert not offending, f"{path.name} imports {sorted(offending)}"
        if path.name in _THIN_CLIENT_MODULES and path.parent.name == "lemoncrow_client":
            lite = sorted(name for name in local if name == "lite" or name.startswith("lite."))
            assert not lite, f"thin client {path.name} imports Runtime Lite: {lite}"

    for path in iter_modules():
        if path.parent.name == "lite":
            continue
        body = code_only(path).upper()
        for statement in ("CREATE TABLE", "CREATE INDEX", "CREATE VIRTUAL TABLE"):
            assert statement not in body, f"{path.name} issues {statement!r}"


def test_an_unreachable_server_writes_nothing_into_the_worktree(
    unreachable_config: ClientConfig, worktree: Path
) -> None:
    before = {path for path in worktree.rglob("*")}
    session = RemoteSession(unreachable_config)
    session.bootstrap(deadline_s=2.0)
    Dispatcher(unreachable_config, session).call("read", {"files": ["pkg/alpha.py"]})
    assert {path for path in worktree.rglob("*")} == before


def test_mcp_initialize_stays_alive_and_reports_one_process_degradation(unreachable_config: ClientConfig) -> None:
    server = McpServer(unreachable_config)
    answer = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert answer is not None
    server._ensure_session(retry=True)
    instructions = server._session_status()
    assert "server-side tools unavailable" in instructions
    assert "client-side tools still work" in instructions
    assert server.dispatcher.session.bootstrapped is False
    assert server.dispatcher.session.state.reason
    server.close()


# --------------------------------------------------------------------------- #
# Server lost mid-session                                                     #
# --------------------------------------------------------------------------- #


def test_a_server_lost_mid_session_returns_typed_errors_not_a_local_answer(
    stub: StubServer, bootstrapped: RemoteSession, config: ClientConfig
) -> None:
    dispatcher = Dispatcher(config, bootstrapped)
    assert not dispatcher.call("relations", {"path": "pkg/alpha.py"}).is_error

    stub.close()
    outcome = dispatcher.call("relations", {"path": "pkg/alpha.py"})
    assert outcome.is_error
    assert outcome.site == "refused"
    assert ErrorCode.SERVER_UNREACHABLE.value in outcome.content[0]["text"]
    assert not outcome.degraded


def test_client_tools_are_unaffected_by_a_mid_session_loss(
    stub: StubServer, bootstrapped: RemoteSession, config: ClientConfig
) -> None:
    stub.close()
    dispatcher = Dispatcher(config, bootstrapped)
    assert "still-here" in dispatcher.call("bash", {"command": "printf still-here"}).content[0]["text"]
    assert dispatcher.call("grep", {"regex": "def"}).content


def test_an_edit_that_cannot_be_pushed_keeps_the_write_and_says_the_view_is_behind(
    stub: StubServer, bootstrapped: RemoteSession, config: ClientConfig, worktree: Path
) -> None:
    """The file is already written; losing that would be worse than a warning.

    And reporting it as an error is how agents came to redo applied edits: the
    edit succeeded, so the result says so, flagged with why the View lags.
    """
    stub.close()
    outcome = Dispatcher(config, bootstrapped).call(
        "edit", {"edits": [{"path": "pkg/alpha.py", "old": "'alpha'", "new": "'LOCAL'"}]}
    )
    assert (worktree / "pkg/alpha.py").read_text() == "def alpha():\n    return 'LOCAL'\n"
    assert not outcome.is_error
    assert outcome.degraded
    assert "the server's view is behind" in outcome.degraded_reason
    rendered = outcome.to_mcp()
    assert any("the server's view is behind" in block["text"] for block in rendered["content"])


def test_a_degraded_server_answer_is_carried_through_unchanged(
    stub: StubServer, bootstrapped: RemoteSession, config: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cold link layer flags the answer; the client must not hide the flag."""
    from lemoncrow_client import session as session_module

    original = session_module._unwrap

    def flagged(tool, payload, fallback):  # type: ignore[no-untyped-def]
        answer = original(tool, dict(payload) | {"degraded": True, "degraded_reason": "link:cold"}, fallback)
        return answer

    monkeypatch.setattr(session_module, "_unwrap", flagged)
    outcome = Dispatcher(config, bootstrapped).call("relations", {"path": "pkg/beta.py"})
    assert outcome.degraded
    assert outcome.degraded_reason == "link:cold"


def test_session_unknown_reopens_once_and_retries_the_tool(
    stub: StubServer, bootstrapped: RemoteSession, config: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher = Dispatcher(config, bootstrapped)
    original_post_tool = RemoteSession._post_tool
    original_bootstrap = RemoteSession.bootstrap
    calls = {"post_tool": 0, "bootstrap": 0}

    def stale_once(self: RemoteSession, tool: str, arguments, *, reuse_validator: str = ""):
        calls["post_tool"] += 1
        if calls["post_tool"] == 1:
            from lemoncrow_client.errors import AgentAction, ClientError

            raise ClientError(
                ErrorCode.SESSION_UNKNOWN,
                "session is not available",
                action=AgentAction.REOPEN_SESSION,
            )
        return original_post_tool(self, tool, arguments, reuse_validator=reuse_validator)

    def counted_bootstrap(self: RemoteSession, **kwargs):
        calls["bootstrap"] += 1
        return original_bootstrap(self, **kwargs)

    monkeypatch.setattr(RemoteSession, "_post_tool", stale_once)
    monkeypatch.setattr(RemoteSession, "bootstrap", counted_bootstrap)
    outcome = dispatcher.call("read", {"symbol": "alpha"})
    assert not outcome.is_error
    assert calls == {"post_tool": 2, "bootstrap": 1}
    assert bootstrapped.state.online is True
    events = [event for event in bootstrapped.state.recovery_events if event.kind == "session_reopen"]
    assert [event.outcome for event in events[-2:]] == ["retrying", "recovered"]
    assert events[-1].tool == "read"


def test_view_unknown_reopens_once_and_retries_the_tool(
    stub: StubServer, bootstrapped: RemoteSession, config: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher = Dispatcher(config, bootstrapped)
    original_post_tool = RemoteSession._post_tool
    original_bootstrap = RemoteSession.bootstrap
    calls = {"post_tool": 0, "bootstrap": 0}

    def stale_once(self: RemoteSession, tool: str, arguments, *, reuse_validator: str = ""):
        calls["post_tool"] += 1
        if calls["post_tool"] == 1:
            from lemoncrow_client.errors import AgentAction, ClientError

            raise ClientError(ErrorCode.VIEW_UNKNOWN, "view is not bound", action=AgentAction.REOPEN_VIEW)
        return original_post_tool(self, tool, arguments, reuse_validator=reuse_validator)

    def counted_bootstrap(self: RemoteSession, **kwargs):
        calls["bootstrap"] += 1
        return original_bootstrap(self, **kwargs)

    monkeypatch.setattr(RemoteSession, "_post_tool", stale_once)
    monkeypatch.setattr(RemoteSession, "bootstrap", counted_bootstrap)
    outcome = dispatcher.call("read", {"symbol": "alpha"})
    assert not outcome.is_error
    assert calls == {"post_tool": 2, "bootstrap": 1}
    events = [event for event in bootstrapped.state.recovery_events if event.kind == "session_reopen"]
    assert [event.outcome for event in events[-2:]] == ["retrying", "recovered"]
    assert events[-1].error_code == ErrorCode.VIEW_UNKNOWN.value


def test_session_unknown_rebootstrap_failure_returns_retryable_typed_error(
    bootstrapped: RemoteSession, config: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher = Dispatcher(config, bootstrapped)

    from lemoncrow_client.errors import AgentAction, ClientError
    from lemoncrow_client.session import BootstrapReport

    monkeypatch.setattr(
        RemoteSession,
        "_post_tool",
        lambda self, *args, **kwargs: (_ for _ in ()).throw(
            ClientError(ErrorCode.SESSION_UNKNOWN, "gone", action=AgentAction.REOPEN_SESSION)
        ),
    )
    monkeypatch.setattr(
        RemoteSession,
        "bootstrap",
        lambda self, **kwargs: BootstrapReport(
            ok=False,
            reason="server still starting",
            session_id="",
            view_id="",
            view_revision=0,
            warm=False,
            files=0,
            uploaded_blobs=0,
            uploaded_bytes=0,
            manifest_chunks_sent=0,
            elapsed_s=0.0,
        ),
    )
    outcome = dispatcher.call("code_search", {"query": "alpha"})
    assert outcome.is_error
    body = outcome.content[0]["text"]
    assert ErrorCode.SERVER_SESSION_UNAVAILABLE.value in body
    assert "retry_later" in body
    events = [event for event in bootstrapped.state.recovery_events if event.kind == "session_reopen"]
    assert [event.outcome for event in events[-2:]] == ["retrying", "failed"]


def test_project_id_is_refused_rather_than_silently_ignored(unreachable_config: ClientConfig) -> None:
    """This client serves one workspace; running the call there would be the wrong tree."""
    dispatcher = Dispatcher(unreachable_config, RemoteSession(unreachable_config))

    for tool in ("bash", "edit", "read", "code_search"):
        outcome = dispatcher.call(tool, {"project_id": "proj_00000000000000000000"})
        assert outcome.is_error, tool
        assert "project_id" in outcome.content[0]["text"], tool

    ran = dispatcher.call("bash", {"command": "printf here", "project_id": ""})
    assert not ran.is_error and "here" in ran.content[0]["text"]

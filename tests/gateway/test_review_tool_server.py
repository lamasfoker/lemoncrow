from __future__ import annotations

from pathlib import Path

from lemoncrow.pro.capabilities.review import tool_server


def _request(method: str, *, request_id: int = 1, **params):
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params:
        payload["params"] = params
    response = tool_server.handle(payload)
    assert response is not None
    return response


def _call(name: str, arguments: dict):
    return _request("tools/call", name=name, arguments=arguments)["result"]


def test_autonomous_profile_exposes_only_snapshot_read_tools(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    monkeypatch.setenv("LEMONCROW_REVIEW_SNAPSHOT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_MCP_TOOL_PROFILE", "review_autonomous")

    listed = _request("tools/list")["result"]["tools"]
    assert {tool["name"] for tool in listed} == {"code_search", "grep", "read"}
    assert _call("bash", {"command": "pwd"})["isError"] is True

    searched = _call("code_search", {"query": "answer", "limit": 3})
    assert searched["isError"] is False
    assert "src/app.py:L" in searched["content"][0]["text"]


def test_review_profile_confines_source_and_runs_bash_only_in_execution_copy(monkeypatch, tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    execution = tmp_path / "execution"
    snapshot.mkdir()
    execution.mkdir()
    (snapshot / "safe.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    (execution / "marker.txt").write_text("execution-only\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    monkeypatch.setenv("LEMONCROW_REVIEW_SNAPSHOT_ROOT", str(snapshot))
    monkeypatch.setenv("LEMONCROW_BASH_WORKSPACE_ROOT", str(execution))
    monkeypatch.setenv("LEMONCROW_MCP_TOOL_PROFILE", "review")

    listed = _request("tools/list")["result"]["tools"]
    assert {tool["name"] for tool in listed} == {"bash", "code_search", "grep", "read", "web_fetch"}

    read = _call("read", {"path": "safe.txt", "start_line": 2, "end_line": 3})
    assert read["isError"] is False
    assert read["content"][0]["text"] == "2\tbeta\n3\tgamma"

    escaped = _call("read", {"path": "../outside.txt"})
    assert escaped["isError"] is True
    assert "leaves the Review Snapshot" in escaped["content"][0]["text"]

    grep = _call("grep", {"pattern": "beta"})
    assert grep["isError"] is False
    assert "safe.txt:2: beta" in grep["content"][0]["text"]

    from tests.gateway.test_gate_g_qualification import _shell_sandbox_works

    bash = _call("bash", {"command": "pwd; cat marker.txt"})
    text = bash["content"][0]["text"]
    if not _shell_sandbox_works():
        # Gate G: without bubblewrap/sandbox-exec Review bash never runs at all.
        assert "execution-only" not in text
        return
    assert bash["isError"] is False
    assert str(execution) in text
    assert "execution-only" in text
    assert str(snapshot) not in text


def test_search_tools_never_follow_symlinks_out_of_the_snapshot(monkeypatch, tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    outside = tmp_path / "outside"
    snapshot.mkdir()
    outside.mkdir()
    (outside / "credentials").write_text("aws_secret_access_key = leaked-secret\n", encoding="utf-8")
    (snapshot / "notes.txt").symlink_to(outside / "credentials")
    (snapshot / "docs").symlink_to(outside, target_is_directory=True)
    (snapshot / "app.py").write_text("token = 'inside'\n", encoding="utf-8")
    monkeypatch.setenv("LEMONCROW_REVIEW_SNAPSHOT_ROOT", str(snapshot))
    monkeypatch.setenv("LEMONCROW_MCP_TOOL_PROFILE", "review_autonomous")

    for scope in ("", "docs", "notes.txt"):
        grep = _call("grep", {"pattern": "secret|inside", "path": scope})
        assert "leaked-secret" not in grep["content"][0]["text"], scope
        searched = _call("code_search", {"query": "aws_secret_access_key", "path": scope})
        assert "leaked-secret" not in searched["content"][0]["text"], scope

    assert "inside" in _call("grep", {"pattern": "inside"})["content"][0]["text"]

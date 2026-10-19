from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "integrations" / "copilot-cli" / "hooks"


def _run_hook(name: str, root: Path, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["LEMONCROW_ROOT"] = str(root)
    return subprocess.run(
        [sys.executable, str(HOOKS / name)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def _run_failure(root: Path, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["LEMONCROW_ROOT"] = str(root)
    return subprocess.run(
        [sys.executable, str(HOOKS / "post_tool_use_failure.py")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_copilot_repeated_failure_injects_rescue_on_second_match(tmp_path: Path) -> None:
    payload = {
        "sessionId": "s1",
        "toolName": "bash",
        "toolArgs": {"command": "make test"},
        "error": "same failure",
    }

    first = _run_failure(tmp_path / ".lemoncrow", payload)
    second = _run_failure(tmp_path / ".lemoncrow", payload)

    assert first.returncode == 0
    assert first.stdout == ""
    assert second.returncode == 2
    assert "Call 'rescue' before any retry" in second.stdout


def test_copilot_different_failure_does_not_trigger_rescue(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    base = {
        "sessionId": "s1",
        "toolName": "bash",
        "toolArgs": {"command": "make test"},
    }

    first = _run_failure(root, {**base, "error": "failure one"})
    second = _run_failure(root, {**base, "error": "failure two"})

    assert first.returncode == 0
    assert second.returncode == 0
    assert second.stdout == ""


def test_copilot_session_start_writes_session_state_bridge(tmp_path: Path) -> None:
    """sessionStart must bridge the live session id into session_state.json.

    Copilot CLI doesn't set GITHUB_COPILOT_SESSION_ID for the MCP subprocess,
    so without this bridge every savings row is quarantined unattributed and
    the session shows Saved $0 (mcp_server._workspace_bridge_session_id reads
    this file as its fallback).
    """
    root = tmp_path / ".lemoncrow"
    ws = tmp_path / "repo"
    ws.mkdir()
    env = os.environ.copy()
    env["LEMONCROW_ROOT"] = str(root)
    env.pop("GITHUB_COPILOT_SESSION_ID", None)
    proc = subprocess.run(
        [sys.executable, str(HOOKS / "session_start.py")],
        input=json.dumps({"sessionId": "cop-sess-1", "cwd": str(ws)}),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr

    state_path = ws.resolve() / ".lemoncrow" / "workspace" / "session_state.json"
    assert state_path.is_file()
    assert json.loads(state_path.read_text(encoding="utf-8"))["session_id"] == "cop-sess-1"


def test_copilot_session_start_no_session_id_writes_no_bridge(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    ws = tmp_path / "repo"
    ws.mkdir()
    env = os.environ.copy()
    env["LEMONCROW_ROOT"] = str(root)
    env.pop("GITHUB_COPILOT_SESSION_ID", None)
    proc = subprocess.run(
        [sys.executable, str(HOOKS / "session_start.py")],
        input=json.dumps({"cwd": str(ws)}),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr

    from lemoncrow.core.foundation.paths import workspace_key

    assert not (root / "workspaces" / workspace_key(ws) / "session_state.json").exists()


def test_copilot_user_prompt_marks_exact_session_working(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    ws = tmp_path / "repo"
    ws.mkdir()
    result = _run_hook(
        "user_prompt.py",
        root,
        {"sessionId": "cop-live", "cwd": str(ws), "prompt": "continue the fix", "timestamp": 123},
    )
    assert result.returncode == 0, result.stderr
    state = json.loads((ws / ".lemoncrow" / "workspace" / "session_state.json").read_text(encoding="utf-8"))
    assert state["host"] == "copilot"
    assert state["session_id"] == "cop-live"
    assert state["activity"] == "working"


def test_copilot_agent_stop_injects_review_feedback_as_one_exact_session_continuation(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.session_inbox import enqueue_session_message

    root = tmp_path / ".lemoncrow"
    ws = tmp_path / "repo"
    ws.mkdir()
    enqueue_session_message(
        root,
        host="copilot",
        session_id="cop-live",
        message="Human review feedback from LemonCrow:\n- fix the boundary check",
    )

    result = _run_hook(
        "stop.py",
        root,
        {"sessionId": "cop-live", "cwd": str(ws), "stopHookActive": False},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    assert "fix the boundary check" in payload["reason"]
    assert "exact-session-inbox" in payload["reason"]
    state = json.loads((ws / ".lemoncrow" / "workspace" / "session_state.json").read_text(encoding="utf-8"))
    assert state["activity"] == "working"

    from lemoncrow.pro.capabilities.review.session_inbox import claim_session_messages

    assert claim_session_messages(root, host="copilot", session_id="cop-live") == ()


def test_copilot_agent_stop_feedback_injection_needs_no_lemoncrow_pythonpath(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.session_inbox import enqueue_session_message

    root = tmp_path / ".lemoncrow"
    ws = tmp_path / "repo"
    ws.mkdir()
    enqueue_session_message(root, host="copilot", session_id="cop-portable", message="portable review feedback")
    env = os.environ.copy()
    env["LEMONCROW_ROOT"] = str(root)
    env["PYTHONPATH"] = ""
    result = subprocess.run(
        [sys.executable, str(HOOKS / "stop.py")],
        input=json.dumps({"sessionId": "cop-portable", "cwd": str(ws), "stopHookActive": False}),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["decision"] == "block"
    assert "portable review feedback" in output["reason"]


def test_copilot_post_tool_use_injects_new_feedback_mid_turn(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.session_inbox import claim_session_messages, enqueue_session_message

    root = tmp_path / ".lemoncrow"
    ws = tmp_path / "repo"
    ws.mkdir()
    enqueue_session_message(
        root,
        host="copilot",
        session_id="cop-live",
        message="Human review feedback from LemonCrow:\n- keep the cache invalidation explicit",
    )
    result = _run_hook(
        "post_tool_use.py",
        root,
        {"sessionId": "cop-live", "cwd": str(ws), "toolName": "bash", "toolResult": {"resultType": "success"}},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "cache invalidation explicit" in payload["additionalContext"]
    assert "exact-session-inbox" in payload["additionalContext"]
    assert claim_session_messages(root, host="copilot", session_id="cop-live") == ()
    state = json.loads((ws / ".lemoncrow" / "workspace" / "session_state.json").read_text(encoding="utf-8"))
    assert state["activity"] == "working"


def test_copilot_forced_continuation_consumes_only_new_feedback_and_then_stops(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.session_inbox import claim_session_messages, enqueue_session_message

    root = tmp_path / ".lemoncrow"
    ws = tmp_path / "repo"
    ws.mkdir()
    enqueue_session_message(
        root, host="copilot", session_id="cop-live", message="feedback arriving during forced continuation"
    )
    injected = _run_hook(
        "stop.py",
        root,
        {"sessionId": "cop-live", "cwd": str(ws), "stopHookActive": True},
    )
    assert injected.returncode == 0, injected.stderr
    payload = json.loads(injected.stdout)
    assert payload["decision"] == "block"
    assert "feedback arriving during forced continuation" in payload["reason"]
    assert claim_session_messages(root, host="copilot", session_id="cop-live") == ()

    empty = _run_hook(
        "stop.py",
        root,
        {"sessionId": "cop-live", "cwd": str(ws), "stopHookActive": True},
    )
    assert empty.returncode == 0, empty.stderr
    assert empty.stdout == ""
    state = json.loads((ws / ".lemoncrow" / "workspace" / "session_state.json").read_text(encoding="utf-8"))
    assert state["activity"] == "idle"


def test_copilot_session_end_marks_matching_session_idle(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    ws = tmp_path / "repo"
    ws.mkdir()
    start = _run_hook("user_prompt.py", root, {"sessionId": "cop-live", "cwd": str(ws), "prompt": "work"})
    assert start.returncode == 0
    ended = _run_hook("session_end.py", root, {"sessionId": "cop-live", "cwd": str(ws)})
    assert ended.returncode == 0
    state = json.loads((ws / ".lemoncrow" / "workspace" / "session_state.json").read_text(encoding="utf-8"))
    assert state["activity"] == "idle"
    assert state["ended"] is True


def test_copilot_hooks_manifest_wires_failure_hook() -> None:
    data = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    rendered = json.dumps(data)
    assert "postToolUseFailure" in data["hooks"]
    assert "userPromptSubmitted" in data["hooks"]
    assert "sessionEnd" in data["hooks"]
    assert "agentStop" in data["hooks"]
    assert "postToolUse" in data["hooks"]
    assert "post_tool_use_failure.py" in rendered
    assert "user_prompt.py" in rendered
    assert "session_end.py" in rendered
    assert "stop.py" in rendered
    assert "post_tool_use.py" in rendered
    assert "review_feedback.py" in Path(ROOT / "integrations" / "copilot-cli" / "install.sh").read_text(
        encoding="utf-8"
    )

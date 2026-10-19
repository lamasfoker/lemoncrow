from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pytest
from lemoncrow_client.kit.bash_output import extract_anomaly_windows, strip_ansi

import lemoncrow.pro.capabilities.tool_supervision.bash_exec as bx
from lemoncrow.pro.capabilities.tool_supervision.bash_exec import _compact_result


def test_bash_anomaly_detection_contract() -> None:
    """case: test_extract_anomaly_windows_returns_none_when_nothing_matches"""
    text = "\n".join(f"line {i}: all good" for i in range(50))
    assert extract_anomaly_windows(text, max_chars=6000) is None
    "case: test_extract_anomaly_windows_keeps_a_marker_buried_in_the_middle"
    lines = [f"line {i}: doing routine work" for i in range(300)]
    lines[150] = "FATAL: connection to db refused at line 150"
    text = "\n".join(lines)
    result = extract_anomaly_windows(text, max_chars=6000)
    assert result is not None
    assert "FATAL: connection to db refused" in result
    assert len(result) < len(text)
    "case: test_compact_result_generic_command_surfaces_a_buried_fatal_line"
    lines = [f"line {i}: doing routine work" for i in range(300)]
    lines[150] = "FATAL: connection to db refused at line 150"
    stdout = "\n".join(lines) + "\ndone"
    result = _compact_result(
        command="python3 migrate.py", raw_stdout=stdout, raw_stderr="", exit_code=0, duration_ms=10, max_lines=200
    )
    assert "FATAL: connection to db refused" in result.stdout
    "case: test_compact_result_surfaces_buried_stderr_fatal_instead_of_blind_head_tail"
    lines = [f"stderr line {i}: routine detail" for i in range(500)]
    lines[250] = "FATAL: certificate verification failed for api.example.test"
    result = _compact_result(
        command="custom-deployer --apply",
        raw_stdout="",
        raw_stderr="\n".join(lines),
        exit_code=1,
        duration_ms=10,
        max_lines=200,
        max_chars=5000,
    )
    assert "certificate verification failed" in result.stderr
    assert "stderr line 250" not in result.stderr
    assert "lines omitted" in result.stderr
    assert len(result.stderr) < 5000
    assert result.truncated is True


def test_bash_anomaly_compaction_contract() -> None:
    """case: test_compact_result_ships_under_budget_output_whole"""
    "An under-char-budget body is never line-elided at the default max_lines:\n    bench transcripts showed the same 247-line file middle-elided three times,\n    costing ~7 recon turns for zero token savings."
    lines = [f"line {i}: all good" for i in range(300)]
    stdout = "\n".join(lines)
    result = _compact_result(
        command="python3 build.py", raw_stdout=stdout, raw_stderr="", exit_code=0, duration_ms=10, max_lines=200
    )
    assert "lines omitted" not in result.stdout
    assert "line 0:" in result.stdout
    assert "line 100:" in result.stdout
    assert "line 299:" in result.stdout
    assert result.lines_omitted == 0
    "case: test_compact_result_explicit_max_lines_still_a_hard_bound"
    lines = [f"line {i}: all good" for i in range(300)]
    result = _compact_result(
        command="python3 build.py",
        raw_stdout="\n".join(lines),
        raw_stderr="",
        exit_code=0,
        duration_ms=10,
        max_lines=50,
    )
    assert "lines omitted" in result.stdout
    assert result.lines_omitted > 0
    "case: test_strip_ansi_removes_csi_osc_and_bare_escapes"
    raw = "\x1b]0;window title\x07\x1b[31mred\x1b[0m plain\x1b[2K\x1bM"
    assert strip_ansi(raw) == "red plain"
    "case: test_managed_command_strips_ansi_and_credits_stripped_chars"
    "ANSI escapes emitted by a managed command are stripped from the returned\n    output BEFORE truncation/char accounting, and the stripped bytes flow into\n    chars_omitted — the same field _bash_omitted_tokens_saved credits from."
    started = bx.start_managed_command("printf '\\033]0;title\\007\\033[31mred\\033[0m plain'", timeout=10)
    result = _poll_until_done(str(started["session_id"]))
    stdout = str(result["stdout"])
    assert "red plain" in stdout
    assert "\x1b" not in stdout
    assert "\x1b" not in str(result["stderr"])
    assert result["lines_omitted"] == 0
    assert result["chars_omitted"] == 19
    from lemoncrow.gateway.adapters.mcp_server import _bash_omitted_tokens_saved

    assert _bash_omitted_tokens_saved(result, int(str(result["chars_omitted"]))) == 19 // 4
    "case: test_poll_managed_command_survives_wedged_reader_pipe"
    "Regression: a reader thread that never reaches EOF (e.g. a detached\n    grandchild -- a backgrounded server a task wants left running -- still\n    holding a duplicate of the output pipe open) must not block\n    poll_managed_command forever once the wrapped process has actually\n    exited. Bounded join (bx._READER_JOIN_GRACE_S) ships whatever's captured\n    so far instead of hanging."
    started = bx.start_managed_command("true", timeout=10)
    sid = str(started["session_id"])
    with bx._MANAGED_COMMANDS_LOCK:
        managed = bx._MANAGED_COMMANDS[sid]
    managed.proc.wait(timeout=5)
    block_forever = threading.Event()
    wedged = threading.Thread(target=block_forever.wait, daemon=True)
    wedged.start()
    managed.readers = [wedged]
    start = time.monotonic()
    result = bx.poll_managed_command(sid)
    elapsed = time.monotonic() - start
    block_forever.set()
    wedged.join(timeout=1.0)
    assert elapsed < bx._READER_JOIN_GRACE_S + 3.0
    assert result["exit_code"] == 0
    assert "still be running" in str(result["stderr"])


def test_compact_result_spills_full_output_when_truncated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bare "(N lines omitted)" marker used to discard the middle for good.
    With T7 spill enabled (default), the untouched raw stdout is persisted and a
    recovery hint names the path, so the dropped lines stay reachable via `read`.
    """
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(tmp_path / "spill"))
    monkeypatch.delenv("LEMONCROW_TOOL_OUTPUT_SPILL", raising=False)
    lines = [f"line {i}: all good" for i in range(3000)]
    stdout = "\n".join(lines)
    result = _compact_result(
        command="python3 build.py", raw_stdout=stdout, raw_stderr="", exit_code=0, duration_ms=10, max_lines=200
    )
    assert result.lines_omitted > 0
    assert "line 100: all good" not in result.stdout
    assert "[lc: shrunk" in result.spill_hint
    match = re.search("full: (\\S+\\.txt)\\]", result.spill_hint)
    assert match is not None
    recovered = Path(match.group(1)).read_text(encoding="utf-8")
    assert "line 100: all good" in recovered


def test_compact_result_no_spill_hint_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_TOOL_OUTPUT_SPILL", "0")
    lines = [f"line {i}: all good" for i in range(3000)]
    result = _compact_result(
        command="python3 build.py",
        raw_stdout="\n".join(lines),
        raw_stderr="",
        exit_code=0,
        duration_ms=10,
        max_lines=200,
    )
    assert result.lines_omitted > 0
    assert result.spill_hint == ""


def _poll_until_done(session_id: str, timeout_s: float = 10.0) -> dict[str, object]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        result = bx.poll_managed_command(session_id)
        if result["status"] != "running":
            return result
        time.sleep(0.02)
    raise AssertionError("managed command did not finish in time")


def test_watch_managed_command_soft_timeout_does_not_kill_before_hard_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A command exceeding its per-call `timeout` must not be killed as long
    as it finishes before the (much larger) hard cap -- `timeout` is a soft,
    response-only budget; _MANAGED_COMMAND_HARD_CAP_S is the real kill
    deadline. Regression for the background-service-timeout bug: a task that
    starts a server with a modest `timeout` must not have it killed just
    because that budget elapsed."""
    monkeypatch.setattr(bx, "_MANAGED_COMMAND_HARD_CAP_S", 0.6)
    started = bx.start_managed_command("sleep 0.3; echo survived", timeout=0.1)
    sid = str(started["session_id"])
    with bx._MANAGED_COMMANDS_LOCK:
        managed = bx._MANAGED_COMMANDS[sid]
    managed.proc.wait(timeout=5)
    result = bx.poll_managed_command(sid)
    assert result["exit_code"] == 0
    assert "survived" in str(result["stdout"])
    assert result["status"] == "completed"


def test_watch_managed_command_kills_at_hard_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hard cap is still a real backstop -- a command that outlives even
    that gets killed, same as the old kill-at-`timeout` behavior did for a
    genuinely-abandoned command. Only the *deadline* moved (from the small,
    caller-chosen `timeout` to the much larger hard cap); a forgotten process
    still eventually dies."""
    monkeypatch.setattr(bx, "_MANAGED_COMMAND_HARD_CAP_S", 0.15)
    started = bx.start_managed_command("sleep 2", timeout=0.05)
    deadline = time.monotonic() + 2.0
    sid = str(started["session_id"])
    while True:
        result = bx.poll_managed_command(sid)
        if result["status"] != "running":
            break
        if time.monotonic() >= deadline:
            pytest.fail(f"managed command did not hit hard cap before deadline: {result}")
        time.sleep(0.01)
    assert result["status"] == "timed_out"
    assert result["exit_code"] == -1


def test_compact_result_no_spill_hint_when_nothing_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_TOOL_OUTPUT_SPILL", raising=False)
    result = _compact_result(
        command="echo hi", raw_stdout="hi\n", raw_stderr="", exit_code=0, duration_ms=1, max_lines=200
    )
    assert result.lines_omitted == 0
    assert result.spill_hint == ""

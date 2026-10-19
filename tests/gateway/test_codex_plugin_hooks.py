# ruff: noqa: RUF001

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.capabilities import plugin_runtime
from lemoncrow.infra.runtime.run_ledger import RunLedger

pytestmark = pytest.mark.slow  # Each test spawns a real Python subprocess (~2s each)

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "integrations" / "codex" / "hooks"
STATUSLINE = ROOT / "integrations" / "codex" / "plugin" / "scripts" / "statusline.sh"


def _run_hook(
    script: str, root: Path, payload: dict[str, Any], version: str = "1.0.0"
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "LEMONCROW_ROOT": str(root),
            "LEMONCROW_VERSION": version,
            "LEMONCROW_CTX_NUDGE_TOKENS": "999999999",
            # These are Codex hooks: pin detect_host()'s explicit override so
            # every per-session path lands under host "codex" regardless of
            # the ambient test-runner environment (e.g. a stray CLAUDE_CODE
            # or LEMONCROW_AGENT=claude leaking in from the outer harness).
            "LEMONCROW_AGENT": "codex",
        }
    )
    return subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )


def _run_statusline(
    root: Path, payload: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({"LEMONCROW_ROOT": str(root), "LEMONCROW_NO_COLOR": "1"})
    env.update(env_extra or {})
    return subprocess.run(
        [str(STATUSLINE)],
        input=payload,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )


def test_codex_stop_and_subagent_stop_capture_completed_role_comments(tmp_path: Path) -> None:
    from lemoncrow.core.foundation.paths import session_dir

    root = tmp_path / "store"
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "id": "review-message",
                    "content": [{"type": "output_text", "text": "Independent review complete."}],
                },
            }
        )
        + "\n"
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "main",
        "cwd": str(tmp_path),
        "transcript_path": str(transcript),
        "agent_role": "review",
    }
    _run_hook("stop.py", root, payload)
    _run_hook("stop.py", root, payload)
    run = json.loads((session_dir(root, "codex", "main") / "run.json").read_text())
    comments = [event["payload"] for event in run["events"] if event["payload"].get("completed")]
    assert len(comments) == 1 and comments[0]["agent_role"] == "review"
    payload.update(hook_event_name="SubagentStop", agent_id="child", agent_type="lemoncrow:plan")
    payload.pop("agent_role")
    _run_hook("subagent.py", root, payload)
    run = json.loads((session_dir(root, "codex", "child") / "run.json").read_text())
    comments = [event["payload"] for event in run["events"] if event["payload"].get("completed")]
    assert comments[0]["agent_role"] == "plan" and comments[0]["parent_session_id"] == "main"


def test_codex_statusline_renders_native_footer_in_claude_format(tmp_path: Path) -> None:
    native = "gpt-5.5 xhigh · ~/Projects/leanchain/lemoncrow · 1.11M used · 19.4M in · 61.1K out"

    result = _run_statusline(tmp_path / ".lemoncrow", native)

    assert result.stdout.strip() == ("❯ lc | gpt-5.5 xhigh ctx 1.1M $0.00(I:19.40M C:0 O:61.1k) ↓ $0.00(I:0)")


def test_codex_statusline_recovers_session_from_workspace_state(tmp_path: Path) -> None:
    from lemoncrow.core.foundation.paths import resolve_workspace_store_dir, session_dir

    root = tmp_path / ".lemoncrow"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = resolve_workspace_store_dir(workspace_root=workspace)
    state_dir.mkdir(parents=True)
    (state_dir / "session_state.json").write_text(json.dumps({"session_id": "codex-1"}), encoding="utf-8")
    sidecar = session_dir(root, "codex", "codex-1") / "savings.jsonl"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(
        json.dumps({"tool": "code_search", "tokens": 12_000, "model": "codex-savings-test-model"}) + "\n",
        encoding="utf-8",
    )
    # Pin subprocess pricing so the recovered-savings assertion is deterministic.
    (root / "pricing.yaml").write_text("overrides:\n  codex-savings-test-model:\n    input: 2.5\n    output: 10.0\n")

    result = _run_statusline(
        root,
        "codex-savings-test-model high · ~/workspace · 1.11M used · 19.4M in · 61.1K out",
        env_extra={"CODEX_WORKSPACE_ROOT": str(workspace)},
    )

    assert "$0.03(I:12.0k)" in result.stdout


def test_codex_statusline_renders_json_token_fields_in_claude_format(tmp_path: Path) -> None:
    payload = {
        "model": {"name": "gpt-5.5"},
        "effort": "xhigh",
        "session_id": "c1",
        "context": {"used_tokens": 1_110_000, "used_percent": 12.3},
        "usage": {"input_tokens": 19_400_000, "output_tokens": 61_100},
        "cost": {"total_usd": 1.23456},
    }

    result = _run_statusline(tmp_path / ".lemoncrow", json.dumps(payload))

    assert result.stdout.strip() == ("❯ lc | gpt-5.5 xhigh ctx 1.1M 12% $1.23(I:19.40M C:0 O:61.1k) ↓ $0.00(I:0)")


def test_codex_multi_file_prompt_emits_no_runtime_context(tmp_path: Path) -> None:
    result = _run_hook(
        "user_prompt.py",
        tmp_path / ".lemoncrow",
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "c1",
            "prompt": "Update auth.py and billing.py to share token parsing",
        },
    )

    assert result.stdout == ""


def test_codex_user_prompt_emits_high_context_nudge_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / ".lemoncrow"
    monkeypatch.setattr(
        "lemoncrow.gateway.hosts.context_state.host_context_state",
        lambda host, session_id: (200_000, "gpt-5.5"),
    )
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "c1",
        "prompt": "Continue the implementation",
    }

    first = plugin_runtime.build_codex_user_prompt_output(root, payload)
    second = plugin_runtime.build_codex_user_prompt_output(root, payload)

    assert "high context" in first["uiMessage"]
    assert "additionalContext" not in first
    assert second.get("no_output") is True


def _seed_reread_case(tmp_path: Path, *, lines: int) -> tuple[Path, dict]:
    """Seed a run ledger marking src/a.py edited + write src/a.py with `lines`."""
    root = tmp_path / ".lemoncrow"
    session_id = "c1"
    run_file = plugin_runtime._codex_run_file(root, session_id)
    run_file.parent.mkdir(parents=True, exist_ok=True)
    run_file.write_text(
        json.dumps({"session_id": session_id, "events": [], "files_touched": ["src/a.py"]}),
        encoding="utf-8",
    )
    src = tmp_path / "src" / "a.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("".join(f"x = {i}\n" for i in range(lines)), encoding="utf-8")
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": session_id,
        "tool_name": "mcp__lc__read",
        "tool_input": {"files": [{"path": "src/a.py", "full": True}]},
        "cwd": str(tmp_path),
    }
    return root, payload


def test_codex_pre_tool_use_blocks_full_reread_of_large_edited_file(tmp_path: Path) -> None:
    root, payload = _seed_reread_case(tmp_path, lines=600)
    output = json.loads(_run_hook("pre_tool_use.py", root, payload).stdout)
    hook = output["hookSpecificOutput"]
    assert hook["hookEventName"] == "PreToolUse"
    assert hook["permissionDecision"] == "deny"
    assert "range" in hook["permissionDecisionReason"]


def test_codex_pre_tool_use_stays_silent_for_small_edited_file(tmp_path: Path) -> None:
    # Small file: a full re-read isn't real waste. Codex PreToolUse honours only
    # "deny", so the happy path emits nothing -- an "allow" decision errors the
    # hook ("unsupported permissionDecision:allow").
    root, payload = _seed_reread_case(tmp_path, lines=8)
    result = _run_hook("pre_tool_use.py", root, payload)
    assert result.stdout.strip() == ""


def test_codex_savings_reporter_updates_session_stats(tmp_path: Path) -> None:
    from lemoncrow.core.foundation.paths import session_dir

    root = tmp_path / ".lemoncrow"
    result = _run_hook(
        "savings_reporter.py",
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "c1",
            "tool_name": "mcp__plugin_lemoncrow_lc__Edit",
            "tool_input": {"edits": [{"file_path": "a.py"}, {"file_path": "b.py"}]},
        },
    )

    stats = json.loads((session_dir(root, "codex", "c1") / "stats.json").read_text(encoding="utf-8"))
    assert result.stdout == ""
    assert stats["total_tool_calls"] == 1
    assert stats["tools_used"]["mcp__plugin_lemoncrow_lc__Edit"] == 1
    assert stats["event_counts"]["PostToolUse"] == 1


def test_codex_savings_reporter_is_quiet_after_repeated_searches(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    for now_ms in (1_000, 601_001, 601_002):
        result = _run_hook(
            "savings_reporter.py",
            root,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "c1",
                "tool_name": "mcp__plugin_lemoncrow_lc__Search",
                "tool_input": {},
                "now_ms": now_ms,
            },
        )
        assert result.stdout == ""


def test_codex_savings_reporter_records_loop_state_without_output(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    root.mkdir()
    session_id = "loop-run"
    ledger = RunLedger(session_id=session_id, agent="codex", root=root, task="debug repeated read loop")
    for index in range(3):
        ledger.record_tool_call("Search", {"query": "why is this looping"})
        ledger.record_tool_call("Read", {"path": f"src/module_{index}.py"})
    ledger.persist(root)
    (root / "session_state.json").write_text(
        json.dumps({"active_session_id": session_id, "lemoncrow_root": str(root)}),
        encoding="utf-8",
    )

    result = _run_hook(
        "savings_reporter.py",
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "c1",
            "tool_name": "mcp__plugin_lemoncrow_lc__Search",
            "tool_input": {},
            "now_ms": 2_000,
        },
    )

    assert result.stdout == ""
    from lemoncrow.core.foundation.paths import session_dir

    stats = json.loads((session_dir(root, "codex", "c1") / "stats.json").read_text(encoding="utf-8"))
    assert stats["total_tool_calls"] == 1


def test_codex_savings_reporter_ignores_non_lemoncrow_tools(tmp_path: Path) -> None:
    result = _run_hook(
        "savings_reporter.py",
        tmp_path / ".lemoncrow",
        {
            "hook_event_name": "PostToolUse",
            "session_id": "c1",
            "tool_name": "Read",
            "tool_input": {},
        },
    )

    assert result.stdout == ""


def test_codex_subagent_hook_tracks_start_and_stop(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    start_payload = {
        "hook_event_name": "SubagentStart",
        "session_id": "c1",
        "agent_id": "agent-1",
        "agent_type": "lemoncrow:explore",
    }
    stop_payload = {
        "hook_event_name": "SubagentStop",
        "session_id": "c1",
        "agent_id": "agent-1",
        "agent_type": "lemoncrow:explore",
    }

    start = _run_hook("subagent.py", root, start_payload)
    stop = _run_hook("subagent.py", root, stop_payload)

    from lemoncrow.core.foundation.paths import session_dir

    stats = json.loads((session_dir(root, "codex", "c1") / "stats.json").read_text(encoding="utf-8"))
    assert start.stdout == ""
    assert stop.stdout == ""
    assert stats["subagents_started"] == 1
    assert stats["subagents_completed"] == 1
    assert stats["pending_subagents"] == 0
    assert stats["active_subagents"] == {}
    assert stats["event_counts"]["SubagentStart"] == 1
    assert stats["event_counts"]["SubagentStop"] == 1


def test_codex_stop_hook_emits_session_summary(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    _run_hook(
        "user_prompt.py",
        root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "c1",
            "turn_id": "turn-1",
            "model": "gpt-5",
            "usage": {
                "input_tokens": 1000,
                "cache_write_tokens": 200,
                "cache_read_tokens": 3000,
                "output_tokens": 400,
            },
        },
    )
    _run_hook(
        "savings_reporter.py",
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "c1",
            "tool_name": "mcp__lc__edit",
            "tool_input": {"edits": [{"file_path": "a.py"}, {"file_path": "b.py"}]},
        },
    )
    from lemoncrow.core.foundation.paths import session_dir

    sess_dir = session_dir(root, "codex", "c1")
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / "savings.jsonl").write_text(
        json.dumps({"tokens": 500, "calls": 2, "model": "gpt-5"}) + "\n",
        encoding="utf-8",
    )

    result = _run_hook("stop.py", root, {"hook_event_name": "Stop", "session_id": "c1"})

    output = json.loads(result.stdout)
    assert set(output) == {"systemMessage"}
    message = output["systemMessage"]
    assert "lc session complete." not in message
    assert "token breakdown:" not in message
    assert "0 LLM turns · 1 prompt turn · 1 tool call (hooks)" in message
    assert "priced cost: ~$" in message
    # Routing/carry/output are all 0 in this fixture -- suppressed like
    # Claude Code's stop hook (component omitted at exactly $0), instead of
    # Codex's old always-show-at-$0.0000 behavior.
    assert "est. LC savings: $0.00(I:500)" in message
    assert "2 calls avoided" in message
    assert "routing $0.00" not in message
    assert "carry $0.00" not in message
    assert "tools: mcp__lc__edit×1" in message


def test_codex_tool_summary_merges_lemoncrow_mcp_aliases(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    plugin_runtime.update_session_stats(
        root,
        {"hook_event_name": "PostToolUse", "session_id": "c-tools", "tool_name": "bash"},
    )
    plugin_runtime.update_session_stats(
        root,
        {
            "hook_event_name": "StatuslineUpdate",
            "session_id": "c-tools",
            "tools_used": {
                "lc.bash": 26,
                "bash": 26,
                "lc.read": 23,
                "read": 23,
                "lc.code_search": 15,
                "code_search": 15,
                "lc.edit": 14,
                "edit": 15,
            },
            "total_tool_calls": 157,
            "tool_call_source": "transcript",
            "context_window": {"current_usage": {"input_tokens": 1}},
        },
    )

    result = plugin_runtime.build_codex_stop_output(root, {"hook_event_name": "Stop", "session_id": "c-tools"})

    message = result["systemMessage"]
    assert "tools: lc.bash×26 · lc.read×23 · lc.code_search×15 · lc.edit×14 · edit×1" in message
    assert " · bash×26" not in message
    assert " · read×23" not in message
    assert " · code_search×15" not in message
    from lemoncrow.core.foundation.paths import session_dir

    assert (session_dir(root, "codex", "c-tools") / "savings.jsonl").exists()


def test_codex_stop_hook_reads_status_style_token_fields(tmp_path: Path) -> None:
    from lemoncrow.core.capabilities.pricing import override_pricing

    root = tmp_path / ".lemoncrow"
    override_pricing(
        "codex-test-model",
        input_usd=1.0,
        output_usd=10.0,
        cache_read_usd=0.1,
        cache_write_usd=2.0,
    )

    result = plugin_runtime.build_codex_stop_output(
        root,
        {
            "hook_event_name": "Stop",
            "session_id": "c-status",
            "model": "codex-test-model",
            "tokens": {
                "input": "19.4M",
                "output": "61.1K",
                "cache": {"read": "2.5M", "write": "100k"},
            },
        },
    )

    message = result["systemMessage"]
    assert "(I:19.5M C:2.5M O:61.1k)" in message
    assert "priced cost: ~$20.46" in message


def test_codex_stop_hook_has_no_account_login_dynamic_status(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    root.mkdir()
    (root / "plugin_settings.json").write_text(json.dumps({"lemoncrow": {"statusLineTips": False}}))
    (root / "statusline_frame_state.json").write_text(json.dumps({"counter": 3, "ts": 9_000_000_000}))

    payload = {
        "hook_event_name": "Stop",
        "session_id": "c-dynamic",
        "model": "codex-test-model",
        "tokens": {"input": "1000", "output": "10"},
    }
    message = plugin_runtime.build_codex_stop_output(root, payload)["systemMessage"]
    assert "account login" not in message.lower()
    assert "unlock pro" not in message.lower()


def test_codex_stop_hook_uses_native_statusline_snapshot_without_session_id(tmp_path: Path) -> None:
    from lemoncrow.core.capabilities.pricing import override_pricing

    root = tmp_path / ".lemoncrow"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    override_pricing("codex-test-model", input_usd=1.0, output_usd=10.0)

    statusline = _run_statusline(
        root,
        "codex-test-model high · ~/workspace · 1.11M used · 19.4M in · 61.1K out",
        env_extra={"CODEX_WORKSPACE_ROOT": str(workspace)},
    )
    assert "codex-test-model high" in statusline.stdout

    result = plugin_runtime.build_codex_stop_output(
        root,
        {"hook_event_name": "Stop", "session_id": "c-native", "cwd": str(workspace)},
    )

    message = result["systemMessage"]
    assert "(I:19.4M C:0 O:61.1k)" in message
    assert "priced cost: ~$20.01" in message


def test_codex_stop_hook_recovers_usage_from_local_codex_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.core.capabilities.pricing import override_pricing

    root = tmp_path / ".lemoncrow"
    codex_home = tmp_path / ".codex"
    workspace = tmp_path / "workspace"
    transcript_dir = codex_home / "sessions" / "2026" / "06" / "16"
    transcript_dir.mkdir(parents=True)
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    override_pricing("codex-test-model", input_usd=1.0, output_usd=10.0, cache_read_usd=0.1)

    transcript = transcript_dir / "rollout-2026-06-16T21-42-38-session-abc.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "session-abc", "cwd": str(workspace)},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn_context",
                        "payload": {"model": "codex-test-model", "cwd": str(workspace)},
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "arguments": '{"cmd": "rg foo"}',
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 1000,
                                    "cached_input_tokens": 700,
                                    "output_tokens": 50,
                                },
                                "total_token_usage": {
                                    "input_tokens": 1000,
                                    "cached_input_tokens": 700,
                                    "output_tokens": 50,
                                    "reasoning_output_tokens": 30,
                                    "total_tokens": 1050,
                                },
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "apply_patch",
                            "arguments": '{"patch": "*** Begin Patch\\n*** End Patch"}',
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 300,
                                    "cached_input_tokens": 100,
                                    "output_tokens": 20,
                                },
                                "total_token_usage": {
                                    "input_tokens": 1300,
                                    "cached_input_tokens": 800,
                                    "output_tokens": 70,
                                    "reasoning_output_tokens": 40,
                                    "total_tokens": 1370,
                                },
                            },
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    plugin_runtime.update_session_stats(
        root,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-abc",
            "turn_id": "human-prompt-1",
        },
    )
    plugin_runtime.update_session_stats(
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "session-abc",
            "tool_name": "Bash",
        },
    )

    result = plugin_runtime.build_codex_stop_output(
        root,
        {"hook_event_name": "Stop", "session_id": "session-abc", "cwd": str(workspace)},
    )

    message = result["systemMessage"]
    assert "2 LLM turns · 2 tool calls (transcript)" in message
    assert "(I:500 C:800 O:70)" in message
    assert "token breakdown:" not in message
    assert "priced cost: ~$0.00" in message
    assert "tools: apply_patch×1 · exec_command×1" in message


def test_codex_stop_hook_is_quiet_without_session_activity(tmp_path: Path) -> None:
    result = _run_hook("stop.py", tmp_path / ".lemoncrow", {"hook_event_name": "Stop", "session_id": "c1"})

    assert result.stdout == ""


def test_codex_session_start_is_quiet_and_records_session(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    result = _run_hook(
        "update_notification.py",
        root,
        {"hook_event_name": "SessionStart", "session_id": "c1", "cwd": str(cwd), "model": "gpt-5.6-terra"},
    )

    assert result.stdout == ""
    state_files = list((root / "workspaces").glob("*/session_state.json"))
    assert len(state_files) == 1
    state = json.loads(state_files[0].read_text(encoding="utf-8"))
    assert state["session_id"] == "c1"
    assert state["model"] == "gpt-5.6-terra"


def test_codex_session_end_closes_run_once_and_marks_workspace_idle(tmp_path: Path) -> None:
    from lemoncrow.core.foundation.paths import session_dir

    root = tmp_path / ".lemoncrow"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "id": "final",
                    "content": [{"type": "output_text", "text": "Completed demo implementation."}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _run_hook(
        "update_notification.py",
        root,
        {"hook_event_name": "SessionStart", "session_id": "c-end", "cwd": str(workspace), "model": "gpt-test"},
    )
    payload = {
        "hook_event_name": "SessionEnd",
        "session_id": "c-end",
        "cwd": str(workspace),
        "transcript_path": str(transcript),
    }
    assert _run_hook("session_end.py", root, payload).stdout == ""
    first = json.loads((session_dir(root, "codex", "c-end") / "run.json").read_text(encoding="utf-8"))
    first_updated = first["updated_at"]
    assert first["status"] == "completed"
    comments = [event["payload"] for event in first["events"] if event.get("payload", {}).get("completed")]
    assert [item["message"] for item in comments] == ["Completed demo implementation."]

    assert _run_hook("session_end.py", root, payload).stdout == ""
    second = json.loads((session_dir(root, "codex", "c-end") / "run.json").read_text(encoding="utf-8"))
    assert second["updated_at"] == first_updated
    comments = [event["payload"] for event in second["events"] if event.get("payload", {}).get("completed")]
    assert len(comments) == 1

    state_files = list((root / "workspaces").glob("*/session_state.json"))
    state = json.loads(state_files[0].read_text(encoding="utf-8"))
    assert state["session_id"] == "c-end"
    assert state["activity"] == "idle"
    assert state["ended"] is True


def test_codex_hooks_manifest_wires_reporter_and_update() -> None:
    data = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    assert "SessionStart" in data["hooks"]
    assert "SessionEnd" in data["hooks"]
    assert "UserPromptSubmit" in data["hooks"]
    assert "PreToolUse" in data["hooks"]
    assert "PostToolUse" in data["hooks"]
    assert "SubagentStart" in data["hooks"]
    assert "SubagentStop" in data["hooks"]
    assert "Stop" in data["hooks"]
    rendered = json.dumps(data)
    assert "update_notification.py" in rendered
    assert "session_end.py" in rendered
    assert "user_prompt.py" in rendered
    assert "pre_tool_use.py" in rendered
    assert "savings_reporter.py" in rendered
    assert "subagent.py" in rendered
    assert "stop.py" in rendered
    assert "compact.py" in rendered
    assert "${PLUGIN_ROOT}/hooks/" in rendered
    assert "__LEMONCROW_PYTHON__" in rendered
    assert "__LEMONCROW_REPO_SRC__" in rendered
    assert "LEMONCROW_CODEX_PLUGIN_ROOT" not in rendered
    for groups in data["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                assert hook["command"].startswith("LEMONCROW_AGENT=codex ")
    for event in ("PreCompact", "PostCompact", "SubagentStart", "SubagentStop"):
        assert event in data["hooks"]


def test_codex_compact_hook_bumps_epoch(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    cwd = tmp_path / "ws"
    cwd.mkdir()

    result = _run_hook(
        "compact.py",
        root,
        {"hook_event_name": "PostCompact", "session_id": "c1", "cwd": str(cwd), "trigger": "auto"},
    )

    assert result.stdout == ""
    state_files = list((root / "workspaces").glob("*/session_state.json"))
    assert len(state_files) == 1
    assert json.loads(state_files[0].read_text(encoding="utf-8"))["compaction_epoch"] == 1


def test_codex_savings_reporter_is_fail_open_on_unwritable_root(tmp_path: Path) -> None:
    # LEMONCROW_ROOT points at a regular file, so session_stats writes raise OSError.
    # The hook MUST still exit 0 (fail-open) rather than crash with a traceback.
    bad_root = tmp_path / "rootfile"
    bad_root.write_text("not a directory", encoding="utf-8")
    env = os.environ.copy()
    env.update({"LEMONCROW_ROOT": str(bad_root), "LEMONCROW_CTX_NUDGE_TOKENS": "999999999"})
    result = subprocess.run(
        [sys.executable, str(HOOKS / "savings_reporter.py")],
        input=json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "c1",
                "tool_name": "mcp__lc__edit",
                "tool_input": {"file_path": "a.py"},
            }
        ),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr

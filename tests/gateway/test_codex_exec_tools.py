"""Exec wrappers must expose their nested tool names in Codex summaries."""

import json

from lemoncrow.core.capabilities import plugin_runtime


def test_exec_source_skips_examples_and_comments():
    source = """
    // tools.fake()
    /* tools.fake() */
    const example = 'tools.fake()';
    const template = `tools.fake()`;
    text(await tools.mcp__lc__read({files: ["a.py"]}));
    await Promise.all([tools.mcp__lc__bash({}), tools.mcp__lc__bash({})]);
    """
    assert plugin_runtime._codex_exec_tool_names(source) == ["mcp__lc__read", "mcp__lc__bash", "mcp__lc__bash"]


def test_transcript_counts_exec_and_nested_tools(tmp_path):
    path = tmp_path / "session.jsonl"
    call = {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "id": "exec-1",
            "name": "exec",
            "input": "await tools.mcp__lc__read({}); await tools.mcp__lc__bash({});",
        },
    }
    path.write_text(
        "\n".join(
            json.dumps(event)
            for event in [
                call,
                call,
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "exec-1",
                        "output": "tools.fake()",
                    },
                },
            ]
        )
    )
    snapshot = plugin_runtime._codex_transcript_snapshot(path)
    assert snapshot["tools_used"] == {"exec": 1, "mcp__lc__read": 1, "mcp__lc__bash": 1}
    assert snapshot["total_tool_calls"] == 3
    assert snapshot["inferred_exec_tools"] is True


def test_exact_session_beats_newer_fork(tmp_path, monkeypatch):
    paths = [tmp_path / "fork.jsonl", tmp_path / "original.jsonl"]
    for path, session_id, name in zip(paths, ["fork", "original"], ["exec", "mcp__lc__read"], strict=True):
        events = [
            {"type": "session_meta", "payload": {"id": session_id, "cwd": str(tmp_path)}},
            {"type": "response_item", "payload": {"type": "function_call", "name": name}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {"input_tokens": 100, "output_tokens": 10},
                    },
                },
            },
        ]
        path.write_text("\n".join(json.dumps(event) for event in events))
    monkeypatch.setattr(plugin_runtime, "_codex_transcript_paths", lambda *args: paths)
    captured = []
    monkeypatch.setattr(plugin_runtime, "update_session_stats", lambda root, payload: captured.append(payload))
    plugin_runtime._apply_codex_transcript_snapshot(tmp_path, "original", {"cwd": str(tmp_path)})
    assert captured[0]["tools_used"] == {"mcp__lc__read": 1}


def test_stop_separates_cost_from_estimated_savings(tmp_path, monkeypatch):
    monkeypatch.setattr(
        plugin_runtime,
        "build_savings_report",
        lambda *args, **kwargs: {
            "session": {
                "turns": 1,
                "usage": {
                    "input_tokens": 2000,
                    "cache_write_tokens": 100,
                    "cache_read_tokens": 3000,
                    "output_tokens": 500,
                },
            },
            "cost": {"saved_usd": 0.25},
            "tokens_saved": 100,
            "calls_avoided": 2,
        },
    )
    output = plugin_runtime.build_codex_stop_output(
        tmp_path,
        {
            "hook_event_name": "Stop",
            "session_id": "compact",
            "cost": {"total_cost_usd": 0.125},
        },
    )
    lines = output["systemMessage"].splitlines()
    assert "cost: $0.1250(I:2.1k C:3.0k O:500)" in lines
    assert "est. LC savings: $0.25(I:100) · ~66.7% lower · 2 calls avoided" in lines
    assert not any(line.startswith(("savings:", "tokens:")) for line in lines)

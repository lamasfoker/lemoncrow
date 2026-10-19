"""OpenCode V2 plugin and native-config contracts (no OpenCode daemon needed)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

from scripts.opencode_v2_config import merge_cli, merge_v2, read_jsonc, remove_cli, remove_v2

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "integrations/opencode/plugins/lemoncrow-nudge-v2.js"


def _v2_hooks(script: str, tmp_path: Path) -> dict:
    launcher = tmp_path / "python-shim"
    launcher.write_text("""#!/bin/sh
if [ "$1" = "-c" ]; then exit 0; fi
input="$(cat)"
case "$input" in
  *'"event":"idle"'*) printf '%s\\n' '{"continuePrompt":"Verify changes before done"}' ;;
  *'"event":"post_tool"'*) printf '%s\\n' '{"uiMessage":"Post-tool advisory"}' ;;
  *) printf '%s\\n' '{"additionalContext":"Use LemonCrow indexed context"}' ;;
esac
""")
    launcher.chmod(0o755)
    env = {**os.environ, "LEMONCROW_PYTHON": str(launcher), "LEMONCROW_ROOT": str(tmp_path / ".lemoncrow")}
    node = f"""
import plugin from {json.dumps(PLUGIN.as_uri())};
const hooks = {{}};
const calls = [];
let published;
const ctx = {{
  location: {{directory: process.cwd()}},
  session: {{
    hook: async (name, callback) => {{ hooks[name] = callback; }},
    prompt: async (payload) => calls.push(payload),
  }},
  tool: {{hook: async (name, callback) => {{hooks[name] = callback;}}}},
  event: {{subscribe: () => ({{ async *[Symbol.asyncIterator]() {{
    if (published) yield published;
  }} }})}},
}};
const cleanup = await plugin.setup(ctx);
{script}
console.log(JSON.stringify({{id: plugin.id, registered: Object.keys(hooks), calls, ...result}}));
cleanup();
"""
    run = subprocess.run(
        ["node", "--input-type=module", "-e", node], text=True, capture_output=True, env=env, check=True
    )
    return json.loads(run.stdout)


def test_v2_hooks_and_nudge_delivery(tmp_path: Path) -> None:
    data = _v2_hooks(
        """
const input = {sessionID: "s1", prompt: {text: "Review this"}};
await hooks.prompt(input);
const context = {sessionID: "s1", options: {}, system: []};
await hooks.context(context);
const next = {sessionID: "s1", options: {maxTokens: 10000}, system: []};
await hooks.context(next);
const result = {input, context, next};
""",
        tmp_path,
    )
    assert data["id"] == "lemoncrow.nudge"
    assert set(data["registered"]) == {"prompt", "context", "execute.after"}
    assert data["input"]["prompt"]["text"] == "Review this"  # don't mutate durable user prompt
    assert data["context"]["options"]["maxTokens"] == 1200
    assert "Use LemonCrow indexed context" in data["context"]["system"][0]["text"]
    assert data["next"]["options"]["maxTokens"] == 1200
    assert data["next"]["system"] == []  # no repeated hint injection


def test_v2_tool_rescue_and_phase_token_limits(tmp_path: Path) -> None:
    data = _v2_hooks(
        """
const failed = {sessionID: "s1", tool: "bash", input: {command: "make test"},
                status: "error", error: {message: "Error: same failure"}};
await hooks["execute.after"](failed);
await hooks["execute.after"](failed);
await hooks.prompt({sessionID: "s1", prompt: {text: "Retry"}});
const repair = {sessionID: "s1", options: {}, system: []};
await hooks.context(repair);
const result = {repair};
""",
        tmp_path,
    )
    assert data["repair"]["options"]["maxTokens"] == 5200
    assert "Call 'rescue'" in data["repair"]["system"][0]["text"]


def test_v2_native_config_merge_is_idempotent_and_reversible(tmp_path: Path) -> None:
    config = tmp_path / "opencode.jsonc"
    config.write_text("""{
      // Preserve account and unrelated MCP settings
      "uri": "https://example.test/path,}",
      "model": "anthropic/test", "mcp": {"old": {"type":"local"}, "servers": {"other": {"type": "remote"}},},
      "permission": {"lc_*": "allow", "read": "ask"},
    }""")
    entry = {"mcp": {"servers": {"lc": {"type": "local", "command": ["lc", "runtime", "lite-mcp"]}}}}
    merge_v2(config, entry)
    merge_v2(config, entry)
    data = read_jsonc(config)
    assert data["model"] == "anthropic/test"
    assert data["uri"] == "https://example.test/path,}"
    assert data["mcp"]["servers"]["other"] == {"type": "remote"}
    assert "old" in data["mcp"]  # unrelated config remains untouched
    assert "lc" not in data["mcp"]
    assert data["mcp"]["servers"]["lc"]["command"][0] == "lc"
    assert data["permission"] == {"read": "ask"}
    assert data["permissions"] == [{"action": "lc_*", "resource": "*", "effect": "allow"}]
    remove_v2(config)
    data = read_jsonc(config)
    assert "lc" not in data["mcp"]["servers"]
    assert data["mcp"]["servers"]["other"] == {"type": "remote"}
    assert "permissions" not in data


def test_v2_sidebar_registration_preserves_other_cli_plugins(tmp_path: Path) -> None:
    config = tmp_path / "cli.json"
    config.write_text('{"theme": "auto", "plugins": ["external"]}')
    merge_cli(config, "/path/lemoncrow-status-v2")
    merge_cli(config, "/path/lemoncrow-status-v2")
    assert read_jsonc(config)["plugins"] == ["external", "/path/lemoncrow-status-v2"]
    remove_cli(config, "/path/lemoncrow-status-v2")
    assert read_jsonc(config)["plugins"] == ["external"]


def test_v2_session_schema_is_read_without_v1_tables(tmp_path: Path) -> None:
    from lemoncrow.gateway.hosts.session_parsers.opencode import (
        find_opencode_sessions,
        serialize_opencode_session,
    )

    db = tmp_path / "opencode.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, title TEXT, time_created INTEGER, time_updated INTEGER)"
        )
        conn.execute(
            "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, time_created INTEGER, seq INTEGER, data TEXT)"
        )
        conn.execute("INSERT INTO session_v2 VALUES ('s2', 'V2 change', 1000, 2000)")
        conn.execute(
            "INSERT INTO session_message VALUES ('u1', 's2', 'user', 1200, 1, ?)",
            (json.dumps({"role": "user", "text": "Fix the checkout flow"}),),
        )
        conn.execute(
            "INSERT INTO session_message VALUES ('a1', 's2', 'assistant', 1300, 2, ?)",
            (
                json.dumps(
                    {
                        "role": "assistant",
                        "modelID": "gpt-5",
                        "providerID": "openai",
                        "usage": {"tokens": {"input": 99, "output": 30}},
                        "parts": [{"type": "text", "text": "Updated the checkout flow"}],
                    }
                ),
            ),
        )
    sessions = find_opencode_sessions(db)
    assert [item["id"] for item in sessions] == ["s2"]
    assert sessions[0]["_lemoncrow_schema"] == 2
    text = serialize_opencode_session("s2", db)
    events = [json.loads(line) for line in text.splitlines()]
    assert any(e["_type"] == "v2_message" for e in events)
    assert any(
        e["_type"] == "part" and e["data"]["type"] == "step-finish" and e["data"]["tokens"]["input"] == 99
        for e in events
    )
    assert any(e["_type"] == "part" and e["data"]["text"] == "Fix the checkout flow" for e in events)


def test_build_bundle_includes_opencode_v2_migrator() -> None:
    script = (ROOT / "scripts/build.sh").read_text(encoding="utf-8")
    assert "scripts/opencode_v2_config.py" in script


def test_v2_usage_and_context_probe_use_session_message(tmp_path: Path) -> None:
    from lemoncrow.core.capabilities.host_usage import opencode_usage_calls
    from lemoncrow.gateway.hosts.context_state import _opencode_probe

    db = tmp_path / "opencode.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, time_created INTEGER, seq INTEGER, data TEXT)"
        )
        conn.execute("INSERT INTO session_v2 VALUES ('s2')")
        conn.execute(
            "INSERT INTO session_message VALUES ('u1', 's2', 'user', 1000, 1, ?)",
            (json.dumps({"role": "user", "text": "Please fix it"}),),
        )
        conn.execute(
            "INSERT INTO session_message VALUES ('a1', 's2', 'assistant', 2000, 2, ?)",
            (
                json.dumps(
                    {
                        "role": "assistant",
                        "modelID": "model-v2",
                        "providerID": "provider-v2",
                        "cost": 0.01,
                        "usage": {
                            "tokens": {
                                "input": 80,
                                "output": 30,
                                "reasoning": 3,
                                "cache": {"read": 20, "write": 5},
                            }
                        },
                    }
                ),
            ),
        )
    calls = opencode_usage_calls("s2", db)
    assert len(calls) == 1
    assert calls[0]["source_id"] == "a1"
    assert calls[0]["model"] == "provider-v2/model-v2"
    assert calls[0]["input_tokens"] == 80
    assert calls[0]["output_tokens"] == 30
    assert calls[0]["cache_read_tokens"] == 20
    assert calls[0]["cost_usd"] == 0.01
    assert _opencode_probe("s2", db) == (100, "provider-v2/model-v2")

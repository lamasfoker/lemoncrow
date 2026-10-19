"""Tests for the Plan-Mode-safe PreToolUse allow hook for lc's read-only MCP tools.

The hook is a standalone script reading a JSON payload on stdin and printing an
optional JSON allow decision on stdout, so it is exercised as a subprocess with
crafted payloads (same shape as test_agent_redirect_hook.py).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[2] / "integrations" / "claude" / "plugin" / "hooks" / "mcp_read_allow.py"


def _run(
    payload: dict, env_extra: dict | None = None, stdin_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=stdin_text if stdin_text is not None else json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, **(env_extra or {})},
        timeout=30,
    )


def test_allows_read_only_tools() -> None:
    for tool_name in [
        "mcp__lc__read",
        "mcp__lc__code_search",
        "mcp__lc__search",
        "mcp__lc__grep",
        "mcp__lc__relations",
        "mcp__lc__context",
        "mcp__lc__web_fetch",
        "mcp__lemoncrow__read",
    ]:
        proc = _run({"tool_name": tool_name, "tool_input": {}})
        assert proc.returncode == 0, proc.stderr
        hook_out = json.loads(proc.stdout)["hookSpecificOutput"]
        assert hook_out["hookEventName"] == "PreToolUse"
        assert hook_out["permissionDecision"] == "allow"


def test_stays_silent_for_write_capable_tools() -> None:
    for tool_name in [
        "mcp__lc__edit",
        "mcp__lc__bash",
        "mcp__lc__sql",
        "mcp__lc__codemod",
        "mcp__lc__memory",
        "mcp__lc__verify",
        "mcp__lc__tool",
    ]:
        proc = _run({"tool_name": tool_name, "tool_input": {}})
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""


def test_stays_silent_for_foreign_tool_names() -> None:
    for tool_name in ["Read", "Bash", "mcp__other__read", "mcp__lc__read__extra", "lc__read", ""]:
        proc = _run({"tool_name": tool_name, "tool_input": {}})
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""


def test_opt_out_env_var_disables_allow() -> None:
    proc = _run({"tool_name": "mcp__lc__read"}, env_extra={"LEMONCROW_MCP_READ_ALLOW": "0"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""

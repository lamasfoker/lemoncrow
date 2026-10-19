from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lemoncrow.gateway.adapters.mcp.bash import _run_bash_tool
from lemoncrow.pro.capabilities.tool_supervision import bash_exec


def test_background_run_returns_started_handle_without_polling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A detached command keeps its id even if the process could finish instantly."""

    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    started = {
        "status": "running",
        "session_id": "bg1234",
        "pid": 1234,
        "duration_ms": 0,
        "timeout_remaining_ms": 120_000,
        "over_budget": False,
        "timeout": 120,
        "explicit_background": True,
    }
    monkeypatch.setattr(bash_exec, "start_managed_command", lambda *args, **kwargs: dict(started))

    def unexpected_poll(*args: Any, **kwargs: Any) -> dict[str, Any]:
        pytest.fail("bg=True must return the durable handle before polling")

    monkeypatch.setattr(bash_exec, "poll_managed_command", unexpected_poll)

    result = _run_bash_tool("printf hi", cwd=str(tmp_path), background=True)

    assert result == started

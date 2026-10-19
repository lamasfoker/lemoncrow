"""Claude plugin hooks must fail open when the full LemonCrow package is absent.

``_run_hook.sh`` falls back to a bare ``python3`` when no interpreter can import
``lemoncrow`` (Runtime Lite and Review-only installs). Every hook then has to
exit 0 quietly instead of surfacing a traceback to the host.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[2] / "integrations" / "claude" / "plugin" / "hooks"
_PAYLOAD = {
    "session_id": "s1",
    "transcript_path": "/nonexistent/transcript.jsonl",
    "hook_event_name": "Stop",
    "stop_hook_active": False,
    "tool_name": "Bash",
    "tool_input": {"command": "ls"},
    "tool_response": {"stdout": "x"},
}


def test_hook_exits_cleanly_without_the_full_package(tmp_path: Path) -> None:
    for case_index, hook in enumerate(sorted(HOOKS.glob("*.py"))):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        completed = subprocess.run(
            [sys.executable, "-I", "-S", str(hook)],
            input=json.dumps(_PAYLOAD),
            capture_output=True,
            text=True,
            cwd=case_tmp_path,
            env={"HOME": str(case_tmp_path), "LEMONCROW_HOME": str(case_tmp_path / "state"), "PATH": "/usr/bin:/bin"},
            check=False,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr
        assert "Traceback" not in completed.stderr


def test_standalone_hooks_fail_open_on_malformed_json(tmp_path: Path) -> None:
    hook_names = [
        "agent_redirect.py",
        "mcp_read_allow.py",
        "mcp_output_shrink.py",
        "post_tool_use_failure.py",
        "bash_output_shrink.py",
    ]
    for index, name in enumerate(hook_names):
        root = tmp_path / f"case-{index}"
        root.mkdir()
        completed = subprocess.run(
            [sys.executable, str(HOOKS / name)],
            input="not json at all {{{",
            capture_output=True,
            text=True,
            cwd=root,
            env={
                "HOME": str(root),
                "CLAUDE_WORKSPACE_ROOT": str(root),
                "LEMONCROW_HOME": str(root / "state"),
                "PATH": "/usr/bin:/bin",
            },
            check=False,
            timeout=30,
        )
        assert completed.returncode == 0, (name, completed.stderr)
        assert completed.stdout == "", name

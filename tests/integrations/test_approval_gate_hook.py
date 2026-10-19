"""The Claude Code PreToolUse approval gate, run as the host runs it (P12-AGENT item 14).

The hook is a standalone script reading the tool call on stdin and printing an
optional decision on stdout. Unconfigured classes pass through untouched;
configured ones fail closed when the gate cannot load, cannot read its
settings or cannot reach the Review.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.action_approval import (
    MAX_WAIT_SECONDS,
    RISK_CLASSES,
    ApprovalConfigError,
    read_policy,
)
from lemoncrow.pro.capabilities.review.approval_hook import GATED_TOOLS, REQUEST_TIMEOUT_SECONDS

PLUGIN = Path(__file__).resolve().parents[2] / "integrations" / "claude" / "plugin"
HOOK = PLUGIN / "hooks" / "approval_gate.py"
WRAPPER = PLUGIN / "hooks" / "approval_gate.sh"


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


def _env(tmp_path: Path, *, settings: object | None = None, **extra: str) -> dict[str, str]:
    root = tmp_path / "lemoncrow-root"
    root.mkdir(exist_ok=True)
    if settings is not None:
        text = settings if isinstance(settings, str) else json.dumps(settings)
        (root / "plugin_settings.json").write_text(text, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("LEMONCROW_")}
    env.update(
        {
            "LEMONCROW_ROOT": str(root),
            "LEMONCROW_HOME": str(tmp_path / "home"),
            "LEMONCROW_URL": f"http://127.0.0.1:{_closed_port()}",
            "LEMONCROW_REVIEW_ID": "rv-test",
            **extra,
        }
    )
    return env


def _run(tmp_path: Path, payload: dict[str, Any], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"session_id": "author", "cwd": str(tmp_path), **payload}),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _decision(proc: subprocess.CompletedProcess[str]) -> str:
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return "pass"
    return str(json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"])


def _push() -> dict[str, Any]:
    return {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}}


def test_hooks_json_gates_every_writing_tool_and_outlasts_the_longest_wait() -> None:
    data = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    entries = [
        (entry, hook)
        for entry in data["hooks"]["PreToolUse"]
        for hook in entry["hooks"]
        if "approval_gate" in hook["command"]
    ]
    assert len(entries) == 1
    entry, hook = entries[0]
    assert set(entry["matcher"].split("|")) == set(GATED_TOOLS)
    # The shell entry fails closed when python cannot run the gate at all.
    assert hook["command"] == "bash ${CLAUDE_PLUGIN_ROOT}/hooks/approval_gate.sh"
    assert '"${here}/_run_hook.sh" "${here}/approval_gate.py"' in WRAPPER.read_text(encoding="utf-8")
    assert os.access(WRAPPER, os.X_OK)
    # A host timeout fails open, so it must outlast the hook's own bounded wait
    # plus its network calls; the hook then always answers first.
    assert hook["timeout"] >= MAX_WAIT_SECONDS + 4 * REQUEST_TIMEOUT_SECONDS


def test_ordinary_tools_and_unconfigured_classes_pass_untouched(tmp_path: Path) -> None:
    env = _env(tmp_path)
    assert _decision(_run(tmp_path, {"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}}, env)) == "pass"
    assert _decision(_run(tmp_path, _push(), env)) == "pass"
    configured = _env(tmp_path, settings={"review.approval.network_cloud": True})
    assert _decision(_run(tmp_path, _push(), configured)) == "pass"
    assert _decision(_run(tmp_path, {"tool_name": "Bash", "tool_input": {"command": "ls"}}, configured)) == "pass"


def test_a_configured_class_fails_closed_when_the_review_is_unreachable(tmp_path: Path) -> None:
    env = _env(tmp_path, settings={"review.approval.git_push": True, "review.approval.wait_seconds": 5})
    proc = _run(tmp_path, _push(), env)
    assert _decision(proc) == "deny"
    reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Git push" in reason and "fail closed" in reason


def test_unreadable_settings_fail_closed_for_risky_actions_only(tmp_path: Path) -> None:
    env = _env(tmp_path, settings="{broken")
    assert _decision(_run(tmp_path, _push(), env)) == "deny"
    assert _decision(_run(tmp_path, {"tool_name": "Bash", "tool_input": {"command": "ls"}}, env)) == "pass"


def test_a_broken_install_fails_closed_only_when_approval_is_configured(tmp_path: Path) -> None:
    for case_index, (settings, expected) in enumerate(
        [
            (None, "pass"),
            ({"review.approval.git_push": False}, "pass"),
            ({"lemoncrow": {"review.approval.secret_access": "on"}}, "deny"),
            ({f"review.approval.{name}": name == "outside_write" for name in RISK_CLASSES}, "deny"),
            ({f"review.approval.{name}": False for name in RISK_CLASSES}, "pass"),
            ("{broken", "deny"),
        ]
    ):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        shadow = case_tmp_path / "shadow" / "lemoncrow"
        shadow.mkdir(parents=True)
        (shadow / "__init__.py").write_text('raise ImportError("broken install")\n', encoding="utf-8")
        env = _env(case_tmp_path, settings=settings, PYTHONPATH=str(case_tmp_path / "shadow"))
        proc = _run(case_tmp_path, _push(), env)
        assert _decision(proc) == expected
        if expected == "deny":
            assert "could not load" in json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        read = _run(case_tmp_path, {"tool_name": "Read", "tool_input": {}}, env)
        assert _decision(read) == "pass"


def _plugin_copy(tmp_path: Path) -> Path:
    """The gate's files alone, so no install-time ``lemoncrow-python`` pointer is found."""

    hooks = tmp_path / "plugin" / "hooks"
    hooks.mkdir(parents=True)
    for name in ("approval_gate.sh", "_run_hook.sh", "approval_gate.py"):
        shutil.copy2(PLUGIN / "hooks" / name, hooks / name)
    return hooks / "approval_gate.sh"


def _run_wrapper(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run(
        [bash, str(_plugin_copy(tmp_path))],
        input=json.dumps({"session_id": "author", "cwd": str(tmp_path), **_push()}),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _without_python(tmp_path: Path, settings: object | None) -> dict[str, str]:
    """A PATH with the shell tools the wrapper uses and no python, lemoncrow or lc."""

    tools = tmp_path / "bin-without-python"
    tools.mkdir()
    for name in ("bash", "dirname", "cat", "sed", "tr", "grep", "head", "tail", "mkdir", "rm"):
        found = shutil.which(name)
        assert found is not None, name
        (tools / name).symlink_to(found)
    env = _env(tmp_path, settings=settings)
    return {
        "PATH": str(tools),
        "HOME": str(tmp_path / "home"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "LEMONCROW_ROOT": env["LEMONCROW_ROOT"],
    }


def _python_policy_on(root: Path) -> bool:
    try:
        return bool(read_policy(root).classes)
    except ApprovalConfigError:
        return True  # the gate itself fails closed on these


def test_without_python_the_gate_blocks_exactly_when_approval_is_configured(tmp_path: Path) -> None:
    for case_index, (settings, blocked) in enumerate(
        [
            (None, False),
            ({"review.approval.git_push": False}, False),
            ({"review.approval.wait_seconds": 30}, False),
            ({f"review.approval.{name}": "off" for name in RISK_CLASSES}, False),
            ({"lemoncrow": {"review.approval.git_push": "0"}}, False),
            ({"review.approval.git_push": True}, True),
            ({"review.approval.git_push": 1}, True),
            ({"lemoncrow": {"review.approval.secret_access": "on"}}, True),
            ('{\n  "review.approval.outside_write" :\n    "Yes"\n}\n', True),
            ("{broken", True),
            ("[]", True),
        ]
    ):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        env = _without_python(case_tmp_path, settings)
        proc = _run_wrapper(case_tmp_path, env)
        assert proc.stdout == ""
        if blocked:
            assert proc.returncode == 2, proc.stderr
            assert "approval gate could not run" in proc.stderr and "fail closed" in proc.stderr
        else:
            assert proc.returncode != 2, proc.stderr
        if _python_policy_on(Path(env["LEMONCROW_ROOT"])):
            assert blocked


@pytest.mark.parametrize(("settings", "expected"), [(None, "pass"), ({"review.approval.git_push": True}, "deny")])
def test_the_shell_entry_passes_the_gates_own_answer_through(
    tmp_path: Path, settings: object | None, expected: str
) -> None:
    env = _env(
        tmp_path,
        settings=settings,
        LEMONCROW_PYTHON=sys.executable,
        XDG_CACHE_HOME=str(tmp_path / "cache"),
    )
    proc = _run_wrapper(tmp_path, env)
    assert _decision(proc) == expected


def test_garbage_input_never_blocks_or_crashes(tmp_path: Path) -> None:
    env = _env(tmp_path, settings={"review.approval.git_push": True})
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input="not json", capture_output=True, text=True, env=env, timeout=60
    )
    assert proc.returncode == 0 and proc.stdout == "", proc.stderr

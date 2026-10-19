#!/usr/bin/env python3
"""Inject newly-arrived Review feedback at the next Copilot tool boundary."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from review_feedback import ack_feedback, feedback_context, lease_feedback


def _session_id(payload: dict[str, Any]) -> str:
    return str(
        payload.get("sessionId") or payload.get("session_id") or os.environ.get("GITHUB_COPILOT_SESSION_ID", "") or ""
    ).strip()


def _set_working(payload: dict[str, Any], session_id: str) -> None:
    if not session_id:
        return
    workspace = (
        Path(str(payload.get("cwd") or os.environ.get("COPILOT_PROJECT_DIR") or os.getcwd())).expanduser().resolve()
    )
    path = workspace / ".lemoncrow" / "workspace" / "session_state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(state, dict):
            state = {}
    except (OSError, json.JSONDecodeError):
        state = {}
    state.update({"host": "copilot", "session_id": session_id, "activity": "working"})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    session_id = _session_id(payload)
    _set_working(payload, session_id)
    if not session_id:
        return 0
    try:
        messages, message_ids = lease_feedback(session_id)
    except ValueError:
        messages, message_ids = (), ()
    context = feedback_context(messages, continue_now=False)
    if context:
        # Copilot CLI processes command-hook postToolUse.additionalContext and
        # appends it to this tool result before the same model turn continues.
        sys.stdout.write(json.dumps({"additionalContext": context}, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        ack_feedback(session_id, message_ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Copilot CLI userPromptSubmitted hook for Review/session continuity."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# This script is only invoked by Copilot CLI; stamp the shared runtime before
# importing any host-aware LemonCrow helper.
os.environ["LEMONCROW_AGENT"] = "copilot"


def _root() -> Path:
    return Path(os.environ.get("LEMONCROW_ROOT", "") or Path.home() / ".lemoncrow")


def _workspace(payload: dict[str, Any]) -> Path:
    return (
        Path(
            str(
                payload.get("cwd")
                or os.environ.get("COPILOT_PROJECT_DIR")
                or os.environ.get("LEMONCROW_WORKSPACE_ROOT")
                or os.getcwd()
            )
        )
        .expanduser()
        .resolve()
    )


def _session_id(payload: dict[str, Any]) -> str:
    return str(
        payload.get("sessionId") or payload.get("session_id") or os.environ.get("GITHUB_COPILOT_SESSION_ID", "") or ""
    ).strip()


def _write_activity(workspace: Path, session_id: str, activity: str) -> None:
    if not session_id:
        return
    path = workspace / ".lemoncrow" / "workspace" / "session_state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(state, dict):
            state = {}
    except (OSError, json.JSONDecodeError):
        state = {}
    state.update({"host": "copilot", "session_id": session_id, "activity": activity})
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
    workspace = _workspace(payload)
    session_id = _session_id(payload)
    prompt = str(payload.get("prompt") or "")
    _write_activity(workspace, session_id, "working")

    if session_id and prompt.strip():
        normalized = dict(payload)
        normalized.update({"session_id": session_id, "hook_event_name": "UserPromptSubmit", "cwd": str(workspace)})
        try:
            from lemoncrow.core.capabilities.plugin_runtime import (
                _seed_live_review_prepare,
                append_visible_session_turns,
                session_stats_path,
                update_session_stats,
            )

            stats = update_session_stats(_root(), normalized)
            if _seed_live_review_prepare(_root(), normalized, stats, host="copilot", workspace=str(workspace)):
                stats_path = session_stats_path(_root(), session_id)
                stats_path.parent.mkdir(parents=True, exist_ok=True)
                stats_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
            append_visible_session_turns(
                _root(),
                host="copilot",
                session_id=session_id,
                turns=[("user", f"prompt:{payload.get('timestamp') or ''}", prompt)],
                workspace=str(workspace),
            )
        except (ImportError, OSError, TypeError, ValueError):
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

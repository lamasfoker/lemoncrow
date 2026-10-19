#!/usr/bin/env python3
"""Copilot CLI sessionEnd hook: close the exact-session activity bridge."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def main() -> int:
    try:
        payload: dict[str, Any] = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    session_id = str(
        payload.get("sessionId") or payload.get("session_id") or os.environ.get("GITHUB_COPILOT_SESSION_ID", "") or ""
    ).strip()
    workspace = (
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
    path = workspace / ".lemoncrow" / "workspace" / "session_state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return 0
    if (
        not isinstance(state, dict)
        or state.get("host") != "copilot"
        or str(state.get("session_id") or "") != session_id
    ):
        return 0
    state["activity"] = "idle"
    state["ended"] = True
    try:
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

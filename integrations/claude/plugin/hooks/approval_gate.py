#!/usr/bin/env python3
"""PreToolUse hook: pause configured risky agent actions for a human decision.

LemonCrow Review (Phase 12 item 14). Only the classes switched on with
``lc settings set review.approval.<class> true`` are gated; everything else
passes through untouched. For a gated action the hook asks the Review, waits a
bounded time for a human, and blocks the action unless it was approved.

FAIL CLOSED for configured classes: if the gate cannot load, cannot read its
settings, cannot reach the Review service, or gets no decision in time, the
action is denied with a reason. With no class configured it never blocks.
The logic lives in ``lemoncrow.pro.capabilities.review.approval_hook``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_CLASSES = (
    "dependency_install",
    "schema_migration",
    "network_cloud",
    "secret_access",
    "git_push",
    "outside_write",
)
_GATED_TOOLS = frozenset({"Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"})


def _deny(reason: str) -> str:
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


def _any_class_configured() -> bool:
    """Standard-library read of the approval settings, for when LemonCrow cannot load."""

    root = os.environ.get("LEMONCROW_ROOT", "").strip()
    path = (Path(root).expanduser() if root else Path.home() / ".lemoncrow") / "plugin_settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True  # unreadable settings: assume configured (fail closed)
    if not isinstance(data, dict):
        return True
    nested = data.get("lemoncrow")
    merged = {**data, **nested} if isinstance(nested, dict) else data
    return any(
        merged.get(f"review.approval.{name}") is True
        or str(merged.get(f"review.approval.{name}", "")).strip().lower() in {"1", "true", "yes", "on"}
        for name in _CLASSES
    )


def main() -> int:
    raw = sys.stdin.read()
    try:
        from lemoncrow.pro.capabilities.review.approval_hook import main_from_text
    except Exception as exc:
        try:
            tool = str((json.loads(raw or "{}") or {}).get("tool_name") or "")
        except (ValueError, AttributeError):
            tool = ""
        if tool in _GATED_TOOLS and _any_class_configured():
            sys.stdout.write(
                _deny(
                    f"LemonCrow's approval gate could not load ({type(exc).__name__}); risky-action approval "
                    "is configured, so this action is blocked (fail closed). Reinstall LemonCrow or turn off "
                    "review.approval settings."
                )
            )
        return 0
    output = main_from_text(raw)
    if output:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())

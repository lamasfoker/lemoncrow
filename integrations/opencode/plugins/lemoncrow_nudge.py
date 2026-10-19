"""OpenCode chat.message adapter for LemonCrow prompt-time nudges."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


# Stamp the host before any lemoncrow.core import runs. This helper is shared by
# OpenCode and LemonCode. The LemonCode installer writes a tiny sibling marker
# because workspace installs intentionally keep the upstream `.opencode/` path,
# so path naming alone cannot distinguish the fork. Explicit env still wins.
def _host_name() -> str:
    explicit = os.environ.get("LEMONCROW_AGENT", "").strip().lower()
    if explicit in {"opencode", "lemoncode"}:
        return explicit
    marker = Path(__file__).with_name(".lemoncrow-host")
    try:
        marked = marker.read_text(encoding="utf-8").strip().lower()
    except OSError:
        marked = ""
    if marked in {"opencode", "lemoncode"}:
        return marked
    if os.environ.get("LEMONCODE_SESSION_ID") or os.environ.get("LEMONCODE_CLI"):
        return "lemoncode"
    return "opencode"


HOST = _host_name()
os.environ["LEMONCROW_AGENT"] = HOST


def _lemoncrow_root() -> Path:
    root = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    return Path(root) if root else Path.home() / ".lemoncrow"


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        root = _lemoncrow_root()
        event = str(payload.pop("event", None) or "prompt")
        output: dict[str, object] = {"no_output": True}
        try:
            if event == "post_tool":
                from lemoncrow.core.capabilities.plugin_runtime import build_opencode_post_tool_use_output

                output = build_opencode_post_tool_use_output(root, payload, host=HOST)
            elif event == "idle":
                from lemoncrow.core.capabilities.plugin_runtime import build_opencode_stop_output

                output = build_opencode_stop_output(root, payload, host=HOST)
            else:
                from lemoncrow.core.capabilities.plugin_runtime import build_opencode_user_prompt_output

                output = build_opencode_user_prompt_output(root, payload, host=HOST)
        except (ImportError, KeyError, TypeError, ValueError, OSError):
            pass
        ack_ids = output.pop("_lemoncrow_review_inbox_ids", []) if isinstance(output, dict) else []
        if output and not output.get("no_output"):
            sys.stdout.write(json.dumps(output) + "\n")
            sys.stdout.flush()
            if ack_ids:
                try:
                    from lemoncrow.pro.capabilities.review.session_inbox import ack_session_messages

                    session_id = str(payload.get("session_id") or payload.get("sessionId") or "").strip()
                    if session_id:
                        ack_session_messages(
                            root,
                            host=HOST,
                            session_id=session_id,
                            message_ids=(str(item) for item in ack_ids),
                        )
                except (ImportError, OSError, TypeError, ValueError):
                    pass
    except (ImportError, json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Codex SessionEnd hook: close the durable session after transcript flush."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _lemoncrow_root() -> Path:
    root = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    if root:
        return Path(root)
    return Path.home() / ".lemoncrow"


def main() -> int:
    try:
        from lemoncrow.core.capabilities.plugin_runtime import build_codex_session_end_output

        payload = json.loads(sys.stdin.read() or "{}")
        build_codex_session_end_output(_lemoncrow_root(), payload)
    except (ImportError, json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

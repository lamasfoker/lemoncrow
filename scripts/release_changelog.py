#!/usr/bin/env python3
"""Finalize the current CHANGELOG [Unreleased] section for a release."""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = REPO_ROOT / "CHANGELOG.md"


def finalize(text: str, version: str, date: str) -> str:
    version_header = re.compile(rf"(?m)^## \[{re.escape(version)}\](?:\s+-\s+.*)?$")
    marker = "## [Unreleased]"
    if text.count(marker) != 1:
        raise ValueError(f"expected exactly one {marker!r} section")
    before, after = text.split(marker, 1)
    existing = version_header.search(after)
    if existing:
        # Late fixes may land after a candidate was pre-versioned. Fold them
        # into that same release; never silently leave fixes "Unreleased".
        pending = after[: existing.start()].strip()
        if not pending:
            return text
        return (
            before
            + marker
            + "\n\n"
            + after[existing.start() : existing.end()]
            + "\n\n"
            + pending
            + "\n"
            + after[existing.end() :]
        )
    if not after.strip():
        raise ValueError("[Unreleased] section is empty")

    return before + marker + "\n\n" + f"## [{version}] - {date}" + after


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--date", default=dt.date.today().isoformat())
    parser.add_argument("--changelog", type=Path, default=CHANGELOG)
    args = parser.parse_args(argv)
    try:
        text = args.changelog.read_text(encoding="utf-8")
        args.changelog.write_text(finalize(text, args.version, args.date), encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"release changelog failed: {exc}", file=sys.stderr)
        return 1
    print(f"✓ Finalized CHANGELOG.md for {args.version} ({args.date})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

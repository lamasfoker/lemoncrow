"""Check a compiled candidate's fresh durable Review after a v0.7 -> v0.8 upgrade.

Pre-v0.8 Reviews are intentionally not migrated. This drill still demands a
usable Review, recorded mark and comment, and a second read of its Snapshot.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


class UpgradeVerificationError(ValueError):
    """A missing or incomplete new-release Review makes the upgrade fail."""


def _json_object(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpgradeVerificationError(f"unreadable candidate Review result {path.name}: {exc}") from exc
    if not isinstance(result, dict):
        raise UpgradeVerificationError(f"candidate Review result {path.name} is not an object")
    return result


def verify_upgrade(old_db: Path, old_counts: Path, captured_state: Path, reloaded_review: Path) -> str:
    """Demand functional new state, without requiring legacy schema adoption."""
    before = _json_object(old_counts)
    if not old_db.is_file():
        raise UpgradeVerificationError("the previous release's Review database was deleted")
    # Read the previous database in read-only mode; never create or migrate it.
    try:
        with sqlite3.connect(old_db.as_uri() + "?mode=ro", uri=True) as db:
            for table, recorded in (
                ("review_sessions", before.get("sessions")),
                ("review_revisions", before.get("revisions")),
                ("review_marks", before.get("marks")),
                ("annotations", before.get("annotations")),
            ):
                if not isinstance(recorded, int) or recorded < 1:
                    raise UpgradeVerificationError(f"invalid pre-upgrade {table} evidence")
                actual = db.execute(f"select count(*) from {table}").fetchone()[0]
                if actual < recorded:
                    raise UpgradeVerificationError(f"previous release database lost {table} rows")
    except sqlite3.DatabaseError as exc:
        raise UpgradeVerificationError(f"previous release database became unreadable: {exc}") from exc

    try:
        lines = captured_state.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise UpgradeVerificationError(f"candidate capture stderr unavailable: {exc}") from exc
    record = None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and str(row.get("review_id", "")).startswith("r/"):
            record = row
            break
    if record is None:
        raise UpgradeVerificationError("candidate did not return a durable Review reference")
    review_ref = record["review_id"]
    if not str(record.get("revision_id") or "").startswith("rr/"):
        raise UpgradeVerificationError("candidate did not persist a Snapshot identity")
    if not isinstance(record.get("revision_number"), int) or record["revision_number"] < 0:
        raise UpgradeVerificationError("candidate did not record a Snapshot number")
    if int(record.get("unit_count") or 0) < 1:
        raise UpgradeVerificationError("candidate produced no reviewable units")
    marks = record.get("mark_counts")
    if not isinstance(marks, dict) or int(marks.get("reviewed") or 0) < 1:
        raise UpgradeVerificationError("candidate did not persist the new Review mark")
    annotation = record.get("created_annotation")
    if not isinstance(annotation, dict) or annotation.get("body") != "new-release durable comment":
        raise UpgradeVerificationError("candidate did not persist the new Review comment")

    detail = _json_object(reloaded_review)
    review = detail.get("review")
    revision = detail.get("revision")
    if not isinstance(review, dict) or review.get("ref") != review_ref:
        raise UpgradeVerificationError("candidate could not reopen the newly captured Review")
    if not isinstance(revision, dict) or revision.get("revision_number") != record["revision_number"]:
        raise UpgradeVerificationError("candidate did not reopen the same saved Snapshot")
    if int(detail.get("revision_count") or 0) < 1:
        raise UpgradeVerificationError("candidate reports no durable Snapshots")
    return review_ref


def main() -> int:
    if len(sys.argv) != 5:
        print("usage: verify_release_review_bootstrap.py OLD_DB OLD_COUNTS CAPTURE_LOG RELOADED_JSON", file=sys.stderr)
        return 2
    try:
        ref = verify_upgrade(*(Path(arg) for arg in sys.argv[1:]))
    except UpgradeVerificationError as exc:
        print(f"candidate Review upgrade check failed: {exc}", file=sys.stderr)
        return 1
    print(f"✓ new Review {ref} persisted with mark, comment and readable Snapshot; legacy database retained")
    return 0


if __name__ == "__main__":
    sys.exit(main())

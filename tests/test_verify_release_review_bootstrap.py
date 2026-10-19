"""v0.8's upgrade is allowed to change Review stores, but not to lose new data."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.verify_release_review_bootstrap import UpgradeVerificationError, verify_upgrade


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    old_db = tmp_path / "lemoncrow_reviews.db"
    tables = {
        "sessions": "review_sessions",
        "revisions": "review_revisions",
        "marks": "review_marks",
        "annotations": "annotations",
    }
    with sqlite3.connect(old_db) as connection:
        for name in tables.values():
            connection.execute(f"CREATE TABLE {name}(id INTEGER PRIMARY KEY)")
            connection.execute(f"INSERT INTO {name}(id) VALUES (1)")
    old_counts = tmp_path / "before-counts.json"
    old_counts.write_text(json.dumps(dict.fromkeys(tables, 1)))
    captured = tmp_path / "after-state.txt"
    state = {
        "review_id": "r/test-123",
        "revision_id": "rr/revision-12",
        "revision_number": 0,
        "unit_count": 3,
        "mark_counts": {"reviewed": 1},
        "created_annotation": {"body": "new-release durable comment"},
    }
    captured.write_text("progress: preparing\n" + json.dumps(state) + "\n")
    shown = tmp_path / "after-show.json"
    shown.write_text(
        json.dumps(
            {
                "review": {"ref": "r/test-123"},
                "revision": {"revision_number": 0},
                "revision_count": 1,
            }
        )
    )
    return old_db, old_counts, captured, shown


def test_upgrade_accepts_new_storage_without_migrating_previous_review_schema(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    # No current Review schema migration is required in the previous database.
    assert verify_upgrade(*files) == "r/test-123"


def test_candidate_requires_an_actual_recorded_mark(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    state_file = files[2]
    state = json.loads(state_file.read_text().splitlines()[-1])
    state["mark_counts"] = {}
    state_file.write_text(json.dumps(state))
    with pytest.raises(UpgradeVerificationError, match="Review mark"):
        verify_upgrade(*files)


def test_candidate_requires_an_actual_recorded_comment(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    state_file = files[2]
    state = json.loads(state_file.read_text().splitlines()[-1])
    state["created_annotation"] = None
    state_file.write_text(json.dumps(state))
    with pytest.raises(UpgradeVerificationError, match="Review comment"):
        verify_upgrade(*files)


def test_new_review_identity_must_be_read_back_from_server(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    shown = files[3]
    data = json.loads(shown.read_text())
    data["review"]["ref"] = "r/other"
    shown.write_text(json.dumps(data))
    with pytest.raises(UpgradeVerificationError, match="reopen the newly captured Review"):
        verify_upgrade(*files)


def test_the_previous_db_is_not_wiped_even_though_the_new_reader_does_not_import_it(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    with sqlite3.connect(files[0]) as connection:
        connection.execute("DELETE FROM annotations")
    with pytest.raises(UpgradeVerificationError, match="lost annotations"):
        verify_upgrade(*files)


def test_missing_candidate_revision_is_not_mistaken_for_a_valid_upgrade(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    shown = files[3]
    shown.write_text(json.dumps({"review": {"ref": "r/test-123"}, "revision_count": 0}))
    with pytest.raises(UpgradeVerificationError, match="same saved Snapshot"):
        verify_upgrade(*files)

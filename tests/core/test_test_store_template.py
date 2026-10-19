from __future__ import annotations

import sqlite3
from pathlib import Path

from lemoncrow.infra.storage.factory import create_store
from tests.helpers import _initialized_store_template, init_store_at


def _db_names(root: Path) -> set[str]:
    return {path.name for path in root.glob("*.db")}


def test_init_store_at_clones_independent_checkpointed_stores(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    init_store_at(str(first))
    init_store_at(str(second))

    expected = {
        "lemoncrow_history.db",
        "lemoncrow_jobs.db",
        "lemoncrow_knowledge.db",
        "lemoncrow_lessons.db",
        "lemoncrow_memory.db",
        "lemoncrow_telemetry.db",
    }
    assert _db_names(first) == expected
    assert _db_names(second) == expected
    template = _initialized_store_template()
    assert not list(template.glob("*.db-wal"))
    assert not list(template.glob("*.db-shm"))

    # Cloning the DBs must not skip init()'s filesystem side effects. In
    # particular KnowledgeStore writes block/rubric mirrors outside the DB.
    first_bundle = create_store(first)
    second_bundle = create_store(second)
    assert first_bundle.knowledge.blocks_dir.is_dir() and first_bundle.knowledge.rubrics_dir.is_dir()
    assert second_bundle.knowledge.blocks_dir.is_dir() and second_bundle.knowledge.rubrics_dir.is_dir()

    with sqlite3.connect(first / "lemoncrow_knowledge.db") as conn:
        conn.execute("CREATE TABLE clone_marker (value TEXT)")
        conn.execute("INSERT INTO clone_marker VALUES ('first-only')")
        conn.commit()

    with sqlite3.connect(second / "lemoncrow_knowledge.db") as conn:
        marker = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='clone_marker'"
        ).fetchone()[0]
    assert marker == 0


def test_init_store_at_preserves_an_existing_store(tmp_path: Path) -> None:
    root = tmp_path / "existing"
    store = create_store(root)
    store.init()
    with sqlite3.connect(root / "lemoncrow_knowledge.db") as conn:
        conn.execute("CREATE TABLE preserved_marker (value TEXT)")
        conn.execute("INSERT INTO preserved_marker VALUES ('keep-me')")
        conn.commit()

    # First helper call for this path: the existing DB must force the original
    # idempotent init path rather than replacing it from the empty template.
    init_store_at(str(root))

    with sqlite3.connect(root / "lemoncrow_knowledge.db") as conn:
        value = conn.execute("SELECT value FROM preserved_marker").fetchone()[0]
    assert value == "keep-me"

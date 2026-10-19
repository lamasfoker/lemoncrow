"""Shared test helpers - reusable across test files without conftest import hacks."""

from __future__ import annotations

import atexit
import functools
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path


def grant_oauth_pro(
    monkeypatch: object,
    *,
    plan: str = "pro",
    email: str = "dev@example.com",
) -> None:
    """Legacy test compatibility: local capabilities are always enabled."""
    del monkeypatch, plan, email


def python_script_with_development_cap(script: Path) -> list[str]:
    """Legacy helper name: run the hook script directly in the local runtime."""
    import sys

    return [sys.executable, str(script)]


def deny_oauth(monkeypatch: object) -> None:
    """Legacy test compatibility: local capability access has no account state."""
    del monkeypatch


@functools.cache
def _initialized_store_template() -> Path:
    """One checkpointed empty SQLite StoreBundle per pytest process."""

    from lemoncrow.infra.storage.factory import create_store

    root = Path(tempfile.mkdtemp(prefix="lc-test-store-template-"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    create_store(root).init()
    # Store initialization uses WAL. Consolidate each empty schema into its
    # main DB once so clones need only copy ordinary files, never stale WAL/shm
    # sidecars or six rounds of schema/migration setup.
    for db_path in root.glob("*.db"):
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    for pattern in ("*.db-wal", "*.db-shm"):
        for sidecar in root.glob(pattern):
            sidecar.unlink(missing_ok=True)
    return root


@functools.cache
def init_store_at(root_str: str) -> None:
    """Initialize LemonCrow at *root_str* with a fast empty-store clone.

    Fresh SQLite test roots are cloned from one checkpointed, fully migrated
    template. Existing stores and non-default storage backends keep the original
    idempotent initialization path so this helper never overwrites test data or
    changes backend semantics.

    Caller must pass a **string** (not a Path) so the cache can hash it.
    """
    from lemoncrow.infra.storage.factory import create_store

    root = Path(root_str)
    backend = os.environ.get("LEMONCROW_STORAGE_BACKEND", "sqlite").strip().lower() or "sqlite"
    if backend != "sqlite" or any(root.glob("*.db")):
        create_store(root).init()
        return
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_initialized_store_template(), root, dirs_exist_ok=True)

    # KnowledgeStore owns workspace-relative block/rubric file mirrors whose
    # location is resolved from the *target* root/environment. Those empty
    # directories and packaged mirror files cannot be baked into a generic
    # template, so replay only this already-migrated store's cheap init step.
    bundle = create_store(root)
    bundle.knowledge.init()
    with sqlite3.connect(bundle.knowledge.db_path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    for suffix in ("-wal", "-shm"):
        Path(f"{bundle.knowledge.db_path}{suffix}").unlink(missing_ok=True)
    # DB schemas/migrations are already present in the clone, so this is a
    # millisecond-scale idempotent init. Keep it because store initialization
    # also creates non-SQLite filesystem contracts (knowledge block/rubric
    # mirrors, raw/traces directories, packaged-rubric sync) that callers rely on.
    create_store(root).init()

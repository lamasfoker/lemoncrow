"""Offline, restartable migration from the shared index to repository databases.

The source is checkpointed and write-locked, never deleted. Work is written under
a private staging directory; a complete, checked catalogue is published by one
directory rename. An interrupted repository is rebuilt on resume, while verified
repositories are reused only if the source fingerprint is unchanged.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.parse
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

from .contracts import AnalysisArtifact
from .repositories import _UNASSIGNED, RepositoryIndex, repository_index_root

_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS migration_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS migration_repositories (
    org_id TEXT NOT NULL, repo_id TEXT NOT NULL,
    blobs INTEGER NOT NULL, analyses INTEGER NOT NULL, views INTEGER NOT NULL,
    PRIMARY KEY (org_id, repo_id)
) WITHOUT ROWID;
"""


def source_signature(path: Path) -> str:
    info = path.stat()
    return json.dumps([str(path.resolve()), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns])


def _connect(path: Path, mode: str = "ro") -> sqlite3.Connection:
    conn = sqlite3.connect(
        "file:" + urllib.parse.quote(str(path.resolve())) + "?mode=" + mode,
        uri=True,
        timeout=1,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    return conn


def migration_source(catalog: Path) -> str | None:
    with closing(_connect(catalog)) as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='migration_state'").fetchone() is None:
            return None
        row = conn.execute("SELECT value FROM migration_state WHERE key='complete_source'").fetchone()
        return str(row[0]) if row else None


def _batches(cursor: sqlite3.Cursor) -> Iterator[list[sqlite3.Row]]:
    batch: list[sqlite3.Row] = []
    size = 0
    try:
        for row in cursor:
            batch.append(row)
            size += sum(len(value) if isinstance(value, (str, bytes)) else 8 for value in row)
            if len(batch) >= 128 or size >= 4 * 1024**2:
                yield batch
                batch, size = [], 0
        if batch:
            yield batch
    finally:
        cursor.close()


def _copy(source: sqlite3.Connection, destination: Any, table: str, where: str, parameters: Sequence[Any]) -> int:
    source_columns = {str(row["name"]) for row in source.execute(f"PRAGMA table_info({table})")}
    columns = [
        str(row["name"])
        for row in destination.rows(f"PRAGMA table_info({table})")
        if str(row["name"]) in source_columns
    ]
    names = ",".join(columns)
    query = f"SELECT {names} FROM {table} WHERE {where}"
    insert = f"INSERT INTO {table} ({names}) VALUES ({','.join('?' for _ in columns)})"
    copied = 0
    for batch in _batches(source.execute(query, tuple(parameters))):
        with destination.transaction() as conn:
            conn.executemany(insert, [tuple(row) for row in batch])
            if table == "manifest_rows":
                # Initialization marked the empty destination index as ready.
                # Fill its path candidates in the same bounded transaction as
                # these immutable rows, including during resumed migrations.
                conn.executemany(
                    "INSERT INTO manifest_path_search (org_id, root, path) VALUES (?, ?, ?)",
                    [(row["org_id"], row["root"], row["path"]) for row in batch],
                )
        copied += len(batch)
    expected = int(source.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", tuple(parameters)).fetchone()[0])
    if copied != expected:
        raise RuntimeError(f"migration count mismatch in {table}")
    return copied


def _prepare_ownership(source: sqlite3.Connection) -> None:
    source.executescript("""
        CREATE TEMP TABLE migration_owned (
            org_id TEXT NOT NULL, repo_id TEXT NOT NULL, digest TEXT NOT NULL,
            PRIMARY KEY (org_id, repo_id, digest)
        ) WITHOUT ROWID;
        CREATE INDEX migration_owned_digest ON migration_owned(org_id, digest);
        INSERT OR IGNORE INTO migration_owned SELECT org_id, repo_id, digest FROM content_provenance;
        INSERT OR IGNORE INTO migration_owned
            SELECT v.org_id, v.repo_id, m.content_digest
            FROM views v JOIN manifest_rows m ON m.org_id=v.org_id AND m.root=v.root;
        INSERT OR IGNORE INTO migration_owned
            SELECT v.org_id, v.repo_id, o.content_digest
            FROM views v JOIN overlay o ON o.view_id=v.view_id WHERE o.tombstone=0;
        INSERT OR IGNORE INTO migration_owned
            SELECT v.org_id, v.repo_id, m.content_digest
            FROM views v JOIN manifest_staging m ON m.view_id=v.view_id;
        CREATE TEMP TABLE migration_roots (
            org_id TEXT NOT NULL, repo_id TEXT NOT NULL, root TEXT NOT NULL,
            PRIMARY KEY (org_id, repo_id, root)
        ) WITHOUT ROWID;
        INSERT OR IGNORE INTO migration_roots SELECT org_id, repo_id, root FROM views;
    """)
    for table in ("blobs", "analysis"):
        source.execute(
            f"INSERT OR IGNORE INTO migration_owned SELECT a.org_id, ?, a.digest FROM {table} a "
            "WHERE NOT EXISTS (SELECT 1 FROM migration_owned o WHERE o.org_id=a.org_id AND o.digest=a.digest)",
            (_UNASSIGNED,),
        )
    source.execute(
        "INSERT OR IGNORE INTO migration_roots SELECT r.org_id, ?, r.root FROM manifest_roots r "
        "WHERE NOT EXISTS (SELECT 1 FROM migration_roots m WHERE m.org_id=r.org_id AND m.root=r.root)",
        (_UNASSIGNED,),
    )


def _discard_partial(owner: RepositoryIndex, org_id: str, repo_id: str) -> None:
    # Only a migration-owned staging directory reaches here, after its persisted
    # source fingerprint was validated. Published repositories are never removed.
    key = (org_id, repo_id)
    index = owner._indexes.pop(key, None)
    owner._backends.pop(key, None)
    if index is not None:
        index.close()
    with owner._catalog.transaction() as conn:
        conn.execute("DELETE FROM repository_views WHERE org_id=? AND repo_id=?", key)
        conn.execute("DELETE FROM repository_indexes WHERE org_id=? AND repo_id=?", key)
    path = owner.repository_path(*key)
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def _copy_repository(
    source: sqlite3.Connection,
    owner: RepositoryIndex,
    org_id: str,
    repo_id: str,
    progress: Callable[[str], None],
) -> tuple[int, int, int]:
    _discard_partial(owner, org_id, repo_id)
    index = owner.index(org_id, repo_id)
    assert index is not None
    db = index._db
    key = (org_id, repo_id)
    owned = "org_id=? AND digest IN (SELECT digest FROM migration_owned WHERE org_id=? AND repo_id=?)"
    owned_args = (org_id, *key)
    blobs = _copy(source, db, "blobs", owned, owned_args)
    analyses = 0
    cursor = source.execute("SELECT org_id,digest,parser_profile,payload FROM analysis WHERE " + owned, owned_args)
    started = time.monotonic()
    for batch in _batches(cursor):
        with index._analysis.batch():
            for row in batch:
                artifact = AnalysisArtifact.from_json(json.loads(str(row["payload"])))
                if artifact.digest != row["digest"] or artifact.parser_profile != row["parser_profile"]:
                    raise RuntimeError("source analysis identity does not match its payload")
                index._analysis.ensure(org_id, str(row["digest"]), str(row["parser_profile"]), b"", artifact=artifact)
                analyses += 1
        if time.monotonic() - started >= 5:
            progress(f"{org_id}/{repo_id}: {analyses} analyses converted")
            started = time.monotonic()
    expected = int(source.execute("SELECT COUNT(*) FROM analysis WHERE " + owned, owned_args).fetchone()[0])
    if analyses != expected:
        raise RuntimeError("migration analysis count mismatch")
    roots = "org_id=? AND root IN (SELECT root FROM migration_roots WHERE org_id=? AND repo_id=?)"
    for table in ("manifest_roots", "manifest_rows"):
        _copy(source, db, table, roots, (org_id, *key))
    views = _copy(source, db, "views", "org_id=? AND repo_id=?", key)
    for table in ("manifest_chunks", "manifest_staging", "overlay", "replays", "link_edges", "link_graphs"):
        _copy(source, db, table, "view_id IN (SELECT view_id FROM views WHERE org_id=? AND repo_id=?)", key)
    _copy(source, db, "content_provenance", "org_id=? AND repo_id=?", key)
    for row in db.rows("SELECT view_id FROM views"):
        owner.record_view(org_id, repo_id, str(row["view_id"]))
    # Merge FTS segments once after bulk ingest, then verify both SQLite and FTS.
    # Optimizing is offline; ordinary maintenance never rewrites a live repository.
    for table in ("search_terms", "search_grams"):
        db._conn.execute(f"INSERT INTO {table}({table}) VALUES ('optimize')")
        db._conn.execute(f"INSERT INTO {table}({table}) VALUES ('integrity-check')")
    checks = [str(row[0]) for row in db._conn.execute("PRAGMA quick_check")]
    if checks != ["ok"] or db._conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("migrated repository failed SQLite integrity verification")
    db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    return blobs, analyses, views


def migrate_index(
    database: str | Path,
    *,
    content_size_cap: int = 1024**2,
    max_repository_bytes: int = 64 * 1024**3,
    max_wal_bytes: int = 128 * 1024**2,
    progress: Callable[[str], None] = lambda _message: None,
) -> dict[str, Any]:
    source_path = Path(database).resolve()
    root = repository_index_root(source_path)
    staging = root.with_name(root.name + ".migrating")
    if max_repository_bytes <= max_wal_bytes or max_wal_bytes <= 0:
        raise ValueError("repository disk budget must exceed a positive WAL budget")
    if root.exists():
        raise RuntimeError("repository destination already exists; no data was overwritten")
    lock = _connect(source_path, "rw")
    lock_open = True
    owner: RepositoryIndex | None = None
    source: sqlite3.Connection | None = None
    try:
        # Normalize the source WAL before fingerprinting. A busy checkpoint is a
        # concrete refusal, not permission to copy while another server writes.
        if int(lock.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]):
            raise RuntimeError("source database is in use; stop the server before migrating")
        lock.execute("BEGIN IMMEDIATE")
        signature = source_signature(source_path)
        source = _connect(source_path)
        required = {
            "repos",
            "views",
            "blobs",
            "analysis",
            "content_provenance",
            "manifest_roots",
            "manifest_rows",
            "manifest_chunks",
            "manifest_staging",
            "overlay",
            "replays",
            "link_edges",
            "link_graphs",
        }
        present = {str(row[0]) for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= present:
            raise RuntimeError("source is not a supported shared index")
        if "search_documents" in present:
            raise RuntimeError("source is already a compact repository index")
        if staging.exists():
            catalog = staging / "catalog.sqlite3"
            with closing(_connect(catalog)) as check:
                row = check.execute("SELECT value FROM migration_state WHERE key='source'").fetchone()
            if row is None or str(row[0]) != signature:
                raise RuntimeError("migration source changed; existing staging data was left untouched")
        owner = RepositoryIndex(
            staging,
            content_size_cap=content_size_cap,
            max_repository_bytes=max_repository_bytes,
            max_wal_bytes=max_wal_bytes,
        )
        owner._catalog._conn.executescript(_STATE_SCHEMA)
        with owner._catalog.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO migration_state VALUES ('source', ?)", (signature,))
            for row in source.execute("SELECT org_id,claim_key,repo_id FROM repos"):
                conn.execute("INSERT OR IGNORE INTO repos VALUES (?,?,?)", tuple(row))
        _prepare_ownership(source)
        repositories = [
            tuple(row)
            for row in source.execute(
                "SELECT org_id,repo_id FROM repos UNION SELECT org_id,repo_id FROM views "
                "UNION SELECT org_id,repo_id FROM migration_owned "
                "UNION SELECT org_id,repo_id FROM migration_roots ORDER BY 1,2"
            )
        ]
        totals = {"repositories": 0, "blobs": 0, "analyses": 0, "views": 0}
        for org_id, repo_id in repositories:
            done = owner._catalog.row(
                "SELECT blobs,analyses,views FROM migration_repositories WHERE org_id=? AND repo_id=?",
                (org_id, repo_id),
            )
            if done is None:
                progress(f"{org_id}/{repo_id}: converting repository")
                counts = _copy_repository(source, owner, org_id, repo_id, progress)
                with owner._catalog.transaction() as conn:
                    conn.execute("INSERT INTO migration_repositories VALUES (?,?,?,?,?)", (org_id, repo_id, *counts))
            else:
                counts = (int(done["blobs"]), int(done["analyses"]), int(done["views"]))
                progress(f"{org_id}/{repo_id}: reusing verified repository")
            totals["repositories"] += int(repo_id != _UNASSIGNED)
            for name, count in zip(("blobs", "analyses", "views"), counts, strict=True):
                totals[name] += count
        if totals["views"] != int(source.execute("SELECT COUNT(*) FROM views").fetchone()[0]):
            raise RuntimeError("migration did not preserve every view")
        # One source digest may legitimately be copied to several repositories.
        # Every source blob and analysis must still have at least one owner.
        for table in ("blobs", "analysis"):
            missing = int(
                source.execute(
                    f"SELECT COUNT(*) FROM {table} a WHERE NOT EXISTS "
                    "(SELECT 1 FROM migration_owned o WHERE o.org_id=a.org_id AND o.digest=a.digest)"
                ).fetchone()[0]
            )
            if missing:
                raise RuntimeError(f"migration left unassigned {table} rows")
        with owner._catalog.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO migration_state VALUES ('complete_source', ?)", (signature,))
        owner._catalog._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        owner.close()
        owner = None
        source.close()
        source = None
        lock.execute("ROLLBACK")
        lock.close()
        lock_open = False
        os.rename(staging, root)
        # Persist the directory publication before reporting success.
        directory_fd = os.open(root.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        total_bytes = sum(path.stat().st_size for path in root.rglob("*.sqlite3"))
        return {
            **totals,
            "source_bytes": source_path.stat().st_size,
            "repository_bytes": total_bytes,
            "destination": str(root),
            "source_retained": True,
        }
    finally:
        if owner is not None:
            owner.close()
        if source is not None:
            source.close()
        if lock_open:
            if lock.in_transaction:
                lock.execute("ROLLBACK")
            lock.close()

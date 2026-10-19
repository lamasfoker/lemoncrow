import asyncio
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from lemoncrow_server_core.errors import ErrorCode, ServerError
from lemoncrow_server_core.index.contracts import sha256_hex
from lemoncrow_server_core.index.sqlite import SqliteIndex


def _pinned_wal(tmp_path: Path) -> tuple[SqliteIndex, sqlite3.Connection]:
    """A store whose WAL is over budget while a reader holds a snapshot of it."""
    path = tmp_path / "index.sqlite3"
    index = SqliteIndex(path, content_size_cap=4 * 1024**2, compact=True, max_wal_bytes=64 * 1024)
    first = os.urandom(256 * 1024)
    index.backend.content.put("org", sha256_hex(first), first)
    reader = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM sqlite_master").fetchone()
    return index, reader


def test_a_write_waits_briefly_for_the_reader_pinning_the_wal(tmp_path: Path) -> None:
    index, reader = _pinned_wal(tmp_path)
    release = threading.Timer(0.3, lambda: reader.execute("COMMIT"))
    release.start()
    try:
        second = os.urandom(1024)
        index.backend.content.put("org", sha256_hex(second), second)
        assert index.backend.content.get("org", sha256_hex(second)) == second
    finally:
        release.join()
        reader.close()
        index.close()


def test_a_wal_pinned_past_the_wait_is_still_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow_server_core.index import sqlite as sqlite_module

    monkeypatch.setattr(sqlite_module, "_WAL_PIN_WAIT_S", 0.1)
    index, reader = _pinned_wal(tmp_path)
    try:
        second = os.urandom(1024)
        with pytest.raises(ServerError) as refusal:
            index.backend.content.put("org", sha256_hex(second), second)
        assert refusal.value.code == ErrorCode.STORAGE_LIMIT
        assert refusal.value.retryable is True
    finally:
        reader.execute("COMMIT")
        reader.close()
        index.close()


def test_a_wal_pinned_write_on_an_event_loop_is_refused_without_waiting(tmp_path: Path) -> None:
    """Hosted routes run local stores inline on the loop: sleeping there would
    stall every request, so the refusal is immediate and the client retries."""
    index, reader = _pinned_wal(tmp_path)
    second = os.urandom(1024)

    async def on_the_loop() -> float:
        started = time.monotonic()
        with pytest.raises(ServerError) as refusal:
            index.backend.content.put("org", sha256_hex(second), second)
        assert refusal.value.code == ErrorCode.STORAGE_LIMIT
        assert refusal.value.details["kind"] == "wal"
        return time.monotonic() - started

    try:
        assert asyncio.run(on_the_loop()) < 0.5
    finally:
        reader.execute("COMMIT")
        reader.close()
        index.close()


def test_lower_budget_keeps_existing_data_readable_and_allows_cleanup(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite3"
    data = b"x" * (2 * 1024**2)
    digest = sha256_hex(data)
    original = SqliteIndex(path, content_size_cap=4 * 1024**2, compact=True)
    original.backend.content.put("org", digest, data)
    original.close()

    recovered = SqliteIndex(
        path,
        content_size_cap=4 * 1024**2,
        compact=True,
        max_bytes=1024**2,
        max_wal_bytes=128 * 1024,
    )
    try:
        assert recovered.backend.content.get("org", digest) == data
        pages = recovered._db.scalar_int("PRAGMA page_count")
        assert recovered._db._conn.execute("PRAGMA max_page_count").fetchone()[0] == pages
        extra = b"y" * (2 * 1024**2)
        with pytest.raises(ServerError) as refusal:
            recovered.backend.content.put("org", sha256_hex(extra), extra)
        assert refusal.value.code == ErrorCode.STORAGE_LIMIT
        assert recovered._db.scalar_int("PRAGMA page_count") == pages
        recovered.backend.content.mark_unreferenced({}, 1.0)
        assert recovered.backend.content.sweep_marked(2.0).blob_count == 1
        assert recovered.backend.content.get("org", digest) is None
    finally:
        recovered.close()

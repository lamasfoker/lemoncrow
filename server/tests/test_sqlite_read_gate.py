"""Reads on pooled read-only connections run a bounded number at a time.

SQLite serializes its allocator process-wide, so eight concurrent statements
contended instead of scaling and ran slower together than in sequence.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_server_core.index import sqlite as index_sqlite
from lemoncrow_server_core.index.sqlite import _CONCURRENT_READS, _Db


def _db(tmp_path: Path) -> _Db:
    db = _Db(str(tmp_path / "index.sqlite3"))
    with db.transaction() as conn:
        conn.execute("CREATE TABLE probe (k INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO probe VALUES (1)")
    return db


@contextmanager
def _every_slot_taken() -> Iterator[None]:
    for _ in range(_CONCURRENT_READS):
        assert index_sqlite._READ_GATE.acquire(timeout=5)
    try:
        yield
    finally:
        for _ in range(_CONCURRENT_READS):
            index_sqlite._READ_GATE.release()


def _finishes(target: Any) -> bool:
    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout=5)
    return not worker.is_alive()


def test_concurrent_reads_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(tmp_path)
    active, peak = [0], [0]
    lock = threading.Lock()
    real = _Db._read_rows

    def slow(connection: sqlite3.Connection, sql: str, parameters: Sequence[Any]) -> list[sqlite3.Row]:
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            time.sleep(0.05)
            return real(connection, sql, parameters)
        finally:
            with lock:
                active[0] -= 1

    monkeypatch.setattr(_Db, "_read_rows", staticmethod(slow))
    results: list[int] = []
    workers = [
        threading.Thread(target=lambda: results.append(db.scalar_int("SELECT COUNT(*) FROM probe"))) for _ in range(12)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    assert results == [1] * 12
    assert peak[0] == _CONCURRENT_READS
    db.close()


def test_an_event_loop_never_queues_for_a_read(tmp_path: Path) -> None:
    db = _db(tmp_path)
    seen: list[int] = []

    async def on_loop() -> None:
        seen.append(db.scalar_int("SELECT COUNT(*) FROM probe"))

    with _every_slot_taken():
        assert _finishes(lambda: asyncio.run(on_loop())), "a read on the event loop waited for a slot"
    assert seen == [1]
    db.close()


def test_a_writer_reading_its_own_rows_never_queues(tmp_path: Path) -> None:
    db = _db(tmp_path)
    seen: list[int] = []

    def write_then_read() -> None:
        with db.transaction() as conn:
            conn.execute("INSERT INTO probe VALUES (2)")
            seen.append(db.scalar_int("SELECT COUNT(*) FROM probe"))

    with _every_slot_taken():
        assert _finishes(write_then_read), "a write transaction's own read waited for a slot"
    assert seen == [2]
    db.close()


def test_a_pooled_read_waits_for_a_slot(tmp_path: Path) -> None:
    db = _db(tmp_path)
    seen: list[int] = []
    worker = threading.Thread(target=lambda: seen.append(db.scalar_int("SELECT COUNT(*) FROM probe")), daemon=True)
    with _every_slot_taken():
        worker.start()
        worker.join(timeout=0.2)
        assert worker.is_alive(), "a pooled read ran while every slot was taken"
    worker.join(timeout=5)
    assert seen == [1]
    db.close()

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from lemoncrow_server_core.app import CoreServerRoutes
from lemoncrow_server_core.errors import ErrorCode, ServerError
from lemoncrow_server_core.index.sqlite import _Db
from lemoncrow_server_core.read_budget import ReadBudget, current_read_budget, read_budget

_LONG_QUERY = """
WITH RECURSIVE numbers(n) AS (
  VALUES(1) UNION ALL SELECT n + 1 FROM numbers WHERE n < 100000000
) SELECT sum(n) FROM numbers
"""


@pytest.mark.parametrize("durable", [False, True])
def test_deadline_interrupts_query_and_connection_is_reusable(tmp_path, durable):
    db = _Db(str(tmp_path / "index.sqlite") if durable else ":memory:")
    try:
        with read_budget(ReadBudget(deadline=time.monotonic() + 0.02)):
            with pytest.raises(ServerError) as caught:
                db.rows(_LONG_QUERY)
        assert caught.value.code is ErrorCode.DEADLINE_EXCEEDED
        assert db.scalar_int("SELECT 42") == 42
        assert current_read_budget() is None
    finally:
        db.close()


def test_cancelled_read_releases_wal_snapshot(tmp_path):
    db = _Db(str(tmp_path / "index.sqlite"))
    db._conn.execute("CREATE TABLE probe(n INTEGER)")
    db._conn.executemany("INSERT INTO probe VALUES (?)", [(i,) for i in range(1000)])
    reader = db._borrow_reader()
    assert reader is not None
    started = threading.Event()

    def tick():
        started.set()
        return 1

    reader.create_function("tick", 0, tick)
    db._return_reader(reader)
    budget = ReadBudget()

    def query():
        with read_budget(budget):
            return db.rows("SELECT sum(a.n + b.n + c.n) FROM probe a, probe b, probe c WHERE tick() = 1")

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(query)
            try:
                assert started.wait(2)
                db._conn.execute("UPDATE probe SET n = n + 1")
                db._conn.execute("PRAGMA busy_timeout=0")
                assert db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 1
                budget.cancelled.set()
                with pytest.raises(ServerError) as caught:
                    future.result(timeout=2)
                assert caught.value.code is ErrorCode.CANCELLED
                assert db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
                assert db.scalar_int("SELECT count(*) FROM probe") == 1000
            finally:
                budget.cancelled.set()
    finally:
        db.close()


@pytest.mark.parametrize("reason", ["deadline", "client_cancel", "handler_cancel"])
def test_dispatch_stops_worker_database_read(tmp_path, reason):
    async def exercise():
        db = _Db(str(tmp_path / "index.sqlite"))
        started = threading.Event()
        stopped = threading.Event()

        def dispatch(invocation):
            started.set()
            try:
                return db.rows(_LONG_QUERY)
            finally:
                stopped.set()

        token = asyncio.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            routes = SimpleNamespace(
                _executor=executor,
                config=SimpleNamespace(limits=SimpleNamespace(tool_deadline_s=0.1 if reason == "deadline" else 5)),
                dispatcher=SimpleNamespace(dispatch=dispatch),
            )
            task = asyncio.create_task(CoreServerRoutes._dispatch(routes, SimpleNamespace(tool="code_search"), token))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                if reason == "client_cancel":
                    token.set()
                elif reason == "handler_cancel":
                    task.cancel()
                if reason == "handler_cancel":
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    with pytest.raises(ServerError) as caught:
                        await task
                    assert caught.value.code is (
                        ErrorCode.DEADLINE_EXCEEDED if reason == "deadline" else ErrorCode.CANCELLED
                    )
                assert await asyncio.to_thread(stopped.wait, 2)
                assert db.scalar_int("SELECT 7") == 7
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        db.close()

    asyncio.run(exercise())

    asyncio.run(exercise())


def test_unrelated_sql_error_is_preserved():
    db = _Db(":memory:")
    try:
        with read_budget(ReadBudget()):
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                db.rows("SELECT * FROM missing")
        assert db.scalar_int("SELECT 1") == 1
    finally:
        db.close()

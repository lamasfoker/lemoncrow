from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lemoncrow_server_core import local_review
from lemoncrow_server_core.read_budget import current_read_budget


def _request(path="/api/reviews/r/ask"):
    transport = SimpleNamespace(is_closing=lambda: False, get_extra_info=lambda name: None)
    return SimpleNamespace(
        read=AsyncMock(return_value=b""),
        raw_headers=[],
        version=SimpleNamespace(major=1, minor=1),
        method="GET",
        scheme="http",
        path=path,
        query_string="",
        transport=transport,
        host="localhost",
        url=SimpleNamespace(port=7420),
    )


@pytest.mark.parametrize(
    "path", ["/api/reviews/r/ask", "/api/reviews/r/source-state/events", "/api/reviews/r/live-state/events"]
)
@pytest.mark.parametrize("failure", ["prepare", "write", "before_headers", "handler_cancel"])
def test_disconnect_cleans_up_application_and_queue(monkeypatch, failure, path):
    async def exercise():
        request = _request(path)
        response = SimpleNamespace(prepare=AsyncMock(), write=AsyncMock(), write_eof=AsyncMock())
        if failure in {"prepare", "write"}:
            getattr(response, failure).side_effect = ConnectionResetError("client disconnected")
        monkeypatch.setattr(local_review.web, "StreamResponse", lambda **kwargs: response)
        started = asyncio.Event()
        stopped = asyncio.Event()
        budgets = []
        before = asyncio.all_tasks()

        async def app(scope, receive, send):
            budgets.append(current_read_budget())
            started.set()
            try:
                if failure in {"before_headers", "handler_cancel"}:
                    await asyncio.Event().wait()
                await send({"type": "http.response.start", "status": 200})
                while True:
                    await send({"type": "http.response.body", "body": b"chunk", "more_body": True})
            finally:
                stopped.set()

        bridge = asyncio.create_task(local_review._dispatch_asgi(app, request))
        await started.wait()
        if failure == "before_headers":
            request.transport.is_closing = lambda: True
        elif failure == "handler_cancel":
            bridge.cancel()
        with pytest.raises(
            asyncio.CancelledError if failure in {"before_headers", "handler_cancel"} else ConnectionResetError
        ):
            await asyncio.wait_for(bridge, 2)
        assert stopped.is_set()
        assert budgets[0] is not None and budgets[0].cancelled.is_set()
        assert not (asyncio.all_tasks() - before)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "path", ["/api/reviews/r/ask", "/api/reviews/r/source-state/events", "/api/reviews/r/live-state/events"]
)
def test_stream_completes_without_false_disconnect(monkeypatch, path):
    async def exercise():
        request = _request(path)
        response = SimpleNamespace(prepare=AsyncMock(), write=AsyncMock(), write_eof=AsyncMock())
        monkeypatch.setattr(local_review.web, "StreamResponse", lambda **kwargs: response)

        async def app(scope, receive, send):
            assert (await receive())["type"] == "http.request"
            receiving = asyncio.create_task(receive())
            try:
                await asyncio.sleep(0)
                assert not receiving.done()
                await send({"type": "http.response.start", "status": 200})
                await send({"type": "http.response.body", "body": b"one", "more_body": True})
                await send({"type": "http.response.body", "body": b"two", "more_body": False})
            finally:
                receiving.cancel()
                await asyncio.gather(receiving, return_exceptions=True)

        assert await local_review._dispatch_asgi(app, request) is response
        assert [call.args[0] for call in response.write.await_args_list] == [b"one", b"two"]
        response.write_eof.assert_awaited_once()

    asyncio.run(exercise())


def test_non_streaming_request_runs_on_its_own_thread_and_returns_a_buffered_response():
    import threading

    async def exercise():
        request = _request("/api/reviews/r/annotations")
        seen = {}

        async def app(scope, receive, send):
            seen["thread"] = threading.current_thread()
            assert (await receive())["type"] == "http.request"
            await send({"type": "http.response.start", "status": 201, "headers": [(b"x-a", b"b")]})
            await send({"type": "http.response.body", "body": b"ok", "more_body": False})

        response = await local_review._dispatch_asgi(app, request)
        assert (response.status, response.body, response.headers["x-a"]) == (201, b"ok", "b")
        assert seen["thread"] is not threading.main_thread()

    asyncio.run(exercise())


def test_a_blocking_handler_does_not_stall_other_requests():
    import time

    async def exercise():
        async def slow(scope, receive, send):
            time.sleep(0.6)  # blocking work inside an async handler
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"slow"})

        async def quick(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"quick"})

        slow_task = asyncio.create_task(local_review._dispatch_asgi(slow, _request("/api/reviews/r/map")))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        response = await local_review._dispatch_asgi(quick, _request("/api/reviews/r/journal"))
        assert response.body == b"quick" and time.monotonic() - started < 0.4
        assert (await slow_task).body == b"slow"

    asyncio.run(exercise())


def test_cancelling_a_non_streaming_request_cancels_the_app_and_frees_its_thread():
    async def exercise():
        flags = {"started": threading_event(), "stopped": threading_event()}

        async def app(scope, receive, send):
            flags["started"].set()
            try:
                await asyncio.Event().wait()
            finally:
                flags["stopped"].set()

        bridge = asyncio.create_task(local_review._dispatch_asgi(app, _request("/api/reviews/r/map")))
        while not flags["started"].is_set():
            await asyncio.sleep(0.01)
        bridge.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(bridge, 2)
        assert flags["stopped"].is_set()

    asyncio.run(exercise())


def threading_event():
    import threading

    return threading.Event()


def test_reader_serves_an_unchanged_store_read_from_cache_and_misses_after_a_write(tmp_path, monkeypatch):
    import sqlite3

    from lemoncrow_server_core import local_reviews

    db = tmp_path / "review.db"
    sqlite3.connect(db).execute("create table t(x)").connection.commit()
    calls = []

    async def app(scope, receive, send):
        calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"n":%d}' % len(calls)})

    # In no change set (Phase 9R item 42), so the stamp is this store's alone.
    store = SimpleNamespace(db_path=db, list_relationships_by_kind=lambda _kind: ())
    reader = local_reviews.LocalReviewReader.__new__(local_reviews.LocalReviewReader)
    reader._responses = local_reviews.OrderedDict()
    reader._inflight = {}
    monkeypatch.setattr(local_reviews.LocalReviewReader, "_resolved_pair", lambda self, path, query: (app, store))

    def get(path):
        request = _request(path)
        request.method = "GET"
        request.query = {}
        return reader.handle(request)

    async def exercise():
        first = await get("/api/reviews/r1/targets")
        second = await get("/api/reviews/r1/targets")
        assert first.body == second.body == b'{"n":1}' and calls == ["/api/reviews/r1/targets"]
        uncached = await get("/api/reviews/r1/journal")  # not a pure store read
        assert uncached.body == b'{"n":2}'
        with sqlite3.connect(db) as conn:  # agent-timeline ingestion is not a cached input
            conn.execute("create table development_events(x)")
        await get("/api/reviews/r1/targets")  # schema change: triggers re-installed, one miss
        with sqlite3.connect(db) as conn:
            conn.execute("insert into development_events values (1)")
        hits = len(calls)
        assert (await get("/api/reviews/r1/targets")).body == b'{"n":%d}' % hits
        assert len(calls) == hits
        with sqlite3.connect(db) as conn:  # any write to a tracked table
            conn.execute("insert into t values (1)")
        after_write = await get("/api/reviews/r1/targets")
        assert after_write.body == b'{"n":%d}' % (hits + 1)

    asyncio.run(exercise())

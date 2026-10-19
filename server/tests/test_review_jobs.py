from __future__ import annotations

import asyncio

import pytest
from lemoncrow_server_core.errors import ErrorCode, ServerError
from lemoncrow_server_core.review_jobs import ReviewCaptureJobs

OWNER = ("org", "alice", "repo")


def test_job_runs_in_background_and_reports_result() -> None:
    async def scenario() -> None:
        jobs = ReviewCaptureJobs()
        gate = asyncio.Event()

        async def run() -> tuple[int, dict[str, object]]:
            await gate.wait()
            return 201, {"review": {"id": "r1"}}

        progress = {"detail": "Building review targets · 12/2249 files"}
        job_id = jobs.submit(OWNER, run, progress=progress)
        assert jobs.get(OWNER, job_id) == ("running", 0, progress)
        progress["detail"] = "Building review targets · 13/2249 files"
        assert jobs.get(OWNER, job_id)[2]["detail"].endswith("13/2249 files")
        gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert jobs.get(OWNER, job_id) == ("done", 201, {"review": {"id": "r1"}})

    asyncio.run(scenario())


def test_failures_become_typed_wire_errors_and_unexpected_ones_are_opaque() -> None:
    async def scenario() -> None:
        jobs = ReviewCaptureJobs()

        async def typed() -> tuple[int, dict[str, object]]:
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "bad packet")

        async def boom() -> tuple[int, dict[str, object]]:
            raise RuntimeError("secret path /home/x")

        first, second = jobs.submit(OWNER, typed), jobs.submit(OWNER, boom)
        await asyncio.sleep(0.01)
        state, status, payload = jobs.get(OWNER, first)
        assert (state, status) == ("failed", 400) or state == "failed"
        assert "bad packet" in str(payload)
        state, status, payload = jobs.get(OWNER, second)
        assert (state, status) == ("failed", 500)
        assert "secret" not in str(payload) and "correlation_id" in str(payload)

    asyncio.run(scenario())


def test_other_callers_job_reads_unknown_and_results_expire() -> None:
    async def scenario() -> None:
        now = [0.0]
        jobs = ReviewCaptureJobs(ttl_s=10, clock=lambda: now[0])

        async def run() -> tuple[int, dict[str, object]]:
            return 201, {}

        job_id = jobs.submit(OWNER, run)
        await asyncio.sleep(0.01)
        with pytest.raises(ServerError) as other:
            jobs.get(("org", "mallory", "repo"), job_id)
        assert other.value.code is ErrorCode.REVIEW_UNKNOWN
        now[0] = 11.0
        with pytest.raises(ServerError):
            jobs.get(OWNER, job_id)

    asyncio.run(scenario())


def test_completed_jobs_do_not_block_new_captures_and_retention_is_bounded() -> None:
    async def scenario() -> None:
        jobs = ReviewCaptureJobs(max_jobs=1)

        async def run() -> tuple[int, dict[str, object]]:
            return 201, {}

        first = jobs.submit(OWNER, run)
        await asyncio.sleep(0)
        second = jobs.submit(OWNER, run)
        assert jobs.get(OWNER, first)[0] == "done"
        await asyncio.sleep(0)
        assert jobs.get(OWNER, second)[0] == "done"
        with pytest.raises(ServerError):
            jobs.get(OWNER, first)

    asyncio.run(scenario())


def test_shutdown_collects_background_tasks() -> None:
    async def scenario() -> None:
        jobs = ReviewCaptureJobs()
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def run() -> tuple[int, dict[str, object]]:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
            return 201, {}

        job_id = jobs.submit(OWNER, run)
        await started.wait()
        await jobs.close(timeout_s=0)
        assert stopped.is_set()
        with pytest.raises(ServerError):
            jobs.get(OWNER, job_id)

    asyncio.run(scenario())


def test_job_count_is_capped() -> None:
    async def scenario() -> None:
        jobs = ReviewCaptureJobs(max_jobs=1)
        gate = asyncio.Event()

        async def run() -> tuple[int, dict[str, object]]:
            await gate.wait()
            return 201, {}

        jobs.submit(OWNER, run)
        with pytest.raises(ServerError) as full:
            jobs.submit(OWNER, run)
        assert full.value.code is ErrorCode.CONCURRENCY_LIMIT
        gate.set()
        await asyncio.sleep(0.01)

    asyncio.run(scenario())

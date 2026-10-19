"""The worker heartbeats a running job's lease and records an outcome only while it holds it (BI2 review item 6)."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.service.jobs import JOB_CONSOLIDATE_BLOCKS
from lemoncrow.core.service.worker import Worker
from lemoncrow.infra.storage.bundle import build_sqlite_store_bundle


def _row(bundle: Any, job_id: str) -> dict[str, Any]:
    return next(job for job in bundle.jobs.list_jobs(limit=50) if job["id"] == job_id)


@pytest.mark.parametrize(("lease_seconds", "runtime"), [(1, 1.5), (3, 4.5)])
def test_a_long_job_is_heartbeated_so_the_reaper_never_re_runs_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lease_seconds: int, runtime: float
) -> None:
    monkeypatch.setenv("LEMONCROW_JOB_LEASE_SECONDS", str(lease_seconds))
    bundle = build_sqlite_store_bundle(tmp_path / "store")
    bundle.init()
    job_id = bundle.jobs.enqueue_job(JOB_CONSOLIDATE_BLOCKS, {})
    stolen: list[object] = []

    def slow(_payload: dict[str, Any]) -> dict[str, Any]:
        time.sleep(runtime)  # longer than the lease
        stolen.append(bundle.jobs.claim_job("another-worker"))  # would reap and re-run an unrenewed lease
        return {"status": "ok"}

    assert Worker(bundle, dispatch={JOB_CONSOLIDATE_BLOCKS: slow}).run_once() == job_id
    assert stolen == [None]
    row = _row(bundle, job_id)
    assert (row["status"], row["attempts"]) == ("succeeded", 1)


def test_a_worker_whose_lease_was_lost_records_nothing(tmp_path: Path) -> None:
    bundle = build_sqlite_store_bundle(tmp_path / "store")
    bundle.init()
    job_id = bundle.jobs.enqueue_job(JOB_CONSOLIDATE_BLOCKS, {})

    def reclaimed(_payload: dict[str, Any]) -> dict[str, Any]:
        # The worker stalled past its lease; another worker reaped and reclaimed the job.
        stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        with sqlite3.connect(bundle.jobs.db_path) as conn:
            conn.execute("UPDATE jobs SET locked_at = ? WHERE id = ?", (stale, job_id))
            conn.commit()
        assert bundle.jobs.claim_job("another-worker") is not None
        return {"status": "ok"}

    assert Worker(bundle, dispatch={JOB_CONSOLIDATE_BLOCKS: reclaimed}).run_once() == job_id
    row = _row(bundle, job_id)
    # The late result did not complete another worker's attempt.
    assert (row["status"], row["locked_by"], row["attempts"]) == ("running", "another-worker", 2)


def test_reclaimed_attempts_by_the_same_worker_cannot_accept_a_late_result(tmp_path: Path) -> None:
    bundle = build_sqlite_store_bundle(tmp_path / "store")
    bundle.init()
    job_id = bundle.jobs.enqueue_job(JOB_CONSOLIDATE_BLOCKS, {})
    started, release = threading.Event(), threading.Event()
    errors: list[BaseException] = []
    owner: list[str] = []
    thread: threading.Thread | None = None

    def reclaim() -> None:
        try:
            worker.run_once()
        except BaseException as exc:
            errors.append(exc)

    def handle(_payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal thread
        row = _row(bundle, job_id)
        if row["attempts"] == 2:
            owner.append(str(row["locked_by"]))
            started.set()
            assert release.wait(5)
            return {"attempt": 2}
        stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        with sqlite3.connect(bundle.jobs.db_path) as conn:
            conn.execute("UPDATE jobs SET locked_at = ? WHERE id = ?", (stale, job_id))
            conn.commit()
        thread = threading.Thread(target=reclaim)
        thread.start()
        assert started.wait(5)
        return {"attempt": 1}

    worker = Worker(bundle, dispatch={JOB_CONSOLIDATE_BLOCKS: handle})
    try:
        assert worker.run_once() == job_id
        row = _row(bundle, job_id)
        assert (row["status"], row["locked_by"], row["attempts"]) == ("running", owner[0], 2)
    finally:
        release.set()
        if thread is not None:
            thread.join(5)
    assert thread is not None and not thread.is_alive()
    assert errors == []
    assert _row(bundle, job_id)["status"] == "succeeded"

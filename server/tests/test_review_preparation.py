from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig
from lemoncrow_server_core.review_preparation import ReviewPreparationJobs


def test_startup_steps_precede_source_capture_and_publication() -> None:
    jobs = ReviewPreparationJobs()
    row = jobs.create()
    for stage, detail in (
        ("browser_done", "Review Reader opened; preparation continues"),
        ("range_done", "Review range ready"),
        ("capture_done", "Source preparation started"),
        ("packet_ready_done", "Change packet ready"),
        ("publish", "Publishing new review"),
    ):
        jobs.update(row["id"], stage=stage, detail=detail)
    keys = [step["key"] for step in row["steps"]]
    assert keys[:3] == ["browser", "range", "capture"]
    assert keys.index("packet_source") < keys.index("packet_ready") < keys.index("publish")
    assert [step["state"] for step in row["steps"][:3]] == ["done"] * 3


def test_parallel_steps_keep_their_own_elapsed_times() -> None:
    jobs = ReviewPreparationJobs()
    with patch("lemoncrow_server_core.review_preparation.time.time", return_value=100):
        row = jobs.create()
        jobs.update(row["id"], stage="packet_ranking", detail="Ranking")
    with patch("lemoncrow_server_core.review_preparation.time.time", return_value=102):
        jobs.update(row["id"], stage="packet_symbols", detail="Symbols")
    with patch("lemoncrow_server_core.review_preparation.time.time", return_value=105):
        jobs.update(row["id"], stage="packet_symbols_done", detail="Symbols ready")
    with patch("lemoncrow_server_core.review_preparation.time.time", return_value=108):
        jobs.update(row["id"], stage="packet_ranking_done", detail="Order ready")
    steps = {step["key"]: step for step in row["steps"]}
    assert steps["packet_symbols"]["elapsed"] == 3
    assert steps["packet_ranking"]["elapsed"] == 8
    assert steps["packet_ranking"]["state"] == "done"


def test_reused_packet_skips_unrun_work_and_failure_keeps_source_link() -> None:
    jobs = ReviewPreparationJobs()
    row = jobs.create()
    jobs.update(row["id"], stage="packet_source_done", detail="Prepared source reused")
    jobs.update(row["id"], stage="packet_ready_done", detail="Packet ready")
    steps = {step["key"]: step for step in row["steps"]}
    assert steps["packet_diff"]["state"] == "skipped"
    assert steps["packet_source"]["elapsed"] is None
    jobs.update(row["id"], review_url="/r/abc12345#p=prep-1&sr=1")
    jobs.update(row["id"], stage="state_units", detail="Loading review targets and marks")
    jobs.update(row["id"], stage="failed", detail="State load timed out")
    assert row["review_url"] == "/r/abc12345#p=prep-1&sr=1"
    assert row["state"] == "failed"
    assert next(step for step in row["steps"] if step["key"] == "state_units")["state"] == "failed"
    jobs.update(row["id"], stage="ready", detail="Late update")
    assert row["state"] == "failed"


def test_repeated_file_progress_preserves_elapsed_time_and_keeps_job_alive() -> None:
    jobs = ReviewPreparationJobs()
    with patch("lemoncrow_server_core.review_preparation.time.time", return_value=100):
        row = jobs.create()
        jobs.update(row["id"], stage="publish", detail="Creating new snapshot")
    with patch("lemoncrow_server_core.review_preparation.time.time", return_value=650):
        jobs.update(row["id"], stage="publish", detail="Building review targets · 12/2249 files")
    with patch("lemoncrow_server_core.review_preparation.time.time", return_value=750):
        assert jobs.get(row["id"])["state"] == "running"
        jobs.update(row["id"], stage="publish_done", detail="Snapshot ready")
    step = next(step for step in row["steps"] if step["key"] == "publish")
    assert step["started_at"] == 100
    assert step["elapsed"] == 650


def test_abandoned_jobs_fail_and_memory_is_bounded() -> None:
    jobs = ReviewPreparationJobs()
    with patch("lemoncrow_server_core.review_preparation.time.time", return_value=100):
        row = jobs.create()
    with patch("lemoncrow_server_core.review_preparation.time.time", return_value=701):
        assert jobs.get(row["id"])["state"] == "failed"
        for _ in range(128):
            jobs.create()
        assert jobs.get(row["id"]) is None


def test_progress_api_is_available_before_any_review_exists(tmp_path: Path) -> None:
    asyncio.run(_exercise_progress(tmp_path))


async def _exercise_progress(tmp_path: Path) -> None:
    server, _state = build_local_server(
        LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True)
    )
    url = await server.start()
    try:
        async with ClientSession() as client:
            async with client.post(url + "/api/review-preparations", json={}) as response:
                assert response.status == 200
                row = await response.json()
            path = "/api/review-preparations/" + row["id"]
            async with client.post(
                url + path, json={"stage": "packet_diff", "detail": "Reading diff", "review_url": ""}
            ) as response:
                assert response.status == 200
            async with client.get(url + path) as response:
                assert response.status == 200
                assert response.headers["Cache-Control"] == "no-store"
                row = await response.json()
                assert next(step for step in row["steps"] if step["key"] == "packet_diff")["state"] == "running"
                assert row["review_url"] == ""
            async with client.post(
                url + path, json={"stage": "", "detail": "", "review_url": "https://outside.example/r/id"}
            ) as response:
                assert response.status == 400
            async with client.get(url + "/api/review-preparations/missing") as response:
                assert response.status == 404
    finally:
        await server.stop()

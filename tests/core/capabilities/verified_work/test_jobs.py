from __future__ import annotations

import json
import os
import time
from pathlib import Path

from lemoncrow.pro.capabilities.verified_work.jobs import (
    claim_next,
    enqueue_after_bash,
    finish_job,
    queue_counts,
)


def test_job_survives_enqueue_and_is_claimed_atomically(tmp_path: Path) -> None:
    job_id = enqueue_after_bash(tmp_path, {"command": "npm run typecheck", "exit_code": 2})
    assert queue_counts(tmp_path)["pending"] == 1

    job = claim_next(tmp_path)

    assert job is not None
    assert job.id == job_id
    assert job.payload["command"] == "npm run typecheck"
    assert queue_counts(tmp_path)["pending"] == 0
    assert queue_counts(tmp_path)["running"] == 1

    finish_job(tmp_path, job, {"handled": True, "promoted": False, "reason": "shadow_candidate_verified"})
    assert queue_counts(tmp_path)["running"] == 0
    assert queue_counts(tmp_path)["done"] == 1


def test_stale_running_job_is_requeued_after_worker_crash(tmp_path: Path) -> None:
    job_id = enqueue_after_bash(tmp_path, {"command": "ruff check .", "exit_code": 1})
    first = claim_next(tmp_path)
    assert first is not None
    old = time.time() - 1000
    os.utime(first.path, (old, old))

    second = claim_next(tmp_path)

    assert second is not None
    assert second.id == job_id
    raw = json.loads(second.path.read_text())
    assert raw["attempts"] == 2

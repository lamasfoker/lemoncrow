"""Durable local job queue for non-blocking Verified Local Work shadow attempts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1
_MAX_DONE = 100
_STALE_RUNNING_S = 600.0


@dataclass(frozen=True, slots=True)
class WorkJob:
    id: str
    path: Path
    payload: dict[str, Any]


def jobs_root(root: Path) -> Path:
    return root.expanduser().resolve() / "verified_work_jobs"


def _dir(root: Path, state: str) -> Path:
    path = jobs_root(root) / state
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def enqueue_after_bash(root: Path, payload: dict[str, Any]) -> str:
    job_id = uuid.uuid4().hex
    envelope = {
        "schema_version": _SCHEMA_VERSION,
        "id": job_id,
        "created_at": time.time(),
        "attempts": 0,
        "payload": payload,
    }
    _atomic_json(_dir(root, "pending") / f"{job_id}.json", envelope)
    return job_id


def _requeue_stale(root: Path, *, stale_after_s: float = _STALE_RUNNING_S) -> int:
    running = _dir(root, "running")
    pending = _dir(root, "pending")
    now = time.time()
    moved = 0
    for path in running.glob("*.json"):
        try:
            if now - path.stat().st_mtime < stale_after_s:
                continue
            target = pending / path.name
            os.replace(path, target)
            moved += 1
        except OSError:
            continue
    return moved


def claim_next(root: Path) -> WorkJob | None:
    _requeue_stale(root)
    pending = _dir(root, "pending")
    running = _dir(root, "running")
    for path in sorted(pending.glob("*.json"), key=lambda item: (item.stat().st_mtime, item.name)):
        target = running / path.name
        try:
            os.replace(path, target)
        except OSError:
            continue
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            payload = raw.get("payload") if isinstance(raw, dict) else None
            if not isinstance(payload, dict):
                raise ValueError("job payload is not an object")
            raw["attempts"] = max(0, int(raw.get("attempts") or 0)) + 1
            raw["claimed_at"] = time.time()
            _atomic_json(target, raw)
            return WorkJob(id=str(raw.get("id") or target.stem), path=target, payload=payload)
        except (OSError, ValueError, json.JSONDecodeError):
            target.unlink(missing_ok=True)
    return None


def finish_job(root: Path, job: WorkJob, result: dict[str, Any]) -> None:
    done = _dir(root, "done")
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "id": job.id,
        "finished_at": time.time(),
        "result": result,
    }
    target = done / f"{job.id}.json"
    _atomic_json(target, payload)
    job.path.unlink(missing_ok=True)
    rows = sorted(done.glob("*.json"), key=lambda item: (item.stat().st_mtime, item.name))
    for old in rows[:-_MAX_DONE]:
        old.unlink(missing_ok=True)


def fail_job(root: Path, job: WorkJob, reason: str) -> None:
    finish_job(root, job, {"handled": False, "promoted": False, "reason": reason})


def queue_counts(root: Path) -> dict[str, int]:
    return {state: len(list(_dir(root, state).glob("*.json"))) for state in ("pending", "running", "done")}


def spawn_worker(root: Path) -> bool:
    argv = [
        sys.executable,
        "-m",
        "lemoncrow.gateway.cli",
        "--root",
        str(root.expanduser().resolve()),
        "runtime",
        "work",
        "drain",
        "--one",
    ]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(argv, **kwargs)
        return True
    except OSError:
        return False


def enqueue_and_spawn(root: Path, payload: dict[str, Any]) -> tuple[str, bool]:
    job_id = enqueue_after_bash(root, payload)
    return job_id, spawn_worker(root)


__all__ = [
    "WorkJob",
    "claim_next",
    "enqueue_after_bash",
    "enqueue_and_spawn",
    "fail_job",
    "finish_job",
    "jobs_root",
    "queue_counts",
    "spawn_worker",
]

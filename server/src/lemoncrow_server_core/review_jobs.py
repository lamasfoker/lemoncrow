"""Background Review captures, so no request has to outlive a client timeout.

A client that sends ``Prefer: respond-async`` on a capture gets ``202`` and a job
id at once; the capture runs on the worker pool and the client polls the job. A
client (or server) without this simply gets the ordinary synchronous answer.

A job is a handle on work in flight, never a Review: it is bound to the
principal, organization and repository that submitted it, a finished job keeps
its answer for a short TTL then disappears, and the number of retained jobs is
capped. Losing one (restart, expiry) costs the client a fresh capture, which the
capture path already deduplicates by binding.

lc-debt: process-local like ``ReviewDrafts``; upgrade path is a durable job table.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final

from .errors import AgentAction, ErrorCode, ServerError, error_payload

__all__ = ["ReviewCaptureJobs"]

_LOG = logging.getLogger(__name__)
_RESULT_TTL_S: Final[float] = 900.0
_MAX_JOBS: Final[int] = 16


@dataclass(slots=True)
class _Job:
    owner: tuple[str, str, str]
    task: asyncio.Task[None] | None = None
    state: str = "running"
    status: int = 0
    payload: dict[str, Any] = field(default_factory=dict)
    expires: float = 0.0


class ReviewCaptureJobs:
    __slots__ = ("_clock", "_jobs", "_max_jobs", "_ttl_s")

    def __init__(
        self,
        *,
        max_jobs: int = _MAX_JOBS,
        ttl_s: float = _RESULT_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_jobs = max_jobs
        self._ttl_s = ttl_s
        self._clock = clock
        self._jobs: dict[str, _Job] = {}

    def submit(
        self,
        owner: tuple[str, str, str],
        run: Callable[[], Awaitable[tuple[int, dict[str, Any]]]],
        *,
        progress: dict[str, Any] | None = None,
    ) -> str:
        """Start ``run`` (returns ``(http status, payload)``); return the job id."""
        self._sweep()
        if sum(job.state == "running" for job in self._jobs.values()) >= self._max_jobs:
            raise ServerError(
                ErrorCode.CONCURRENCY_LIMIT,
                "too many review captures are in progress",
                action=AgentAction.RETRY_LATER,
            )
        job_id = f"rjob_{secrets.token_hex(16)}"
        job = _Job(owner=owner, payload=progress if progress is not None else {})
        self._jobs[job_id] = job
        job.task = asyncio.get_running_loop().create_task(self._drive(job, run))
        return job_id

    async def _drive(self, job: _Job, run: Callable[[], Awaitable[tuple[int, dict[str, Any]]]]) -> None:
        try:
            job.status, job.payload = await run()
            job.state = "done" if job.status < 400 else "failed"
        except asyncio.CancelledError:
            job.state, job.status = "failed", 503
            job.payload = ServerError(
                ErrorCode.INTERNAL, "the server stopped before the capture finished", action=AgentAction.RETRY_LATER
            ).to_wire()
            raise
        except ServerError as exc:
            job.state, job.status, job.payload = "failed", exc.status, exc.to_wire()
        except Exception:
            correlation_id = uuid.uuid4().hex
            _LOG.exception("unhandled review capture job error correlation_id=%s", correlation_id)
            job.state, job.status = "failed", 500
            job.payload = error_payload(
                ErrorCode.INTERNAL,
                f"internal error (correlation_id={correlation_id})",
                details={"correlation_id": correlation_id},
            )
        finally:
            job.expires = self._clock() + self._ttl_s
            self._sweep()

    def get(self, owner: tuple[str, str, str], job_id: str) -> tuple[str, int, dict[str, Any]]:
        """Return ``(state, status, payload)``; another caller's job reads as unknown."""
        self._sweep()
        job = self._jobs.get(job_id)
        if job is None or job.owner != owner:
            raise ServerError(
                ErrorCode.REVIEW_UNKNOWN,
                "the review capture job is unknown or expired",
                details={"field": "job_id"},
                action=AgentAction.ABANDON,
            )
        return job.state, job.status, job.payload

    async def close(self, timeout_s: float) -> None:
        """Drain captures, then cancel and collect any tasks beyond the grace period."""
        tasks = [job.task for job in self._jobs.values() if job.task is not None and not job.task.done()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout_s)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self._jobs.clear()

    def _sweep(self) -> None:
        now = self._clock()
        completed = sorted(
            ((key, job.expires) for key, job in self._jobs.items() if job.state != "running"),
            key=lambda item: item[1],
        )
        # Finished results have a separate retention cap; they cannot consume
        # the concurrency budget and block the next capture for the whole TTL.
        for index, (key, expires) in enumerate(completed):
            if expires <= now or index < len(completed) - self._max_jobs:
                del self._jobs[key]

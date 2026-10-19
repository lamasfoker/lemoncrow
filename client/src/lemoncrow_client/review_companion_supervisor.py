"""Standalone, foreground Review worker lease supervisor.

Uses the existing hosted Review worker queue; never starts Runtime/MCP or a
coding-agent session. A local qualified adapter is required *before* claiming.
The generic supervisor deliberately cannot publish unverified PASS: until a
separately signed evidence path is installed it reports bounded typed failure
to the same durable queue, which owns retry attempts, expiry and cancellation.

No second spool, scheduler or process management service.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from .errors import ClientError
from .review_worker_client import ReviewWorkerClient, WorkerLease

ReviewFailure = Literal[
    "executor_unavailable",
    "execution_failed",
    "lease_cancelled",
    "source_unavailable",
    "policy_not_approved",
    "evidence_unavailable",
    "worker_transport_failure",
]
_TRANSIENT = frozenset({"executor_unavailable", "source_unavailable", "worker_transport_failure"})


@dataclass(frozen=True)
class WorkerPublication:
    """Acknowledgement from the authoritative server, never a worker PASS claim."""

    job_id: str
    state: Literal["succeeded"]
    publication_state: Literal["published"]


class LocalWorker(Protocol):
    """Locally constructed adapter; server jobs cannot choose an executor."""

    def qualify(self) -> tuple[bool, str]: ...
    def run(self, client: ReviewWorkerClient, lease: WorkerLease) -> ReviewFailure | WorkerPublication: ...


@dataclass(frozen=True)
class SupervisorConfig:
    execution_class: Literal["runtime_validation", "review_intelligence"] = "runtime_validation"
    model_route_id: str = ""
    idle_poll_s: float = 5.0
    retry_backoff_s: float = 5.0
    max_backoff_s: float = 60.0
    max_jobs: int = 0  # 0 = foreground until interrupted

    def __post_init__(self) -> None:
        if self.execution_class == "review_intelligence" and not self.model_route_id:
            raise ValueError("Review Intelligence requires a locally attested model route")
        if self.execution_class == "runtime_validation" and self.model_route_id:
            raise ValueError("verification cannot acquire an AI model route")
        if not 1 <= self.idle_poll_s <= 300 or not 1 <= self.retry_backoff_s <= 300:
            raise ValueError("Review poll intervals must be bounded")
        if not self.retry_backoff_s <= self.max_backoff_s <= 600:
            raise ValueError("Review retry backoff must be bounded")
        if type(self.max_jobs) is not int or not 0 <= self.max_jobs <= 100_000:
            raise ValueError("Review loop maximum jobs must be a bounded integer")


@dataclass
class SupervisorStats:
    polls: int = 0
    idle: int = 0
    claimed: int = 0
    failed: int = 0
    published: int = 0
    refused: int = 0
    transport_errors: int = 0


@dataclass
class ReviewCompanionSupervisor:
    client: ReviewWorkerClient
    worker: LocalWorker
    config: SupervisorConfig = field(default_factory=SupervisorConfig)
    sleep: Callable[[float], None] = time.sleep
    stopped: Callable[[], bool] = lambda: False
    stats: SupervisorStats = field(default_factory=SupervisorStats)

    def poll_once(self) -> Literal["idle", "unqualified", "failed", "published", "cancelled"]:
        """One qualified claim. No claim before local sandbox qualification."""
        if self.stopped():
            return "cancelled"
        self.stats.polls += 1
        qualified, _reason = self.worker.qualify()
        if not qualified:
            self.stats.refused += 1
            return "unqualified"
        lease = self.client.claim(
            execution_class=self.config.execution_class,
            model_route_id=self.config.model_route_id,
        )
        if lease is None:
            self.stats.idle += 1
            return "idle"
        self.stats.claimed += 1
        if self.stopped():
            # The existing queue reclaims the lease after its bounded expiry.
            return "cancelled"
        try:
            if self.client.heartbeat(lease) != "running":
                return "cancelled"
            reason = self.worker.run(self.client, lease)
            if isinstance(reason, WorkerPublication):
                if (
                    reason.job_id != lease.job_id
                    or reason.state != "succeeded"
                    or reason.publication_state != "published"
                ):
                    raise ValueError("Review worker publication receipt is invalid")
                self.stats.published += 1
                return "published"
            if reason not in {
                "executor_unavailable",
                "execution_failed",
                "lease_cancelled",
                "source_unavailable",
                "policy_not_approved",
                "evidence_unavailable",
                "worker_transport_failure",
            }:
                raise ValueError("local worker result is not a bounded failure code")
            if self.stopped():
                return "cancelled"
            if self.client.heartbeat(lease) != "running":
                return "cancelled"
        except (ClientError, OSError, TimeoutError, ValueError):
            # If a remote operation failed or the lease was revoked/currentness
            # changed, do not submit a result under a potentially stale lease.
            self.stats.transport_errors += 1
            return "cancelled"
        self.client.fail(lease, reason=reason, retryable=reason in _TRANSIENT)
        self.stats.failed += 1
        return "failed"

    def run(self) -> SupervisorStats:
        """Bounded foreground loop with idle backoff and graceful KeyboardInterrupt."""
        failures = 0
        try:
            while not self.stopped() and (self.config.max_jobs == 0 or self.stats.claimed < self.config.max_jobs):
                try:
                    state = self.poll_once()
                except (ClientError, OSError, TimeoutError):
                    self.stats.transport_errors += 1
                    failures += 1
                    if self.stopped():
                        break
                    self.sleep(
                        min(self.config.max_backoff_s, self.config.retry_backoff_s * (2 ** min(failures - 1, 5)))
                    )
                    continue
                failures = 0
                if state == "unqualified":
                    # Never repeatedly claim work on a machine missing its
                    # trust boundary. Reconfiguration is an explicit action.
                    break
                if state == "cancelled":
                    # Revoked/stale lease or shutdown: stop instead of
                    # generating many rejected claims.
                    break
                if self.stopped():
                    break
                if state == "idle":
                    self.sleep(self.config.idle_poll_s)
        except KeyboardInterrupt:
            pass
        return self.stats


__all__ = [
    "LocalWorker",
    "ReviewCompanionSupervisor",
    "ReviewFailure",
    "SupervisorConfig",
    "SupervisorStats",
    "WorkerPublication",
]

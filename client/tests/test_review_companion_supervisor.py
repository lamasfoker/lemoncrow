"""Bounded Review-only foreground claim/heartbeat/failure loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from lemoncrow_client.review_companion_supervisor import (
    ReviewCompanionSupervisor,
    SupervisorConfig,
    WorkerPublication,
)


@dataclass
class Lease:
    job_id: str = "job123"


class Worker:
    def __init__(self, *, qualified: bool = True, reason: str | WorkerPublication = "evidence_unavailable") -> None:
        self.qualified = qualified
        self.reason = reason
        self.runs = 0
        self.qualifications = 0

    def qualify(self) -> tuple[bool, str]:
        self.qualifications += 1
        return self.qualified, "ok" if self.qualified else "no sandbox"

    def run(self, client: Any, lease: Any) -> str:
        self.runs += 1
        assert lease.job_id == "job123"
        return self.reason


class Client:
    def __init__(self, *, idle: bool = False, heartbeat: str = "running") -> None:
        self.idle = idle
        self.heartbeat_state = heartbeat
        self.calls: list[tuple[Any, ...]] = []
        self.fail_state = "failed"
        self.fail_once = False

    def claim(self, *, execution_class: str, model_route_id: str = "") -> Lease | None:
        self.calls.append(("claim", execution_class, model_route_id))
        if self.fail_once:
            self.fail_once = False
            raise OSError("offline")
        return None if self.idle else Lease()

    def heartbeat(self, lease: Lease) -> str:
        self.calls.append(("heartbeat", lease.job_id))
        return self.heartbeat_state

    def fail(self, lease: Lease, *, reason: str, retryable: bool) -> dict[str, str]:
        self.calls.append(("fail", lease.job_id, reason, retryable))
        return {"job_id": lease.job_id, "state": self.fail_state}


def supervisor(client: Client, worker: Worker, **kw: Any) -> ReviewCompanionSupervisor:
    return ReviewCompanionSupervisor(
        client=client,
        worker=worker,
        config=SupervisorConfig(**kw),
        sleep=lambda _delay: None,
    )  # type: ignore[arg-type]


def test_no_claim_without_qualified_executor() -> None:
    cli = Client()
    worker = Worker(qualified=False)
    task = supervisor(cli, worker)
    assert task.poll_once() == "unqualified"
    assert task.run().refused == 2
    assert cli.calls == []
    assert worker.runs == 0


def test_idle_is_safe_with_no_task() -> None:
    cli = Client(idle=True)
    worker = Worker()
    task = supervisor(cli, worker)
    assert task.poll_once() == "idle"
    assert task.stats.idle == 1 and task.stats.claimed == 0
    assert cli.calls == [("claim", "runtime_validation", "")]


@pytest.mark.parametrize(
    "reason,retryable",
    [
        ("evidence_unavailable", False),
        ("source_unavailable", True),
        ("executor_unavailable", True),
        ("worker_transport_failure", True),
        ("policy_not_approved", False),
        ("execution_failed", False),
    ],
)
def test_result_is_typed_failure_not_unverified_pass(reason: str, retryable: bool) -> None:
    cli = Client()
    worker = Worker(reason=reason)
    task = supervisor(cli, worker)
    assert task.poll_once() == "failed"
    assert cli.calls == [
        ("claim", "runtime_validation", ""),
        ("heartbeat", "job123"),
        ("heartbeat", "job123"),
        ("fail", "job123", reason, retryable),
    ]
    assert (task.stats.claimed, task.stats.failed, worker.runs) == (1, 1, 1)


def test_published_verification_uses_server_acknowledgement_without_second_heartbeat_or_failure() -> None:
    cli = Client()
    worker = Worker(reason=WorkerPublication("job123", "succeeded", "published"))
    task = supervisor(cli, worker)
    assert task.poll_once() == "published"
    assert (task.stats.claimed, task.stats.published, task.stats.failed) == (1, 1, 0)
    assert cli.calls == [
        ("claim", "runtime_validation", ""),
        ("heartbeat", "job123"),
    ]


def test_foreign_job_publication_receipt_can_never_be_accepted_as_success() -> None:
    cli = Client()
    worker = Worker(reason=WorkerPublication("foreign", "succeeded", "published"))
    task = supervisor(cli, worker)
    assert task.poll_once() == "cancelled"
    assert task.stats.published == 0 and task.stats.transport_errors == 1
    assert not any(row[0] == "fail" for row in cli.calls)


def test_cancelled_lease_never_executes_or_reports() -> None:
    cli = Client(heartbeat="cancel_requested")
    worker = Worker()
    task = supervisor(cli, worker)
    assert task.poll_once() == "cancelled"
    assert task.stats.claimed == 1
    assert worker.runs == 0
    assert not any(row[0] == "fail" for row in cli.calls)


def test_bad_adapter_response_can_never_become_pass() -> None:
    cli = Client()
    worker = Worker(reason="PASS")
    task = supervisor(cli, worker)
    assert task.poll_once() == "cancelled"
    assert task.stats.failed == 0
    assert cli.calls[-1][0] != "fail"


def test_graceful_shutdown_after_claim_drops_authority_without_reporting() -> None:
    cli = Client()
    worker = Worker()
    states = iter([False, True])
    task = supervisor(cli, worker)
    task.stopped = lambda: next(states)
    assert task.poll_once() == "cancelled"
    assert worker.runs == 0
    assert cli.calls == [("claim", "runtime_validation", "")]


def test_retry_backoff_bounded_and_does_not_duplicate_queue() -> None:
    cli = Client()
    cli.fail_once = True
    worker = Worker()
    delays: list[float] = []
    task = supervisor(cli, worker, max_jobs=1, retry_backoff_s=3, max_backoff_s=12)
    task.sleep = delays.append
    outcome = task.run()
    assert outcome.claimed == 1 and outcome.failed == 1
    assert outcome.transport_errors == 1
    assert delays == [3]
    assert [r for r in cli.calls if r[0] == "fail"] == [("fail", "job123", "evidence_unavailable", False)]


@pytest.mark.parametrize(
    "params",
    [
        {"execution_class": "review_intelligence"},
        {"execution_class": "runtime_validation", "model_route_id": "route"},
        {"idle_poll_s": 0.01},
        {"max_backoff_s": 0.5},
        {"max_jobs": -1},
    ],
)
def test_insecure_supervisor_config_rejected(params: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        SupervisorConfig(**params)


def test_review_ai_requires_explicit_route_and_is_never_automatically_reported() -> None:
    cli = Client()
    worker = Worker()
    task = supervisor(cli, worker, execution_class="review_intelligence", model_route_id="attested")
    assert task.poll_once() == "failed"
    assert cli.calls[0] == ("claim", "review_intelligence", "attested")
    assert cli.calls[-1][2] == "evidence_unavailable"

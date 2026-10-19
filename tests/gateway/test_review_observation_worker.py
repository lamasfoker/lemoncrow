"""The customer-side production observation worker: client, pins, one lease, the dry run and the CLI.

The hosted control plane is a scripted transport and the telemetry is a fake
``http_get``: no real endpoint is contacted. The loop against the real hosted
routes is covered by ``enterprise/server/tests/test_hosted_observation_jobs.py``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli.commands.review_observation_cli import observation_worker_group
from lemoncrow.pro.capabilities.review.behavior import BehaviorDelta
from lemoncrow.pro.capabilities.review.outcomes import observability as obs
from lemoncrow.pro.capabilities.review.outcomes.deployments import shifted
from lemoncrow.pro.capabilities.review.outcomes.observation_worker import (
    HEARTBEAT_ATTEMPTS,
    HEARTBEAT_RETRY_SECONDS,
    ConnectorPin,
    HostedObservationClient,
    ObservationWorkerError,
    check_worker,
    execute_lease,
    load_connector_pins,
    parse_connector_pins,
    run_once,
)
from lemoncrow.pro.capabilities.review.side_effects import PRODUCER_ID

START = "2026-10-01T10:00:00.000000Z"
WORKER_TOKEN = "lcw_worker_token_never_printed"
PROM_TOKEN = "prom-secret-value"
SENTRY_TOKEN = "sntrys_secret_value"
ENVIRON = {"PROM_TOKEN": PROM_TOKEN, "SENTRY_TOKEN": SENTRY_TOKEN}
PINS = (
    ConnectorPin("prometheus", "https://prom.example.com", "PROM_TOKEN"),
    ConnectorPin("sentry", "https://sentry.example.com", "SENTRY_TOKEN"),
)
_VECTOR = json.dumps({"status": "success", "data": {"result": [{"metric": {}, "value": [0, "3"]}]}}).encode()


def _settings() -> obs.ObservabilitySettings:
    return obs.parse_observability_settings(
        {
            "connectors": [
                {"kind": "prometheus", "base_url": "https://prom.example.com", "credential": {"env": "PROM_TOKEN"}},
                {
                    "kind": "sentry",
                    "base_url": "https://sentry.example.com",
                    "organization": "acme",
                    "project": "api",
                    "credential": {"env": "SENTRY_TOKEN"},
                },
            ],
            "services": ["checkout"],
            "observation_window_hours": 24,
            "query_budget": 200,
            "locus": "customer_worker",
        },
        source="test",
    )


def _lease(*, attempt: int = 1, max_attempts: int = 3) -> dict[str, Any]:
    settings = _settings()
    delta = BehaviorDelta(
        kind="side_effect",
        producer=PRODUCER_ID,
        subject="route:POST /v1/charges",
        title="changed",
        summary="",
        change="changed",
    )
    predictions, _ = obs.predictions_from_deltas([delta])
    window = obs.observation_window(START, settings)
    assert window is not None
    plan = obs.plan_queries(predictions, settings, window)
    return {
        "job_id": "job-1",
        "observation": {
            "plan": {
                "queries": [item.to_wire() for item in plan.queries],
                "remaining_budget": 200,
                "routes": ["POST /v1/charges"],
                "hosts": [],
            },
            "settings": settings.to_wire(),
            "attempt": attempt,
            "max_attempts": max_attempts,
        },
    }


NOW = shifted(START, hours=30)  # every query is due


@dataclass
class Telemetry:
    prometheus: BaseException | int = 200
    sentry: BaseException | int = 200
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append((url, dict(headers)))
        outcome = self.prometheus if url.startswith("https://prom.example.com/") else self.sentry
        if not url.startswith(("https://prom.example.com/", "https://sentry.example.com/")):
            raise AssertionError("contacted an endpoint nobody pinned")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome, (_VECTOR if "prom." in url else b"[]")


@dataclass
class Hosted:
    """A scripted hosted control plane: path suffix -> (status, document)."""

    lease: dict[str, Any] | None = None
    result: tuple[int, Any] = (200, {"publication": {"state": "published", "accepted": 1}})
    fail: tuple[int, Any] = (200, {"state": "queued", "dead_lettered": False})
    heartbeat: tuple[int, Any] = (200, {"state": "running"})
    check: tuple[int, Any] = (200, {})
    calls: list[tuple[str, dict[str, Any], dict[str, str]]] = field(default_factory=list)

    def __call__(
        self, url: str, body: Mapping[str, Any], headers: Mapping[str, str], timeout: float
    ) -> tuple[int, Any]:
        self.calls.append((url, dict(body), dict(headers)))
        if url.endswith("/claim"):
            return 200, ({"job": self.lease, "envelope": "env-1"} if self.lease else {"job": None})
        for suffix in ("result", "fail", "heartbeat", "check"):
            if url.endswith("/" + suffix):
                status, document = getattr(self, suffix)
                return status, document
        raise AssertionError(url)

    def paths(self) -> list[str]:
        return [url.rsplit("/", 1)[1] for url, _body, _headers in self.calls]

    def client(self) -> HostedObservationClient:
        return HostedObservationClient("https://lemoncrow.example.com", WORKER_TOKEN, transport=self)


def _run(hosted: Hosted, telemetry: Telemetry, **kwargs: Any) -> Any:
    return run_once(
        hosted.client(), repository_ids=["repo-1"], pins=PINS, environ=ENVIRON, now=NOW, http_get=telemetry, **kwargs
    )


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "http://lemoncrow.example.com",
        "https://user:pw@lemoncrow.example.com",
        "https://lemoncrow.example.com/?next=x",
        "ftp://lemoncrow.example.com",
        "https://",
    ],
)
def test_the_client_refuses_anything_but_a_plain_https_server(url: str) -> None:
    with pytest.raises(ValueError):
        HostedObservationClient(url, WORKER_TOKEN)


def test_the_client_allows_loopback_http_and_never_shows_its_token() -> None:
    client = HostedObservationClient("http://127.0.0.1:8080/", WORKER_TOKEN)
    assert WORKER_TOKEN not in repr(client) and WORKER_TOKEN not in str(client)
    with pytest.raises(ValueError):
        HostedObservationClient("https://lemoncrow.example.com", "")


def test_the_client_maps_transport_and_server_errors_to_fixed_codes() -> None:
    def raising(exc: BaseException) -> Any:
        def transport(*_args: Any) -> tuple[int, Any]:
            raise exc

        return transport

    cases: list[tuple[Any, str, int]] = [
        (raising(TimeoutError("slow " + WORKER_TOKEN)), "timeout", 0),
        (raising(ConnectionRefusedError("refused " + WORKER_TOKEN)), "connection_failed", 0),
        (
            lambda *_a: (409, {"error": {"code": "review_state_conflict", "message": WORKER_TOKEN}}),
            "review_state_conflict",
            409,
        ),
        (lambda *_a: (502, "<html>" + WORKER_TOKEN), "http_error", 502),
        (lambda *_a: (400, {"error": {"code": "x" * 500}}), "x" * 64, 400),
    ]
    for transport, code, status in cases:
        client = HostedObservationClient("https://lemoncrow.example.com", WORKER_TOKEN, transport=transport)
        with pytest.raises(ObservationWorkerError) as raised:
            client.check([])
        assert (raised.value.code, raised.value.status) == (code, status)
        assert WORKER_TOKEN not in str(raised.value)


# --------------------------------------------------------------------------- #
# Pins
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"version": 2, "connectors": []},
        {"version": 1, "connectors": {}},
        {"version": 1, "connectors": [{"kind": "datadog", "base_url": "https://x.example.com"}]},
        {"version": 1, "connectors": [{"kind": "prometheus", "base_url": "http://x.example.com"}]},
        {"version": 1, "connectors": [{"kind": "prometheus", "base_url": "https://u:p@x.example.com"}]},
        {"version": 1, "connectors": [{"kind": "prometheus", "base_url": "https://x.example.com", "token": "t"}]},
        {"version": 1, "connectors": [{"kind": "prometheus", "base_url": "https://x.example.com"}] * 65},
    ],
)
def test_pins_refuse_anything_but_kind_https_endpoint_and_credential_name(document: Any) -> None:
    with pytest.raises(ValueError):
        parse_connector_pins(document)


def test_pins_load_at_the_bound_and_normalize_the_endpoint(tmp_path: Path) -> None:
    many = [{"kind": "prometheus", "base_url": f"https://p{i}.example.com/"} for i in range(64)]
    assert len(parse_connector_pins({"version": 1, "connectors": many})) == 64
    path = tmp_path / "pins.json"
    path.write_text(json.dumps({"version": 1, "connectors": [many[0]]}))
    assert load_connector_pins(path) == (ConnectorPin("prometheus", "https://p0.example.com", ""),)
    path.write_text("{not json")
    with pytest.raises(ValueError, match="not JSON"):
        load_connector_pins(path)
    path.write_bytes(b" " * (256 * 1024 + 1))
    with pytest.raises(ValueError, match="too large"):
        load_connector_pins(path)


# --------------------------------------------------------------------------- #
# One lease
# --------------------------------------------------------------------------- #


def test_a_lease_runs_only_pinned_connectors_and_logs_no_endpoint_or_credential(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    telemetry = Telemetry()
    execution = execute_lease(_lease(), pins=PINS[:1], environ=ENVIRON, now=NOW, http_get=telemetry)
    assert execution.aggregates and not execution.failure
    assert telemetry.calls and all(url.startswith("https://prom.example.com/") for url, _h in telemetry.calls)
    assert execution.skipped >= 1  # the unpinned Sentry queries stay unanswered
    for secret in (PROM_TOKEN, SENTRY_TOKEN, "prom.example.com", "sentry.example.com"):
        assert secret not in caplog.text
    assert all(SENTRY_TOKEN not in json.dumps(headers) for _url, headers in telemetry.calls)


def test_a_lease_whose_endpoint_or_credential_name_changed_is_not_run() -> None:
    lease = _lease()
    settings = lease["observation"]["settings"]
    for connector in settings["connectors"]:
        connector["base_url"] = connector["base_url"].replace("example.com", "attacker.example.net")
    telemetry = Telemetry()
    assert execute_lease(lease, pins=PINS, environ=ENVIRON, now=NOW, http_get=telemetry).failure == (
        "connector_not_pinned"
    )
    assert telemetry.calls == []
    renamed = _lease()
    renamed["observation"]["settings"]["connectors"][0]["credential"] = {"env": "SENTRY_TOKEN"}
    execution = execute_lease(renamed, pins=PINS, environ=ENVIRON, now=NOW, http_get=telemetry)
    assert execution.failure == ""  # Sentry is still pinned and runs
    assert telemetry.calls and all(url.startswith("https://sentry.example.com/") for url, _h in telemetry.calls)
    assert execute_lease({"job_id": "j"}, pins=PINS, environ=ENVIRON, now=NOW).failure == "unsupported"


def test_one_pass_reports_bounded_aggregates_and_the_publication_state() -> None:
    hosted = Hosted(lease=_lease())
    outcome = _run(hosted, Telemetry())
    assert outcome.state == "published" and outcome.job_id == "job-1"
    assert hosted.paths() == ["claim", "result"]
    _url, body, headers = hosted.calls[-1]
    observation = body["result"]["summary"]["observation"]
    assert set(observation) == {"aggregates", "skipped"} and observation["aggregates"]
    assert headers["Authorization"] == f"Bearer {WORKER_TOKEN}"
    sent = json.dumps([call[1] for call in hosted.calls])
    assert PROM_TOKEN not in sent and SENTRY_TOKEN not in sent and "PROM_TOKEN" not in sent
    assert _run(Hosted(), Telemetry()).state == "idle"


def test_a_transient_failure_retries_until_the_last_attempt_then_reports_what_it_has() -> None:
    hosted = Hosted(lease=_lease(attempt=1, max_attempts=3))
    assert _run(hosted, Telemetry(prometheus=TimeoutError())).state == "retry"
    assert hosted.paths() == ["claim", "fail"] and hosted.calls[-1][1]["reason"] == "timeout"
    assert hosted.calls[-1][1]["retryable"] is True
    hosted = Hosted(lease=_lease(attempt=2), fail=(200, {"state": "failed", "dead_lettered": True}))
    assert _run(hosted, Telemetry(prometheus=503)).state == "dead_lettered"
    last = Hosted(lease=_lease(attempt=3, max_attempts=3))
    assert _run(last, Telemetry(prometheus=ConnectionRefusedError())).state == "published"
    assert last.paths() == ["claim", "result"]
    permanent = Hosted(lease=_lease(attempt=1))
    assert _run(permanent, Telemetry(prometheus=401)).state == "published"  # unauthorized is not retried


def test_a_lease_this_worker_cannot_run_is_dead_lettered_not_retried() -> None:
    hosted = Hosted(lease=_lease())
    outcome = run_once(hosted.client(), repository_ids=[], pins=(), environ=ENVIRON, now=NOW, http_get=Telemetry())
    assert outcome.state == "dead_lettered"
    assert hosted.calls[-1][1] == {"envelope": "env-1", "reason": "connector_not_pinned", "retryable": False}


def test_a_cancelled_heartbeat_stops_the_run_and_releases_the_lease() -> None:
    hosted = Hosted(
        lease=_lease(), heartbeat=(200, {"state": "cancel_requested", "cancel_reason": "stale deployment: x"})
    )
    telemetry = Telemetry()
    outcome = _run(hosted, telemetry, heartbeat_seconds=0.0)
    assert outcome.state == "cancelled" and outcome.detail["reason"] == "stale deployment: x"
    assert telemetry.calls == [] and hosted.paths() == ["claim", "heartbeat", "fail"]
    assert hosted.calls[-1][1]["reason"] == "lease_cancelled"


def test_a_refused_report_loses_the_lease_and_a_failed_one_gives_it_back() -> None:
    for status, code in ((409, "review_state_conflict"), (403, "forbidden"), (401, "unauthenticated")):
        lost = Hosted(lease=_lease(), result=(status, {"error": {"code": code}}))
        assert _run(lost, Telemetry()).state == "lost", code
        assert lost.paths() == ["claim", "result"]  # nothing to give back: the lease is not this worker's
    broken = Hosted(lease=_lease(), result=(500, {"error": {"code": "internal"}}))
    assert _run(broken, Telemetry()).state == "retry"
    assert broken.paths() == ["claim", "result", "fail"]
    assert broken.calls[-1][1] == {"envelope": "env-1", "reason": "http_error", "retryable": True}
    # The give-back failing too leaves the lease to expire; the pass still ends instead of raising.
    down = Hosted(lease=_lease(), result=(502, "<html>"), fail=(502, "<html>"))
    assert _run(down, Telemetry()).state == "lost"


@dataclass
class Unreachable(Hosted):
    """The control plane drops the next ``heartbeat_failures`` heartbeats in transport."""

    heartbeat_failures: int = 99

    def __call__(
        self, url: str, body: Mapping[str, Any], headers: Mapping[str, str], timeout: float
    ) -> tuple[int, Any]:
        if url.endswith("/heartbeat") and self.heartbeat_failures > 0:
            self.calls.append((url, dict(body), dict(headers)))
            self.heartbeat_failures -= 1
            raise TimeoutError("heartbeat timed out")
        return super().__call__(url, body, headers, timeout)


def test_a_heartbeat_that_keeps_failing_in_transport_gives_the_lease_back_after_bounded_retries() -> None:
    sleeps: list[float] = []
    hosted, telemetry = Unreachable(lease=_lease()), Telemetry()
    outcome = _run(hosted, telemetry, heartbeat_seconds=0.0, sleep=sleeps.append)
    assert outcome.state == "retry", outcome
    assert hosted.paths() == ["claim", *["heartbeat"] * HEARTBEAT_ATTEMPTS, "fail"]
    assert hosted.calls[-1][1] == {"envelope": "env-1", "reason": "timeout", "retryable": True}
    assert sleeps == [HEARTBEAT_RETRY_SECONDS * tried for tried in range(1, HEARTBEAT_ATTEMPTS)]
    assert telemetry.calls == []  # no connector ran on a lease it could not renew
    # A blip inside the bound: the run renews, continues and reports.
    blip = Unreachable(lease=_lease(), heartbeat_failures=HEARTBEAT_ATTEMPTS - 1)
    assert _run(blip, Telemetry(), heartbeat_seconds=0.0, sleep=sleeps.append).state == "published"
    # An answer that the lease is gone ends the pass at once, without retries or a give-back.
    for status, code in ((409, "review_state_conflict"), (403, "forbidden")):
        gone = Hosted(lease=_lease(), heartbeat=(status, {"error": {"code": code}}))
        assert _run(gone, Telemetry(), heartbeat_seconds=0.0).state == "lost", code
        assert gone.paths() == ["claim", "heartbeat"]


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #


def _check_document(*, designated: bool = True, admitted: bool = True) -> dict[str, Any]:
    return {
        "worker_id": "svc_review",
        "designated": designated,
        "repositories": [
            {
                "repo_id": "repo-1",
                "configured": True,
                "admitted": admitted,
                "locus": {"locus": "customer_worker"},
                "connectors": _settings().to_wire()["connectors"],
            }
        ],
    }


def test_the_dry_run_probes_without_reading_telemetry_and_never_probes_an_unpinned_endpoint() -> None:
    telemetry = Telemetry()
    hosted = Hosted(check=(200, _check_document()))
    result = check_worker(hosted.client(), repository_ids=[], pins=PINS, environ=ENVIRON, http_get=telemetry)
    assert result["ready"] is True and result["telemetry_ingested"] == 0
    assert [url for url, _h in telemetry.calls] == [
        "https://prom.example.com/api/v1/query?query=vector%281%29",
        "https://sentry.example.com/api/0/projects/acme/api/",
    ]
    assert PROM_TOKEN not in json.dumps(result) and SENTRY_TOKEN not in json.dumps(result)
    telemetry = Telemetry(sentry=403)
    partial = check_worker(hosted.client(), repository_ids=[], pins=PINS[:1], environ={}, http_get=telemetry)
    probes = {item["kind"]: item["probe"] for item in partial["repositories"][0]["connectors"]}
    assert probes == {"prometheus": "credential_missing", "sentry": "not_pinned"} and telemetry.calls == []
    assert partial["ready"] is False
    denied = check_worker(
        hosted.client(), repository_ids=[], pins=PINS, environ=ENVIRON, http_get=Telemetry(sentry=403)
    )
    assert denied["ready"] is False
    assert [item["probe"] for item in denied["repositories"][0]["connectors"]] == ["ok", "unauthorized"]
    for document in (_check_document(designated=False), _check_document(admitted=False)):
        hosted = Hosted(check=(200, document))
        assert (
            check_worker(hosted.client(), repository_ids=[], pins=PINS, environ=ENVIRON, http_get=Telemetry())["ready"]
            is False
        )
    unreachable = Hosted(check=(401, {"error": {"code": "unauthenticated"}}))
    result = check_worker(unreachable.client(), repository_ids=[], pins=PINS, environ=ENVIRON)
    assert result == {"reachable": False, "error": "unauthenticated", "status": 401, "repositories": []}
    # Reached but not designated: the server withholds connector details and nothing is probed.
    telemetry = Telemetry()
    forbidden = Hosted(check=(403, {"error": {"code": "forbidden"}}))
    result = check_worker(forbidden.client(), repository_ids=[], pins=PINS, environ=ENVIRON, http_get=telemetry)
    assert result["reachable"] is True and result["designated"] is False and result["ready"] is False
    assert result["error"] == "forbidden" and result["repositories"] == [] and telemetry.calls == []


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_the_cli_reads_the_token_from_the_environment_and_never_prints_it(tmp_path: Path) -> None:
    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps({"version": 1, "connectors": []}))
    runner = CliRunner()
    missing = runner.invoke(
        observation_worker_group,
        ["check", "--server", "https://x.example.com", "--pins", str(pins)],
        env={"LEMONCROW_WORKER_TOKEN": ""},
    )
    assert missing.exit_code != 0 and "LEMONCROW_WORKER_TOKEN" in missing.output
    refused = runner.invoke(
        observation_worker_group,
        ["check", "--server", "http://x.example.com", "--pins", str(pins)],
        env={"LEMONCROW_WORKER_TOKEN": WORKER_TOKEN},
    )
    assert refused.exit_code != 0 and "https" in refused.output and WORKER_TOKEN not in refused.output
    down = runner.invoke(
        observation_worker_group,
        ["check", "--server", "http://127.0.0.1:9", "--pins", str(pins), "--json"],
        env={"LEMONCROW_WORKER_TOKEN": WORKER_TOKEN},
    )
    assert down.exit_code == 1 and json.loads(down.output)["reachable"] is False
    assert WORKER_TOKEN not in down.output
    fast = runner.invoke(
        observation_worker_group,
        ["run", "--server", "https://x.example.com", "--pins", str(pins), "--interval", "1"],
        env={"LEMONCROW_WORKER_TOKEN": WORKER_TOKEN},
    )
    assert fast.exit_code == 2  # polls are at least 5 seconds apart


class _Stop(Exception):
    """Ends the otherwise endless ``run`` loop from inside its sleep."""


def test_the_cli_loop_survives_a_failed_pass_and_backs_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.gateway.cli.commands import review_observation_cli
    from lemoncrow.pro.capabilities.review.outcomes import observation_worker

    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps({"version": 1, "connectors": []}))
    passes: list[Any] = [
        RuntimeError(f"boom at https://prom.example.com with {PROM_TOKEN}"),
        ObservationWorkerError("timeout"),
        RuntimeError("again"),
        [],
    ]
    delays: list[float] = []

    def run_until_idle(*_args: Any, **_kwargs: Any) -> Any:
        item = passes.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def sleep(seconds: float) -> None:
        delays.append(seconds)
        if not passes:
            raise _Stop

    monkeypatch.setattr(observation_worker, "run_until_idle", run_until_idle)
    monkeypatch.setattr(review_observation_cli.time, "sleep", sleep)
    result = CliRunner().invoke(
        observation_worker_group,
        ["run", "--server", "https://x.example.com", "--pins", str(pins), "--interval", "10"],
        env={"LEMONCROW_WORKER_TOKEN": WORKER_TOKEN},
    )
    assert isinstance(result.exception, _Stop), result.output
    # Doubling after each consecutive failure, back to the interval after a clean pass.
    assert delays == [10.0, 20.0, 40.0, 10.0]
    text = result.output + result.stderr
    assert "worker pass failed: RuntimeError" in text and "hosted call failed: timeout" in text
    for secret in (PROM_TOKEN, "prom.example.com", WORKER_TOKEN):
        assert secret not in text
    # A long outage polls no less often than the cap.
    passes[:] = [RuntimeError("down")] * 4
    delays.clear()
    result = CliRunner().invoke(
        observation_worker_group,
        ["run", "--server", "https://x.example.com", "--pins", str(pins), "--interval", "300"],
        env={"LEMONCROW_WORKER_TOKEN": WORKER_TOKEN},
    )
    assert isinstance(result.exception, _Stop) and review_observation_cli.MAX_FAILURE_BACKOFF_SECONDS == 900.0
    assert delays == [300.0, 600.0, 900.0, 900.0]

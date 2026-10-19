"""P15-OBS on the local server: the maintenance pass observes production for merged Changes.

The local server is the local locus: it runs the typed connectors itself with
credentials named in ``review-observability.json`` and read from its own
environment. The transport is faked at ``urllib`` (no live telemetry).
"""

from __future__ import annotations

import json
import logging
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from lemoncrow_server_core.local_reviews import LocalReviewReader, LocalReviewRepository, _ResolvedReader
from test_local_deployment_watch import MERGED, PORT, _at, _deployment, _world
from test_local_landing_watch import _change, _status

from lemoncrow.pro.capabilities.review.behavior import BehaviorDelta
from lemoncrow.pro.capabilities.review.outcomes.local_observability import LOCAL_OBSERVABILITY_FILE
from lemoncrow.pro.capabilities.review.outcomes.observation_outcomes import ObservationLedger
from lemoncrow.pro.capabilities.review.record_signing import list_records
from lemoncrow.pro.capabilities.review.side_effects import DERIVED_KIND, PRODUCER_ID, STAMP, StaticReport
from lemoncrow.pro.capabilities.review.store import DerivedResult

PROM_SECRET = "local-prom-secret-5c1e"
OBSERVABILITY = {
    "connectors": [
        {"kind": "prometheus", "base_url": "http://127.0.0.1:9090", "credential": {"env": "LOCAL_PROM_TOKEN"}}
    ],
    "services": ["checkout"],
    "observation_window_hours": 1,
    "query_budget": 40,
}


class _Response:
    def __init__(self, body: Any) -> None:
        self.status = 200
        self._body = json.dumps(body).encode()

    def read(self, limit: int = -1) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _Prometheus:
    """Answers every instant query: a removed host still called, a route serving traffic, no alerts."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def open(self, request: urllib.request.Request, timeout: float = 0) -> _Response:
        self.calls.append((request.full_url, dict(request.header_items())))
        query = parse_qs(urlsplit(request.full_url).query)["query"][0]
        if "ALERTS_FOR_STATE" in query:
            return _Response({"status": "success", "data": {"result": []}})
        value = "0.2" if "histogram_quantile" in query else "6"
        return _Response({"status": "success", "data": {"result": [{"metric": {}, "value": [0, value]}]}})


def _observability(root: Path, document: dict[str, Any] | None) -> None:
    if document is not None:
        payload = {"version": 1, "observability": document}
        (root / LOCAL_OBSERVABILITY_FILE).write_text(json.dumps(payload), encoding="utf-8")


def _inventory(repository: LocalReviewRepository, repo_id: str, review_id: str) -> None:
    store = repository.store(repo_id)
    revision = store.latest_revision(review_id)
    assert revision is not None
    deltas = tuple(
        BehaviorDelta(
            kind="side_effect",
            producer=PRODUCER_ID,
            subject=subject,
            title=subject,
            summary="",
            change=change,  # type: ignore[arg-type]
        )
        for subject, change in (("route:POST /v1/charges", "changed"), ("outbound_host:old.example.com", "removed"))
    )
    store.put_derived_results(
        [
            DerivedResult(
                review_id,
                revision.id,
                DERIVED_KIND,
                "current",
                STAMP,
                StaticReport(status="ran", deltas=deltas).to_wire(),
            )
        ]
    )


def _production(repository: LocalReviewRepository, repo_id: str, review_id: str) -> dict[str, Any]:
    reader = LocalReviewReader(repository, listen_port=PORT)
    app = reader._app(_ResolvedReader(repo_id, repository.store(repo_id)))
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as client:
        response = client.get(f"/api/reviews/{review_id}/production")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observability: dict[str, Any] | None
) -> tuple[LocalReviewRepository, Any, _Prometheus]:
    repository, github, root = _world(
        tmp_path, monkeypatch, {"connector": "github_deployments", "production_environments": ["production"]}
    )
    _observability(root, observability)
    monkeypatch.setenv("LOCAL_PROM_TOKEN", PROM_SECRET)
    prometheus = _Prometheus()
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: prometheus)
    armed = _change(repository, "repo-a", "github:acme/widgets#7", landing="armed", repo_root="local:repo-a")
    _inventory(repository, "repo-a", armed.id)
    github.deployments = [_deployment(5, MERGED, "production", _at(hours=-2))]
    github.statuses = {
        "5": [{"id": 51, "state": "success", "created_at": _at(hours=-2, minutes=2), "environment": "production"}]
    }
    return repository, armed, prometheus


def test_the_maintenance_pass_observes_production_locally_and_never_logs_the_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    repository, armed, prometheus = _setup(tmp_path, monkeypatch, OBSERVABILITY)
    store = repository.store("repo-a")

    repository.reconcile_landings()

    assert _status(repository, "repo-a", armed) == "merged"
    assert prometheus.calls and all(
        url.startswith("http://127.0.0.1:9090/api/v1/query?") for url, _ in prometheus.calls
    )
    assert all(headers.get("Authorization") == f"Bearer {PROM_SECRET}" for _url, headers in prometheus.calls)
    assert all(
        'service_name=~"^(?:checkout)$"' in parse_qs(urlsplit(url).query)["query"][0] for url, _ in prometheus.calls
    )
    [(_stamp, record)] = ObservationLedger(store).records(armed.id).values()
    assert record.spent <= record.budget == 40
    assert "contradicted" in {item.state for item in record.outcomes}
    production = _production(repository, "repo-a", armed.id)
    assert production["observability"]["locus"] == "local" and production["in_readiness"] is False
    assert any("old.example.com was removed" in item["detail"] for item in production["problems"])
    records = [row.record_id for row in list_records(store, armed.id)]

    # The next pass closes the ended window (every final answered) without querying again.
    queried = len(prometheus.calls)
    repository.reconcile_landings()
    [(_stamp, closed)] = ObservationLedger(store).records(armed.id).values()
    assert closed.closed and closed.answers == {} and len(prometheus.calls) == queried
    assert [row.record_id for row in list_records(store, armed.id)] == records
    assert PROM_SECRET not in caplog.text and PROM_SECRET not in json.dumps(production)


def test_without_a_local_observability_file_nothing_is_queried_or_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, armed, prometheus = _setup(tmp_path, monkeypatch, None)
    repository.reconcile_landings()
    repository.reconcile_landings()
    assert prometheus.calls == []
    assert ObservationLedger(repository.store("repo-a")).records(armed.id) == {}
    assert _production(repository, "repo-a", armed.id)["observability"] == {"configured": False}


def test_a_hosted_or_customer_worker_locus_in_the_local_file_configures_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, armed, prometheus = _setup(tmp_path, monkeypatch, {**OBSERVABILITY, "locus": "customer_worker"})
    repository.reconcile_landings()
    assert prometheus.calls == []
    observability = _production(repository, "repo-a", armed.id)["observability"]
    assert observability == {"configured": False}

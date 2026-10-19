"""P15-OBS: observability connectors, predicted vs observed, incident linkage, bounds (core model + store).

Pure model first (settings and credential references, predictions, the
bounded window and query plan, the aggregate sanitizer, the comparison), then
the connectors against a fake transport (scoping, credentials, logging), then
the store-bound ledger (start, plans, aggregates, contradiction attention,
incident links, the closer and raw-signal expiry, the projection).
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from lemoncrow.pro.capabilities.review.behavior import BehaviorDelta
from lemoncrow.pro.capabilities.review.freshness import freshness_class
from lemoncrow.pro.capabilities.review.outcomes import observability as obs
from lemoncrow.pro.capabilities.review.outcomes import observation_outcomes
from lemoncrow.pro.capabilities.review.outcomes.deployment_outcomes import (
    DEPLOYMENT_EVIDENCE_KIND,
    apply_observations,
    close_observation_windows,
    outcome_attention_triggers,
)
from lemoncrow.pro.capabilities.review.outcomes.deployments import (
    DeploymentObservation,
    DeploymentSettings,
    MergedCommit,
    shifted,
)
from lemoncrow.pro.capabilities.review.outcomes.incidents import (
    INCIDENT_ROW_KIND,
    MAX_CANDIDATES,
    Candidate,
    IncidentLink,
    correlate,
    review_links,
)
from lemoncrow.pro.capabilities.review.outcomes.observability_connectors import (
    MAX_RESPONSE_BYTES,
    run_observation_plan,
    urllib_get,
)
from lemoncrow.pro.capabilities.review.outcomes.observation_outcomes import (
    CLOSE_CLAIM_MINUTES,
    CLOSE_GRACE_HOURS,
    SUMMARIZED_ACTIVITY,
    ObservationLedger,
    close_observations,
    due_plans,
    observation_projection,
    record_aggregates,
    start_observations,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.side_effects import DERIVED_KIND, PRODUCER_ID, STAMP, StaticReport
from lemoncrow.pro.capabilities.review.store import DerivedResult, ReviewStore, new_revision_id, new_session_id

MERGED_SHA = "a" * 40
MERGED_AT = "2026-10-01T10:00:00.000000Z"
DEPLOYED = DeploymentSettings(connector="github_deployments", observation_window_hours=72, link_horizon_days=14)
MERGED = MergedCommit(provider="github", sha=MERGED_SHA, merged_at=MERGED_AT, ref="main")
TOKEN = "s3cr3t-prom-token-value"
SENTRY_TOKEN = "sntrys_very_secret_value"


def _at(hours: float) -> str:
    return shifted(MERGED_AT, hours=hours)


def _settings(**overrides: Any) -> obs.ObservabilitySettings:
    document: dict[str, Any] = {
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
        **overrides,
    }
    return obs.parse_observability_settings(document, source="test")


def _delta(subject: str, *, change: str = "added", kind: str = "side_effect") -> BehaviorDelta:
    return BehaviorDelta(
        kind=kind, producer=PRODUCER_ID, subject=subject, title=f"{change}: {subject}", summary="", change=change
    )


def test_observability_settings_and_credentials_contract() -> None:
    """case: test_settings_default_to_not_configured_and_validate_bounds_at_and_past_each_limit"""
    assert not obs.NOT_CONFIGURED.configured
    assert not obs.parse_observability_settings(None, source="x").configured
    settings = _settings()
    assert settings.configured and settings.locus == "customer_worker"
    assert [item.kind for item in settings.connectors] == ["prometheus", "sentry"]
    assert _settings(observation_window_hours=obs.MAX_OBSERVATION_WINDOW_HOURS).observation_window_hours == 168
    assert _settings(query_budget=obs.MAX_QUERY_BUDGET).query_budget == 200
    for field, value in (
        ("observation_window_hours", obs.MAX_OBSERVATION_WINDOW_HOURS + 1),
        ("observation_window_hours", 0),
        ("query_budget", obs.MAX_QUERY_BUDGET + 1),
        ("query_budget", 0),
        ("latency_regression_ratio", 1.0),
    ):
        with pytest.raises(ValueError, match=field):
            _settings(**{field: value})
    with pytest.raises(ValueError, match="services"):
        _settings(services=[])
    "case: test_credentials_are_named_never_written_into_policy"
    for connector in [
        {"kind": "prometheus", "base_url": "https://p.example.com", "token": "abc"},
        {"kind": "prometheus", "base_url": "https://p.example.com", "headers": {"Authorization": "Bearer x"}},
        {"kind": "sentry", "base_url": "https://s.example.com", "organization": "a", "project": "b", "api_key": "x"},
        {"kind": "prometheus", "base_url": "https://p.example.com", "credential": {"env": "X", "value": "abc"}},
        {"kind": "prometheus", "base_url": "https://p.example.com", "credential": {"env": "lower-case"}},
        {"kind": "prometheus", "base_url": "https://user:pw@p.example.com"},
        {"kind": "prometheus", "base_url": "https://p.example.com?token=abc"},
        {"kind": "prometheus", "base_url": "http://p.example.com"},
        {"kind": "datadog", "base_url": "https://d.example.com"},
    ]:
        with pytest.raises(ValueError):
            obs.parse_observability_settings({"connectors": [connector], "services": ["svc"]}, source="x")
    "case: test_a_secret_looking_connector_key_is_refused_as_a_credential_at_any_depth"
    for extra, where in [
        ({"token": "s3cr3t-v"}, "observability.connectors[0].token"),
        ({" API_KEY ": "s3cr3t-v"}, "observability.connectors[0]. API_KEY "),
        ({"tls": {"password": "s3cr3t-v"}}, "observability.connectors[0].tls.password"),
        ({"options": {"auth": {"bearer": "s3cr3t-v"}}}, "observability.connectors[0].options.auth"),
    ]:
        "The credential guard runs before the unknown-field check, so the refusal says what to do instead."
        document = {
            "connectors": [{"kind": "prometheus", "base_url": "https://p.example.com", **extra}],
            "services": ["checkout"],
        }
        with pytest.raises(ValueError, match="credentials are never written into policy") as refused:
            obs.parse_observability_settings(document, source="test")
        assert str(refused.value).startswith(f"{where}:") and "s3cr3t-v" not in str(refused.value)
        with pytest.raises(ValueError, match="unknown field 'token'") as top:
            obs.parse_observability_settings({"token": "s3cr3t-v", "services": ["checkout"]}, source="test")
        assert "s3cr3t-v" not in str(top.value)
    "case: test_connectors_run_only_on_customer_workers_or_the_local_server"
    document = {"connectors": [{"kind": "prometheus", "base_url": "https://p.example.com"}], "services": ["s"]}
    with pytest.raises(ValueError, match="customer"):
        obs.parse_observability_settings({**document, "locus": "lemoncrow_hosted"}, source="x")
    with pytest.raises(ValueError, match="customer"):
        obs.parse_observability_settings({**document, "locus": "local"}, source="x")
    with pytest.raises(ValueError):
        obs.parse_observability_settings(document, source="x", locus="lemoncrow_hosted")
    local = obs.parse_observability_settings(
        {"connectors": [{"kind": "prometheus", "base_url": "http://127.0.0.1:9090"}], "services": ["s"]},
        source="x",
        locus="local",
    )
    assert local.configured and local.locus == "local"
    wire = json.dumps(_settings().to_wire())
    assert "PROM_TOKEN" in wire and TOKEN not in wire


def test_observability_prediction_contract() -> None:
    """case: test_predictions_cover_hosts_routes_and_name_why_the_rest_is_unobservable"""
    deltas = [
        _delta("outbound_host:API.Stripe.com."),
        _delta("route:POST /v1/charges", change="changed"),
        _delta("db_table:charges"),
        _delta("outbound_host:*more"),
        _delta("GET /health", kind="api_response"),
        _delta("checkout-scenario", kind="api_response"),
        _delta("tests/test_x.py::test_y", kind="test_outcome"),
    ]
    predictions, dropped = obs.predictions_from_deltas(deltas)
    by_subject = {(item.kind, item.subject or item.target_kind + ":" + item.note[:20]) for item in predictions}
    assert ("outbound_host", "api.stripe.com") in by_subject
    assert ("route", "POST /v1/charges") in by_subject and ("route", "GET /health") in by_subject
    unobservable = [item for item in predictions if item.kind == "unobservable"]
    assert len(unobservable) == 4 and all(item.note for item in unobservable) and (dropped == 0)
    assert predictions[0].kind != "unobservable"
    "case: test_prediction_cap_holds_at_and_past_the_bound"
    at = [_delta(f"outbound_host:h{index}.example.com") for index in range(obs.MAX_PREDICTIONS)]
    assert obs.predictions_from_deltas(at)[1] == 0
    past = [*at, _delta("outbound_host:extra.example.com")]
    predictions, dropped = obs.predictions_from_deltas(past)
    assert len(predictions) == obs.MAX_PREDICTIONS and dropped == 1
    "case: test_window_is_bounded_by_settings_the_maximum_and_the_deployment_window"
    window = obs.observation_window(_at(1), _settings())
    assert window is not None and window.start == _at(1) and (window.end == _at(25))
    assert window.baseline_start == _at(-23)
    bounded = obs.observation_window(_at(1), _settings(), deployment_window_closes_at=_at(5))
    assert bounded is not None and bounded.end == _at(5)
    maximal = obs.observation_window(_at(0), replace(_settings(), observation_window_hours=10000))
    assert maximal is not None and maximal.end == _at(obs.MAX_OBSERVATION_WINDOW_HOURS)
    assert obs.observation_window("", _settings()) is None
    "case: test_query_plan_never_exceeds_the_budget_at_and_past_the_bound"
    predictions, _ = obs.predictions_from_deltas(
        [_delta(f"route:GET /r{index}", change="changed") for index in range(10)]
        + [_delta(f"outbound_host:h{index}.example.com") for index in range(10)]
    )
    window = obs.observation_window(_at(1), _settings())
    assert window is not None
    for budget in (1, 5, 24, 200):
        plan = obs.plan_queries(predictions, _settings(query_budget=budget), window)
        assert plan.cost <= budget and sum(item.cost for item in plan.queries) == plan.cost
    tight = obs.plan_queries(predictions, _settings(query_budget=5), window)
    assert {item.checkpoint for item in tight.queries} == {"final"}
    assert any(("budget (5)" in reason for _key, reason in tight.unplanned))
    roomy = obs.plan_queries(predictions, _settings(query_budget=200), window)
    assert not roomy.unplanned and {item.signal for item in roomy.queries} >= {"incident_scan", "route_latency_p95"}
    subjects = {item.subject for item in predictions}
    assert all(item.services == ("checkout",) for item in roomy.queries)
    assert all(item.subject in subjects for item in roomy.queries if item.signal != "incident_scan")


def test_observability_sanitizer_contract() -> None:
    """case: test_the_sanitizer_refuses_anything_but_bounded_aggregates"""
    for raw in [
        {"query_id": QID, "status": "ok", "value": 1, "message": "Traceback ..."},
        {
            "query_id": QID,
            "status": "ok",
            "groups": [{"fingerprint": "abc123ef", "first_seen": MERGED_AT, "title": "KeyError: 'card'"}],
        },
        {"query_id": QID, "status": "ok", "groups": [{"fingerprint": "not-hex!", "first_seen": MERGED_AT}]},
        {"query_id": QID, "status": "ok", "value": float("nan")},
        {"query_id": QID, "status": "error", "reason": "GET https://prom.example.com failed: 500"},
        {
            "query_id": QID,
            "status": "ok",
            "incidents": [{"source": "alert", "id": "a", "started_at": MERGED_AT, "labels": {}}],
        },
        {"query_id": "raw-payload", "status": "ok"},
        {"query_id": QID, "status": "ok", "groups": [{"fingerprint": "ab12cd34", "first_seen": MERGED_AT}] * 21},
    ]:
        with pytest.raises(obs.AggregateRefused):
            obs.sanitize_aggregate(raw)
    "case: test_the_sanitizer_accepts_numbers_fingerprints_and_typed_incidents"
    aggregate = obs.sanitize_aggregate(
        {
            "query_id": QID,
            "status": "ok",
            "value": 3,
            "groups": [{"fingerprint": "ab12cd34ef", "first_seen": MERGED_AT, "count": 2, "level": "error"}],
            "incidents": [{"source": "alert", "id": "prom-1", "name": "HighErrorRate", "started_at": MERGED_AT}],
        }
    )
    assert (
        aggregate.value == 3.0 and aggregate.groups[0].count == 2 and (aggregate.incidents[0].name == "HighErrorRate")
    )
    "case: test_each_prediction_ends_confirmed_contradicted_or_not_observed_with_its_reason"
    removed_host = _outcome(
        "outbound_host:old.example.com",
        {("outbound_calls", "interim"): {"status": "ok", "value": 12}},
        change="removed",
    )
    assert removed_host.state == "contradicted" and "12 calls" in removed_host.reason
    gone = _outcome(
        "outbound_host:old.example.com", {("outbound_calls", "final"): {"status": "ok", "value": 0}}, change="removed"
    )
    assert gone.state == "confirmed"
    error_group = _outcome(
        "route:POST /v1/charges",
        {
            ("route_error_groups", "interim"): {
                "status": "ok",
                "groups": [{"fingerprint": "ab12cd34ef56", "first_seen": _at(2), "count": 40}],
            }
        },
        final=False,
    )
    assert error_group.state == "contradicted" and "ab12cd34ef56" in error_group.reason
    old_group = _outcome(
        "route:POST /v1/charges",
        {
            ("route_error_groups", "final"): {
                "status": "ok",
                "groups": [{"fingerprint": "ab12cd34ef56", "first_seen": _at(-30), "count": 40}],
            },
            ("route_requests", "final"): {"status": "ok", "value": 500},
        },
    )
    assert old_group.state == "confirmed" and "500 requests" in old_group.reason
    slow = _outcome(
        "route:POST /v1/charges", {("route_latency_p95", "final"): {"status": "ok", "value": 0.5, "baseline": 0.2}}
    )
    assert slow.state == "contradicted" and "200 ms to 500 ms" in slow.reason
    quiet = _outcome("route:POST /v1/charges", {("route_requests", "final"): {"status": "ok", "value": 0}})
    assert quiet.state == "not_observed"
    failed = _outcome("route:POST /v1/charges", {("route_requests", "final"): {"status": "error", "reason": "timeout"}})
    assert failed.state == "not_observed" and "timeout" in failed.reason
    assert _outcome("route:POST /v1/charges", {}, final=False).state == "observing"
    assert _outcome("db_table:charges", {}).state == "not_observed"
    "case: test_prediction_subjects_are_escaped_inside_promql_label_matchers_and_add_no_matcher"
    "Route subjects come from the Change itself: a quote or backslash must stay inside its label value."
    hostile = 'route:POST /v1/x",service_name=~".*'
    backslash = 'route:GET /a\\b"c'
    settings = _settings(query_budget=200)
    http = FakeHttp()
    report = run_observation_plan(
        _plan(settings, hostile, backslash, budget=200),
        settings,
        environ={"PROM_TOKEN": "t", "SENTRY_TOKEN": "s"},
        now=_at(30),
        http_get=http,
    )
    assert report.aggregates
    queries = [parse_qs(urlsplit(url).query)["query"][0] for url, _ in http.calls if "/api/v1/query" in url]
    routed = [query for query in queries if "http_route=" in query]
    assert routed
    for query in routed:
        for selector in _matchers(query):
            labels = [label for label, _value in selector]
            assert labels.count("service_name") == 1 and labels.count("http_route") <= 1, query
            assert set(labels) <= {"service_name", "http_route", "http_request_method", "alertstate"}, query
    assert any('http_route="/v1/x\\",service_name=~\\".*"' in query for query in routed)
    assert any('http_route="/a\\\\b\\"c"' in query for query in routed)
    "case: test_a_response_body_past_the_size_bound_is_refused_and_one_at_the_bound_is_read"
    settings = _settings(query_budget=200)
    plan = _plan(settings, "outbound_host:api.stripe.com", budget=200)
    calls = {query["id"] for query in plan["queries"] if query["signal"] == "outbound_calls"}
    assert calls

    def sized(size: int) -> Any:

        def get(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
            document: dict[str, Any] = {
                "status": "success",
                "data": {"result": [{"metric": {}, "value": [0, "5"]}]},
                "pad": "",
            }
            document["pad"] = "x" * (size - len(json.dumps(document).encode()))
            body = json.dumps(document).encode()
            assert len(body) == size
            return (200, body)

        return get

    def answers(size: int) -> list[dict[str, Any]]:
        report = run_observation_plan(
            plan, settings, environ={"PROM_TOKEN": "t", "SENTRY_TOKEN": "s"}, now=_at(30), http_get=sized(size)
        )
        return [item for item in report.aggregates if item["query_id"] in calls]

    at_bound = answers(MAX_RESPONSE_BYTES)
    assert at_bound and {(item["status"], item.get("value")) for item in at_bound} == {("ok", 5.0)}
    past = answers(MAX_RESPONSE_BYTES + 1)
    assert past and {(item["status"], item["reason"]) for item in past} == {("error", "unparseable")}


def test_observability_runner_contract() -> None:
    """case: test_the_runner_enforces_budget_due_time_and_service_scope_on_the_executing_side"""
    settings = _settings(query_budget=200)
    plan = _plan(settings, "route:POST /v1/charges", budget=3)
    http = FakeHttp()
    report = run_observation_plan(
        plan, settings, environ={"PROM_TOKEN": "t", "SENTRY_TOKEN": "s"}, now=_at(30), http_get=http
    )
    assert report.spent <= 3 and len(http.calls) <= 3 and (report.skipped >= 1)
    early = run_observation_plan(
        _plan(settings, "route:POST /v1/charges", budget=200), settings, environ={}, now=_at(1), http_get=FakeHttp()
    )
    assert early.aggregates == ()
    foreign = _plan(settings, "route:POST /v1/charges", budget=200)
    for query in foreign["queries"]:
        query["services"] = ["billing"]
    http = FakeHttp()
    assert run_observation_plan(foreign, settings, environ={}, now=_at(30), http_get=http).aggregates == ()
    assert http.calls == []
    missing = run_observation_plan(
        _plan(settings, "route:POST /v1/charges", budget=200), settings, environ={}, now=_at(30), http_get=FakeHttp()
    )
    assert {item["reason"] for item in missing.aggregates} == {"credential_missing"}
    assert run_observation_plan(plan, obs.NOT_CONFIGURED, environ={}, now=_at(30), http_get=http).aggregates == ()
    "case: test_correlation_needs_a_concrete_reason_names_it_and_caps_candidates"
    incident = obs.IncidentSignal(
        source="alert",
        id="prom-1",
        name="HighErrorRate",
        service="checkout",
        route="POST /v1/charges",
        started_at=_at(5),
    )
    assert correlate(incident, [_candidate("time-only", 1)]) == ()
    assert correlate(incident, [_candidate("outside", 10, routes=frozenset({"POST /v1/charges"}))]) == ()
    links = correlate(
        incident,
        [_candidate(f"r{index}", 1 + index * 0.1, services=("checkout",)) for index in range(MAX_CANDIDATES + 2)]
        + [_candidate("route", 0.5, routes=frozenset({"POST /v1/charges"}))],
    )
    assert len(links) == MAX_CANDIDATES and links[0].review_id == "route"
    assert links[0].reasons == ("time_window", "touched_route")
    assert all(link.reasons[0] == "time_window" and len(link.reasons) >= 2 for link in links)
    assert all("may be related" in link.summary and "not a cause" in link.summary for link in links)


QID = "q-" + "0" * 24


def _outcome(
    subject: str, answers: Mapping[tuple[str, str], dict[str, Any]], *, change: str = "changed", final: bool = True
) -> obs.TargetOutcome:
    prediction = obs.prediction_for(_delta(subject, change=change), "beh:x")
    window = obs.observation_window(_at(0), _settings())
    assert window is not None
    parsed = {key: obs.sanitize_aggregate({"query_id": QID, **value}) for key, value in answers.items()}
    return obs.compare(prediction, parsed, _settings(), window, final=final)


class FakeHttp:

    def __init__(self, responses: Mapping[str, Any] | None = None, status: int = 200) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.responses = dict(responses or {})
        self.status = status

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append((url, dict(headers)))
        if "/api/v1/query" in url:
            query = parse_qs(urlsplit(url).query)["query"][0]
            if "ALERTS_FOR_STATE" in query:
                body: Any = self.responses.get("alerts", {"status": "success", "data": {"result": []}})
            elif "histogram_quantile" in query:
                body = {"status": "success", "data": {"result": [{"metric": {}, "value": [0, "0.2"]}]}}
            else:
                body = {"status": "success", "data": {"result": [{"metric": {}, "value": [0, "42"]}]}}
        else:
            body = self.responses.get(
                "sentry",
                [
                    {
                        "id": "4501",
                        "title": "KeyError: 'card_number' for user alice@example.com",
                        "culprit": "POST /v1/charges",
                        "firstSeen": _at(3),
                        "count": "17",
                        "level": "error",
                        "metadata": {"value": "secret payload"},
                    }
                ],
            )
        return (self.status, json.dumps(body).encode())


def _plan(settings: obs.ObservabilitySettings, *subjects: str, budget: int = 24) -> dict[str, Any]:
    predictions, _ = obs.predictions_from_deltas([_delta(item, change="changed") for item in subjects])
    window = obs.observation_window(_at(0), settings)
    assert window is not None
    plan = obs.plan_queries(predictions, settings, window)
    return {
        "queries": [item.to_wire() for item in plan.queries],
        "remaining_budget": budget,
        "routes": [item.subject for item in predictions if item.kind == "route"],
        "hosts": [item.subject for item in predictions if item.kind == "outbound_host"],
    }


def test_connectors_scope_queries_send_the_credential_only_as_a_header_and_never_log_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(query_budget=200)
    http = FakeHttp()
    environ = {"PROM_TOKEN": TOKEN, "SENTRY_TOKEN": SENTRY_TOKEN}
    caplog.set_level(logging.DEBUG)
    report = run_observation_plan(
        _plan(settings, "route:POST /v1/charges", "outbound_host:api.stripe.com", budget=200),
        settings,
        environ=environ,
        now=_at(30),
        http_get=http,
    )
    assert report.aggregates and http.calls
    for url, headers in http.calls:
        assert TOKEN not in url and SENTRY_TOKEN not in url
        if "prom.example.com" in url:
            assert headers["Authorization"] == f"Bearer {TOKEN}"
            query = parse_qs(urlsplit(url).query)["query"][0]
            assert 'service_name=~"^(?:checkout)$"' in query
        else:
            assert headers["Authorization"] == f"Bearer {SENTRY_TOKEN}"
    logged = caplog.text + json.dumps(list(report.aggregates))
    for secret in (TOKEN, SENTRY_TOKEN, "prom.example.com", "sentry.example.com", "alice@example.com", "card_number"):
        assert secret not in logged
    groups = [group for item in report.aggregates for group in item["groups"]]
    assert groups and all(set(group) == {"fingerprint", "first_seen", "count", "level", "route"} for group in groups)


def test_the_default_transport_never_follows_a_redirect_with_the_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    received: list[dict[str, str]] = []

    class Elsewhere(BaseHTTPRequestHandler):

        def do_GET(self) -> None:
            received.append(dict(self.headers))
            body = b'{"status": "success", "data": {"result": []}}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return None

    elsewhere = ThreadingHTTPServer(("127.0.0.1", 0), Elsewhere)
    target = f"http://127.0.0.1:{elsewhere.server_address[1]}/api/v1/query?query=up"

    class Redirecting(BaseHTTPRequestHandler):

        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            return None

    origin = ThreadingHTTPServer(("127.0.0.1", 0), Redirecting)
    servers = (elsewhere, origin)
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        answer = urllib_get(
            f"http://127.0.0.1:{origin.server_address[1]}/api/v1/query?query=up",
            {"Accept": "application/json", "Authorization": f"Bearer {TOKEN}"},
            5.0,
        )
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    assert [headers.get("Authorization") for headers in received] == []
    assert all("Authorization" not in headers for headers in received)
    assert answer == (302, b"")
    assert received == []


def _matchers(promql: str) -> list[list[tuple[str, str]]]:
    """Each ``{...}`` selector's ``(label, raw value)`` matchers, reading strings the way PromQL does."""
    selectors: list[list[tuple[str, str]]] = []
    index = 0
    while (start := promql.find("{", index)) != -1:
        matchers: list[tuple[str, str]] = []
        cursor, label = (start + 1, "")
        while promql[cursor] != "}":
            if promql[cursor] == '"':
                end = cursor + 1
                while promql[end] != '"':
                    end += 2 if promql[end] == "\\" else 1
                matchers.append((label.strip(" ,=~!"), promql[cursor + 1 : end]))
                cursor, label = (end + 1, "")
                continue
            label += promql[cursor]
            cursor += 1
        selectors.append(matchers)
        index = cursor + 1
    return selectors


def _review(store: ReviewStore, *, repo_root: str = "/repo/obs") -> ReviewSession:
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="pull_request",
            repo_root=repo_root,
            range_mode="commit_range",
            source_ref="github:acme/widgets#7",
        )
    )
    store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=0,
            range_mode="commit_range",
            tree_fingerprint="tree-1",
            packet_schema_version=1,
            head_sha="f" * 40,
        ),
        [],
    )
    current = store.get_session(session.id)
    assert current is not None
    return current


def _inventory(store: ReviewStore, session: ReviewSession, *deltas: BehaviorDelta) -> None:
    revision = store.latest_revision(session.id)
    assert revision is not None
    report = StaticReport(status="ran", deltas=tuple(deltas))
    store.put_derived_results(
        [
            DerivedResult(
                review_id=session.id,
                revision_id=revision.id,
                kind=DERIVED_KIND,
                scope_key="current",
                stamp=STAMP,
                payload=report.to_wire(),
            )
        ]
    )


def _deploy(store: ReviewStore, session: ReviewSession, deployment_id: str = "1", *, at: float = 1.0) -> None:
    observation = DeploymentObservation(
        provider="github",
        deployment_id=deployment_id,
        environment="production",
        commit=MERGED_SHA,
        state="success",
        created_at=_at(at),
        status_at=_at(at),
        version=f"{_at(at)}|{1:020d}",
    )
    apply_observations(
        store, session, MERGED, [observation], {observation.key: "exact"}, DEPLOYED, actor_id="p", now=_at(at + 0.1)
    )


def _started(tmp_path: Path, settings: obs.ObservabilitySettings | None = None) -> tuple[ReviewStore, ReviewSession]:
    store = ReviewStore(tmp_path / "store")
    session = _review(store)
    _inventory(
        store,
        session,
        _delta("route:POST /v1/charges", change="changed"),
        _delta("outbound_host:old.example.com", change="removed"),
        _delta("db_table:charges"),
    )
    _deploy(store, session)
    assert start_observations(store, settings or _settings(query_budget=200), now=_at(1.2)) == (session.id,)
    return (store, session)


def _answers(plan: Mapping[str, Any], values: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for query in plan["queries"]:
        body = values.get(query["signal"], {"status": "no_data"})
        out.append({"query_id": query["id"], **body})
    return out


def test_nothing_starts_or_appears_without_a_connector(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "store")
    session = _review(store)
    _inventory(store, session, _delta("route:POST /v1/charges"))
    _deploy(store, session)
    assert start_observations(store, obs.NOT_CONFIGURED, now=_at(2)) == ()
    assert ObservationLedger(store).records(session.id) == {}
    assert observation_projection(store, session, obs.NOT_CONFIGURED) == {"configured": False}
    assert outcome_attention_triggers(store, session, now=_at(3)) == ()


def test_predictions_are_frozen_once_per_production_deployment(tmp_path: Path) -> None:
    store, session = _started(tmp_path)
    assert start_observations(store, _settings(query_budget=200), now=_at(1.5)) == ()
    [(_stamp, record)] = ObservationLedger(store).records(session.id).values()
    assert {item.kind for item in record.predictions} == {"route", "outbound_host", "unobservable"}
    assert record.window.start == _at(1) and record.window.end == _at(25)
    assert record.budget == 200 and record.services == ("checkout",)
    staging = _review(store)
    _inventory(store, staging, _delta("route:GET /x"))
    observation = DeploymentObservation(
        provider="github",
        deployment_id="9",
        environment="staging",
        commit=MERGED_SHA,
        state="success",
        created_at=_at(1),
        status_at=_at(1),
        version=f"{_at(1)}|{1:020d}",
    )
    apply_observations(
        store, staging, MERGED, [observation], {observation.key: "exact"}, DEPLOYED, actor_id="p", now=_at(1.1)
    )
    assert start_observations(store, _settings(), now=_at(1.5)) == ()


def test_aggregates_must_be_planned_due_unanswered_and_in_budget(tmp_path: Path) -> None:
    store, session = _started(tmp_path, _settings(query_budget=7))
    assert due_plans(store, now=_at(2)) == []
    [plan] = due_plans(store, now=_at(13.5))
    scope = plan["scope"]
    assert all(query["checkpoint"] == "interim" for query in plan["queries"])
    unknown = record_aggregates(store, session.id, scope, [{"query_id": "q-" + "f" * 24, "status": "ok"}], now=_at(14))
    assert unknown.refused == (("q-" + "f" * 24, "unknown_query"),)
    raw = record_aggregates(
        store, session.id, scope, [{"query_id": plan["queries"][0]["id"], "status": "ok", "body": "{...}"}], now=_at(14)
    )
    assert raw.refused[0][1] == "not_aggregate" and (not raw.accepted)
    [(_stamp, record)] = ObservationLedger(store).records(session.id).values()
    final = [query for query in record.queries if query.checkpoint == "final"]
    early = record_aggregates(store, session.id, scope, [{"query_id": final[0].id, "status": "no_data"}], now=_at(14))
    assert early.refused == ((final[0].id, "not_due"),)
    assert sum(query.cost for query in record.queries) <= 7
    stamp, record = next(iter(ObservationLedger(store).records(session.id).values()))
    assert ObservationLedger(store).write(replace(record, budget=3, version=record.version + 1), expected_stamp=stamp)
    every = [{"query_id": query.id, "status": "no_data"} for query in record.queries]
    result = record_aggregates(store, session.id, scope, every, now=_at(25.5))
    [(_stamp, after)] = ObservationLedger(store).records(session.id).values()
    assert after.spent <= after.budget == 3 and result.accepted
    assert {reason for _id, reason in result.refused} == {"budget"}
    again = record_aggregates(store, session.id, scope, every[:1], now=_at(25.6))
    assert again.refused[0][1] in {"already_answered", "budget"}
    with pytest.raises(LookupError):
        record_aggregates(store, session.id, "deploy:github:404", every, now=_at(26))


def test_a_contradiction_is_recorded_once_raises_attention_and_never_touches_readiness(tmp_path: Path) -> None:
    store, session = _started(tmp_path)
    before_session = store.get_session(session.id)
    before_marks = store.list_marks(session.id)
    [plan] = due_plans(store, now=_at(13.5))
    answers = _answers(plan, {"outbound_calls": {"status": "ok", "value": 7}})
    result = record_aggregates(store, session.id, plan["scope"], answers, now=_at(13.6))
    assert len(result.contradicted) == 1
    events = store.list_activity_events(session.id, kinds=("observation.contradicted",))
    assert len(events) == 1 and "old.example.com was removed" in events[0].summary
    triggers = outcome_attention_triggers(store, session, now=_at(14))
    assert [item.kind for item in triggers] == ["behavior_contradicted"]
    assert outcome_attention_triggers(store, session, now=_at(13.6 + 73)) == ()
    assert store.get_session(session.id) == before_session and store.list_marks(session.id) == before_marks


def test_the_closer_keeps_outcomes_and_summary_and_expires_the_raw_signal(tmp_path: Path) -> None:
    store, session = _started(tmp_path)
    [plan] = due_plans(store, now=_at(25.1))
    answers = _answers(
        plan,
        {
            "route_requests": {"status": "ok", "value": 900},
            "route_latency_p95": {"status": "ok", "value": 0.21, "baseline": 0.2},
            "route_error_groups": {"status": "ok", "groups": []},
            "outbound_calls": {"status": "ok", "value": 0},
            "incident_scan": {"status": "no_data"},
        },
    )
    record_aggregates(store, session.id, plan["scope"], answers, now=_at(25.2))
    [(_stamp, open_record)] = ObservationLedger(store).records(session.id).values()
    assert open_record.answers
    assert close_observations(store, now=_at(24.9)).closed == 0
    report = close_observation_windows(store, now=_at(25.3))
    assert session.id in report.review_ids
    [(_stamp, closed)] = ObservationLedger(store).records(session.id).values()
    assert closed.closed and closed.answers == {} and (closed.raw_dropped == len(open_record.answers))
    states = {item.subject or item.kind: item.state for item in closed.outcomes}
    assert states == {"POST /v1/charges": "confirmed", "old.example.com": "confirmed", "unobservable": "not_observed"}
    summaries = [
        row
        for row in store.list_evidence(session.id)
        if row.kind == DEPLOYMENT_EVIDENCE_KIND and row.source_ref.startswith("observation-summary:")
    ]
    assert len(summaries) == 1 and freshness_class(summaries[0]) == "external_observation"
    detail = json.loads(summaries[0].detail)
    assert detail["observation"] is True and detail["counts"]["confirmed"] == 2
    assert "groups" not in summaries[0].detail and "incidents" not in summaries[0].detail
    assert close_observations(store, now=_at(30)).closed == 0
    projection = observation_projection(store, session, _settings())
    assert projection["configured"] is True and projection["in_readiness"] is False
    assert projection["status_line"] == "Production: 2 confirmed, 1 not observed"


def test_a_missing_final_answer_waits_for_the_grace_then_closes_not_observed(tmp_path: Path) -> None:
    store, session = _started(tmp_path)
    assert close_observations(store, now=_at(25 + CLOSE_GRACE_HOURS - 0.1)).closed == 0
    assert close_observations(store, now=_at(25 + CLOSE_GRACE_HOURS)).closed == 1
    [(_stamp, closed)] = ObservationLedger(store).records(session.id).values()
    assert {item.state for item in closed.outcomes} == {"not_observed"}
    assert due_plans(store, now=_at(26)) == []


def _summaries(store: ReviewStore, review_id: str) -> list[Any]:
    return [row for row in store.list_evidence(review_id) if row.source_ref.startswith("observation-summary:")]


_HEALTHY = {
    "route_requests": {"status": "ok", "value": 900},
    "route_latency_p95": {"status": "ok", "value": 0.21, "baseline": 0.2},
    "route_error_groups": {"status": "ok", "groups": []},
    "outbound_calls": {"status": "ok", "value": 0},
    "incident_scan": {"status": "no_data"},
}


def test_a_closer_that_loses_the_compare_and_set_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P15 ordering: the closer read the window before a worker answer landed; its freeze loses and it publishes nothing."""

    store, session = _started(tmp_path)
    [plan] = due_plans(store, now=_at(25.1))
    original = ObservationLedger.write
    raced: list[bool] = []

    def racing_write(self: ObservationLedger, record: Any, *, expected_stamp: str | None) -> bool:
        if record.summary_pending and not raced:
            raced.append(True)  # a worker's answers land between the closer's read and its freeze
            landed = record_aggregates(store, session.id, plan["scope"], _answers(plan, _HEALTHY), now=_at(25.2))
            assert landed.accepted
        return original(self, record, expected_stamp=expected_stamp)

    monkeypatch.setattr(ObservationLedger, "write", racing_write)
    lost = close_observations(store, now=_at(25 + CLOSE_GRACE_HOURS))  # stale read: no final answers yet
    assert raced and lost.closed == 0 and lost.conflicts == 1 and lost.review_ids == ()
    assert _summaries(store, session.id) == []
    assert store.list_activity_events(session.id, kinds=(SUMMARIZED_ACTIVITY,)) == ()
    [(_stamp, still_open)] = ObservationLedger(store).records(session.id).values()
    assert not still_open.closed and not still_open.summary_pending and still_open.answers
    monkeypatch.setattr(ObservationLedger, "write", original)
    won = close_observations(store, now=_at(25 + CLOSE_GRACE_HOURS))
    assert won.closed == 1 and won.review_ids == (session.id,)
    [summary] = _summaries(store, session.id)
    assert json.loads(summary.detail)["counts"]["confirmed"] == 2  # from the current row, not the stale read
    assert len(store.list_activity_events(session.id, kinds=(SUMMARIZED_ACTIVITY,))) == 1


def test_a_closer_that_stops_after_freezing_is_finished_once_after_its_claim_lapses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session = _started(tmp_path)
    [plan] = due_plans(store, now=_at(25.1))
    record_aggregates(
        store,
        session.id,
        plan["scope"],
        _answers(plan, {**_HEALTHY, "outbound_calls": {"status": "ok", "value": 7}}),
        now=_at(25.2),
    )
    original = observation_outcomes._activity

    def crash_before_summary(store_: Any, record: Any, *, kind: str, summary: str, detail: Mapping[str, Any]) -> None:
        if kind == SUMMARIZED_ACTIVITY:
            raise RuntimeError("closer process died")
        original(store_, record, kind=kind, summary=summary, detail=detail)

    monkeypatch.setattr(observation_outcomes, "_activity", crash_before_summary)
    at = _at(25.3)
    crashed = close_observations(store, now=at)  # the failure is isolated and counted, not raised
    assert crashed.failed == 1 and crashed.closed == 0 and crashed.review_ids == ()
    monkeypatch.setattr(observation_outcomes, "_activity", original)
    [(stamp, frozen)] = ObservationLedger(store).records(session.id).values()
    assert frozen.closed and frozen.summary_pending and frozen.answers == {} and ":closing:" in stamp
    assert len(_summaries(store, session.id)) == 1  # written before the crash
    assert close_observations(store, now=at).closed == 0  # its claim is still honoured
    assert close_observations(store, now=shifted(at, hours=(CLOSE_CLAIM_MINUTES - 1) / 60)).closed == 0
    finished = close_observations(store, now=shifted(at, hours=CLOSE_CLAIM_MINUTES / 60))
    assert finished.closed == 1 and finished.review_ids == (session.id,)
    [(stamp, closed)] = ObservationLedger(store).records(session.id).values()
    assert closed.closed and not closed.summary_pending and ":closed:" in stamp
    assert closed.closing_contradictions == ()
    assert len(_summaries(store, session.id)) == 1
    assert len(store.list_activity_events(session.id, kinds=(SUMMARIZED_ACTIVITY,))) == 1
    contradicted = store.list_activity_events(session.id, kinds=("observation.contradicted",))
    assert len(contradicted) == 1  # announced at record time; the closer does not repeat it
    assert close_observations(store, now=_at(40)).closed == 0  # idempotent
    assert len(store.list_activity_events(session.id, kinds=(SUMMARIZED_ACTIVITY,))) == 1


def test_a_poison_row_is_isolated_counted_and_retried_while_the_other_windows_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store, poisoned = _started(tmp_path)
    healthy = _review(store)
    _inventory(store, healthy, _delta("route:POST /v1/charges", change="changed"))
    _deploy(store, healthy, "2")
    assert start_observations(store, _settings(query_budget=200), now=_at(1.2)) == (healthy.id,)
    original = observation_outcomes._evaluate

    def poison(record: Any, answers: Mapping[str, Mapping[str, Any]], *, final: bool) -> Any:
        if record.review_id == poisoned.id:
            raise ValueError("a row this build cannot evaluate")
        return original(record, answers, final=final)

    monkeypatch.setattr(observation_outcomes, "_evaluate", poison)
    at = _at(25 + CLOSE_GRACE_HOURS)
    report = close_observations(store, now=at)
    assert (report.closed, report.failed, report.review_ids) == (1, 1, (healthy.id,))
    assert "observation close failed" in caplog.text
    [(_stamp, still_open)] = ObservationLedger(store).records(poisoned.id).values()
    [(_stamp, closed)] = ObservationLedger(store).records(healthy.id).values()
    assert not still_open.closed and closed.closed
    # The next pass retries the isolated row; once it can be evaluated it closes like any other.
    monkeypatch.setattr(observation_outcomes, "_evaluate", original)
    again = close_observations(store, now=at)
    assert (again.closed, again.failed, again.review_ids) == (1, 0, (poisoned.id,))
    assert close_observations(store, now=at) == observation_outcomes.ObservationCloseReport()


def test_a_poison_row_in_close_recovery_is_isolated_while_the_other_frozen_windows_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store, poisoned = _started(tmp_path)
    healthy = _review(store)
    _inventory(store, healthy, _delta("route:POST /v1/charges", change="changed"))
    _deploy(store, healthy, "2")
    assert start_observations(store, _settings(query_budget=200), now=_at(1.2)) == (healthy.id,)
    original = observation_outcomes._activity
    victims: set[str] = {poisoned.id, healthy.id}

    def crash_before_summary(store_: Any, record: Any, *, kind: str, summary: str, detail: Mapping[str, Any]) -> None:
        if kind == SUMMARIZED_ACTIVITY and record.review_id in victims:
            raise RuntimeError("closer process died")
        original(store_, record, kind=kind, summary=summary, detail=detail)

    monkeypatch.setattr(observation_outcomes, "_activity", crash_before_summary)
    at = _at(25 + CLOSE_GRACE_HOURS)
    crashed = close_observations(store, now=at)  # both windows freeze, neither finishes publishing
    assert (crashed.closed, crashed.failed) == (0, 2)
    # Recovery after the claim lapses: one frozen row still cannot publish; the other finishes.
    victims.discard(healthy.id)
    lapsed = shifted(at, hours=CLOSE_CLAIM_MINUTES / 60)
    recovered = close_observations(store, now=lapsed)
    assert (recovered.closed, recovered.failed, recovered.review_ids) == (1, 1, (healthy.id,))
    assert "observation close recovery failed" in caplog.text
    [(stamp, stuck)] = ObservationLedger(store).records(poisoned.id).values()
    assert stuck.summary_pending and ":closing:" in stamp
    # Once it can publish, a later pass (after the failed recovery's own claim lapses) finishes it.
    victims.clear()
    finished = close_observations(store, now=shifted(lapsed, hours=CLOSE_CLAIM_MINUTES / 60))
    assert (finished.closed, finished.failed, finished.review_ids) == (1, 0, (poisoned.id,))


def _candidate(review_id: str, start: float, **extra: Any) -> Candidate:
    window = obs.observation_window(_at(start), _settings())
    assert window is not None
    return Candidate(
        review_id=review_id,
        revision_id=f"rev-{review_id}",
        environment="production",
        deployment_id=review_id,
        window=window,
        **extra,
    )


def test_incident_scans_link_advisory_reasons_once_and_raise_attention(tmp_path: Path) -> None:
    store, session = _started(tmp_path)
    [plan] = due_plans(store, now=_at(13.5))
    scan = next(query for query in plan["queries"] if query["signal"] == "incident_scan")
    incident = {
        "source": "alert",
        "id": "prom-abc",
        "name": "HighLatency",
        "route": "POST /v1/charges",
        "started_at": _at(4),
    }
    result = record_aggregates(
        store,
        session.id,
        plan["scope"],
        [{"query_id": scan["id"], "status": "ok", "incidents": [incident]}],
        now=_at(13.6),
    )
    assert [link.reasons for link in result.incidents] == [("time_window", "touched_route")]
    assert len(review_links(store, session.id)) == 1
    events = store.list_activity_events(session.id, kinds=("incident.linked",))
    assert len(events) == 1 and "may be related" in events[0].summary
    assert {item.kind for item in outcome_attention_triggers(store, session, now=_at(14))} == {"incident_linked"}


def test_a_stored_link_without_time_and_a_concrete_reason_is_dropped_when_read(tmp_path: Path) -> None:
    store, session = _started(tmp_path)
    [plan] = due_plans(store, now=_at(13.5))
    scan = next(query for query in plan["queries"] if query["signal"] == "incident_scan")
    incident = {
        "source": "alert",
        "id": "prom-t",
        "name": "HighLatency",
        "route": "POST /v1/charges",
        "started_at": _at(4),
    }
    record_aggregates(
        store,
        session.id,
        plan["scope"],
        [{"query_id": scan["id"], "status": "ok", "incidents": [incident]}],
        now=_at(13.6),
    )
    [link] = review_links(store, session.id)
    wire = link.to_wire()
    assert IncidentLink.from_wire(wire) == link
    for reasons in (["time_window"], ["touched_route", "touched_service"], ["time_window", "blame"], []):
        assert IncidentLink.from_wire({**wire, "reasons": reasons}) is None, reasons
    [row] = store.derived_rows_by_scope(INCIDENT_ROW_KIND, f"incident:{link.incident.key}")
    store.put_derived_results([replace(row, payload={**row.payload, "reasons": ["time_window"]})])
    assert review_links(store, session.id) == ()
    assert "incident_linked" not in {item.kind for item in outcome_attention_triggers(store, session, now=_at(14))}
    assert observation_projection(store, session, _settings())["incidents"] == []

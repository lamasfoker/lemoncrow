"""Typed observability connectors (P15-OBS item 2): OpenTelemetry/Prometheus and Sentry.

This module runs where the telemetry lives -- a customer worker for a hosted
repository, the local server for a local one -- never on LemonCrow-hosted
infrastructure (the hosted server imports nothing from here). It executes one
deployment's due queries (planned by :mod:`observability`) and returns
bounded aggregates only:

* **Prometheus** (OpenTelemetry HTTP semantic-convention metrics by default):
  instant PromQL queries scoped by the configured service label and the
  prediction's route or outbound host -- request and call counts, p95 latency
  and per-request query counts against the baseline window, and active alerts
  (``ALERTS_FOR_STATE``) of the configured services as incident signals.
* **Sentry**: unresolved issues first seen in the window on the prediction's
  route (error groups), and new issues as incident signals. Each issue is kept
  as a digest fingerprint, first-seen time, event count and level: never its
  title, message, culprit text, stack or event payload.

Credentials are read from the named environment variable at request time and
sent only in the ``Authorization`` header. Nothing here logs a URL, a header,
a response body or an exception message: a log line carries the connector id,
the signal and a fixed status code.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import quote, urlencode

from lemoncrow.pro.capabilities.review.freshness import parse_timestamp
from lemoncrow.pro.capabilities.review.outcomes.deployments import sortable_time
from lemoncrow.pro.capabilities.review.outcomes.observability import (
    MAX_ERROR_GROUPS,
    MAX_QUERY_BUDGET,
    AggregateRefused,
    ConnectorConfig,
    ErrorGroup,
    IncidentSignal,
    ObservabilitySettings,
    SignalAggregate,
    SignalQuery,
    sanitize_aggregate,
)

_LOG = logging.getLogger(__name__)

HttpGet = Callable[[str, Mapping[str, str], float], tuple[int, bytes]]
"""``(url, headers, timeout_seconds) -> (status, body)``; raises ``OSError`` on transport failure."""

MAX_RESPONSE_BYTES: Final[int] = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS: Final[float] = 10.0
_ALERT_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_LEVELS: Final[frozenset[str]] = frozenset({"fatal", "error", "warning", "info"})


class ConnectorFailure(Exception):
    """A connector request failed; ``reason`` is a fixed code (never the transport's message)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # never forward the credential to another location


def urllib_get(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    """The default transport: one GET, no redirects, the body capped at :data:`MAX_RESPONSE_BYTES`."""

    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            return int(response.status), body
    except urllib.error.HTTPError as exc:
        return int(exc.code), b""


def _fingerprint(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def _unix(instant: str) -> float:
    parsed = parse_timestamp(instant)
    if parsed is None:
        raise ConnectorFailure("unparseable")
    return float(parsed.timestamp())


def _seconds(start: str, end: str) -> int:
    return max(1, int(_unix(end) - _unix(start)))


def _json(status: int, body: bytes) -> Any:
    if status in {401, 403}:
        raise ConnectorFailure("unauthorized")
    if status != 200:
        raise ConnectorFailure("http_error")
    if len(body) > MAX_RESPONSE_BYTES:
        raise ConnectorFailure("unparseable")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConnectorFailure("unparseable") from exc


@dataclass(frozen=True, slots=True)
class PlanContext:
    """The deployment's own prediction subjects: incident labels outside them are never kept."""

    routes: frozenset[str] = frozenset()
    hosts: frozenset[str] = frozenset()
    services: frozenset[str] = frozenset()

    def route_for(self, method: str, path: str) -> str:
        if not path:
            return ""
        exact = f"{method.upper()} {path}" if method else ""
        if exact and exact in self.routes:
            return exact
        return next((route for route in sorted(self.routes) if route.split(" ", 1)[1] == path), "")


def _call(http_get: HttpGet, url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    try:
        return http_get(url, headers, timeout)
    except TimeoutError as exc:
        raise ConnectorFailure("timeout") from exc
    except (OSError, ValueError) as exc:
        raise ConnectorFailure("connection_failed") from exc


class PrometheusConnector:
    """Instant PromQL queries against one Prometheus-compatible endpoint."""

    def __init__(self, config: ConnectorConfig, *, token: str, http_get: HttpGet, timeout: float) -> None:
        self._config = config
        self._token = token
        self._http_get = http_get
        self._timeout = timeout

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def _services(self, services: Sequence[str]) -> str:
        pattern = "|".join(re.escape(item) for item in services)
        return f'{self._config.service_label}=~"{self._escape(f"^(?:{pattern})$")}"'

    def _route_selector(self, query: SignalQuery) -> str:
        method, _sep, path = query.subject.partition(" ")
        parts = [self._services(query.services), f'{self._config.route_label}="{self._escape(path)}"']
        if self._config.method_label and method != "ANY":
            parts.append(f'{self._config.method_label}="{self._escape(method)}"')
        return ",".join(parts)

    def _host_selector(self, query: SignalQuery) -> str:
        return ",".join([self._services(query.services), f'{self._config.host_label}="{self._escape(query.subject)}"'])

    def _instant(self, promql: str, at: str) -> list[tuple[Mapping[str, Any], float]]:
        params = urlencode({"query": promql, "time": f"{_unix(at):.3f}"})
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        status, body = _call(self._http_get, f"{self._config.base_url}/api/v1/query?{params}", headers, self._timeout)
        document = _json(status, body)
        data = document.get("data") if isinstance(document, Mapping) else None
        if not isinstance(document, Mapping) or document.get("status") != "success" or not isinstance(data, Mapping):
            raise ConnectorFailure("unparseable")
        out: list[tuple[Mapping[str, Any], float]] = []
        for item in data.get("result") or ():
            if not isinstance(item, Mapping):
                continue
            raw_metric = item.get("metric")
            metric: Mapping[str, Any] = raw_metric if isinstance(raw_metric, Mapping) else {}
            value = item.get("value")
            try:
                number = float(value[1]) if isinstance(value, list | tuple) and len(value) == 2 else None
            except (TypeError, ValueError):
                number = None
            if number is None or number != number or number in {float("inf"), float("-inf")}:
                continue
            out.append((metric, number))
        return out

    def _scalar(self, promql: str, at: str) -> float | None:
        rows = self._instant(promql, at)
        return None if not rows else max(0.0, rows[0][1])

    def run(self, query: SignalQuery, context: PlanContext) -> SignalAggregate:
        config = self._config
        at = query.end if query.checkpoint == "final" else query.due_at
        window = _seconds(query.start, at)
        baseline = _seconds(query.baseline_start, query.start)
        value: float | None
        base: float | None = None
        if query.signal == "outbound_calls":
            value = self._scalar(
                f"sum(increase({config.client_duration_metric}_count{{{self._host_selector(query)}}}[{window}s]))", at
            )
        elif query.signal == "route_requests":
            value = self._scalar(
                f"sum(increase({config.server_duration_metric}_count{{{self._route_selector(query)}}}[{window}s]))", at
            )
        elif query.signal == "route_latency_p95":
            selector = self._route_selector(query)

            def p95(seconds: int) -> str:
                return (
                    f"histogram_quantile(0.95, sum by (le) "
                    f"(rate({config.server_duration_metric}_bucket{{{selector}}}[{seconds}s])))"
                )

            value = self._scalar(p95(window), at)
            base = self._scalar(p95(baseline), query.start)
        elif query.signal == "route_query_count":
            if not config.query_count_metric:
                raise ConnectorFailure("unsupported")
            selector = self._route_selector(query)

            def per_request(seconds: int) -> str:
                metric = config.query_count_metric
                return (
                    f"sum(increase({metric}_sum{{{selector}}}[{seconds}s])) / "
                    f"sum(increase({metric}_count{{{selector}}}[{seconds}s]))"
                )

            value = self._scalar(per_request(window), at)
            base = self._scalar(per_request(baseline), query.start)
        elif query.signal == "incident_scan":
            return self._alerts(query, context, at=at, window=window)
        else:
            raise ConnectorFailure("unsupported")
        if value is None:
            return SignalAggregate(query_id=query.id, status="no_data", baseline=base)
        return SignalAggregate(query_id=query.id, status="ok", value=value, baseline=base)

    def _alerts(self, query: SignalQuery, context: PlanContext, *, at: str, window: int) -> SignalAggregate:
        config = self._config
        labels = ", ".join(
            dict.fromkeys(
                item
                for item in (
                    "alertname",
                    config.service_label,
                    config.route_label,
                    config.host_label,
                    config.method_label,
                )
                if item
            )
        )
        rows = self._instant(
            f"max by ({labels}) (max_over_time(ALERTS_FOR_STATE{{{self._services(query.services)}}}[{window}s]))", at
        )
        incidents: list[IncidentSignal] = []
        for metric, value in rows:
            name = str(metric.get("alertname") or "")
            service = str(metric.get(config.service_label) or "")
            path = str(metric.get(config.route_label) or "")
            method = str(metric.get(config.method_label) or "") if config.method_label else ""
            host = str(metric.get(config.host_label) or "").lower()
            try:
                started = sortable_time(datetime.fromtimestamp(value, tz=UTC))
            except (OverflowError, OSError, ValueError):
                continue
            fingerprint = _fingerprint("prometheus", config.id, name, service, path, method, host)
            incidents.append(
                IncidentSignal(
                    source="alert",
                    id=f"prom-{fingerprint}",
                    name=name if _ALERT_NAME.match(name) else "alert",
                    service=service if service in context.services else "",
                    route=context.route_for(method, path),
                    host=host if host in context.hosts else "",
                    started_at=started,
                    fingerprint=fingerprint,
                )
            )
        incidents.sort(key=lambda item: (item.started_at, item.id))
        return SignalAggregate(
            query_id=query.id, status="ok" if incidents else "no_data", incidents=tuple(incidents[:MAX_ERROR_GROUPS])
        )


class SentryConnector:
    """Unresolved issues of one Sentry project, as fingerprints and counts."""

    def __init__(self, config: ConnectorConfig, *, token: str, http_get: HttpGet, timeout: float) -> None:
        self._config = config
        self._token = token
        self._http_get = http_get
        self._timeout = timeout

    @staticmethod
    def _time(instant: str) -> str:
        parsed = parse_timestamp(instant)
        if parsed is None:
            raise ConnectorFailure("unparseable")
        return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")

    def _issues(self, search: str) -> list[Mapping[str, Any]]:
        if not self._token:
            raise ConnectorFailure("credential_missing")
        config = self._config
        params = urlencode(
            {"query": search, "environment": config.environment, "limit": str(MAX_ERROR_GROUPS), "statsPeriod": ""}
        )
        url = (
            f"{config.base_url}/api/0/projects/{quote(config.organization, safe='')}/"
            f"{quote(config.project, safe='')}/issues/?{params}"
        )
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._token}"}
        document = _json(*_call(self._http_get, url, headers, self._timeout))
        if not isinstance(document, list):
            raise ConnectorFailure("unparseable")
        return [item for item in document if isinstance(item, Mapping)][:MAX_ERROR_GROUPS]

    def _group(self, issue: Mapping[str, Any], route: str) -> ErrorGroup | None:
        identifier = str(issue.get("id") or "")
        first_seen = sortable_time(issue.get("firstSeen"))
        if not identifier or not first_seen:
            return None
        try:
            count = max(0, min(int(str(issue.get("count") or "0")), 10**12))
        except ValueError:
            count = 0
        level = str(issue.get("level") or "")
        return ErrorGroup(
            fingerprint=_fingerprint("sentry", self._config.organization, self._config.project, identifier),
            first_seen=first_seen,
            count=count,
            level=level if level in _LEVELS else "",
            route=route,
        )

    def run(self, query: SignalQuery, context: PlanContext) -> SignalAggregate:
        at = query.end if query.checkpoint == "final" else query.due_at
        window = f"firstSeen:>={self._time(query.start)} firstSeen:<={self._time(at)}"
        if query.signal == "route_error_groups":
            _method, _sep, path = query.subject.partition(" ")
            names = json.dumps(sorted({path, query.subject}))
            issues = self._issues(f"is:unresolved {window} transaction:{names}")
            groups = [group for issue in issues if (group := self._group(issue, query.subject)) is not None]
            return SignalAggregate(query_id=query.id, status="ok", groups=tuple(groups))
        if query.signal == "incident_scan":
            incidents: list[IncidentSignal] = []
            for issue in self._issues(f"is:unresolved {window}"):
                group = self._group(issue, "")
                if group is None:
                    continue
                culprit = " ".join(str(issue.get("culprit") or "").split())
                method, _sep, path = culprit.partition(" ") if " " in culprit else ("", "", culprit)
                incidents.append(
                    IncidentSignal(
                        source="error_group",
                        id=f"sentry-{group.fingerprint}",
                        name="sentry_issue",
                        route=context.route_for(method, path),
                        started_at=group.first_seen,
                        fingerprint=group.fingerprint,
                    )
                )
            return SignalAggregate(
                query_id=query.id, status="ok" if incidents else "no_data", incidents=tuple(incidents)
            )
        raise ConnectorFailure("unsupported")


def _connector(
    config: ConnectorConfig, *, environ: Mapping[str, str], http_get: HttpGet, timeout: float
) -> PrometheusConnector | SentryConnector:
    token = config.credential.resolve(environ)
    if config.kind == "prometheus":
        return PrometheusConnector(config, token=token, http_get=http_get, timeout=timeout)
    return SentryConnector(config, token=token, http_get=http_get, timeout=timeout)


@dataclass(frozen=True, slots=True)
class RunReport:
    aggregates: tuple[dict[str, Any], ...]
    """Sanitized aggregate wires, ready to post (or record locally)."""
    spent: int
    skipped: int
    """Due queries left for a later pass (budget) or refused (scope)."""


def run_observation_plan(
    plan: Mapping[str, Any],
    settings: ObservabilitySettings,
    *,
    environ: Mapping[str, str],
    now: str,
    http_get: HttpGet = urllib_get,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> RunReport:
    """Execute one deployment's due queries on this (customer or local) machine.

    The executing side enforces the bounds itself as well: a query must be due,
    scoped to services this machine's settings name, answered by a configured
    connector, and fit in the plan's remaining budget (never more than
    :data:`MAX_QUERY_BUDGET`). Every result is re-sanitized before it leaves.
    """

    if not settings.configured:
        return RunReport((), 0, 0)
    remaining = max(0, min(int(plan.get("remaining_budget") or 0), settings.query_budget, MAX_QUERY_BUDGET))
    context = PlanContext(
        routes=frozenset(str(item) for item in plan.get("routes") or ()),
        hosts=frozenset(str(item) for item in plan.get("hosts") or ()),
        services=frozenset(settings.services),
    )
    queries = [
        query
        for raw in plan.get("queries") or ()
        if isinstance(raw, Mapping) and (query := SignalQuery.from_wire(raw)) is not None
    ]
    instant = sortable_time(now)
    connectors: dict[str, PrometheusConnector | SentryConnector] = {}
    out: list[dict[str, Any]] = []
    spent = skipped = 0
    for query in sorted(queries, key=lambda item: (item.checkpoint != "final", item.due_at, item.id)):
        config = settings.connector_by_id(query.connector)
        if (
            config is None
            or not query.due_at
            or query.due_at > instant
            or not query.services
            or not set(query.services) <= set(settings.services)
        ):
            skipped += 1
            continue
        if query.cost > remaining:
            skipped += 1
            continue
        remaining -= query.cost
        spent += query.cost
        connector = connectors.get(config.id)
        if connector is None:
            connector = connectors[config.id] = _connector(config, environ=environ, http_get=http_get, timeout=timeout)
        try:
            if config.credential.env and not config.credential.resolve(environ):
                raise ConnectorFailure("credential_missing")
            aggregate = connector.run(query, context)
            status: str = aggregate.status
        except ConnectorFailure as exc:
            aggregate = SignalAggregate(query_id=query.id, status="error", reason=exc.reason)
            status = f"error:{exc.reason}"
        _LOG.info("observability query connector=%s signal=%s status=%s", config.id, query.signal, status)
        wire = {**aggregate.to_wire(), "observed_at": instant}
        try:
            out.append(sanitize_aggregate(wire).to_wire())
        except AggregateRefused:
            _LOG.warning("observability result refused connector=%s signal=%s", config.id, query.signal)
            out.append(SignalAggregate(query_id=query.id, status="error", reason="unparseable").to_wire())
    return RunReport(tuple(out), spent, skipped)


def run_observation_plans(
    plans: Iterable[Mapping[str, Any]],
    settings: ObservabilitySettings,
    *,
    environ: Mapping[str, str],
    now: str,
    http_get: HttpGet = urllib_get,
) -> list[tuple[Mapping[str, Any], RunReport]]:
    return [(plan, run_observation_plan(plan, settings, environ=environ, now=now, http_get=http_get)) for plan in plans]


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_RESPONSE_BYTES",
    "ConnectorFailure",
    "HttpGet",
    "PlanContext",
    "PrometheusConnector",
    "RunReport",
    "SentryConnector",
    "run_observation_plan",
    "run_observation_plans",
    "urllib_get",
]

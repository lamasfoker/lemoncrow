"""Predicted vs observed in production (Phase 15, P15-OBS items 2, 3, 9) -- the pure model.

A merged Review predicted behavior changes (behavior targets) and side effects
(the side-effect inventory). Once a deployment provably containing the change
runs in production (P15-DEPLOY), typed observability connectors answer a
bounded set of questions about exactly those predictions, inside one bounded
observation window per deployment:

* an **outbound host** the change added or removed: calls to it
  (``outbound_calls``);
* a **route** the change added, changed or removed: requests
  (``route_requests``), new error groups (``route_error_groups``), p95 latency
  and per-request query count against the same-length window before the
  deployment (``route_latency_p95``, ``route_query_count``).

Every other behavior target is ``not_observed`` with the reason. No query is
ever broader than the Review's own inventory, behavior targets and the
repository's configured services: there are no general dashboards.

Outcome rules (``compare``): a contradiction on any checkpoint is final
(removed host still called, removed route still served, a new error group on
the route, a p95 or query-count regression past the configured ratio); a
confirmation is only decided at the final checkpoint, from data the connector
actually returned; everything else ends ``not_observed``.

Bounds (item 9): the window is at most :data:`MAX_OBSERVATION_WINDOW_HOURS`
and never outlives the deployment's own window; each deployment spends at most
its query budget (one unit per connector request); connectors return
aggregates and error fingerprints only (:func:`sanitize_aggregate` refuses
anything else); credentials are named, never stored -- an environment variable
on the executing locus (:class:`CredentialRef`).

Pure data and functions: no store, no network.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, cast
from urllib.parse import urlsplit

from lemoncrow.pro.capabilities.review.outcomes.deployments import shifted, sortable_time

ObservabilityConnectorKind = Literal["prometheus", "sentry"]
OBSERVABILITY_CONNECTOR_KINDS: Final[tuple[str, ...]] = ("prometheus", "sentry")
ObservationLocus = Literal["customer_worker", "local"]
OBSERVATION_LOCI: Final[tuple[str, ...]] = ("customer_worker", "local")

DEFAULT_OBSERVATION_WINDOW_HOURS: Final[int] = 24
MAX_OBSERVATION_WINDOW_HOURS: Final[int] = 7 * 24
DEFAULT_QUERY_BUDGET: Final[int] = 24
MAX_QUERY_BUDGET: Final[int] = 200
MAX_CONNECTORS: Final[int] = 4
MAX_SERVICES: Final[int] = 10
# lc-debt: at most 60 predictions per deployment (observable ones first, then by severity); the
# rest are counted in ``predictions_dropped`` and never queried. Upgrade path: page predictions
# across checkpoints when a Review states more observable side effects than this.
MAX_PREDICTIONS: Final[int] = 60
MAX_ERROR_GROUPS: Final[int] = 20
DEFAULT_LATENCY_RATIO: Final[float] = 1.25
DEFAULT_QUERY_COUNT_RATIO: Final[float] = 1.5
LATENCY_FLOOR_SECONDS: Final[float] = 0.02
QUERY_COUNT_FLOOR: Final[float] = 1.0
_MAX_VALUE: Final[float] = 1e12
_RATIO_BOUNDS: Final[tuple[float, float]] = (1.05, 10.0)

CredentialEnv = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_CONNECTOR_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_SERVICE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,99}$")
_LABEL = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,99}$")
_METRIC = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]{0,199}$")
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}\.)*[a-z0-9-]{1,63}$")
_ROUTE = re.compile(r"^(ANY|GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (/[\x21-\x7e]{0,299})$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{8,64}$")
_QUERY_ID = re.compile(r"^q-[0-9a-f]{24}$")
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})
# Credentials are never written into policy: any of these keys anywhere in a
# connector document refuses it (name an environment variable instead).
_SECRET_KEYS: Final[frozenset[str]] = frozenset(
    {
        "token",
        "api_token",
        "auth_token",
        "password",
        "secret",
        "api_key",
        "apikey",
        "key",
        "authorization",
        "auth",
        "headers",
        "bearer",
        "username",
        "user",
        "basic_auth",
        "dsn",
    }
)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Settings: connectors, credential references, services, window and budget
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CredentialRef:
    """Where a connector's credential lives on the executing locus: an environment variable *name*.

    The value is read only on the customer worker / local machine that runs the
    connector, at request time, and is never stored, returned or logged.
    """

    env: str = ""

    def resolve(self, environ: Mapping[str, str]) -> str:
        return str(environ.get(self.env) or "") if self.env else ""

    def to_wire(self) -> dict[str, str]:
        return {"env": self.env}


@dataclass(frozen=True, slots=True)
class ConnectorConfig:
    """One typed observability connector (no credential values, ever)."""

    kind: str
    id: str
    base_url: str
    credential: CredentialRef = CredentialRef()
    # Prometheus / OpenTelemetry metric conventions (OTel semantic-convention names by default).
    service_label: str = "service_name"
    route_label: str = "http_route"
    host_label: str = "server_address"
    method_label: str = "http_request_method"
    """Empty: route queries match the route label alone (any method)."""
    server_duration_metric: str = "http_server_request_duration_seconds"
    client_duration_metric: str = "http_client_request_duration_seconds"
    query_count_metric: str = ""
    """A per-request database query count histogram; empty: query-count regressions are not observed."""
    # Sentry
    organization: str = ""
    project: str = ""
    environment: str = "production"

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "id": self.id,
            "base_url": self.base_url,
            "credential": self.credential.to_wire(),
        }
        if self.kind == "prometheus":
            out.update(
                {
                    "service_label": self.service_label,
                    "route_label": self.route_label,
                    "host_label": self.host_label,
                    "method_label": self.method_label,
                    "server_duration_metric": self.server_duration_metric,
                    "client_duration_metric": self.client_duration_metric,
                    "query_count_metric": self.query_count_metric,
                }
            )
        else:
            out.update({"organization": self.organization, "project": self.project, "environment": self.environment})
        return out


_PROMETHEUS_KEYS: Final[frozenset[str]] = frozenset(
    {
        "kind",
        "id",
        "base_url",
        "credential",
        "service_label",
        "route_label",
        "host_label",
        "method_label",
        "server_duration_metric",
        "client_duration_metric",
        "query_count_metric",
    }
)
_SENTRY_KEYS: Final[frozenset[str]] = frozenset(
    {"kind", "id", "base_url", "credential", "organization", "project", "environment"}
)
_SETTING_KEYS: Final[frozenset[str]] = frozenset(
    {
        "connectors",
        "services",
        "observation_window_hours",
        "query_budget",
        "latency_regression_ratio",
        "query_count_regression_ratio",
        "locus",
    }
)


@dataclass(frozen=True, slots=True)
class ObservabilitySettings:
    """Observability connectors of one repository; no connector means not configured (the default)."""

    connectors: tuple[ConnectorConfig, ...] = ()
    services: tuple[str, ...] = ()
    observation_window_hours: int = DEFAULT_OBSERVATION_WINDOW_HOURS
    query_budget: int = DEFAULT_QUERY_BUDGET
    latency_regression_ratio: float = DEFAULT_LATENCY_RATIO
    query_count_regression_ratio: float = DEFAULT_QUERY_COUNT_RATIO
    locus: str = "customer_worker"
    source: str = ""
    error: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.connectors) and not self.error

    def connector(self, kind: str) -> ConnectorConfig | None:
        return next((item for item in self.connectors if item.kind == kind), None)

    def connector_by_id(self, connector_id: str) -> ConnectorConfig | None:
        return next((item for item in self.connectors if item.id == connector_id), None)

    def to_wire(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "connectors": [item.to_wire() for item in self.connectors],
            "services": list(self.services),
            "observation_window_hours": self.observation_window_hours,
            "query_budget": self.query_budget,
            "latency_regression_ratio": self.latency_regression_ratio,
            "query_count_regression_ratio": self.query_count_regression_ratio,
            "locus": self.locus,
            "source": self.source,
            "error": self.error,
        }

    @property
    def digest(self) -> str:
        return _digest({key: value for key, value in self.to_wire().items() if key not in {"source", "error"}})[:32]

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any], *, locus: str) -> ObservabilitySettings:
        """Re-validate a wire document (a worker's plan); invalid -> not configured with the error."""

        document = {key: raw[key] for key in _SETTING_KEYS if key in raw and key != "locus"}
        try:
            return parse_observability_settings(document, source=str(raw.get("source") or ""), locus=locus)
        except ValueError as exc:
            return cls(source=str(raw.get("source") or ""), error=str(exc), locus=locus)


NOT_CONFIGURED: Final[ObservabilitySettings] = ObservabilitySettings()


def _secret_key(raw: Mapping[str, Any], *, where: str) -> None:
    for key, value in raw.items():
        if str(key).strip().lower() in _SECRET_KEYS:
            raise ValueError(
                f"{where}.{key}: credentials are never written into policy; name an environment variable on "
                "the executing worker instead (credential.env)"
            )
        if isinstance(value, Mapping) and key != "credential":
            _secret_key(value, where=f"{where}.{key}")


def _text(raw: Mapping[str, Any], key: str, *, where: str, pattern: re.Pattern[str], default: str = "") -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{where}.{key} must be a string")
    text = value.strip()
    if text and not pattern.match(text):
        raise ValueError(f"{where}.{key} is not a valid value")
    return text


def _base_url(raw: object, *, where: str, locus: str) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 500:
        raise ValueError(f"{where}.base_url must be an absolute URL")
    text = raw.strip().rstrip("/")
    parts = urlsplit(text)
    if parts.username or parts.password or "@" in parts.netloc:
        raise ValueError(f"{where}.base_url must not carry credentials")
    if parts.query or parts.fragment or not parts.hostname:
        raise ValueError(f"{where}.base_url must be a plain https URL without query or fragment")
    if parts.scheme == "https":
        return text
    if parts.scheme == "http" and locus == "local" and parts.hostname in _LOOPBACK_HOSTS:
        return text
    raise ValueError(f"{where}.base_url must use https (plain http only for a loopback endpoint on the local server)")


def _connector(raw: object, *, where: str, locus: str) -> ConnectorConfig:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where} must be an object")
    _secret_key(raw, where=where)
    kind = raw.get("kind")
    if kind not in OBSERVABILITY_CONNECTOR_KINDS:
        raise ValueError(f"{where}.kind must be one of {', '.join(OBSERVABILITY_CONNECTOR_KINDS)}")
    allowed = _PROMETHEUS_KEYS if kind == "prometheus" else _SENTRY_KEYS
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"{where} has unknown field {unknown[0]!r}")
    connector_id = _text(raw, "id", where=where, pattern=_CONNECTOR_ID, default=str(kind))
    credential_raw = raw.get("credential", {})
    if not isinstance(credential_raw, Mapping) or set(credential_raw) - {"env"}:
        raise ValueError(f'{where}.credential must be {{"env": "VARIABLE_NAME"}}')
    env = credential_raw.get("env", "")
    if not isinstance(env, str) or (env and not CredentialEnv.match(env)):
        raise ValueError(f"{where}.credential.env must be an environment variable name (A-Z, 0-9, _)")
    kind_text = str(kind)
    identifier = connector_id or kind_text
    base_url = _base_url(raw.get("base_url"), where=where, locus=locus)
    credential = CredentialRef(env=env)
    if kind == "prometheus":
        return ConnectorConfig(
            kind=kind_text,
            id=identifier,
            base_url=base_url,
            credential=credential,
            service_label=_text(raw, "service_label", where=where, pattern=_LABEL, default="service_name")
            or "service_name",
            route_label=_text(raw, "route_label", where=where, pattern=_LABEL, default="http_route") or "http_route",
            host_label=_text(raw, "host_label", where=where, pattern=_LABEL, default="server_address")
            or "server_address",
            method_label=_text(raw, "method_label", where=where, pattern=_LABEL, default="http_request_method"),
            server_duration_metric=_text(
                raw,
                "server_duration_metric",
                where=where,
                pattern=_METRIC,
                default="http_server_request_duration_seconds",
            )
            or "http_server_request_duration_seconds",
            client_duration_metric=_text(
                raw,
                "client_duration_metric",
                where=where,
                pattern=_METRIC,
                default="http_client_request_duration_seconds",
            )
            or "http_client_request_duration_seconds",
            query_count_metric=_text(raw, "query_count_metric", where=where, pattern=_METRIC),
        )
    organization = _text(raw, "organization", where=where, pattern=_SLUG)
    project = _text(raw, "project", where=where, pattern=_SLUG)
    if not organization or not project:
        raise ValueError(f"{where}: a sentry connector names its organization and project")
    return ConnectorConfig(
        kind=kind_text,
        id=identifier,
        base_url=base_url,
        credential=credential,
        organization=organization,
        project=project,
        environment=_text(raw, "environment", where=where, pattern=_SLUG, default="production") or "production",
    )


def _bounded_int(raw: object, *, field: str, low: int, high: int) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or not low <= raw <= high:
        raise ValueError(f"observability.{field} must be an integer between {low} and {high}")
    return raw


def _ratio(raw: object, *, field: str) -> float:
    low, high = _RATIO_BOUNDS
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not low <= float(raw) <= high:
        raise ValueError(f"observability.{field} must be a number between {low} and {high}")
    return float(raw)


def parse_observability_settings(raw: object, *, source: str, locus: str = "customer_worker") -> ObservabilitySettings:
    """Validate one ``observability`` settings document; ``None`` is "not configured".

    Raises ``ValueError`` on anything unknown, out of bounds or carrying a
    credential value (fail closed: an invalid document configures nothing).
    ``locus`` is where connectors run: ``customer_worker`` for a hosted
    repository (the only hosted locus), ``local`` on the local server.
    """

    if locus not in OBSERVATION_LOCI:
        raise ValueError(f"unknown observation locus {locus!r}")
    if raw is None:
        return ObservabilitySettings(source=source, locus=locus)
    if not isinstance(raw, Mapping):
        raise ValueError("observability must be an object")
    unknown = sorted(set(raw) - _SETTING_KEYS)
    if unknown:
        raise ValueError(f"observability has unknown field {unknown[0]!r}")
    requested = raw.get("locus", locus)
    if requested != locus:
        raise ValueError(
            f"observability.locus must be {locus!r}: connectors run on the customer's workers (or the local "
            "server), so telemetry stays customer-local; LemonCrow-hosted telemetry queries are not available"
        )
    connectors_raw = raw.get("connectors", [])
    if not isinstance(connectors_raw, list) or len(connectors_raw) > MAX_CONNECTORS:
        raise ValueError(f"observability.connectors must be an array of at most {MAX_CONNECTORS} connectors")
    connectors = tuple(
        _connector(item, where=f"observability.connectors[{index}]", locus=locus)
        for index, item in enumerate(connectors_raw)
    )
    ids = [item.id for item in connectors]
    if len(set(ids)) != len(ids):
        raise ValueError("observability.connectors ids must be unique")
    kinds = [item.kind for item in connectors]
    if len(set(kinds)) != len(kinds):
        raise ValueError("observability.connectors may name each connector kind once")
    services_raw = raw.get("services", [])
    if not isinstance(services_raw, list) or len(services_raw) > MAX_SERVICES:
        raise ValueError(f"observability.services must be an array of at most {MAX_SERVICES} service names")
    services: list[str] = []
    for value in services_raw:
        if not isinstance(value, str) or not _SERVICE.match(value.strip()):
            raise ValueError("observability.services entries must be service names")
        if value.strip() not in services:
            services.append(value.strip())
    if connectors and not services:
        raise ValueError(
            "observability.services must name the repository's production services: queries are scoped to them"
        )
    window = DEFAULT_OBSERVATION_WINDOW_HOURS
    if "observation_window_hours" in raw:
        window = _bounded_int(
            raw["observation_window_hours"], field="observation_window_hours", low=1, high=MAX_OBSERVATION_WINDOW_HOURS
        )
    budget = DEFAULT_QUERY_BUDGET
    if "query_budget" in raw:
        budget = _bounded_int(raw["query_budget"], field="query_budget", low=1, high=MAX_QUERY_BUDGET)
    latency = (
        _ratio(raw["latency_regression_ratio"], field="latency_regression_ratio")
        if "latency_regression_ratio" in raw
        else DEFAULT_LATENCY_RATIO
    )
    query_count = (
        _ratio(raw["query_count_regression_ratio"], field="query_count_regression_ratio")
        if "query_count_regression_ratio" in raw
        else DEFAULT_QUERY_COUNT_RATIO
    )
    return ObservabilitySettings(
        connectors=connectors,
        services=tuple(services),
        observation_window_hours=window,
        query_budget=budget,
        latency_regression_ratio=latency,
        query_count_regression_ratio=query_count,
        locus=locus,
        source=source,
    )


# --------------------------------------------------------------------------- #
# Predictions: what the merged Review said would change
# --------------------------------------------------------------------------- #

PredictionKind = Literal["outbound_host", "route", "unobservable"]


@dataclass(frozen=True, slots=True)
class Prediction:
    """One behavior target or inventoried side effect of the merged Snapshot, as production can test it."""

    key: str
    """The behavior key (``beh:...``): the same identity the Review judged."""
    kind: PredictionKind
    subject: str
    """Outbound host name or ``METHOD /route``; empty when unobservable."""
    change: str
    title: str
    target_kind: str
    category: str = ""
    severity: str = "medium"
    critical: bool = False
    note: str = ""
    """Why production cannot observe it (unobservable only)."""

    def to_wire(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "subject": self.subject,
            "change": self.change,
            "title": self.title,
            "target_kind": self.target_kind,
            "category": self.category,
            "severity": self.severity,
            "critical": self.critical,
            "note": self.note,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> Prediction:
        kind = str(raw.get("kind") or "unobservable")
        return cls(
            key=str(raw.get("key") or ""),
            kind=cast(PredictionKind, kind if kind in {"outbound_host", "route"} else "unobservable"),
            subject=str(raw.get("subject") or ""),
            change=str(raw.get("change") or "changed"),
            title=str(raw.get("title") or "")[:200],
            target_kind=str(raw.get("target_kind") or ""),
            category=str(raw.get("category") or ""),
            severity=str(raw.get("severity") or "medium"),
            critical=raw.get("critical") is True,
            note=str(raw.get("note") or ""),
        )


_UNOBSERVABLE_LABELS: Final[Mapping[str, str]] = {
    "test_outcome": "a test outcome",
    "visual": "a visual change",
    "migration": "a migration effect",
    "verification_weakening": "a verification change",
    "spec_delta": "a specification change",
    "compatibility": "a compatibility pairing",
    "agent_instruction": "an agent instruction change",
    "artifact": "an artifact change",
    "infra": "an infrastructure change",
}


def _route(text: str) -> str:
    value = " ".join(text.split())
    return value if _ROUTE.match(value) else ""


def prediction_for(delta: Any, key: str) -> Prediction:
    """One behavior delta (contract v1) as a production prediction."""

    kind = str(getattr(delta, "kind", "") or "")
    subject = str(getattr(delta, "subject", "") or "")
    common: dict[str, Any] = {
        "key": key,
        "change": str(getattr(delta, "change", "changed") or "changed"),
        "title": str(getattr(delta, "title", "") or "")[:200],
        "target_kind": kind,
        "severity": str(getattr(delta, "severity", "medium") or "medium"),
        "critical": bool(getattr(delta, "critical", False)),
    }
    if kind == "side_effect":
        category, _sep, name = subject.partition(":")
        common["category"] = category
        if name == "*more":
            return Prediction(kind="unobservable", subject="", note="grouped past the side-effect target cap", **common)
        if category == "outbound_host":
            host = name.strip().lower().rstrip(".")
            if _HOST.match(host) and len(host) <= 253:
                return Prediction(kind="outbound_host", subject=host, **common)
        if category == "route" and (route := _route(name)):
            return Prediction(kind="route", subject=route, **common)
        label = category.replace("_", " ") or "side effect"
        return Prediction(
            kind="unobservable",
            subject="",
            note=f"no configured connector observes a {label} in production",
            **common,
        )
    if kind == "api_response" and (route := _route(subject)):
        return Prediction(kind="route", subject=route, **common)
    if kind == "api_response":
        note = "the scenario names no production route"
    else:
        note = f"no configured connector observes {_UNOBSERVABLE_LABELS.get(kind, 'this behavior')} in production"
    return Prediction(kind="unobservable", subject="", note=note, **common)


_SEVERITY_RANK: Final[Mapping[str, int]] = {"high": 0, "medium": 1, "low": 2}


def predictions_from_deltas(deltas: Iterable[Any]) -> tuple[tuple[Prediction, ...], int]:
    """``(predictions, dropped)``: observable ones first, then by severity; at most :data:`MAX_PREDICTIONS`."""

    from lemoncrow.pro.capabilities.review.behavior import behavior_key

    seen: dict[str, Prediction] = {}
    for delta in deltas:
        key = behavior_key(delta)
        if key not in seen:
            seen[key] = prediction_for(delta, key)
    ordered = sorted(
        seen.values(),
        key=lambda item: (
            1 if item.kind == "unobservable" else 0,
            0 if item.critical else 1,
            _SEVERITY_RANK.get(item.severity, 3),
            item.key,
        ),
    )
    return tuple(ordered[:MAX_PREDICTIONS]), max(0, len(ordered) - MAX_PREDICTIONS)


# --------------------------------------------------------------------------- #
# Window and query plan (bounded by the per-deployment budget)
# --------------------------------------------------------------------------- #

SignalKind = Literal[
    "outbound_calls",
    "route_requests",
    "route_latency_p95",
    "route_query_count",
    "route_error_groups",
    "incident_scan",
]
SIGNAL_KINDS: Final[tuple[str, ...]] = (
    "outbound_calls",
    "route_requests",
    "route_latency_p95",
    "route_query_count",
    "route_error_groups",
    "incident_scan",
)
Checkpoint = Literal["interim", "final"]
# Requests per query: a comparison reads the window and the same-length baseline before it.
SIGNAL_COST: Final[Mapping[str, int]] = {
    "outbound_calls": 1,
    "route_requests": 1,
    "route_latency_p95": 2,
    "route_query_count": 2,
    "route_error_groups": 1,
    "incident_scan": 1,
}
SIGNAL_CONNECTOR: Final[Mapping[str, str]] = {
    "outbound_calls": "prometheus",
    "route_requests": "prometheus",
    "route_latency_p95": "prometheus",
    "route_query_count": "prometheus",
    "route_error_groups": "sentry",
}
# Signals that can contradict a prediction early are also asked at the window's midpoint.
_CONTRADICTING: Final[frozenset[str]] = frozenset({"route_error_groups", "outbound_calls", "route_requests"})


@dataclass(frozen=True, slots=True)
class ObservationWindow:
    """One deployment's bounded observation window and the same-length baseline before it."""

    start: str
    end: str
    baseline_start: str

    @property
    def midpoint(self) -> str:
        start, end = _instant(self.start), _instant(self.end)
        if start is None or end is None:
            return self.end
        return sortable_time(start + (end - start) / 2)

    def contains(self, instant: str) -> bool:
        return bool(instant) and self.start <= instant <= self.end

    def to_wire(self) -> dict[str, str]:
        return {"start": self.start, "end": self.end, "baseline_start": self.baseline_start}

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> ObservationWindow | None:
        start, end = sortable_time(raw.get("start")), sortable_time(raw.get("end"))
        baseline = sortable_time(raw.get("baseline_start"))
        if not start or not end or not baseline or end <= start:
            return None
        return cls(start, end, baseline)


def _instant(text: str) -> Any:
    from lemoncrow.pro.capabilities.review.freshness import parse_timestamp

    return parse_timestamp(text)


def observation_window(
    deployed_at: str, settings: ObservabilitySettings, *, deployment_window_closes_at: str = ""
) -> ObservationWindow | None:
    """The window opens when production runs the change and closes after the configured hours.

    It never exceeds :data:`MAX_OBSERVATION_WINDOW_HOURS` nor outlives the
    deployment's own observation window (P15-DEPLOY), whichever is first.
    """

    start = sortable_time(deployed_at)
    if not start:
        return None
    hours = min(int(settings.observation_window_hours), MAX_OBSERVATION_WINDOW_HOURS)
    end = shifted(start, hours=hours)
    bound = sortable_time(deployment_window_closes_at)
    if bound and bound < end:
        end = bound
    if not end or end <= start:
        return None
    start_at, end_at = _instant(start), _instant(end)
    baseline = sortable_time(start_at - (end_at - start_at))
    return ObservationWindow(start=start, end=end, baseline_start=baseline)


@dataclass(frozen=True, slots=True)
class SignalQuery:
    """One bounded question one connector answers about one prediction subject."""

    id: str
    signal: SignalKind
    connector: str
    subject: str
    services: tuple[str, ...]
    start: str
    end: str
    baseline_start: str
    checkpoint: Checkpoint
    due_at: str
    cost: int
    prediction_keys: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "signal": self.signal,
            "connector": self.connector,
            "subject": self.subject,
            "services": list(self.services),
            "start": self.start,
            "end": self.end,
            "baseline_start": self.baseline_start,
            "checkpoint": self.checkpoint,
            "due_at": self.due_at,
            "cost": self.cost,
            "prediction_keys": list(self.prediction_keys),
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> SignalQuery | None:
        signal = str(raw.get("signal") or "")
        checkpoint = str(raw.get("checkpoint") or "")
        query_id = str(raw.get("id") or "")
        if signal not in SIGNAL_KINDS or checkpoint not in {"interim", "final"} or not _QUERY_ID.match(query_id):
            return None
        return cls(
            id=query_id,
            signal=cast(SignalKind, signal),
            connector=str(raw.get("connector") or ""),
            subject=str(raw.get("subject") or ""),
            services=tuple(str(item) for item in raw.get("services") or ()),
            start=str(raw.get("start") or ""),
            end=str(raw.get("end") or ""),
            baseline_start=str(raw.get("baseline_start") or ""),
            checkpoint=cast(Checkpoint, checkpoint),
            due_at=str(raw.get("due_at") or ""),
            cost=max(1, int(raw.get("cost") or 1)),
            prediction_keys=tuple(str(item) for item in raw.get("prediction_keys") or ()),
        )


def _signals_for(prediction: Prediction, settings: ObservabilitySettings) -> list[str]:
    prometheus = settings.connector("prometheus") is not None
    sentry = settings.connector("sentry") is not None
    out: list[str] = []
    if prediction.kind == "outbound_host" and prometheus:
        out.append("outbound_calls")
    elif prediction.kind == "route":
        if prediction.change == "removed":
            if prometheus:
                out.append("route_requests")
        else:
            if sentry:
                out.append("route_error_groups")
            if prometheus:
                out.extend(("route_latency_p95", "route_requests"))
                connector = settings.connector("prometheus")
                if connector is not None and connector.query_count_metric:
                    out.append("route_query_count")
    return out


def query_id(signal: str, subject: str, checkpoint: str, window: ObservationWindow) -> str:
    return "q-" + _digest([signal, subject, checkpoint, window.start, window.end])[:24]


@dataclass(frozen=True, slots=True)
class QueryPlan:
    queries: tuple[SignalQuery, ...]
    unplanned: tuple[tuple[str, str], ...]
    """``(prediction key, reason)`` for every observable prediction no query covers."""
    cost: int


def plan_queries(
    predictions: Sequence[Prediction], settings: ObservabilitySettings, window: ObservationWindow
) -> QueryPlan:
    """The bounded query plan of one deployment: never more than the query budget.

    Final-checkpoint questions are planned first, breadth-first across
    predictions (each prediction's most telling signal before any second
    signal); midpoint questions for early contradiction and one incident scan
    per connector and checkpoint follow with what the budget leaves.
    """

    budget = min(int(settings.query_budget), MAX_QUERY_BUDGET)
    observable = [item for item in predictions if item.kind != "unobservable"]
    wanted: dict[str, list[str]] = {item.key: _signals_for(item, settings) for item in observable}
    by_query: dict[str, SignalQuery] = {}
    keys_by_query: dict[str, list[str]] = {}
    spent = 0
    covered: set[str] = set()

    def add(signal: str, subject: str, checkpoint: Checkpoint, key: str) -> bool:
        nonlocal spent
        identifier = query_id(signal, subject, checkpoint, window)
        if identifier in by_query:
            keys_by_query[identifier].append(key)
            return True
        cost = SIGNAL_COST[signal]
        if spent + cost > budget:
            return False
        connector = settings.connector(SIGNAL_CONNECTOR.get(signal, "")) if signal != "incident_scan" else None
        by_query[identifier] = SignalQuery(
            id=identifier,
            signal=cast(SignalKind, signal),
            connector="" if connector is None else connector.id,
            subject=subject,
            services=settings.services,
            start=window.start,
            end=window.end,
            baseline_start=window.baseline_start,
            checkpoint=checkpoint,
            due_at=window.end if checkpoint == "final" else window.midpoint,
            cost=cost,
        )
        keys_by_query[identifier] = [key]
        spent += cost
        return True

    depth = max((len(items) for items in wanted.values()), default=0)
    for level in range(depth):
        for prediction in observable:
            signals = wanted[prediction.key]
            if level < len(signals):
                if add(signals[level], prediction.subject, "final", prediction.key):
                    covered.add(prediction.key)
    for prediction in observable:
        for signal in wanted[prediction.key]:
            if signal in _CONTRADICTING:
                add(signal, prediction.subject, "interim", prediction.key)
    for connector in settings.connectors:
        for checkpoint in ("final", "interim"):
            identifier = query_id("incident_scan", connector.id, checkpoint, window)
            if identifier in by_query or spent + 1 > budget:
                continue
            by_query[identifier] = SignalQuery(
                id=identifier,
                signal="incident_scan",
                connector=connector.id,
                subject="",
                services=settings.services,
                start=window.start,
                end=window.end,
                baseline_start=window.baseline_start,
                checkpoint=checkpoint,
                due_at=window.end if checkpoint == "final" else window.midpoint,
                cost=1,
            )
            keys_by_query[identifier] = []
            spent += 1
    unplanned: list[tuple[str, str]] = []
    for prediction in observable:
        if prediction.key in covered:
            continue
        if not wanted[prediction.key]:
            what = "outbound calls" if prediction.kind == "outbound_host" else "route traffic"
            unplanned.append((prediction.key, f"no configured connector observes {what}"))
        else:
            unplanned.append((prediction.key, f"the query budget ({budget}) was spent before this target"))
    queries = tuple(
        SignalQuery(**{**_fields(query), "prediction_keys": tuple(dict.fromkeys(keys_by_query[query.id]))})
        for query in by_query.values()
    )
    return QueryPlan(queries=queries, unplanned=tuple(unplanned), cost=spent)


def _fields(query: SignalQuery) -> dict[str, Any]:
    return {name: getattr(query, name) for name in SignalQuery.__slots__}


# --------------------------------------------------------------------------- #
# Aggregates: what a connector may return (aggregates and fingerprints only)
# --------------------------------------------------------------------------- #

AggregateStatus = Literal["ok", "no_data", "error"]
AGGREGATE_STATUSES: Final[tuple[str, ...]] = ("ok", "no_data", "error")
ERROR_REASONS: Final[frozenset[str]] = frozenset(
    {
        "",
        "timeout",
        "http_error",
        "unauthorized",
        "unparseable",
        "credential_missing",
        "connection_failed",
        "unsupported",
        "budget",
    }
)
_LEVELS: Final[frozenset[str]] = frozenset({"fatal", "error", "warning", "info", ""})
_AGGREGATE_KEYS: Final[frozenset[str]] = frozenset(
    {"query_id", "status", "value", "baseline", "groups", "incidents", "reason", "observed_at"}
)
_GROUP_KEYS: Final[frozenset[str]] = frozenset({"fingerprint", "first_seen", "count", "level", "route"})
_INCIDENT_KEYS: Final[frozenset[str]] = frozenset(
    {"source", "id", "name", "service", "route", "host", "started_at", "fingerprint"}
)
_INCIDENT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_INCIDENT_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


class AggregateRefused(ValueError):
    """A connector result that is not a bounded aggregate (a raw payload, free text, unknown field)."""


@dataclass(frozen=True, slots=True)
class ErrorGroup:
    """One error group as a fingerprint and counts: never its title, message, stack or event payload."""

    fingerprint: str
    first_seen: str
    count: int
    level: str = ""
    route: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "first_seen": self.first_seen,
            "count": self.count,
            "level": self.level,
            "route": self.route,
        }


@dataclass(frozen=True, slots=True)
class IncidentSignal:
    """An alert, error group or incident, as a typed summary (no free text, no payload)."""

    source: str
    """``alert`` or ``error_group``."""
    id: str
    name: str = ""
    service: str = ""
    route: str = ""
    host: str = ""
    started_at: str = ""
    fingerprint: str = ""

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"

    def to_wire(self) -> dict[str, str]:
        return {
            "source": self.source,
            "id": self.id,
            "name": self.name,
            "service": self.service,
            "route": self.route,
            "host": self.host,
            "started_at": self.started_at,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> IncidentSignal | None:
        try:
            return _incident(raw)
        except AggregateRefused:
            return None


@dataclass(frozen=True, slots=True)
class SignalAggregate:
    """What one query returned: numbers, error fingerprints and typed incident summaries only."""

    query_id: str
    status: AggregateStatus
    value: float | None = None
    baseline: float | None = None
    groups: tuple[ErrorGroup, ...] = ()
    incidents: tuple[IncidentSignal, ...] = ()
    reason: str = ""
    observed_at: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "status": self.status,
            "value": self.value,
            "baseline": self.baseline,
            "groups": [item.to_wire() for item in self.groups],
            "incidents": [item.to_wire() for item in self.incidents],
            "reason": self.reason,
            "observed_at": self.observed_at,
        }


def _number(raw: object, *, field: str) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise AggregateRefused(f"{field} must be a number")
    value = float(raw)
    if not math.isfinite(value) or value < 0 or value > _MAX_VALUE:
        raise AggregateRefused(f"{field} is out of range")
    return value


def _group(raw: object) -> ErrorGroup:
    if not isinstance(raw, Mapping):
        raise AggregateRefused("an error group must be an object")
    unknown = sorted(set(raw) - _GROUP_KEYS)
    if unknown:
        raise AggregateRefused(f"error group field {unknown[0]!r} is not an aggregate")
    fingerprint = str(raw.get("fingerprint") or "")
    if not _FINGERPRINT.match(fingerprint):
        raise AggregateRefused("an error group fingerprint must be a hex digest")
    first_seen = sortable_time(raw.get("first_seen"))
    count = raw.get("count", 0)
    if not first_seen or isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 10**12:
        raise AggregateRefused("an error group needs first_seen and a count")
    level = str(raw.get("level") or "")
    if level not in _LEVELS:
        raise AggregateRefused("unknown error group level")
    route_raw = str(raw.get("route") or "")
    route = _route(route_raw) if route_raw else ""
    if route_raw and not route:
        raise AggregateRefused("an error group route must be METHOD /path")
    return ErrorGroup(fingerprint=fingerprint, first_seen=first_seen, count=count, level=level, route=route)


def _incident(raw: object) -> IncidentSignal:
    if not isinstance(raw, Mapping):
        raise AggregateRefused("an incident must be an object")
    unknown = sorted(set(raw) - _INCIDENT_KEYS)
    if unknown:
        raise AggregateRefused(f"incident field {unknown[0]!r} is not a typed summary")
    source = str(raw.get("source") or "")
    identifier = str(raw.get("id") or "")
    if source not in {"alert", "error_group"} or not _INCIDENT_ID.match(identifier):
        raise AggregateRefused("an incident needs a source (alert or error_group) and a safe id")
    name = str(raw.get("name") or "")
    service = str(raw.get("service") or "")
    route_raw = str(raw.get("route") or "")
    host = str(raw.get("host") or "").lower()
    fingerprint = str(raw.get("fingerprint") or "")
    started_at = sortable_time(raw.get("started_at"))
    if (
        (name and not _INCIDENT_NAME.match(name))
        or (service and not _SERVICE.match(service))
        or (host and not _HOST.match(host))
        or (fingerprint and not _FINGERPRINT.match(fingerprint))
        or not started_at
    ):
        raise AggregateRefused("an incident carries only safe names, a start time and a fingerprint")
    route = _route(route_raw) if route_raw else ""
    if route_raw and not route:
        raise AggregateRefused("an incident route must be METHOD /path")
    return IncidentSignal(
        source=source,
        id=identifier,
        name=name,
        service=service,
        route=route,
        host=host,
        started_at=started_at,
        fingerprint=fingerprint,
    )


def sanitize_aggregate(raw: object) -> SignalAggregate:
    """Accept only a bounded aggregate; raise :class:`AggregateRefused` for anything else.

    The vocabulary is closed: numbers, error-group fingerprints with counts,
    typed incident summaries, a fixed error reason code and the observation
    time. A raw payload, message, title, stack trace, URL or any unknown field
    refuses the whole result -- it is never stored.
    """

    if not isinstance(raw, Mapping):
        raise AggregateRefused("an aggregate must be an object")
    unknown = sorted(set(raw) - _AGGREGATE_KEYS)
    if unknown:
        raise AggregateRefused(f"field {unknown[0]!r} is not an aggregate")
    identifier = str(raw.get("query_id") or "")
    status = str(raw.get("status") or "")
    if not _QUERY_ID.match(identifier) or status not in AGGREGATE_STATUSES:
        raise AggregateRefused("an aggregate names its query and an ok/no_data/error status")
    reason = str(raw.get("reason") or "")
    if reason not in ERROR_REASONS:
        raise AggregateRefused("an aggregate reason is a fixed code, never free text")
    groups_raw = raw.get("groups") or []
    incidents_raw = raw.get("incidents") or []
    if not isinstance(groups_raw, list) or len(groups_raw) > MAX_ERROR_GROUPS:
        raise AggregateRefused(f"at most {MAX_ERROR_GROUPS} error groups per aggregate")
    if not isinstance(incidents_raw, list) or len(incidents_raw) > MAX_ERROR_GROUPS:
        raise AggregateRefused(f"at most {MAX_ERROR_GROUPS} incidents per aggregate")
    observed_at = sortable_time(raw.get("observed_at"))
    return SignalAggregate(
        query_id=identifier,
        status=cast(AggregateStatus, status),
        value=_number(raw.get("value"), field="value"),
        baseline=_number(raw.get("baseline"), field="baseline"),
        groups=tuple(_group(item) for item in groups_raw),
        incidents=tuple(_incident(item) for item in incidents_raw),
        reason=reason,
        observed_at=observed_at,
    )


# --------------------------------------------------------------------------- #
# Predicted vs observed
# --------------------------------------------------------------------------- #

OutcomeState = Literal["confirmed", "contradicted", "not_observed", "observing"]
FINAL_STATES: Final[tuple[str, ...]] = ("confirmed", "contradicted", "not_observed")


@dataclass(frozen=True, slots=True)
class TargetOutcome:
    """How production answered one prediction: the state and the reason, in numbers."""

    key: str
    kind: str
    subject: str
    change: str
    title: str
    state: OutcomeState
    reason: str
    signals: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "subject": self.subject,
            "change": self.change,
            "title": self.title,
            "state": self.state,
            "reason": self.reason,
            "signals": list(self.signals),
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> TargetOutcome:
        state = str(raw.get("state") or "not_observed")
        return cls(
            key=str(raw.get("key") or ""),
            kind=str(raw.get("kind") or ""),
            subject=str(raw.get("subject") or ""),
            change=str(raw.get("change") or ""),
            title=str(raw.get("title") or ""),
            state=cast(OutcomeState, state if state in {*FINAL_STATES, "observing"} else "not_observed"),
            reason=str(raw.get("reason") or ""),
            signals=tuple(str(item) for item in raw.get("signals") or ()),
        )


def _count(value: float) -> str:
    return f"{round(value):,}"


def _ms(seconds: float) -> str:
    return f"{seconds * 1000:.0f} ms"


def _when(instant: str) -> str:
    return instant[:16].replace("T", " ") + " UTC" if instant else ""


def _failed(aggregate: SignalAggregate, connector: str) -> str:
    why = aggregate.reason.replace("_", " ") or "no answer"
    return f"the {connector} query failed ({why})"


def compare(
    prediction: Prediction,
    answers: Mapping[tuple[str, str], SignalAggregate],
    settings: ObservabilitySettings,
    window: ObservationWindow,
    *,
    final: bool,
    unplanned: str = "",
) -> TargetOutcome:
    """One prediction against the aggregates its queries returned.

    ``answers`` maps ``(signal, checkpoint)`` to the aggregate of this
    prediction's subject. Contradictions are decided on any checkpoint;
    confirmation only on the final one; the rest ends ``not_observed`` once
    ``final`` (``observing`` before).
    """

    def outcome(state: OutcomeState, reason: str, *signals: str) -> TargetOutcome:
        return TargetOutcome(
            key=prediction.key,
            kind=prediction.kind,
            subject=prediction.subject,
            change=prediction.change,
            title=prediction.title,
            state=state,
            reason=reason,
            signals=tuple(signals),
        )

    pending: OutcomeState = "not_observed" if final else "observing"
    if prediction.kind == "unobservable":
        return outcome("not_observed", f"Not observable in production: {prediction.note}.")
    if unplanned:
        return outcome("not_observed", f"Not observed: {unplanned}.")

    def ok(signal: str, checkpoint: str) -> SignalAggregate | None:
        item = answers.get((signal, checkpoint))
        return item if item is not None and item.status == "ok" else None

    def any_ok(signal: str) -> list[SignalAggregate]:
        return [item for checkpoint in ("interim", "final") if (item := ok(signal, checkpoint)) is not None]

    subject = prediction.subject
    if prediction.kind == "outbound_host":
        calls = any_ok("outbound_calls")
        if prediction.change == "removed":
            seen = [item for item in calls if (item.value or 0) > 0]
            if seen:
                total = max(item.value or 0.0 for item in seen)
                return outcome(
                    "contradicted",
                    f"{subject} was removed by this change, yet {_count(total)} calls to it were observed after the "
                    "deployment.",
                    "outbound_calls",
                )
            final_calls = ok("outbound_calls", "final")
            if final and final_calls is not None:
                return outcome(
                    "confirmed",
                    f"No calls to the removed host {subject} were observed in the window.",
                    "outbound_calls",
                )
        else:
            final_calls = ok("outbound_calls", "final")
            if final and final_calls is not None and (final_calls.value or 0) > 0:
                return outcome(
                    "confirmed",
                    f"{_count(final_calls.value or 0)} calls to {subject} were observed in production in the window.",
                    "outbound_calls",
                )
            if final and final_calls is not None:
                return outcome("not_observed", f"No calls to {subject} were observed in the window.", "outbound_calls")
        failed = answers.get(("outbound_calls", "final"))
        if final and failed is not None and failed.status == "error":
            return outcome("not_observed", f"Not observed: {_failed(failed, 'prometheus')}.", "outbound_calls")
        return outcome(
            pending, f"No answer about calls to {subject} yet." if not final else "Not observed in the window."
        )

    # Routes.
    if prediction.change == "removed":
        served = [item for item in any_ok("route_requests") if (item.value or 0) > 0]
        if served:
            total = max(item.value or 0.0 for item in served)
            return outcome(
                "contradicted",
                f"{subject} was removed by this change, yet it served {_count(total)} requests after the deployment.",
                "route_requests",
            )
        if final and ok("route_requests", "final") is not None:
            return outcome(
                "confirmed", f"The removed route {subject} served no requests in the window.", "route_requests"
            )
        return outcome(pending, "Not observed in the window." if final else f"No answer about {subject} yet.")
    for item in any_ok("route_error_groups"):
        fresh = sorted(
            (group for group in item.groups if window.contains(group.first_seen)),
            key=lambda group: (group.first_seen, group.fingerprint),
        )
        if fresh:
            group = fresh[0]
            more = f" and {len(fresh) - 1} more" if len(fresh) > 1 else ""
            return outcome(
                "contradicted",
                f"New error group {group.fingerprint[:12]}{more} on {subject} after the deployment "
                f"(first seen {_when(group.first_seen)}, {_count(group.count)} events).",
                "route_error_groups",
            )
    latency = ok("route_latency_p95", "final")
    if latency is not None and latency.value is not None and latency.baseline is not None and latency.baseline > 0:
        ratio = latency.value / latency.baseline
        if ratio >= settings.latency_regression_ratio and latency.value - latency.baseline >= LATENCY_FLOOR_SECONDS:
            return outcome(
                "contradicted",
                f"p95 latency on {subject} rose from {_ms(latency.baseline)} to {_ms(latency.value)} "
                f"(x{ratio:.1f}) after the deployment.",
                "route_latency_p95",
            )
    queries = ok("route_query_count", "final")
    if queries is not None and queries.value is not None and queries.baseline is not None and queries.baseline > 0:
        ratio = queries.value / queries.baseline
        if ratio >= settings.query_count_regression_ratio and queries.value - queries.baseline >= QUERY_COUNT_FLOOR:
            return outcome(
                "contradicted",
                f"Database queries per request on {subject} rose from {queries.baseline:.1f} to {queries.value:.1f} "
                "after the deployment.",
                "route_query_count",
            )
    if not final:
        return outcome("observing", f"Watching {subject} until {_when(window.end)}.")
    requests = ok("route_requests", "final")
    served_total = (requests.value or 0.0) if requests is not None else 0.0
    errors_answered = ok("route_error_groups", "final") is not None
    if (
        served_total > 0
        or (latency is not None and latency.value is not None)
        or (errors_answered and requests is None)
    ):
        checked = [
            name
            for name, present in (
                ("no new error group", errors_answered),
                ("no latency regression", latency is not None and latency.value is not None),
                ("no query-count regression", queries is not None and queries.value is not None),
            )
            if present
        ]
        traffic = f"served {_count(served_total)} requests" if served_total > 0 else "was observed"
        detail = ", ".join(checked) if checked else "no regression signal"
        return outcome(
            "confirmed",
            f"{subject} {traffic} in the window with {detail}.",
            *(
                name
                for name in ("route_requests", "route_error_groups", "route_latency_p95", "route_query_count")
                if ok(name, "final") is not None
            ),
        )
    failures = [
        _failed(item, SIGNAL_CONNECTOR.get(signal, "connector"))
        for (signal, checkpoint), item in sorted(answers.items())
        if checkpoint == "final" and item.status == "error"
    ]
    if failures:
        return outcome("not_observed", f"Not observed: {failures[0]}.")
    return outcome("not_observed", f"{subject} served no observed requests in the window.")


def outcome_counts(outcomes: Iterable[TargetOutcome]) -> dict[str, int]:
    counts = {"confirmed": 0, "contradicted": 0, "not_observed": 0, "observing": 0}
    for item in outcomes:
        counts[item.state] = counts.get(item.state, 0) + 1
    return counts


__all__ = [
    "AGGREGATE_STATUSES",
    "DEFAULT_OBSERVATION_WINDOW_HOURS",
    "DEFAULT_QUERY_BUDGET",
    "FINAL_STATES",
    "MAX_ERROR_GROUPS",
    "MAX_OBSERVATION_WINDOW_HOURS",
    "MAX_PREDICTIONS",
    "MAX_QUERY_BUDGET",
    "NOT_CONFIGURED",
    "OBSERVABILITY_CONNECTOR_KINDS",
    "SIGNAL_COST",
    "SIGNAL_KINDS",
    "AggregateRefused",
    "ConnectorConfig",
    "CredentialRef",
    "ErrorGroup",
    "IncidentSignal",
    "ObservabilitySettings",
    "ObservationWindow",
    "OutcomeState",
    "Prediction",
    "QueryPlan",
    "SignalAggregate",
    "SignalQuery",
    "TargetOutcome",
    "compare",
    "observation_window",
    "outcome_counts",
    "parse_observability_settings",
    "plan_queries",
    "prediction_for",
    "predictions_from_deltas",
    "query_id",
    "sanitize_aggregate",
]

"""The customer-side production observation worker (P15-OBS on leased hosted jobs).

Runs where the telemetry and its credentials live. One pass claims a leased
``production_observation`` job from the hosted control plane, executes its
plan with the typed connectors (:mod:`observability_connectors`) and reports
bounded aggregates, or a fixed failure code so the queue retries with backoff
or dead-letters the job.

The control plane names connector endpoints and credential *environment
variable names*; this worker sends a credential only to a connector its own
operator pinned locally (kind, base URL and credential variable must all
match), so a changed or hostile hosted policy cannot redirect a credential.
Nothing here logs a URL, a header, a token or a response body.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, urlencode, urlsplit

from lemoncrow.pro.capabilities.review.outcomes.deployment_outcomes import utc_now
from lemoncrow.pro.capabilities.review.outcomes.observability import ObservabilitySettings
from lemoncrow.pro.capabilities.review.outcomes.observability_connectors import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_RESPONSE_BYTES,
    HttpGet,
    run_observation_plan,
    urllib_get,
)

_LOG = logging.getLogger(__name__)

JsonTransport = Callable[[str, Mapping[str, Any], Mapping[str, str], float], tuple[int, Any]]
"""``(url, json_body, headers, timeout_seconds) -> (status, parsed_json)``: one POST; raises ``OSError``."""

CONNECTOR_KINDS: Final[frozenset[str]] = frozenset({"prometheus", "sentry"})
TRANSIENT_REASONS: Final[frozenset[str]] = frozenset({"timeout", "connection_failed", "http_error"})
HEARTBEAT_SECONDS: Final[float] = 30.0
#: Heartbeat calls per renewal before the worker gives the lease back with a transient code.
HEARTBEAT_ATTEMPTS: Final[int] = 3
HEARTBEAT_RETRY_SECONDS: Final[float] = 2.0
#: Hosted answers meaning this lease is no longer the worker's (stale, reclaimed, revoked designation).
LEASE_LOST_CODES: Final[frozenset[str]] = frozenset({"review_state_conflict", "unauthenticated", "forbidden"})
_LOOPBACK: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})
_MAX_PINS_BYTES: Final[int] = 256 * 1024
_MAX_PINS: Final[int] = 64


class ObservationWorkerError(Exception):
    """A hosted call failed; ``code`` is the server's error code or a fixed transport code (never a body)."""

    def __init__(self, code: str, status: int = 0) -> None:
        super().__init__(f"{code} (HTTP {status})" if status else code)
        self.code = code
        self.status = status


class LeaseCancelled(Exception):
    """The control plane stopped this lease (stale deployment, advanced source, changed policy)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _LeaseLost(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _HostedUnreachable(Exception):
    """A hosted call kept failing in transport (or 5xx); ``code`` is the transient code to fail the lease with."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _transient_code(error: ObservationWorkerError) -> str:
    return error.code if error.code in {"timeout", "connection_failed"} else "http_error"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # the worker token never follows a redirect


def urllib_post_json(url: str, body: Mapping[str, Any], headers: Mapping[str, str], timeout: float) -> tuple[int, Any]:
    """The default hosted transport: one JSON POST, no redirects, the response capped."""

    opener = urllib.request.build_opener(_NoRedirect())
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={**headers, "Content-Type": "application/json"}, method="POST"
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            status, raw = int(response.status), response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status, raw = int(exc.code), exc.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        return status, {}
    try:
        return status, json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        return status, {}


def _server_url(raw: str) -> str:
    text = raw.strip().rstrip("/")
    parts = urlsplit(text)
    if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname:
        raise ValueError("the hosted server URL must be a plain https URL")
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOOPBACK):
        return text
    raise ValueError("the hosted server URL must use https (http only on loopback)")


class HostedObservationClient:
    """The customer worker's calls to the hosted worker protocol (bearer token of the worker identity)."""

    def __init__(
        self,
        server_url: str,
        token: str,
        *,
        transport: JsonTransport = urllib_post_json,
        timeout: float = 30.0,
    ) -> None:
        if not token:
            raise ValueError("the worker token is empty")
        self._base = _server_url(server_url)
        self._token = token
        self._transport = transport
        self._timeout = timeout

    def __repr__(self) -> str:  # never the token
        return f"HostedObservationClient({self._base!r})"

    def _post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        try:
            status, document = self._transport(f"{self._base}{path}", body, headers, self._timeout)
        except TimeoutError as exc:
            raise ObservationWorkerError("timeout") from exc
        except (OSError, ValueError) as exc:
            raise ObservationWorkerError("connection_failed") from exc
        payload = document if isinstance(document, dict) else {}
        if not 200 <= status < 300:
            error = payload.get("error") if isinstance(payload.get("error"), Mapping) else {}
            code = str(error.get("code") or "http_error")[:64] if isinstance(error, Mapping) else "http_error"
            raise ObservationWorkerError(code, status)
        return payload

    def claim(self, repository_ids: Sequence[str]) -> dict[str, Any] | None:
        body = self._post(
            "/v1/review-workers/claim",
            {"execution_class": "production_observation", "repository_ids": list(repository_ids)},
        )
        return body if isinstance(body.get("job"), Mapping) and body.get("envelope") else None

    def heartbeat(self, job_id: str, envelope: str) -> dict[str, Any]:
        return self._post(f"/v1/review-workers/{quote(job_id, safe='')}/heartbeat", {"envelope": envelope})

    def report(
        self, job_id: str, envelope: str, aggregates: Sequence[Mapping[str, Any]], skipped: int
    ) -> dict[str, Any]:
        result = {
            "result_refs": [],
            "evidence_refs": [],
            "summary": {"observation": {"aggregates": [dict(item) for item in aggregates], "skipped": skipped}},
        }
        return self._post(
            f"/v1/review-workers/{quote(job_id, safe='')}/result", {"envelope": envelope, "result": result}
        )

    def fail(self, job_id: str, envelope: str, reason: str, *, retryable: bool) -> dict[str, Any]:
        return self._post(
            f"/v1/review-workers/{quote(job_id, safe='')}/fail",
            {"envelope": envelope, "reason": reason, "retryable": retryable},
        )

    def check(self, repository_ids: Sequence[str]) -> dict[str, Any]:
        body: dict[str, Any] = {"repository_ids": list(repository_ids)} if repository_ids else {}
        return self._post("/v1/review-workers/observations/check", body)


# --------------------------------------------------------------------------- #
# Local connector pins: the worker operator's allow-list for credentials
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ConnectorPin:
    """One connector this worker may call with a credential: kind, endpoint and credential variable."""

    kind: str
    base_url: str
    credential_env: str = ""

    def matches(self, connector: Any) -> bool:
        return bool(
            connector.kind == self.kind
            and connector.base_url == self.base_url
            and connector.credential.env == self.credential_env
        )


def parse_connector_pins(document: Any) -> tuple[ConnectorPin, ...]:
    """``{"version": 1, "connectors": [{"kind", "base_url", "credential_env"}]}``; anything else is refused."""

    if not isinstance(document, Mapping) or document.get("version") != 1:
        raise ValueError("connector pins must be a version-1 object")
    raw = document.get("connectors")
    if not isinstance(raw, list) or len(raw) > _MAX_PINS:
        raise ValueError("connector pins must list at most 64 connectors")
    pins: list[ConnectorPin] = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) - {"kind", "base_url", "credential_env"}:
            raise ValueError("a connector pin has only kind, base_url and credential_env")
        kind, base_url, env = (str(item.get(key) or "").strip() for key in ("kind", "base_url", "credential_env"))
        if kind not in CONNECTOR_KINDS:
            raise ValueError("a connector pin kind must be prometheus or sentry")
        parts = urlsplit(base_url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query:
            raise ValueError("a connector pin base_url must be a plain https URL")
        pins.append(ConnectorPin(kind, base_url.rstrip("/"), env))
    return tuple(pins)


def load_connector_pins(path: Path) -> tuple[ConnectorPin, ...]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"connector pins are unreadable: {path}") from exc
    if len(raw) > _MAX_PINS_BYTES:
        raise ValueError("connector pins file is too large")
    try:
        return parse_connector_pins(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("connector pins file is not JSON") from exc


def pinned_settings(
    settings: ObservabilitySettings, pins: Sequence[ConnectorPin]
) -> tuple[ObservabilitySettings, tuple[str, ...]]:
    """The settings restricted to pinned connectors, and the ids of the connectors left out."""

    kept = tuple(item for item in settings.connectors if any(pin.matches(item) for pin in pins))
    left_out = tuple(item.id for item in settings.connectors if item not in kept)
    return replace(settings, connectors=kept), left_out


# --------------------------------------------------------------------------- #
# One lease
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LeaseExecution:
    aggregates: tuple[dict[str, Any], ...] = ()
    skipped: int = 0
    failure: str = ""
    """A whole-lease failure code (nothing ran)."""
    transient: str = ""
    """The most common transient connector failure among the aggregates, if any."""


def execute_lease(
    lease: Mapping[str, Any],
    *,
    pins: Sequence[ConnectorPin],
    environ: Mapping[str, str],
    now: str,
    http_get: HttpGet = urllib_get,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> LeaseExecution:
    """Run one leased plan with the pinned connectors only; bounded aggregates out."""

    view = lease.get("observation")
    if not isinstance(view, Mapping):
        return LeaseExecution(failure="unsupported")
    plan, raw_settings = view.get("plan"), view.get("settings")
    if not isinstance(plan, Mapping) or not isinstance(raw_settings, Mapping):
        return LeaseExecution(failure="unsupported")
    settings = ObservabilitySettings.from_wire(raw_settings, locus="customer_worker")
    if not settings.configured:
        return LeaseExecution(failure="unsupported")
    allowed, left_out = pinned_settings(settings, pins)
    if left_out:
        _LOG.warning("observation connectors not pinned on this worker count=%d", len(left_out))
    if not allowed.connectors:
        return LeaseExecution(failure="connector_not_pinned")
    report = run_observation_plan(plan, allowed, environ=environ, now=now, http_get=http_get, timeout=timeout)
    reasons = Counter(
        str(item.get("reason") or "")
        for item in report.aggregates
        if item.get("status") == "error" and item.get("reason") in TRANSIENT_REASONS
    )
    transient = reasons.most_common(1)[0][0] if reasons else ""
    return LeaseExecution(report.aggregates, report.skipped, "", transient)


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    """What one pass did: ``idle``, ``published``, ``superseded``, ``not_applicable``, ``retry``,
    ``dead_lettered``, ``cancelled`` or ``lost`` (the lease stopped being authoritative)."""

    state: str
    job_id: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)


def _heartbeating(http_get: HttpGet, beat: Callable[[], None], every: float) -> HttpGet:
    last = [time.monotonic()]

    def get(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        if time.monotonic() - last[0] >= every:
            beat()
            last[0] = time.monotonic()
        return http_get(url, headers, timeout)

    return get


def run_once(
    client: HostedObservationClient,
    *,
    repository_ids: Sequence[str],
    pins: Sequence[ConnectorPin],
    environ: Mapping[str, str],
    now: str | None = None,
    http_get: HttpGet = urllib_get,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> WorkerOutcome:
    """Claim one production observation lease, execute it and report or fail it.

    Once a lease is held, no hosted error escapes: a heartbeat that keeps
    failing in transport (bounded retries) or a report the control plane could
    not take gives the lease back with a transient code, so the queue retries
    it after its backoff instead of waiting for the lease to expire; an answer
    that the lease is no longer this worker's ends the pass as ``lost``.
    """

    claimed = client.claim(repository_ids)
    if claimed is None:
        return WorkerOutcome("idle")
    lease, envelope = claimed["job"], str(claimed["envelope"])
    job_id = str(lease.get("job_id") or "")
    view = lease.get("observation") if isinstance(lease.get("observation"), Mapping) else {}
    assert isinstance(view, Mapping)
    attempt, max_attempts = int(view.get("attempt") or 1), int(view.get("max_attempts") or 1)

    def renew() -> Mapping[str, Any]:
        tried = 1
        while True:
            try:
                return client.heartbeat(job_id, envelope)
            except ObservationWorkerError as exc:
                if exc.code in LEASE_LOST_CODES:
                    raise _LeaseLost(exc.code) from exc
                if tried >= HEARTBEAT_ATTEMPTS:
                    raise _HostedUnreachable(_transient_code(exc)) from exc
                sleep(HEARTBEAT_RETRY_SECONDS * tried)
                tried += 1

    def beat() -> None:
        state = renew()
        if state.get("state") != "running":
            raise LeaseCancelled(str(state.get("cancel_reason") or state.get("state") or "cancelled"))

    def give_back(code: str) -> WorkerOutcome:
        try:
            failed = client.fail(job_id, envelope, code, retryable=True)
        except ObservationWorkerError as exc:
            return WorkerOutcome("lost", job_id, {"code": exc.code})  # the lease expires and is reclaimed
        return WorkerOutcome("dead_lettered" if failed.get("dead_lettered") else "retry", job_id, failed)

    try:
        execution = execute_lease(
            lease,
            pins=pins,
            environ=environ,
            now=now or utc_now(),
            http_get=_heartbeating(http_get, beat, heartbeat_seconds),
        )
    except LeaseCancelled as exc:
        try:
            client.fail(job_id, envelope, "lease_cancelled", retryable=False)  # release the lease now
        except ObservationWorkerError:
            pass  # the lease expires on its own
        return WorkerOutcome("cancelled", job_id, {"reason": exc.reason[:512]})
    except _LeaseLost as exc:
        return WorkerOutcome("lost", job_id, {"code": exc.code})
    except _HostedUnreachable as exc:
        return give_back(exc.code)
    try:
        if execution.failure:
            failed = client.fail(job_id, envelope, execution.failure, retryable=False)
            return WorkerOutcome("dead_lettered", job_id, failed)
        if execution.transient and attempt < max_attempts:
            # A transient connector failure: nothing is published from this attempt;
            # the queue re-offers the whole plan after its backoff.
            failed = client.fail(job_id, envelope, execution.transient, retryable=True)
            return WorkerOutcome("dead_lettered" if failed.get("dead_lettered") else "retry", job_id, failed)
        reported = client.report(job_id, envelope, execution.aggregates, execution.skipped)
    except ObservationWorkerError as exc:
        if exc.code in LEASE_LOST_CODES:
            return WorkerOutcome("lost", job_id, {"code": exc.code})
        return give_back(_transient_code(exc))
    publication = reported.get("publication") if isinstance(reported.get("publication"), Mapping) else {}
    assert isinstance(publication, Mapping)
    return WorkerOutcome(str(publication.get("state") or "not_applicable"), job_id, dict(publication))


def run_until_idle(
    client: HostedObservationClient,
    *,
    repository_ids: Sequence[str],
    pins: Sequence[ConnectorPin],
    environ: Mapping[str, str],
    http_get: HttpGet = urllib_get,
    limit: int = 100,
) -> list[WorkerOutcome]:
    outcomes: list[WorkerOutcome] = []
    for _ in range(max(1, limit)):
        outcome = run_once(client, repository_ids=repository_ids, pins=pins, environ=environ, http_get=http_get)
        outcomes.append(outcome)
        if outcome.state == "idle":
            break
    return outcomes


# --------------------------------------------------------------------------- #
# Dry run: reachability and connector permission, no telemetry ingested
# --------------------------------------------------------------------------- #


def _probe(connector: Mapping[str, Any], *, token: str, http_get: HttpGet, timeout: float) -> str:
    """Prove the credential reaches the endpoint with a request that reads no telemetry."""

    base = str(connector.get("base_url") or "")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if connector.get("kind") == "prometheus":
        url = f"{base}/api/v1/query?{urlencode({'query': 'vector(1)'})}"  # a constant: no series are read
    else:
        organization = quote(str(connector.get("organization") or ""), safe="")
        project = quote(str(connector.get("project") or ""), safe="")
        url = f"{base}/api/0/projects/{organization}/{project}/"  # project metadata; the body is discarded
    try:
        status, _body = http_get(url, headers, timeout)
    except TimeoutError:
        return "timeout"
    except (OSError, ValueError):
        return "connection_failed"
    if status in {401, 403}:
        return "unauthorized"
    return "ok" if status == 200 else "http_error"


def check_worker(
    client: HostedObservationClient,
    *,
    repository_ids: Sequence[str],
    pins: Sequence[ConnectorPin],
    environ: Mapping[str, str],
    http_get: HttpGet = urllib_get,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Worker reachability, designation and per-connector permission; claims nothing and ingests nothing."""

    try:
        server = client.check(repository_ids)
    except ObservationWorkerError as exc:
        if exc.code == "forbidden":
            # Reached, but the operator has not designated this worker (connector details are withheld).
            return {
                "reachable": True,
                "worker_id": "",
                "designated": False,
                "ready": False,
                "error": exc.code,
                "telemetry_ingested": 0,
                "repositories": [],
            }
        return {"reachable": False, "error": exc.code, "status": exc.status, "repositories": []}
    settings_cache: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for repo in server.get("repositories") or ():
        if not isinstance(repo, Mapping):
            continue
        connectors: list[dict[str, Any]] = []
        for raw in repo.get("connectors") or ():
            if not isinstance(raw, Mapping):
                continue
            credential = raw.get("credential") if isinstance(raw.get("credential"), Mapping) else {}
            assert isinstance(credential, Mapping)
            env = str(credential.get("env") or "")
            pin = ConnectorPin(str(raw.get("kind") or ""), str(raw.get("base_url") or ""), env)
            pinned = pin in pins
            token = str(environ.get(env) or "") if env else ""
            if not pinned:
                probe = "not_pinned"  # never send a credential to an endpoint this worker did not pin
            elif env and not token:
                probe = "credential_missing"
            elif raw.get("kind") == "sentry" and not token:
                probe = "credential_missing"
            else:
                key = f"{pin.kind}|{pin.base_url}|{env}|{raw.get('organization')}|{raw.get('project')}"
                if key not in settings_cache:
                    settings_cache[key] = _probe(raw, token=token, http_get=http_get, timeout=timeout)
                probe = settings_cache[key]
            connectors.append(
                {
                    "id": str(raw.get("id") or ""),
                    "kind": pin.kind,
                    "base_url": pin.base_url,
                    "credential_env": env,
                    "credential_present": bool(token),
                    "pinned": pinned,
                    "probe": probe,
                }
            )
        rows.append(
            {
                "repo_id": str(repo.get("repo_id") or ""),
                "locus": repo.get("locus"),
                "configured": bool(repo.get("configured")),
                "admitted": bool(repo.get("admitted")),
                "admission_reason": str(repo.get("admission_reason") or ""),
                "connectors": connectors,
            }
        )
    ready = bool(server.get("designated")) and all(
        row["admitted"] and all(item["probe"] == "ok" for item in row["connectors"])
        for row in rows
        if row["configured"]
    )
    return {
        "reachable": True,
        "worker_id": str(server.get("worker_id") or ""),
        "designated": bool(server.get("designated")),
        "ready": ready,
        "telemetry_ingested": 0,
        "repositories": rows,
    }


__all__ = [
    "ConnectorPin",
    "HostedObservationClient",
    "LeaseCancelled",
    "LeaseExecution",
    "ObservationWorkerError",
    "WorkerOutcome",
    "check_worker",
    "execute_lease",
    "load_connector_pins",
    "parse_connector_pins",
    "pinned_settings",
    "run_once",
    "run_until_idle",
    "urllib_post_json",
]

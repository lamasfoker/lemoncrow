"""Anonymous daily product-event rollup to the telemetry destination.

The destination is lemoncrow.com, the configured LemonCrow server, or a custom
host (see :func:`.config.resolve_destination`).

Once per elapsed UTC day, count the local telemetry store's events and post the
counts -- never the events -- to ``/api/telemetry/events``. Each row is
``(event, dim, context, outcome, bucket, n)`` built only from allowlisted,
low-cardinality props (command and tool names, host, exit reason, error class,
duration bucket); session ids, repo ids, paths and free text never leave the
machine. The install is identified by the same one-way ``install_key`` as the
savings rollup (:mod:`.public_rollup`).

Gated by the same opt-out as every other remote telemetry (``lc rt telemetry
remote off``, ``DO_NOT_TRACK=1``, ``LEMONCROW_TELEMETRY=off``, or a policy
destination of ``off``). Days that elapse while opted out are skipped for
good, never backfilled later.

Scheduling, locking and retry backoff are shared with the savings rollup via
:func:`.public_rollup.run_scheduled_flush`; state lives in
``<root>/telemetry/event_rollup_state.json``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lemoncrow.core.foundation.identity import get_anon_id
from lemoncrow.core.service.telemetry.public_rollup import (
    FLUSH_ATTEMPTS,
    FLUSH_TIMEOUT_SECONDS,
    _service_version,
    run_scheduled_flush,
)
from lemoncrow.core.service.telemetry.schema import DURATION_MS_BUCKETS, DURATION_S_BUCKETS, EVENTS

if TYPE_CHECKING:
    from lemoncrow.core.service.telemetry.config import TelemetryDestination

logger = logging.getLogger("lemoncrow.product.telemetry.event_rollup")

DEFAULT_EVENT_ROLLUP_ENDPOINT = "https://lemoncrow.com/api/telemetry/events"
_STATE_RELPATH = ("telemetry", "event_rollup_state.json")
_LOCK_RELPATH = ("telemetry", "event_rollup.lock")
# The LemonCrow telemetry service accepts at most this many rows per rollup
# (its MAX_ROWS). The busiest rows are kept; the long tail is dropped rather
# than rejected.
MAX_ROWS = 500
_MAX_COUNT = 10_000_000
_RETRY_SLEEP_SECONDS = (0.5, 1.5)

_LABEL_STRIP = re.compile(r"[^A-Za-z0-9_.:+/-]")
# Command/subcommand names are click identifiers; anything else (e.g. prompt
# text an older client mistook for a command) collapses to "other".
_COMMAND_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def event_rollup_endpoint(destination: TelemetryDestination | None = None) -> str:
    """The event rollup URL on the telemetry destination; "" when remote
    telemetry is off. LEMONCROW_EVENT_TELEMETRY_ENDPOINT overrides it for local
    development unless an administrator policy manages the destination."""
    from lemoncrow.core.service.telemetry.config import EVENTS_PATH, destination_url, resolve_destination

    return destination_url(destination or resolve_destination(), EVENTS_PATH, "LEMONCROW_EVENT_TELEMETRY_ENDPOINT")


def _label(value: Any, max_length: int = 64) -> str:
    if not isinstance(value, str):
        return ""
    return _LABEL_STRIP.sub("", value.strip())[:max_length]


_BUCKETS = frozenset(DURATION_MS_BUCKETS) | frozenset(DURATION_S_BUCKETS)


def _bucket(value: Any) -> str:
    return value if isinstance(value, str) and value in _BUCKETS else ""


def _command(props: dict[str, Any]) -> str:
    name = props.get("command_name")
    if not isinstance(name, str) or not _COMMAND_RE.match(name):
        return "other"
    sub = props.get("subcommand")
    if isinstance(sub, str) and _COMMAND_RE.match(sub):
        return f"{name}.{sub}"
    return name


def _outcome(props: dict[str, Any], failure_key: str) -> str:
    ok = props.get("ok")
    if ok is True:
        return "ok"
    if ok is False:
        return _label(props.get(failure_key)) or "error"
    return ""


def row_labels(event: str, props: dict[str, Any]) -> tuple[str, str, str, str]:
    """``(dim, context, outcome, bucket)`` for one event; only allowlisted,
    low-cardinality props are read."""
    if event == "session_start":
        return _label(props.get("agent_host"), 40), "", "", ""
    if event == "session_end":
        return _label(props.get("exit_reason")), "", "", _bucket(props.get("duration_s_bucket"))
    if event == "session_interrupted":
        return _label(props.get("signal")), "", "", _bucket(props.get("elapsed_s_bucket"))
    if event == "cli_command_invoked":
        return _command(props), "", "", ""
    if event == "cli_command_completed":
        context = "background" if props.get("background") is True else "interactive"
        return (
            _command(props),
            context,
            _outcome(props, "error_kind"),
            _bucket(props.get("duration_ms_bucket")),
        )
    if event == "mcp_tool_called":
        return (
            _label(props.get("tool_name")),
            _label(props.get("agent_host"), 40),
            _outcome(props, "error_code"),
            _bucket(props.get("duration_ms_bucket")),
        )
    if event == "code_context_retrieved":
        return _label(props.get("operation")), "", "", ""
    if event == "code_symbol_retrieved":
        return _label(props.get("kind")), "", "", ""
    if event == "frustration_signal_behavioral":
        return _label(props.get("signal_type")), "", "", ""
    if event == "frustration_signal_lexical":
        return _label(props.get("category")), _label(props.get("surface"), 40), "", ""
    if event == "rescue_offered":
        return _label(props.get("rescue_type")), "", "", ""
    if event == "plan_check_blocked":
        return _label(props.get("severity")), "", "", ""
    if event == "optimization_proposal_evaluated":
        return _label(props.get("source")), "", "", ""
    if event == "optimization_proposal_result":
        return _label(props.get("action")), _label(props.get("source"), 40), "", ""
    return "", "", "", ""


def _day_start_ts(day: date) -> float:
    return datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()


def aggregate_event_rows_by_day(db_path: Path, *, since_day: str, today: str) -> dict[str, list[dict[str, Any]]]:
    """Count events per UTC day for every day strictly after ``since_day`` and
    strictly before ``today`` (a day is only reported once it has elapsed)."""
    first = date.fromisoformat(since_day) + timedelta(days=1)
    end = date.fromisoformat(today)
    if first >= end or not db_path.exists():
        return {}
    counts: Counter[tuple[str, str, str, str, str, str]] = Counter()
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=10.0)
    try:
        cursor = conn.execute(
            "SELECT ts, event, props_json FROM events WHERE ts >= ? AND ts < ?",
            (_day_start_ts(first), _day_start_ts(end)),
        )
        for ts, event, props_json in cursor:
            if event not in EVENTS:
                continue
            try:
                props = json.loads(props_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(props, dict):
                continue
            day = datetime.fromtimestamp(float(ts), UTC).strftime("%Y-%m-%d")
            counts[(day, event, *row_labels(event, props))] += 1
    finally:
        # A read failure propagates: the scheduler records an error and keeps
        # the checkpoint, instead of treating unreadable days as empty ones.
        conn.close()

    by_day: dict[str, list[dict[str, Any]]] = {}
    for (day, event, dim, context, outcome, bucket), n in counts.most_common():
        rows = by_day.setdefault(day, [])
        if len(rows) < MAX_ROWS:
            rows.append(
                {
                    "event": event,
                    "dim": dim,
                    "context": context,
                    "outcome": outcome,
                    "bucket": bucket,
                    "n": min(n, _MAX_COUNT),
                }
            )
    return by_day


def post_json(
    endpoint: str,
    payload: dict[str, Any],
    *,
    timeout_s: float,
    attempts: int,
    headers: dict[str, str] | None = None,
) -> int:
    """POST ``payload``; the HTTP status, or 0 when the network failed.
    Retries only transient failures (network, 429, 5xx)."""
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        # The production endpoint's bot filter 403s the default urllib agent.
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"lemoncrow/{payload['lemoncrow_version']}",
            **(headers or {}),
        },
        method="POST",
    )
    status = 0
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            status = int(getattr(exc, "code", 0) or 0)
            if status < 500 and status != 429:
                return status
        except (OSError, urllib.error.URLError):
            status = 0
        if attempt + 1 < attempts:
            time.sleep(_RETRY_SLEEP_SECONDS[min(attempt, len(_RETRY_SLEEP_SECONDS) - 1)])
    return status


def flush_daily_event_rollup(
    root: str | Path,
    *,
    checkpoint_day: str | None,
    db_path: Path | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Post one row set per elapsed UTC day after ``checkpoint_day``.

    ``checkpoint_day is None`` (first run) only sets the baseline, so history
    recorded before this exporter existed is never sent. The checkpoint moves
    past a day once it is stored, rejected as invalid (400/413 -- resending
    the same bytes cannot help), or elapsed while remote telemetry was off.
    """
    del root
    from lemoncrow.core.service.telemetry.config import auth_headers, resolve_destination
    from lemoncrow.core.service.telemetry.local_store import default_db_path

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    if checkpoint_day is None:
        return {"flushed": False, "reason": "baseline"}, yesterday
    if checkpoint_day >= yesterday:
        return {"flushed": False, "reason": "no_new_days"}, checkpoint_day
    destination = resolve_destination(with_token=True)
    endpoint = event_rollup_endpoint(destination)
    if not endpoint:
        return {"flushed": False, "reason": "disabled"}, yesterday

    by_day = aggregate_event_rows_by_day(db_path or default_db_path(), since_day=checkpoint_day, today=today)
    install_key = hashlib.sha256(get_anon_id().encode("utf-8")).hexdigest()
    version = _service_version()
    headers = auth_headers(destination, endpoint)
    checkpoint = checkpoint_day
    posted = 0
    rejected: list[str] = []
    for day in sorted(by_day):
        status = post_json(
            endpoint,
            {"install_key": install_key, "lemoncrow_version": version, "day": day, "rows": by_day[day]},
            timeout_s=FLUSH_TIMEOUT_SECONDS,
            attempts=FLUSH_ATTEMPTS,
            headers=headers,
        )
        if 200 <= status < 300:
            posted += 1
        elif status in (400, 413):
            # The server judged these bytes invalid; resending cannot help. Any
            # other failure (404 before the endpoint ships, 401, 429, 5xx,
            # network) keeps the checkpoint and retries with backoff.
            logger.debug("event_rollup.rejected", extra={"day": day, "status": status})
            rejected.append(day)
        else:
            return {
                "flushed": posted > 0,
                "posted_days": posted,
                "stuck_day": day,
                "reason": "post_failed",
            }, checkpoint
        checkpoint = day
    result: dict[str, Any] = {"flushed": posted > 0, "posted_days": posted, "through_day": yesterday}
    if rejected:
        result["rejected_days"] = rejected
    return result, yesterday


def maybe_flush_event_rollup(
    root: str | Path,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the daily event flush if it is due; cheap no-op otherwise."""
    return run_scheduled_flush(
        root,
        flush=lambda root_path, checkpoint: flush_daily_event_rollup(root_path, checkpoint_day=checkpoint),
        state_relpath=_STATE_RELPATH,
        lock_relpath=_LOCK_RELPATH,
        force=force,
        now=now,
    )


__all__ = [
    "MAX_ROWS",
    "aggregate_event_rows_by_day",
    "event_rollup_endpoint",
    "flush_daily_event_rollup",
    "maybe_flush_event_rollup",
    "row_labels",
]

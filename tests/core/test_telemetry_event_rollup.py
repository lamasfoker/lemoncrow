from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.foundation.identity import get_anon_id
from lemoncrow.core.service.telemetry import event_rollup as event_rollup_mod
from lemoncrow.core.service.telemetry.config import save_telemetry_config
from lemoncrow.core.service.telemetry.event_rollup import (
    MAX_ROWS,
    aggregate_event_rows_by_day,
    flush_daily_event_rollup,
    maybe_flush_event_rollup,
    row_labels,
)
from lemoncrow.core.service.telemetry.local_store import LocalTelemetryStore
from lemoncrow.core.service.telemetry.public_rollup import read_flush_state


@pytest.fixture()
def telemetry_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "telemetry.db"
    monkeypatch.setenv("LEMONCROW_TELEMETRY_DB", str(db_path))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_CONFIG", str(tmp_path / "telemetry.toml"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ID_PATH", str(tmp_path / "telemetry_id"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY", "0")
    return db_path


def _enable_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ALLOW_IN_TESTS", "1")
    monkeypatch.delenv("LEMONCROW_TELEMETRY", raising=False)
    save_telemetry_config(remote_enabled=True)


def _ts(day: date, hour: int = 12) -> float:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC).timestamp()


def _today() -> date:
    return datetime.now(UTC).date()


def test_row_labels_use_only_allowlisted_low_cardinality_props() -> None:
    assert row_labels(
        "mcp_tool_called",
        {
            "tool_name": "search",
            "agent_host": "claude",
            "ok": False,
            "error_code": "deadline_exceeded",
            "duration_ms_bucket": ">10000",
            "session_id": "secret-session",
        },
    ) == ("search", "claude", "deadline_exceeded", ">10000")
    assert row_labels(
        "cli_command_completed",
        {"command_name": "code", "subcommand": "index", "ok": True, "background": True, "duration_ms_bucket": "<100"},
    ) == ("code.index", "background", "ok", "<100")
    # Free text mistaken for a command by an older client never leaves.
    assert row_labels("cli_command_invoked", {"command_name": "fix the login bug"})[0] == "other"
    assert row_labels("code_context_retrieved", {"repo_id": "acme/private", "operation": "search"}) == (
        "search",
        "",
        "",
        "",
    )
    assert row_labels("playbook_retrieved", {"domain": "customer-x"}) == ("", "", "", "")


def test_aggregate_counts_only_elapsed_days_after_the_checkpoint(telemetry_env: Path) -> None:
    store = LocalTelemetryStore(telemetry_env)
    today = _today()
    d1, d2, d3 = today - timedelta(days=3), today - timedelta(days=2), today - timedelta(days=1)
    for day, count in ((d1, 1), (d2, 2), (d3, 3), (today, 5)):
        for _ in range(count):
            store.write_event(
                event="mcp_tool_called",
                props={"tool_name": "search", "agent_host": "claude", "ok": True, "duration_ms_bucket": "<100"},
                exported=False,
                ts=_ts(day),
            )
    store.write_event(event="not_in_schema", props={}, exported=False, ts=_ts(d2))

    by_day = aggregate_event_rows_by_day(telemetry_env, since_day=d1.isoformat(), today=today.isoformat())

    assert sorted(by_day) == [d2.isoformat(), d3.isoformat()]
    assert by_day[d2.isoformat()] == [
        {"event": "mcp_tool_called", "dim": "search", "context": "claude", "outcome": "ok", "bucket": "<100", "n": 2}
    ]
    assert by_day[d3.isoformat()][0]["n"] == 3


def test_aggregate_keeps_the_busiest_rows_under_the_cap(telemetry_env: Path) -> None:
    store = LocalTelemetryStore(telemetry_env)
    day = _today() - timedelta(days=1)
    events = [
        {"event": "code_symbol_retrieved", "props": {"kind": f"k{i}"}, "exported": False, "ts": _ts(day)}
        for i in range(MAX_ROWS + 10)
    ]
    events += [{"event": "code_symbol_retrieved", "props": {"kind": "hot"}, "exported": False, "ts": _ts(day)}] * 3
    store.write_events(events)

    rows = aggregate_event_rows_by_day(
        telemetry_env, since_day=(day - timedelta(days=1)).isoformat(), today=_today().isoformat()
    )[day.isoformat()]

    assert len(rows) == MAX_ROWS
    assert rows[0]["dim"] == "hot" and rows[0]["n"] == 3


def test_first_flush_only_sets_a_baseline(telemetry_env: Path, tmp_path: Path) -> None:
    result, checkpoint = flush_daily_event_rollup(tmp_path, checkpoint_day=None)
    assert result["reason"] == "baseline"
    assert checkpoint == (_today() - timedelta(days=1)).isoformat()


def test_days_elapsed_while_opted_out_are_skipped_not_backfilled(
    telemetry_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(event_rollup_mod, "post_json", lambda *a, **k: posts.append(a[1]) or 200)
    old = (_today() - timedelta(days=5)).isoformat()

    result, checkpoint = flush_daily_event_rollup(tmp_path, checkpoint_day=old)

    assert result["reason"] == "disabled"
    assert checkpoint == (_today() - timedelta(days=1)).isoformat()
    assert posts == []


def test_flush_posts_hashed_counts_and_handles_rejection_and_outage(
    telemetry_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_remote(monkeypatch)
    store = LocalTelemetryStore(telemetry_env)
    today = _today()
    d1, d2, d3 = today - timedelta(days=3), today - timedelta(days=2), today - timedelta(days=1)
    for day in (d1, d2, d3):
        store.write_event(
            event="session_start",
            props={"agent_host": "claude", "session_id": "raw-session-id", "anon_id": "raw-anon"},
            exported=False,
            ts=_ts(day),
        )
    checkpoint_before = (d1 - timedelta(days=1)).isoformat()

    statuses = {d1.isoformat(): 200, d2.isoformat(): 400, d3.isoformat(): 503}
    posts: list[dict[str, Any]] = []

    def fake_post(
        endpoint: str,
        payload: dict[str, Any],
        *,
        timeout_s: float,
        attempts: int,
        headers: dict[str, str] | None = None,
    ) -> int:
        posts.append(payload)
        return statuses[payload["day"]]

    monkeypatch.setattr(event_rollup_mod, "post_json", fake_post)

    result, checkpoint = flush_daily_event_rollup(tmp_path, checkpoint_day=checkpoint_before)

    # 200 stored, 400 skipped for good, 503 retried later from that day.
    assert result["reason"] == "post_failed" and result["stuck_day"] == d3.isoformat()
    assert checkpoint == d2.isoformat()
    assert [p["day"] for p in posts] == [d1.isoformat(), d2.isoformat(), d3.isoformat()]
    payload = posts[0]
    assert payload["install_key"] == hashlib.sha256(get_anon_id().encode()).hexdigest()
    assert payload["rows"] == [
        {"event": "session_start", "dim": "claude", "context": "", "outcome": "", "bucket": "", "n": 1}
    ]
    wire = json.dumps(posts)
    assert "raw-session-id" not in wire and "raw-anon" not in wire

    statuses[d3.isoformat()] = 200
    result, checkpoint = flush_daily_event_rollup(tmp_path, checkpoint_day=checkpoint)
    assert result == {"flushed": True, "posted_days": 1, "through_day": d3.isoformat()}
    assert checkpoint == d3.isoformat()


def test_maybe_flush_keeps_its_own_schedule_state(telemetry_env: Path, tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    first = maybe_flush_event_rollup(root)
    assert first["reason"] == "baseline"
    state = read_flush_state(root, ("telemetry", "event_rollup_state.json"))
    assert state["checkpoint_day"] == (_today() - timedelta(days=1)).isoformat()
    assert not (root / "telemetry" / "public_rollup_state.json").exists()
    assert maybe_flush_event_rollup(root)["reason"] == "not_due"

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lemoncrow.core.capabilities import plugin_runtime
from lemoncrow.core.capabilities.host_usage import opencode_usage_calls, persist_host_usage
from lemoncrow.core.foundation.paths import session_dir
from lemoncrow.core.service.api import _savings_summary_payload
from lemoncrow.infra.runtime.run_ledger import RunLedger
from lemoncrow.infra.runtime.session_report import list_run_files, list_run_snapshots, load_report
from lemoncrow.pro.capabilities.usage.spend import window_spend


def _call(at: datetime, source_id: str, cost: float = 1.0) -> dict:
    return {
        "at": at.isoformat(),
        "source_id": source_id,
        "model": "gpt-5",
        "input_tokens": 100,
        "output_tokens": 10,
        "cost_usd": cost,
    }


def _write(root: Path, partition: str, calls: list[dict]) -> Path:
    path = root / "sessions" / partition / "claude" / "same" / "run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "session_id": "same",
                "status": "completed",
                "events": [],
                "created_at": calls[0]["at"],
                "updated_at": calls[-1]["at"],
                "cost": {"calls": calls, "total_cost_usd": sum(c["cost_usd"] for c in calls)},
            }
        )
    )
    return path


def test_partitions_merge_before_pagination_and_keep_unique_calls(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    a, b, c = [_call(now - timedelta(hours=3 - i), str(i)) for i in range(3)]
    _write(tmp_path, "2026/09/18", [a, b])
    _write(tmp_path, "2026/09/19", [a, b, c])
    _write(tmp_path, "2026/09/20", [a, b, c])
    assert len(list_run_files(tmp_path)) == 1
    assert len(list_run_snapshots(tmp_path)[0][1]["cost"]["calls"]) == 3
    assert load_report("same", tmp_path).total_cost_usd == 3
    assert window_spend(tmp_path, days=7).total_usd == 3


def test_window_filters_calls_in_resumed_session(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _write(tmp_path, "2026/09/18", [_call(now - timedelta(days=10), "old", 8)])
    _write(tmp_path, "2026/09/20", [_call(now - timedelta(hours=1), "new", 2)])
    assert window_spend(tmp_path, days=7, now=now).total_usd == 2
    assert window_spend(tmp_path, days=30, now=now).total_usd == 10


def test_cost_is_independent_of_savings_and_missing_is_explicit(tmp_path: Path) -> None:
    path = session_dir(tmp_path, "opencode", "missing") / "run.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {"session_id": "missing", "created_at": datetime.now(UTC).isoformat(), "cost": {"calls": []}, "events": []}
        )
    )
    payload = _savings_summary_payload(tmp_path, window_days=7)
    assert payload["spend_available"] is False
    assert payload["savings_available"] is False
    _write(tmp_path, "2026/09/20", [_call(datetime.now(UTC), "measured", 2)])
    payload = _savings_summary_payload(tmp_path, window_days=7)
    assert payload["spend_available"] is True
    assert payload["actually_cost_usd"] == 2
    assert payload["savings_available"] is False


def test_host_usage_is_idempotent_and_survives_mcp_save(tmp_path: Path) -> None:
    calls = [_call(datetime.now(UTC), "turn")]
    persist_host_usage(tmp_path, "codex", "session", calls)
    persist_host_usage(tmp_path, "codex", "session", calls)
    path = session_dir(tmp_path, "codex", "session")
    assert len((path / "savings.jsonl").read_text().splitlines()) == 1
    ledger = RunLedger(root=tmp_path, agent="codex", session_id="session")
    ledger.record_call(operation="internal", model="gpt-5", input_tokens=10, output_tokens=1)
    ledger.persist()
    snapshot = json.loads((path / "run.json").read_text())
    assert len(snapshot["cost"]["calls"]) == 2
    assert sum(c.get("usage_source") == "codex" for c in snapshot["cost"]["calls"]) == 1


def test_codex_session_end_ingests_transcript_and_dedupes_token_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_AGENT", "codex")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.setattr(plugin_runtime, "_capture_codex_comment", lambda *_: None)
    now = datetime.now(UTC)
    transcript = tmp_path / "rollout.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "sid", "cwd": str(tmp_path)}},
        {"type": "turn_context", "payload": {"model": "gpt-5"}},
        {
            "type": "event_msg",
            "timestamp": now.isoformat(),
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 10}},
            },
        },
    ]
    records.append(records[-1])
    transcript.write_text("\n".join(json.dumps(r) for r in records))
    payload = {
        "hook_event_name": "SessionEnd",
        "session_id": "sid",
        "transcript_path": str(transcript),
        "cwd": str(tmp_path),
    }
    plugin_runtime._apply_codex_transcript_snapshot(tmp_path, "other-sid", {**payload, "session_id": "other-sid"})
    assert not (session_dir(tmp_path, "codex", "other-sid") / "run.json").exists()
    plugin_runtime.build_codex_session_end_output(tmp_path, payload)
    run_path = session_dir(tmp_path, "codex", "sid") / "run.json"
    first_run = json.loads(run_path.read_text())
    plugin_runtime.build_codex_session_end_output(tmp_path, payload)
    run = json.loads(run_path.read_text())
    assert run["updated_at"] == first_run["updated_at"]
    assert len(run["cost"]["calls"]) == 1
    assert run["cost"]["calls"][0]["input_tokens"] == 60
    assert run["cost"]["calls"][0]["cache_read_tokens"] == 40
    assert run["cost"]["total_cost_usd"] > 0
    assert run["status"] == "completed"
    assert (session_dir(tmp_path, "codex", "sid") / "savings.jsonl").stat().st_size > 0


def test_opencode_idle_ingests_measured_steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("LEMONCROW_AGENT", "opencode")
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_VERIFY_BEFORE_DONE", "0")
    db = home / ".local/share/opencode/opencode.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE message (id TEXT, session_id TEXT, data TEXT, time_created INTEGER)")
        conn.execute("CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, data TEXT, time_created INTEGER)")
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        conn.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            ("m", "sid", json.dumps({"role": "assistant", "modelID": "gpt-5", "providerID": "openai"}), now_ms),
        )
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            (
                "p",
                "m",
                "sid",
                json.dumps(
                    {"type": "step-finish", "cost": 0.25, "tokens": {"input": 100, "output": 20, "cache": {"read": 30}}}
                ),
                now_ms,
            ),
        )
    assert len(opencode_usage_calls("sid", db)) == 1
    for _ in range(2):
        plugin_runtime.build_opencode_stop_output(tmp_path, {"session_id": "sid", "cwd": str(tmp_path)})
    path = session_dir(tmp_path, "opencode", "sid")
    run = json.loads((path / "run.json").read_text())
    assert len(run["cost"]["calls"]) == 1
    assert run["cost"]["total_cost_usd"] == 0.25
    assert (path / "savings.jsonl").stat().st_size > 0

    assert all(json.loads(line)["kind"] == "session_end" for line in (path / "savings.jsonl").read_text().splitlines())


def test_duplicate_partitions_preserve_identical_calls_within_a_partition(tmp_path: Path) -> None:
    call = _call(datetime.now(UTC), "unused")
    call.pop("source_id")
    _write(tmp_path, "2026/09/18", [call, call])
    _write(tmp_path, "2026/09/19", [call, call])
    assert len(list_run_snapshots(tmp_path)[0][1]["cost"]["calls"]) == 2
    assert load_report("same", tmp_path).total_cost_usd == 2


def test_unpriced_host_usage_remains_unknown(tmp_path: Path) -> None:
    call = _call(datetime.now(UTC), "unpriced")
    call.pop("cost_usd")
    call["model"] = "unlisted-host-model"
    persist_host_usage(tmp_path, "codex", "session", [call])
    payload = _savings_summary_payload(tmp_path, window_days=7)
    assert payload["spend_available"] is False
    assert payload["spend"]["unpriced_rows"] == 1
    snapshot = json.loads((session_dir(tmp_path, "codex", "session") / "run.json").read_text())
    assert "cost_usd" not in snapshot["cost"]["calls"][0]


def test_runs_api_dedupes_before_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from lemoncrow.core.service.api import create_app

    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "0")
    monkeypatch.setattr("lemoncrow.core.service.savings_reconcile.start_savings_reconciler", lambda *_: None)
    calls = [_call(datetime.now(UTC) - timedelta(hours=1), "one")]
    for date in ("2026/09/18", "2026/09/19", "2026/09/20"):
        _write(tmp_path, date, calls)
    path = session_dir(tmp_path, "codex", "other") / "run.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "session_id": "other",
                "created_at": calls[0]["at"],
                "updated_at": calls[0]["at"],
                "cost": {"calls": [], "total_cost_usd": 0},
                "events": [],
            }
        )
    )
    with TestClient(create_app(store_root=tmp_path)) as client:
        response = client.get("/v1/sessions?since=36500d&limit=2")
        assert response.status_code == 200
        rows = response.json()
    assert {row["session_id"] for row in rows} == {"same", "other"}
    assert sum(row["total_cost_usd"] for row in rows) == 1


def test_insights_include_only_resumed_session_calls_in_window(tmp_path: Path) -> None:
    from lemoncrow.pro.runtime.insights import build_insights

    now = datetime.now(UTC)
    _write(tmp_path, "2026/09/18", [_call(now - timedelta(days=10), "old", 8)])
    _write(tmp_path, "2026/09/20", [_call(now - timedelta(hours=1), "new", 2)])
    insights = build_insights(tmp_path, since=now - timedelta(days=7), until=now)
    assert insights.session_count == 1
    assert insights.total_cost_usd == 2

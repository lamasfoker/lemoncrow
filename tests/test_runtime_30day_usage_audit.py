"""Local 30-day usage audit never counts streaming snapshots or copied sessions twice."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from scripts.runtime_30day_usage_audit import analyze

START = date(2026, 9, 11)
END = date(2026, 10, 10)


def _write(path: Path, *events: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def _message(message_id: str, day: str, *, model: str = "claude-opus-5-5", output: int = 100) -> dict:
    return {
        "type": "assistant",
        "timestamp": f"{day}T12:00:00Z",
        "message": {
            "id": message_id,
            "model": model,
            "usage": {
                "input_tokens": 100,
                "cache_read_input_tokens": 500,
                "cache_creation_input_tokens": 200,
                "output_tokens": output,
                "cache_creation": {"ephemeral_1h_input_tokens": 50},
            },
        },
    }


def test_deduplicates_streaming_snapshots_and_cross_file_copies(tmp_path: Path) -> None:
    parent = tmp_path / "-home-lemoncrow-dev" / "root.jsonl"
    child = tmp_path / "-home-lemoncrow-dev" / "subagents" / "agent-one.jsonl"
    _write(parent, _message("message-a", "2026-09-11", output=10), _message("message-a", "2026-09-11", output=100))
    _write(child, _message("message-a", "2026-09-11", output=100), _message("message-b", "2026-10-10", output=200))
    report = analyze(tmp_path, start=START, end=END)
    assert report["unique_provider_messages"] == 2
    assert report["duplicate_stream_events_ignored"] == 2
    assert report["periods"]["first_15_days.all"]["calls"] == 1
    assert report["periods"]["first_15_days.all"]["fresh_input_tokens"] == 300
    assert report["periods"]["last_15_days.subagent"]["calls"] == 1
    assert report["periods"]["last_15_days.all"]["output_tokens"] == 200
    assert report["cost_basis"].endswith("NOT_actual_bills_or_subscription_credits")
    assert "message-a" not in json.dumps(report)


def test_does_not_report_unpriced_provider_usage_as_free(tmp_path: Path) -> None:
    _write(
        tmp_path / "lemoncrow-dev" / "root.jsonl", _message("unknown", "2026-09-24", model="unpriced-brand-new-model")
    )
    report = analyze(tmp_path, start=START, end=END)
    old = report["periods"]["first_15_days.all"]
    assert old["calls"] == 1
    assert old["unpriced_calls"] == 1
    assert "estimated_cost_usd" not in old
    assert report["unpriced_models"] == {"unpriced-brand-new-model": 1}


def test_rejects_incompatible_period_and_unrelated_projects(tmp_path: Path) -> None:
    _write(tmp_path / "unrelated-project" / "root.jsonl", _message("x", "2026-09-15"))
    report = analyze(tmp_path, start=START, end=END)
    assert report["unique_provider_messages"] == 0
    assert report["data_available"] is False
    assert "cost is unknown" in report["warnings"][0]
    with pytest.raises(ValueError, match="exactly 30"):
        analyze(tmp_path, start=START, end=date(2026, 10, 11))


def test_non_provider_events_are_ignored(tmp_path: Path) -> None:
    file = tmp_path / "-lemoncrow" / "root.jsonl"
    _write(
        file,
        {
            "type": "assistant",
            "timestamp": "2026-09-11T12:00:00Z",
            "message": {"id": "missing", "model": "claude-opus-5-5"},
        },
        _message("synthetic", "2026-09-11", model="<synthetic>"),
        _message("outside", "2026-09-10"),
        _message("included", "2026-09-11"),
    )
    assert analyze(tmp_path, start=START, end=END)["unique_provider_messages"] == 1

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from lemoncrow.core.foundation.models import Trace, UsageEntry
from lemoncrow.gateway.cli.commands import sessions


def _trace(*, reported_cost: float = 12.34) -> Trace:
    return Trace(
        id="trace-cost",
        session_id="session-cost",
        agent="claude",
        host="claude",
        domain="coding",
        task="measure cost",
        status="success",
        model="claude-opus-5-5",
        input_tokens=1_000,
        cached_input_tokens=100_000,
        cache_creation_input_tokens=5_000,
        output_tokens=2_000,
        usage_entries=[
            UsageEntry(
                model="claude-opus-5-5",
                input_tokens=1_000,
                cached_input_tokens=100_000,
                cache_creation_input_tokens=5_000,
                output_tokens=2_000,
                cost_usd=99.0,
            )
        ],
        telemetry={"host_reported_cost_usd": reported_cost},
        created_at=datetime(2026, 10, 4, tzinfo=UTC),
    )


def test_best_trace_cost_prefers_host_reported_total() -> None:
    trace = _trace(reported_cost=12.34)

    chosen, reported, estimated = sessions._best_trace_cost(trace)

    assert reported == pytest.approx(12.34)
    assert estimated > 0
    assert chosen == pytest.approx(reported)
    assert chosen != pytest.approx(sessions._trace_cost_usd(trace))


def test_claude_row_keeps_reported_cost_and_marks_savings_estimated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = _trace(reported_cost=12.34)
    monkeypatch.setattr(sessions, "_claude_subagent_count", lambda _sid: 0)
    monkeypatch.setattr(sessions, "_claude_subagent_cost_usd", lambda _sid: 0.0)
    monkeypatch.setattr(sessions, "_claude_transcript_block", lambda _sid: None)
    monkeypatch.setattr(
        sessions,
        "_claude_live_savings_summary",
        lambda _sid, _root: (2.0, 1_000, 3, 1.0, 5_000, 99.0, 0.0),
    )

    # raw_artifact_ids is empty, so _host_subagent_count never dereferences store.
    row = sessions._build_session_row(trace, None, "claude", tmp_path)  # type: ignore[arg-type]

    assert row["cost_usd"] == pytest.approx(12.34)
    assert row["reported_cost_usd"] == pytest.approx(12.34)
    assert row["estimated_cost_usd"] != pytest.approx(99.0)
    assert sum(
        row[key] for key in ("cost_input_usd", "cost_cache_read_usd", "cost_cache_write_usd", "cost_output_usd")
    ) == pytest.approx(12.34, abs=1e-5)
    assert row["saved_usd"] == pytest.approx(2.0)
    assert row["carry_usd"] == pytest.approx(1.0)
    assert row["savings_estimated"] is True


def _host_trace(host: str, *, with_usage: bool = True, telemetry: dict | None = None) -> Trace:
    usage = (
        [
            UsageEntry(
                model="gpt-6.1-sol" if host == "codex" else "opencode/big-pickle",
                input_tokens=1_000,
                cached_input_tokens=10_000,
                output_tokens=200,
                cost_usd=0.99,
            )
        ]
        if with_usage
        else []
    )
    model = "gpt-6.1-sol" if host == "codex" else "opencode/big-pickle" if host == "opencode" else "cursor/unknown"
    return Trace(
        id=f"{host}-trace",
        session_id=f"{host}-session",
        agent="lemoncrow:code",
        host=host,
        domain="coding",
        task="measure provenance",
        status="success",
        model=model,
        input_tokens=1_000 if with_usage else 0,
        cached_input_tokens=10_000 if with_usage else 0,
        output_tokens=200 if with_usage else 0,
        usage_entries=usage,
        telemetry=telemetry or {},
        created_at=datetime(2026, 10, 4, tzinfo=UTC),
    )


@pytest.mark.parametrize("host", ["codex", "opencode"])
def test_measured_token_hosts_label_cost_as_priced(host: str, tmp_path: Path) -> None:
    trace = _host_trace(host, telemetry={"usage_measurement_source": "host_transcript"})

    row = sessions._build_session_row(trace, None, host, tmp_path)  # type: ignore[arg-type]

    assert row["usage_measurement_source"] == "host_transcript"
    assert row["actual_cost_source"] == "priced"
    assert row["cost_status"] == "estimated"
    assert row["cost_usd"] > 0
    assert row["reported_cost_usd"] == 0
    assert row["savings_estimated"] is False


def test_cursor_without_host_usage_is_explicitly_unavailable(tmp_path: Path) -> None:
    trace = _host_trace("cursor", with_usage=False, telemetry={"usage_measurement_source": "unavailable"})

    row = sessions._build_session_row(trace, None, "cursor", tmp_path)  # type: ignore[arg-type]

    assert row["usage_measurement_source"] == "unavailable"
    assert row["actual_cost_source"] == "unavailable"
    assert row["cost_status"] == "unavailable"
    assert row["cost_usd"] == 0


def test_authoritative_zero_cost_is_not_replaced_by_pricing(tmp_path: Path) -> None:
    trace = _host_trace(
        "codex",
        telemetry={
            "usage_measurement_source": "host_transcript",
            "actual_cost_source": "host_reported",
            "actual_cost_usd": 0.0,
        },
    )

    row = sessions._build_session_row(trace, None, "codex", tmp_path)  # type: ignore[arg-type]

    assert row["actual_cost_source"] == "host_reported"
    assert row["cost_status"] == "recorded"
    assert row["cost_usd"] == 0.0
    assert row["estimated_cost_usd"] > 0

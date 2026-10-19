"""Closed live route outcomes persist into the learned-routing tables."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.telemetry_store import ROUTE_OUTCOME_RETENTION_DAYS, TelemetryStore
from lemoncrow.pro.capabilities.model_routing.success_predictor import (
    build_success_table,
    features_from_state,
    load_outcome_rows,
    outcome_label,
    persistable_route_outcome,
    telemetry_route_sink,
    verb_bucket,
)
from lemoncrow.pro.runtime import outcome_capture


def _db(root: Path) -> Path:
    return root / "lemoncrow_telemetry.db"


def _schedule(session_id: str, writer: Any, *, task_text: str, tool: str = "edit") -> str:
    return outcome_capture.schedule_route(
        session_id=session_id,
        tool=tool,
        recommended_tier="cheap",
        recommended_model="claude-haiku-4-5",
        recommendation_followed=True,
        scored_state={
            "turn_number": 3,
            "prior_errors": 0,
            "task_verb": verb_bucket(task_text),
            "retrieval_status": "useful",
            "retrieval_confidence": 0.8,
        },
        writer=writer,
    )


def test_closed_route_window_round_trips_to_predictor_key(tmp_path: Path) -> None:
    session_id = f"s-{uuid.uuid4().hex}"
    writer = outcome_capture.FileStateWriter(tmp_path / "outcomes.json", route_sink=telemetry_route_sink(tmp_path))
    task_text = "fix the failing parser test"
    _schedule(session_id, writer, task_text=task_text)

    for _ in range(4):
        outcome_capture.advance(session_id, tool_name="bash", writer=writer)
    assert load_outcome_rows(_db(tmp_path)) == []  # window still open

    outcome_capture.advance(session_id, tool_name="bash", writer=writer)
    rows = load_outcome_rows(_db(tmp_path))

    assert len(rows) == 1
    assert rows[0].success is True
    live = features_from_state("edit", task_text, {"prior_errors": 0})
    assert rows[0].features == live
    assert build_success_table(rows).probability(live, min_samples=1) == 1.0

    # More turns never re-persist an already-closed window.
    outcome_capture.advance(session_id, tool_name="bash", writer=writer)
    with sqlite3.connect(_db(tmp_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM verification_envelope").fetchone()[0] == 1
        reason = conn.execute("SELECT reason FROM route_decision").fetchone()[0]
    assert "retrieval=useful" in reason


def test_close_session_persists_partial_window(tmp_path: Path) -> None:
    session_id = f"s-{uuid.uuid4().hex}"
    writer = outcome_capture.FileStateWriter(tmp_path / "outcomes.json", route_sink=telemetry_route_sink(tmp_path))
    _schedule(session_id, writer, task_text="read the config")

    outcome_capture.close_session(session_id, writer=writer)

    assert len(load_outcome_rows(_db(tmp_path))) == 1


def test_sink_failure_does_not_break_capture(tmp_path: Path) -> None:
    session_id = f"s-{uuid.uuid4().hex}"

    def broken(_sid: str, _outcome: dict[str, Any]) -> None:
        raise RuntimeError("db down")

    writer = outcome_capture.FileStateWriter(tmp_path / "outcomes.json", route_sink=broken)
    _schedule(session_id, writer, task_text="fix it")
    for _ in range(5):
        outcome_capture.advance(session_id, tool_name="bash", writer=writer)

    assert outcome_capture.get_outcomes(session_id)["route_outcomes"][0]["outcome_window"] is not None


def test_outcome_labels_and_open_windows() -> None:
    assert outcome_label(1.0) == "pass"
    assert outcome_label(0.8) == "pass"
    assert outcome_label(0.6) == "warn"
    assert outcome_label(0.4) == "fail"
    assert persistable_route_outcome("s", {"decision_id": "d", "outcome_window": None}) is None


def test_many_prior_errors_maps_to_escalation_bucket(tmp_path: Path) -> None:
    rows = persistable_route_outcome(
        "s",
        {
            "decision_id": "d1",
            "tool": "edit",
            "scored_state": {"prior_errors": 5, "task_verb": "edit"},
            "outcome_window": {"outcome_score": 0.2, "captured_at": datetime.now(UTC).isoformat()},
        },
    )
    assert rows is not None
    store = TelemetryStore(tmp_path)
    store.init()
    store.persist_route_outcome(*rows)

    [row] = load_outcome_rows(_db(tmp_path))
    assert row.success is False
    assert row.features == features_from_state("edit", "fix", {"prior_errors": 5})


def test_retention_prunes_old_route_outcomes(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path)
    store.init()
    old = (datetime.now(UTC) - timedelta(days=ROUTE_OUTCOME_RETENTION_DAYS + 1)).isoformat()
    new = datetime.now(UTC).isoformat()

    for decision_id, at in (("old", old), ("new", new)):
        rows = persistable_route_outcome(
            "s",
            {"decision_id": decision_id, "tool": "edit", "outcome_window": {"outcome_score": 1.0, "captured_at": at}},
        )
        assert rows is not None
        store.persist_route_outcome(*rows)

    with sqlite3.connect(_db(tmp_path)) as conn:
        assert [r[0] for r in conn.execute("SELECT id FROM route_decision")] == ["new"]
        assert [r[0] for r in conn.execute("SELECT route_decision_id FROM verification_envelope")] == ["new"]

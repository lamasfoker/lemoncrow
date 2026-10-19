"""IR-5 shadow feed: code_search EvidenceState reaches the live route decision."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from lemoncrow.gateway.tools import model_routing
from lemoncrow.infra.runtime.run_ledger import RunLedger
from lemoncrow.pro.capabilities.code_context.evidence_state import EvidenceState


class _Decision:
    mode = "auto"
    provider = "anthropic"
    model = "claude-sonnet-4.6"

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "provider": self.provider, "model": self.model, "tier": "medium"}


def _hooks(seen: list[dict[str, Any]]) -> model_routing.ModelRoutingHooks:
    def select(**kwargs: Any) -> _Decision:
        seen.append(dict(kwargs["session_state"]))
        return _Decision()

    return model_routing.ModelRoutingHooks(
        read_workspace_state=dict,
        write_workspace_state=lambda _state: None,
        state_lock=threading.Lock(),
        select_owned_execution_route=select,
        detect_agent=lambda: "claude",
        append_live_savings_event=lambda _event: None,
        get_host_session_sidecar_path=lambda: None,
        make_outcome_writer=lambda _led: None,
        latest_cache_affinity_model=lambda _led: None,
        ledger_turn_count=lambda _led: 1,
    )


def _record_evidence(led: RunLedger, status: str, confidence: float) -> None:
    state = EvidenceState(status=status, confidence=confidence)  # type: ignore[arg-type]
    led.record_runtime_decision(state.to_runtime_event(session_id="s1"))


def test_latest_evidence_feeds_route_state_and_is_attributed(tmp_path: Path) -> None:
    led = RunLedger(session_id="s1", agent="claude", root=tmp_path)
    _record_evidence(led, "useful", 0.8)
    _record_evidence(led, "dark", 0.0)
    seen: list[dict[str, Any]] = []

    payload, state, _workflow, _step = model_routing.prepare_model_recommendation(
        _hooks(seen), "edit", {"task": "fix it"}, led
    )

    assert seen[0]["retrieval_status"] == "dark"
    assert seen[0]["retrieval_confidence"] == 0.0
    assert state["retrieval_status"] == "dark"
    assert payload["retrieval_status"] == "dark"
    assert payload["model"] == "claude-sonnet-4.6"  # shadow: route unchanged
    route_event = [e for e in led.events if e.kind == "route_decision"][-1]
    assert route_event.payload["retrieval_status"] == "dark"


def test_no_evidence_leaves_route_state_untouched(tmp_path: Path) -> None:
    led = RunLedger(session_id="s1", agent="claude", root=tmp_path)
    seen: list[dict[str, Any]] = []

    payload, state, _workflow, _step = model_routing.prepare_model_recommendation(
        _hooks(seen), "edit", {"task": "fix it"}, led
    )

    assert "retrieval_status" not in state
    assert "retrieval_status" not in payload
    route_event = [e for e in led.events if e.kind == "route_decision"][-1]
    assert "retrieval_status" not in route_event.payload


def test_evidence_outside_recency_window_is_stale(tmp_path: Path) -> None:
    led = RunLedger(session_id="s1", agent="claude", root=tmp_path)
    _record_evidence(led, "useful", 0.8)
    for index in range(model_routing._EVIDENCE_LOOKBACK_EVENTS):
        led.record("note", f"filler {index}", {})

    assert model_routing.latest_retrieval_evidence(led) == {}

    _record_evidence(led, "ambiguous", 0.5)
    assert model_routing.latest_retrieval_evidence(led) == {
        "retrieval_status": "ambiguous",
        "retrieval_confidence": 0.5,
    }

"""Tests for complexity-scored model-tier routing (Workstream 6 / N1)."""

from __future__ import annotations

from lemoncrow.pro.capabilities.model_routing import (
    ComplexitySignals,
    ModelRouter,
    complexity_score,
    signals_from_state,
    tier_for_complexity,
    tier_routing_enabled,
)

# --- pure complexity score -------------------------------------------------


# --- tier mapping: simple -> cheap, complex -> strong ----------------------


# --- escalation never downgrades hard work ---------------------------------


def test_signals_from_state_uses_collection_lengths() -> None:
    sig = signals_from_state(
        "do the thing",
        {"refs": ["a", "b", "c"], "symbols": ["x", "y"], "changed_files": ["f1.py", "f2.py"]},
    )
    assert sig.retrieval_set_size == 3
    assert sig.symbol_count == 2
    assert sig.cross_file_count == 2


# --- default-off preserves the current routing decision --------------------


def test_tier_routing_off_by_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("LEMONCROW_TIER_ROUTING", raising=False)
    assert tier_routing_enabled({}) is False
    assert tier_routing_enabled(None) is False


def test_default_off_leaves_baseline_decision_unchanged(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("LEMONCROW_TIER_ROUTING", raising=False)
    # Heavy cross-file signals that WOULD push to strong if routing were on.
    state = {
        "prior_errors": 0,
        "refs": list(range(20)),
        "symbols": list(range(20)),
        "changed_files": [f"f{i}.py" for i in range(20)],
    }
    rec = ModelRouter().score("read", "explain this function briefly", state)
    # Baseline (read + explain) is cheap; default-off must NOT escalate it.
    assert rec is not None
    assert rec.tier == "cheap"
    assert not any("tier_routing" in r for r in rec.reasons)


# --- opt-in integration through ModelRouter.score --------------------------


def test_opt_in_steps_up_simple_baseline_for_complex_signals(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("LEMONCROW_TIER_ROUTING", raising=False)
    state = {
        "tier_routing": True,
        "prior_errors": 0,
        "refs": list(range(12)),
        "symbols": list(range(10)),
        "changed_files": [f"f{i}.py" for i in range(6)],
        "task_size_chars": 1_300,
    }
    rec = ModelRouter().score("read", "explain this function briefly", state)
    assert rec is not None
    # A read/explain turn baselines cheap, but heavy complexity steps it up.
    assert rec.tier == "expensive"
    assert any("step up" in r for r in rec.reasons)


def test_opt_in_never_downgrades_genuinely_hard_work_via_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LEMONCROW_TIER_ROUTING", "1")
    # Architectural agent task with errors baselines expensive.  Even with tier
    # routing ON and no extra complexity signals, it must stay expensive.
    rec = ModelRouter().score("Agent", "design an end-to-end migration plan", {"prior_errors": 3})
    assert rec is not None
    assert rec.tier == "expensive"
    assert "opus" in rec.model


def test_opt_in_silent_step_down_for_simple_work(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LEMONCROW_TIER_ROUTING", "1")
    # An edit verb baselines medium; with trivial complexity signals and no
    # risk flags it should silently step down toward cheap.
    state = {"prior_errors": 0, "refs": [], "changed_files": ["only.py"], "task_size_chars": 20}
    rec = ModelRouter().score("edit", "fix a typo", state)
    assert rec is not None
    assert rec.tier == "cheap"
    assert any("step down" in r for r in rec.reasons)


def test_complexity_score_contract() -> None:
    assert complexity_score(ComplexitySignals()) == 0
    huge = ComplexitySignals(
        retrieval_set_size=10_000,
        symbol_count=10_000,
        cross_file_count=10_000,
        task_size_chars=1_000_000,
        prior_errors=999,
    )
    assert complexity_score(huge) == 100
    assert complexity_score(ComplexitySignals(cross_file_count=4)) > complexity_score(
        ComplexitySignals(cross_file_count=1)
    )
    signals = ComplexitySignals(retrieval_set_size=5, symbol_count=4, cross_file_count=3, task_size_chars=400)
    assert complexity_score(signals) == complexity_score(signals)


def test_complexity_tier_mapping_contract() -> None:
    cases = [
        (ComplexitySignals(retrieval_set_size=1, task_size_chars=50), "cheap", "cheap"),
        (
            ComplexitySignals(retrieval_set_size=6, symbol_count=4, cross_file_count=3, task_size_chars=300),
            "standard",
            "medium",
        ),
        (
            ComplexitySignals(retrieval_set_size=12, symbol_count=10, cross_file_count=6, task_size_chars=1_200),
            "strong",
            "expensive",
        ),
    ]
    for signals, tier, model_tier in cases:
        result = tier_for_complexity(signals)
        assert (result.tier, result.model_tier) == (tier, model_tier), signals


def test_complexity_escalation_contract() -> None:
    escalated = tier_for_complexity(ComplexitySignals(task_size_chars=10, escalate=True))
    assert escalated.tier == "strong" and escalated.stepped_up and any("escalate" in r for r in escalated.reasons)
    cross_project = tier_for_complexity(ComplexitySignals(task_size_chars=10, cross_project=True))
    assert cross_project.tier == "strong" and cross_project.stepped_up
    repeated = tier_for_complexity(ComplexitySignals(task_size_chars=10, prior_errors=3))
    assert repeated.tier in {"standard", "strong"}

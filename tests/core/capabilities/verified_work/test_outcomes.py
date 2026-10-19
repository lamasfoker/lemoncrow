from __future__ import annotations

import json
from pathlib import Path

from lemoncrow.pro.capabilities.verified_work.outcomes import (
    load_model_outcome,
    outcome_path,
    record_model_outcome,
)


def test_outcome_store_aggregates_without_content(tmp_path: Path) -> None:
    model = "custom/local/qwen"
    record_model_outcome(
        model,
        "typecheck",
        verified=True,
        promoted=False,
        latency_ms=100,
        input_tokens=20,
        output_tokens=10,
        provider_cost_usd=0.0,
        mode="shadow",
        root=tmp_path,
    )
    stats = record_model_outcome(
        model,
        "typecheck",
        verified=False,
        promoted=False,
        latency_ms=300,
        input_tokens=30,
        output_tokens=15,
        provider_cost_usd=None,
        mode="enforce",
        root=tmp_path,
    )

    assert stats.attempts == 2
    assert stats.verified == 1
    assert stats.verification_rate == 0.5
    assert stats.average_latency_ms == 200.0
    assert stats.input_tokens == 50
    assert stats.output_tokens == 25
    assert stats.shadow_attempts == 1
    assert stats.enforce_attempts == 1
    assert stats.shadow_verified == 1
    assert stats.enforce_verified == 0
    assert load_model_outcome(model, "typecheck", root=tmp_path) == stats

    raw = json.loads(outcome_path(tmp_path).read_text(encoding="utf-8"))
    text = json.dumps(raw)
    assert "prompt" not in text
    assert "source" not in text
    assert "content" not in text


def test_outcomes_are_separate_by_repair_kind(tmp_path: Path) -> None:
    model = "custom/local/qwen"
    record_model_outcome(
        model,
        "lint",
        verified=True,
        promoted=True,
        latency_ms=50,
        input_tokens=1,
        output_tokens=1,
        provider_cost_usd=0.0,
        mode="enforce",
        root=tmp_path,
    )

    assert load_model_outcome(model, "lint", root=tmp_path).attempts == 1
    assert load_model_outcome(model, "typecheck", root=tmp_path).attempts == 0


def test_summary_reports_observed_facts_without_claiming_savings(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.verified_work.outcomes import summarize_outcomes

    record_model_outcome(
        "custom/local/qwen",
        "typecheck",
        verified=True,
        promoted=True,
        latency_ms=250,
        input_tokens=100,
        output_tokens=25,
        provider_cost_usd=0.0,
        mode="enforce",
        root=tmp_path,
    )
    report = summarize_outcomes(root=tmp_path)
    totals = report["totals"]
    assert totals["attempts"] == 1
    assert totals["verified"] == 1
    assert totals["promoted"] == 1
    assert totals["enforce_attempts"] == 1
    assert totals["input_tokens"] == 100
    assert report["estimated_frontier_savings"] is None
    assert report["savings_status"] == "not_qualified_without_paired_baseline"


def test_recent_window_overrides_stale_lifetime_success_for_qualification(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.verified_work.outcomes import qualification_model_outcome

    model = "custom/local/qwen"
    # Strong historical record.
    for _ in range(40):
        record_model_outcome(
            model,
            "typecheck",
            verified=True,
            promoted=True,
            latency_ms=100,
            input_tokens=1,
            output_tokens=1,
            provider_cost_usd=0.0,
            mode="enforce",
            root=tmp_path,
        )
    # Runtime/model update regresses the most recent 20 attempts.
    for _ in range(20):
        record_model_outcome(
            model,
            "typecheck",
            verified=False,
            promoted=False,
            latency_ms=100,
            input_tokens=1,
            output_tokens=1,
            provider_cost_usd=0.0,
            mode="enforce",
            root=tmp_path,
        )

    lifetime = load_model_outcome(model, "typecheck", root=tmp_path)
    qualified = qualification_model_outcome(model, "typecheck", root=tmp_path, min_samples=20)

    assert lifetime.verification_rate == 40 / 60
    assert qualified.attempts == 20
    assert qualified.verification_rate == 0.0

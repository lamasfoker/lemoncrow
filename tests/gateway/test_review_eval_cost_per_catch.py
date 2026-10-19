"""Gate I cost per catch (G3-GATE-I): cost per weighted catch replaces the flat cost-rise rule; approval binds values."""

from __future__ import annotations

import dataclasses
import json
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from lemoncrow.pro.capabilities.review.evaluation import metrics as metrics_module
from lemoncrow.pro.capabilities.review.evaluation.corpus import EvalCase, digest, load_corpus
from lemoncrow.pro.capabilities.review.evaluation.fixtures import RecordedWith, Responder, SyntheticReviewer
from lemoncrow.pro.capabilities.review.evaluation.gate import (
    APPROVED,
    PROPOSED_THRESHOLDS,
    Breach,
    GateError,
    Thresholds,
    compare,
    cost_per_review_changes,
    evaluate_gate,
    load_gate_config,
    thresholds_digest,
    update_baseline,
)
from lemoncrow.pro.capabilities.review.evaluation.harness import record_corpus, replay_corpus
from lemoncrow.pro.capabilities.review.evaluation.metrics import case_cost, provider_usd_status
from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallUsage, StructuredExecution

REPO = Path(__file__).resolve().parents[2]
GATE_DIR = REPO / "evals" / "review"
BUILTIN = GATE_DIR / "corpus" / "builtin"


def _thresholds(**overrides: float) -> Thresholds:
    return Thresholds(**{**PROPOSED_THRESHOLDS, **overrides})


def _row(
    *,
    defects: int = 1,
    behavior: int = 1,
    calls: float | None = 1,
    usd: float | None = None,
    usd_status: str = "unreported",
    compute: float | None = 0.0,
    minutes: float = 1.0,
) -> dict[str, Any]:
    """A gate snapshot row; catch and noise rates are left unmeasured so only cost rules can breach.

    *compute* is the corpus total of planned compute minutes (cost per catch); *minutes* is the
    per-Review figure (reported only). Compute defaults to zero so other tests isolate their unit.
    """
    return {
        "catch_rate": None,
        "behavior_catch_rate": None,
        "noise_rate": 0.0,
        "caught": [f"case#defect-{index}" for index in range(defects)],
        "behavior_caught": [f"case#behavior-{index}" for index in range(behavior)],
        "missed_behavior": [],
        "false_findings": [],
        "cost_per_review": {
            "estimated_compute_minutes": minutes,
            "model_calls": None if calls is None else calls / 10,
            "provider_usd": usd,
        },
        "cost_total": {
            "estimated_compute_minutes": compute,
            "model_calls": calls,
            "provider_api_calls": calls,
            "provider_usd": usd,
            "provider_usd_status": usd_status,
        },
        "cases": [f"case-{index}" for index in range(10)],
    }


def _cost_breaches(before: Mapping[str, Any], after: Mapping[str, Any], **overrides: float) -> dict[str, Breach]:
    breaches = compare({"selections": {"auto": before}}, {"selections": {"auto": after}}, _thresholds(**overrides))
    return {item.metric: item for item in breaches if item.metric.startswith("cost")}


def test_cost_per_catch_threshold_contract() -> None:
    """case: test_model_calls_per_catch_at_the_threshold_pass_and_just_past_it_block"""
    assert _cost_breaches(_row(defects=5, behavior=5, calls=10), _row(defects=5, behavior=5, calls=11)) == {}
    assert (
        _cost_breaches(_row(defects=500, behavior=500, calls=1000), _row(defects=500, behavior=500, calls=1100)) == {}
    )
    past = _cost_breaches(_row(defects=500, behavior=500, calls=1000), _row(defects=500, behavior=500, calls=1101))
    assert set(past) == {"cost_per_catch.model_calls"}
    breach = past["cost_per_catch.model_calls"]
    assert (breach.baseline, breach.current, breach.threshold) == (1.0, 1.101, 0.1)
    assert breach.detail == "rose 10.1%"
    "case: test_provider_dollars_per_catch_at_the_threshold_pass_and_just_past_it_block"
    known = {"usd_status": "known"}
    assert _cost_breaches(_row(calls=2, usd=1.0, **known), _row(calls=2, usd=1.1, **known)) == {}
    past = _cost_breaches(_row(calls=2, usd=1.0, **known), _row(calls=2, usd=1.1000001, **known))
    assert set(past) == {"cost_per_catch.provider_usd"}
    assert past["cost_per_catch.provider_usd"].detail == "rose 10.0%"
    "case: test_the_configured_threshold_is_the_limit"
    before, after = (_row(defects=5, behavior=5, calls=10), _row(defects=5, behavior=5, calls=12))
    assert set(_cost_breaches(before, after)) == {"cost_per_catch.model_calls"}
    assert _cost_breaches(before, after, cost_per_catch_rise_max=0.2) == {}
    assert set(_cost_breaches(before, after, cost_per_catch_rise_max=0.19)) == {"cost_per_catch.model_calls"}
    "case: test_more_calls_that_buy_proportionally_more_catches_pass"
    assert _cost_breaches(_row(defects=4, behavior=5, calls=6), _row(defects=6, behavior=5, calls=8)) == {}
    thorough = _cost_breaches(_row(defects=5, behavior=5, calls=15), _row(defects=7, behavior=5, calls=20))
    assert set(thorough) == {"cost_per_catch.model_calls"}
    assert (thorough["cost_per_catch.model_calls"].baseline, thorough["cost_per_catch.model_calls"].current) == (
        1.5,
        1.666667,
    )
    assert thorough["cost_per_catch.model_calls"].detail == "rose 11.1%"
    "case: test_catches_are_weighted_by_the_configured_kind_weights"
    before, after = (_row(defects=2, behavior=2, calls=4), _row(defects=2, behavior=3, calls=5))
    assert _cost_breaches(before, after) == {}
    weighted = _cost_breaches(before, after, defect_catch_weight=3.0)
    assert weighted["cost_per_catch.model_calls"].detail == "rose 11.1%"


def test_cost_per_catch_edge_contract() -> None:
    """case: test_cost_with_no_catches_blocks_unless_the_baseline_also_caught_nothing_and_cost_did_not_rise"""
    nothing = _cost_breaches(_row(defects=1, behavior=0, calls=4), _row(defects=0, behavior=0, calls=1))
    assert nothing["cost_per_catch.model_calls"].current == "infinite"
    assert nothing["cost_per_catch.model_calls"].detail == "cost with no catches"
    assert _cost_breaches(_row(defects=0, behavior=0, calls=4), _row(defects=0, behavior=0, calls=4)) == {}
    assert _cost_breaches(_row(defects=0, behavior=0, calls=4), _row(defects=0, behavior=0, calls=3)) == {}
    rose = _cost_breaches(_row(defects=0, behavior=0, calls=4), _row(defects=0, behavior=0, calls=5))
    assert rose["cost_per_catch.model_calls"].detail == "cost with no catches"
    assert _cost_breaches(_row(defects=1, behavior=0, calls=4), _row(defects=0, behavior=0, calls=0)) == {}
    "case: test_zero_baseline_catches"
    assert _cost_breaches(_row(defects=0, behavior=0, calls=4), _row(defects=1, behavior=0, calls=40)) == {}
    from_zero = _cost_breaches(_row(defects=0, behavior=0, calls=0), _row(defects=1, behavior=0, calls=1))
    assert from_zero["cost_per_catch.model_calls"].detail == "rose from zero"
    assert from_zero["cost_per_catch.model_calls"].baseline == 0.0
    still_nothing = _cost_breaches(_row(defects=0, behavior=0, calls=0), _row(defects=0, behavior=0, calls=1))
    assert still_nothing["cost_per_catch.model_calls"].detail == "cost with no catches"
    "case: test_zero_cost"
    assert _cost_breaches(_row(calls=0), _row(calls=0)) == {}
    assert _cost_breaches(_row(calls=3), _row(calls=0)) == {}
    from_zero = _cost_breaches(_row(defects=0, behavior=5, calls=0), _row(defects=1, behavior=5, calls=1))
    assert from_zero["cost_per_catch.model_calls"].detail == "rose from zero"
    "case: test_unknown_or_foreign_currency_cost_fails_closed"
    for before, after in [
        ((1.0, "known"), (None, "partial")),
        ((1.0, "known"), (None, "unreported")),
        ((1.0, "known"), (None, "other_currency")),
        ((None, "unreported"), (1.0, "known")),
        ((None, "partial"), (None, "partial")),
        ((None, "other_currency"), (None, "other_currency")),
        ((None, "unreported"), (None, "partial")),
        ((1.0, "known"), (2.0, "no-such-status")),
    ]:
        breaches = _cost_breaches(
            _row(calls=2, usd=before[0], usd_status=before[1]), _row(calls=2, usd=after[0], usd_status=after[1])
        )
        assert set(breaches) == {"cost_per_catch.provider_usd"}
        assert breaches["cost_per_catch.provider_usd"].detail.startswith("unknown cost")
        assert "never counted as zero" in breaches["cost_per_catch.provider_usd"].detail
    "case: test_dollars_unreported_on_both_sides_leave_model_calls_to_carry_the_rule"
    unreported = {"usd": None, "usd_status": "unreported"}
    assert _cost_breaches(_row(calls=2, **unreported), _row(calls=2, **unreported)) == {}
    assert set(_cost_breaches(_row(calls=2, **unreported), _row(calls=3, **unreported))) == {
        "cost_per_catch.model_calls"
    }
    "case: test_a_baseline_recorded_before_cost_per_catch_fails_closed"
    legacy = {key: value for key, value in _row().items() if key not in {"behavior_caught", "cost_total"}}
    breaches = _cost_breaches(legacy, _row())
    assert set(breaches) == {"cost_per_catch"}
    assert "predates cost per catch" in breaches["cost_per_catch"].detail
    "case: test_absolute_cost_rise_is_reported_and_never_gates_by_itself"
    before = _row(defects=3, behavior=3, calls=6, compute=0.6, minutes=0.06)
    after = _row(defects=4, behavior=4, calls=8, compute=0.8, minutes=0.08)
    assert compare({"selections": {"auto": before}}, {"selections": {"auto": after}}, _thresholds()) == []
    changes = {
        item.metric: item
        for item in cost_per_review_changes({"selections": {"auto": before}}, {"selections": {"auto": after}})
    }
    assert set(changes) == {"estimated_compute_minutes", "model_calls"}
    assert changes["model_calls"].detail == "rose 33.3% (reported, not gated)"
    assert changes["estimated_compute_minutes"].detail == "rose 33.3% (reported, not gated)"
    assert changes["model_calls"].to_wire() == {
        "selection": "auto",
        "metric": "model_calls",
        "baseline": 0.6,
        "current": 0.8,
        "gated": False,
        "detail": "rose 33.3% (reported, not gated)",
    }


def test_cost_per_catch_reporting_contract() -> None:
    """case: test_thresholds_name_cost_per_catch_and_its_weights_and_no_flat_cost_rule"""
    assert PROPOSED_THRESHOLDS == {
        "catch_rate_drop_max": 0.0,
        "behavior_catch_rate_drop_max": 0.0,
        "lost_catches_max": 0,
        "noise_rate_rise_max": 0.05,
        "cost_per_catch_rise_max": 0.1,
        "defect_catch_weight": 1.0,
        "behavior_catch_weight": 1.0,
    }
    config = load_gate_config(GATE_DIR)
    wire = config.thresholds.to_wire()
    assert "cost_per_review_rise_max" not in wire
    assert wire["cost_per_catch_rise_max"] == 0.1
    assert "cost_per_review_rise_max" not in (GATE_DIR / "gate.yaml").read_text(encoding="utf-8")
    "case: test_provider_dollars_count_only_usd_costs"
    cost = case_cost(_run(_call(0.5, "USD"), _call(9.0, "EUR"), _call(3.0, None), _call(None, None)))
    assert cost.provider_usd == 0.5
    assert cost.usd_unknown_calls == 1
    assert cost.usd_other_currency_calls == 2
    local = case_cost(_run(_call(None, None, driver="ollama")))
    assert (local.local_model_calls, local.usd_unknown_calls, local.usd_other_currency_calls) == (1, 0, 0)
    "case: test_provider_usd_status"
    assert provider_usd_status(provider_api_calls=0, unreported_calls=0, other_currency_calls=0) == "known"
    assert provider_usd_status(provider_api_calls=3, unreported_calls=0, other_currency_calls=0) == "known"
    assert provider_usd_status(provider_api_calls=3, unreported_calls=3, other_currency_calls=0) == "unreported"
    assert provider_usd_status(provider_api_calls=3, unreported_calls=1, other_currency_calls=0) == "partial"
    assert provider_usd_status(provider_api_calls=3, unreported_calls=2, other_currency_calls=1) == "other_currency"
    "case: test_an_inconsistent_dollar_status_reads_as_unknown"
    breaches = _cost_breaches(_row(usd=1.0, usd_status="known"), _row(usd=None, usd_status="known"))
    assert breaches["cost_per_catch.provider_usd"].current == "unknown"
    assert breaches["cost_per_catch.provider_usd"].detail.startswith("unknown cost (baseline known, current unknown)")
    "case: test_checked_in_baseline_carries_catches_and_cost_totals_consistently"
    document = json.loads((GATE_DIR / "baseline.json").read_text(encoding="utf-8"))
    snapshot = document["snapshot"]
    body = {key: value for key, value in snapshot.items() if key != "snapshot_digest"}
    assert snapshot["snapshot_digest"] == document["snapshot_digest"] == digest(body)
    for label, row in snapshot["selections"].items():
        reviews = len(row["cases"])
        spend = row["cost_total"]
        assert round(spend["model_calls"] / reviews, 6) == row["cost_per_review"]["model_calls"], label
        assert round(spend["provider_api_calls"] / reviews, 6) == row["cost_per_review"]["provider_api_calls"], label
        behavior_expected = len(row["behavior_caught"]) + len(row["missed_behavior"])
        assert row["behavior_catch_rate"] == round(len(row["behavior_caught"]) / behavior_expected, 6), label
        assert spend["provider_usd_status"] in {"known", "unreported"}, label
        minutes = row["cost_per_review"]["estimated_compute_minutes"]
        assert round(spend["estimated_compute_minutes"] / reviews, 6) == minutes, label


def test_compute_per_catch_contract() -> None:
    """case: test_compute_minutes_per_catch_at_the_threshold_pass_and_just_past_it_block"""
    assert _cost_breaches(_row(defects=5, behavior=5, compute=10.0), _row(defects=5, behavior=5, compute=11.0)) == {}
    past = _cost_breaches(_row(defects=5, behavior=5, compute=10.0), _row(defects=5, behavior=5, compute=11.0001))
    assert set(past) == {"cost_per_catch.estimated_compute_minutes"}
    assert past["cost_per_catch.estimated_compute_minutes"].detail == "rose 10.0%"
    assert (
        past["cost_per_catch.estimated_compute_minutes"].baseline,
        past["cost_per_catch.estimated_compute_minutes"].current,
    ) == (1.0, 1.10001)
    "case: test_compute_that_outgrows_catches_blocks_even_when_model_calls_per_catch_pass"
    breaches = _cost_breaches(
        _row(defects=4, behavior=5, calls=9, compute=1.1), _row(defects=6, behavior=5, calls=12, compute=4.1)
    )
    assert set(breaches) == {"cost_per_catch.estimated_compute_minutes"}
    assert breaches["cost_per_catch.estimated_compute_minutes"].detail == "rose 205.0%"
    "case: test_compute_edge_cases_follow_the_same_rules"
    nothing = _cost_breaches(_row(defects=1, behavior=0, compute=1.0), _row(defects=0, behavior=0, compute=1.0))
    assert nothing["cost_per_catch.estimated_compute_minutes"].detail == "cost with no catches"
    from_zero = _cost_breaches(_row(compute=0.0), _row(compute=0.5))
    assert from_zero["cost_per_catch.estimated_compute_minutes"].detail == "rose from zero"
    assert _cost_breaches(_row(defects=0, behavior=0, compute=2.0), _row(defects=0, behavior=0, compute=2.0)) == {}
    assert _cost_breaches(_row(defects=0, behavior=0, compute=2.0), _row(defects=1, behavior=0, compute=9.0)) == {}
    "case: test_unknown_compute_fails_closed_and_is_never_zero"
    for before, after in [(None, 1.0), (1.0, None), (None, None), (1.0, float("nan")), (1.0, -1.0)]:
        breaches = _cost_breaches(_row(compute=before), _row(compute=after))
        assert set(breaches) == {"cost_per_catch.estimated_compute_minutes"}
        assert breaches["cost_per_catch.estimated_compute_minutes"].detail.startswith("unknown cost")
        assert "never counted as zero" in breaches["cost_per_catch.estimated_compute_minutes"].detail
    "case: test_unknown_model_calls_fail_closed"
    breaches = _cost_breaches(_row(calls=2), _row(calls=None))
    assert set(breaches) == {"cost_per_catch.model_calls"}
    assert breaches["cost_per_catch.model_calls"].detail.startswith("unknown cost (baseline known, current unknown)")
    "case: test_a_plan_estimate_that_is_not_a_finite_non_negative_number_makes_compute_unknown"
    known = case_cost(
        SimpleNamespace(
            plan=(_plan_row(3.0), _plan_row(2.0, "managed")),
            model_calls=(),
            measured_compute_minutes=0.0,
            wall_seconds=0.0,
        )
    )
    assert (known.estimated_customer_minutes, known.estimated_managed_minutes, known.minutes_unknown_rows) == (
        3.0,
        2.0,
        0,
    )
    for bad in (float("nan"), float("inf"), -1.0):
        cost = case_cost(
            SimpleNamespace(
                plan=(_plan_row(3.0), _plan_row(bad)), model_calls=(), measured_compute_minutes=0.0, wall_seconds=0.0
            )
        )
        assert cost.minutes_unknown_rows == 1
        assert cost.estimated_customer_minutes == 3.0
    "case: test_checked_in_approval_block_is_approved_and_bound_to_the_values"
    raw = yaml.safe_load((GATE_DIR / "gate.yaml").read_text(encoding="utf-8"))["thresholds"]["approval"]
    config = load_gate_config(GATE_DIR)
    assert raw == {
        "status": "approved",
        "approved_by": "Pankaj",
        "approved_at": "2026-10-06",
        "thresholds": dict(PROPOSED_THRESHOLDS),
        "thresholds_digest": thresholds_digest(PROPOSED_THRESHOLDS),
    }
    assert config.thresholds.approved and config.thresholds.approval_problem() is None
    pending = dataclasses.replace(config.thresholds, approval_status="pending_product_owner_approval")
    assert pending.approval_problem() == "thresholds are proposed defaults pending product-owner approval"


def _gate_yaml(tmp_path: Path, *, mode: str = "report", **thresholds: Any) -> Path:
    gate = yaml.safe_load((GATE_DIR / "gate.yaml").read_text(encoding="utf-8"))
    gate["corpus"] = str(BUILTIN)
    gate["mode"] = mode
    gate["thresholds"].update(thresholds)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "gate.yaml").write_text(yaml.safe_dump(gate, sort_keys=False), encoding="utf-8")
    return tmp_path


def test_a_config_that_still_sets_the_removed_flat_rule_is_refused(tmp_path: Path) -> None:
    with pytest.raises(GateError, match="thresholds\\.cost_per_review_rise_max was removed") as refused:
        load_gate_config(_gate_yaml(tmp_path, cost_per_review_rise_max=0.1))
    assert "cost_per_catch_rise_max" in str(refused.value)


def test_catch_weights_must_be_positive_numbers(tmp_path: Path) -> None:
    for case_index, weight in enumerate([0, -1.0, float("inf"), "heavy", True]):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        with pytest.raises(GateError, match="thresholds\\.behavior_catch_weight must be a positive number"):
            load_gate_config(_gate_yaml(case_tmp_path, behavior_catch_weight=weight))


def test_cost_per_catch_threshold_is_validated(tmp_path: Path) -> None:
    with pytest.raises(GateError, match="thresholds\\.cost_per_catch_rise_max must be a non-negative number"):
        load_gate_config(_gate_yaml(tmp_path, cost_per_catch_rise_max=-0.1))


def _call(cost: float | None, currency: str | None, driver: str = "openai") -> Any:
    return SimpleNamespace(
        stage="finding_investigation",
        driver=driver,
        usage=ModelCallUsage(calls=1, provider_cost=cost, currency=currency),
    )


def _run(*calls: Any) -> Any:
    return SimpleNamespace(plan=(), model_calls=calls, measured_compute_minutes=0.0, wall_seconds=0.0)


def _costing_reviewer(cost: float | None, currency: str | None) -> Callable[[EvalCase], Responder]:

    def responder_for(case: EvalCase) -> Responder:
        inner = SyntheticReviewer(case, driver="openai", model="synthetic-reviewer-v1")

        def respond(**kwargs: Any) -> StructuredExecution:
            execution = inner(**kwargs)
            usage = ModelCallUsage(calls=1, provider_cost=cost, currency=currency)
            return StructuredExecution(
                result=execution.result, driver=execution.driver, model=execution.model, usage=usage
            )

        return respond

    return responder_for


def _small_gate(tmp_path: Path, cases: tuple[str, ...] = ("token-compare-timing",)) -> Path:
    root = tmp_path / "evals" / "review"
    corpus = root / "corpus"
    (corpus / "cases").mkdir(parents=True)
    (corpus / "fixtures").mkdir()
    for case_id in cases:
        shutil.copy(BUILTIN / "cases" / f"{case_id}.yaml", corpus / "cases")
        shutil.copy(BUILTIN / "fixtures" / f"{case_id}.json", corpus / "fixtures")
    manifest = yaml.safe_load((BUILTIN / "corpus.yaml").read_text(encoding="utf-8"))
    manifest["cases"] = list(cases)
    (corpus / "corpus.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    gate = yaml.safe_load((GATE_DIR / "gate.yaml").read_text(encoding="utf-8"))
    gate["corpus"] = "corpus"
    gate["replay"]["selections"] = ["quick"]
    gate["authorized_owners"] = ["product-owner"]
    (root / "gate.yaml").write_text(yaml.safe_dump(gate, sort_keys=False), encoding="utf-8")
    return root


def _record(root: Path, cost: float | None, currency: str | None) -> None:
    config = load_gate_config(root)
    recorded = record_corpus(
        load_corpus(config.corpus_path),
        config.spec,
        responder_for=_costing_reviewer(cost, currency),
        recorded_with=RecordedWith("synthetic", config.spec.reviewer.driver, config.spec.reviewer.model),
    )
    assert recorded.ok


def test_the_gate_view_carries_corpus_totals_of_catches_and_cost(tmp_path: Path) -> None:
    config = load_gate_config(_small_gate(tmp_path, cases=("auth-env-bypass", "token-compare-timing")))
    report = replay_corpus(load_corpus(config.corpus_path), config.spec)
    metrics, row = (report.selections["quick"], report.snapshot()["selections"]["quick"])
    assert row["behavior_caught"] == ["auth-env-bypass#reads-auth-bypass-env"]
    assert row["behavior_caught"] == sorted(
        f"{item['case']}#{item['expected']}" for item in metrics["behavior"]["caught"]
    )
    assert row["cost_total"]["model_calls"] == metrics["cost"]["total"]["model_calls"] >= 2
    assert row["cost_total"]["model_calls"] == 2 * row["cost_per_review"]["model_calls"]
    assert row["cost_total"]["provider_api_calls"] == metrics["cost"]["total"]["provider_api_calls"]
    assert row["cost_total"]["provider_usd_status"] == "unreported"
    assert row["cost_total"]["provider_usd"] is None
    minutes = metrics["cost"]["total"]["estimated_compute_minutes"]
    assert row["cost_total"]["estimated_compute_minutes"] == minutes > 0
    assert row["cost_total"]["estimated_compute_minutes"] == pytest.approx(
        2 * row["cost_per_review"]["estimated_compute_minutes"]
    )


def test_recorded_dollars_reach_the_gate_and_a_foreign_currency_fails_closed(tmp_path: Path) -> None:
    root = _small_gate(tmp_path)
    _record(root, 0.02, "USD")
    config = load_gate_config(root)
    first = evaluate_gate(config, replay_corpus(load_corpus(config.corpus_path), config.spec))
    spend = first.snapshot["selections"]["quick"]["cost_total"]
    assert spend["model_calls"] >= 1 and spend["provider_usd_status"] == "known"
    assert spend["provider_usd"] == pytest.approx(0.02 * spend["model_calls"])
    assert first.snapshot["selections"]["quick"]["behavior_caught"] == []
    update_baseline(config, first)
    _record(root, 0.02, "EUR")
    second = evaluate_gate(config, replay_corpus(load_corpus(config.corpus_path), config.spec), mode="enforce")
    assert second.snapshot["selections"]["quick"]["cost_total"]["provider_usd_status"] == "other_currency"
    assert second.snapshot["selections"]["quick"]["cost_total"]["provider_usd"] is None
    assert second.status == "blocked" and second.blocking
    assert [breach.metric for breach in second.breaches] == ["cost_per_catch.provider_usd"]


def test_gate_result_reports_cost_per_catch_and_absolute_cost(tmp_path: Path) -> None:
    root = _small_gate(tmp_path)
    config = load_gate_config(root)
    update_baseline(config, evaluate_gate(config, replay_corpus(load_corpus(config.corpus_path), config.spec)))
    baseline = json.loads(config.baseline_path.read_text(encoding="utf-8"))
    row = baseline["snapshot"]["selections"]["quick"]
    row["cost_total"]["model_calls"] = 0
    row["cost_per_review"]["model_calls"] = 0.0
    body = {key: value for key, value in baseline["snapshot"].items() if key != "snapshot_digest"}
    baseline["snapshot"]["snapshot_digest"] = baseline["snapshot_digest"] = digest(body)
    config.baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    result = evaluate_gate(config, replay_corpus(load_corpus(config.corpus_path), config.spec))
    wire = result.to_wire()
    view = wire["cost_per_catch"]["quick"]
    assert view["baseline"]["model_calls_per_catch"] == 0.0
    assert view["current"]["weighted_catches"] == 1.0
    assert view["current"]["model_calls_per_catch"] == view["current"]["model_calls"] > 0
    assert [item["metric"] for item in wire["cost_per_review_changes"]] == ["model_calls"]
    assert [breach["metric"] for breach in wire["breaches"]] == ["cost_per_catch.model_calls"]
    from lemoncrow.gateway.cli.commands.review_eval import review_eval

    output = CliRunner().invoke(review_eval, ["gate", "--gate-dir", str(root)], catch_exceptions=False).output
    assert "cost/catch" in output
    assert "breach quick cost_per_catch.model_calls: 0.0 -> " in output
    assert "cost/review quick model_calls: 0.0 -> " in output and "(reported, not gated)" in output


def _plan_row(minutes: float, executor: str = "local") -> Any:
    return SimpleNamespace(
        id="checks.test", status="run", estimated_model_calls=0, estimated_minutes=minutes, executor=executor
    )


def test_unknown_planned_minutes_reach_the_gate_as_unknown_compute_and_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_gate_config(_small_gate(tmp_path, cases=("auth-env-bypass", "token-compare-timing")))
    update_baseline(config, evaluate_gate(config, replay_corpus(load_corpus(config.corpus_path), config.spec)))
    real = metrics_module.case_cost
    monkeypatch.setattr(metrics_module, "case_cost", lambda run: dataclasses.replace(real(run), minutes_unknown_rows=1))
    report = replay_corpus(load_corpus(config.corpus_path), config.spec)
    cost, row = (report.selections["quick"]["cost"], report.snapshot()["selections"]["quick"])
    assert cost["unknown"]["compute_minute_rows"] == 2
    assert cost["total"]["estimated_compute_minutes"] is None
    assert cost["per_review"]["estimated_compute_minutes"] is None
    assert row["cost_total"]["estimated_compute_minutes"] is None
    result = evaluate_gate(config, report)
    breach = next(
        item
        for item in result.breaches
        if item.selection == "quick" and item.metric == "cost_per_catch.estimated_compute_minutes"
    )
    assert breach.current == "unknown"
    assert breach.detail.startswith("unknown cost (baseline known, current unknown)")
    assert result.status == "blocked"


def _approval(tmp_path: Path) -> dict[str, Any]:
    pending = load_gate_config(_gate_yaml(tmp_path / "pending")).thresholds
    return {
        "status": APPROVED,
        "approved_by": "po@example",
        "approved_at": "2026-10-05",
        "thresholds": dict(pending.values()),
        "thresholds_digest": pending.digest(),
    }


def test_approved_thresholds_with_their_digest_are_approved_and_may_enforce(tmp_path: Path) -> None:
    config = load_gate_config(_gate_yaml(tmp_path / "gate", mode="enforce", approval=_approval(tmp_path)))
    assert config.mode == "enforce" and config.thresholds.approved
    assert config.thresholds.changed_since_approval == ()
    assert config.thresholds.approval_problem() is None
    assert config.thresholds.to_wire()["digest"] == config.thresholds.digest()


def test_unchanged_values_written_differently_stay_approved(tmp_path: Path) -> None:
    approval = _approval(tmp_path)
    same = {"cost_per_catch_rise_max": 0.1, "lost_catches_max": 0.0, "defect_catch_weight": 1}
    config = load_gate_config(_gate_yaml(tmp_path / "gate", mode="enforce", approval=approval, **same))
    assert config.thresholds.approved


def test_an_edited_threshold_or_weight_after_approval_is_unapproved(tmp_path: Path) -> None:
    for case_index, key in enumerate(list(PROPOSED_THRESHOLDS)):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        approval = _approval(case_tmp_path)
        edited = {key: 1 if key == "lost_catches_max" else float(PROPOSED_THRESHOLDS[key]) + 0.5}
        report = load_gate_config(_gate_yaml(case_tmp_path / "report", approval=approval, **edited))
        assert not report.thresholds.approved
        assert report.thresholds.changed_since_approval == (key,)
        problem = report.thresholds.approval_problem()
        assert problem is not None and f"thresholds changed since approval: {key}" in problem
        with pytest.raises(GateError, match=f"thresholds changed since approval: {key} \\(approved ") as refused:
            load_gate_config(_gate_yaml(case_tmp_path / "enforce", mode="enforce", approval=approval, **edited))
        assert "re-approve" in str(refused.value)
        assert report.thresholds.digest() in str(refused.value)


def test_the_gate_result_notes_an_approval_broken_by_an_edit(tmp_path: Path) -> None:
    root = _small_gate(tmp_path / "small")
    gate = yaml.safe_load((root / "gate.yaml").read_text(encoding="utf-8"))
    gate["thresholds"].update(approval=_approval(tmp_path), noise_rate_rise_max=0.2)
    (root / "gate.yaml").write_text(yaml.safe_dump(gate, sort_keys=False), encoding="utf-8")
    config = load_gate_config(root)
    result = evaluate_gate(config, replay_corpus(load_corpus(config.corpus_path), config.spec))
    assert (
        "thresholds changed since approval: noise_rate_rise_max (approved 0.05, now 0.2); they count as unapproved"
        in result.notes
    )
    wire = result.to_wire()["thresholds"]
    assert wire["approved"] is False and wire["changed_since_approval"] == ["noise_rate_rise_max"]


def test_an_approval_without_a_digest_is_unapproved_until_re_approved(tmp_path: Path) -> None:
    legacy = {"status": APPROVED, "approved_by": "po@example", "approved_at": "2026-10-04"}
    report = load_gate_config(_gate_yaml(tmp_path / "report", approval=legacy))
    assert not report.thresholds.approved
    with pytest.raises(GateError, match="records no thresholds_digest") as refused:
        load_gate_config(_gate_yaml(tmp_path / "enforce", mode="enforce", approval=legacy))
    assert "re-approve" in str(refused.value)


def test_a_digest_that_does_not_match_its_recorded_values_is_unapproved(tmp_path: Path) -> None:
    approval = {**_approval(tmp_path), "thresholds_digest": "0" * 64}
    config = load_gate_config(_gate_yaml(tmp_path / "gate", approval=approval))
    assert not config.thresholds.approved
    assert config.thresholds.changed_since_approval == ()
    assert "thresholds_digest does not match" in str(config.thresholds.approval_problem())


def test_an_edit_against_a_digest_without_recorded_values_is_unapproved(tmp_path: Path) -> None:
    approval = {**_approval(tmp_path), "thresholds": None}
    unchanged = load_gate_config(_gate_yaml(tmp_path / "same", approval=approval))
    edited = load_gate_config(_gate_yaml(tmp_path / "edited", approval=approval, noise_rate_rise_max=0.2))
    assert unchanged.thresholds.approved
    assert not edited.thresholds.approved
    assert edited.thresholds.changed_since_approval == ()
    assert "thresholds_digest does not match" in str(edited.thresholds.approval_problem())

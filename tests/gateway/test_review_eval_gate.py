"""Gate I: report/enforce modes, deliberate regressions, acceptance records and the CLI."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from lemoncrow.pro.capabilities.review import ai_review, capability_catalog, intelligence_runtime
from lemoncrow.pro.capabilities.review.evaluation.corpus import EvalCase, load_corpus
from lemoncrow.pro.capabilities.review.evaluation.fixtures import RecordedWith, Responder, SyntheticReviewer
from lemoncrow.pro.capabilities.review.evaluation.gate import (
    PROPOSED_THRESHOLDS,
    GateConfig,
    GateError,
    GateResult,
    Thresholds,
    compare,
    evaluate_gate,
    load_gate_config,
    update_baseline,
    write_acceptance,
)
from lemoncrow.pro.capabilities.review.evaluation.harness import record_corpus, replay_corpus
from lemoncrow.pro.capabilities.review.intelligence_runtime import StructuredExecution

REPO = Path(__file__).resolve().parents[2]
GATE_DIR = REPO / "evals" / "review"
BUILTIN = GATE_DIR / "corpus" / "builtin"
OWNER = "product-owner"


def _make_gate(
    tmp_path: Path,
    *,
    cases: Sequence[str] = ("token-compare-timing", "auth-env-bypass"),
    selections: Sequence[Any] = ("quick",),
    owners: Sequence[str] = (OWNER,),
    **overrides: Any,
) -> Path:
    """A gate directory with a small copy of the built-in corpus (same cases and fixtures)."""

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
    gate["replay"]["selections"] = list(selections)
    gate["authorized_owners"] = list(owners)
    gate.update(overrides)
    (root / "gate.yaml").write_text(yaml.safe_dump(gate, sort_keys=False), encoding="utf-8")
    return root


def _evaluate(root: Path, mode: Any = None) -> tuple[GateConfig, GateResult]:
    config = load_gate_config(root)
    report = replay_corpus(load_corpus(config.corpus_path, require_visibility="public"), config.spec)
    return config, evaluate_gate(config, report, mode=mode)


def _baseline(root: Path) -> None:
    config, result = _evaluate(root)
    assert result.status == "no_baseline"
    update_baseline(config, result)
    assert _evaluate(root)[1].status == "pass"


def _dropping_reviewer(config: GateConfig, drop_case: str) -> Callable[[EvalCase], Responder]:
    """The scripted reviewer, except that it no longer confirms anything on *drop_case*."""

    reviewer = config.spec.reviewer

    def responder_for(case: EvalCase) -> Responder:
        inner = SyntheticReviewer(case, driver=reviewer.driver, model=reviewer.model)

        def respond(**kwargs: Any) -> StructuredExecution:
            execution = inner(**kwargs)
            if case.id != drop_case or execution.result.get("status") != "confirmed":
                return execution
            missed = {
                **execution.result,
                "status": "not_confirmed",
                "title": "",
                "explanation": "",
                "consequence": "",
                "evidence_refs": [],
            }
            return StructuredExecution(
                result=missed, driver=execution.driver, model=execution.model, usage=execution.usage
            )

        return respond

    return responder_for


def _revise_investigation_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    original = ai_review._investigation_prompt

    def revised(*args: Any, **kwargs: Any) -> tuple[str, str]:
        system, prompt = original(*args, **kwargs)
        return system + "\nRevised investigation instructions.", prompt

    monkeypatch.setattr(ai_review, "_investigation_prompt", revised)


def _cli(*args: str) -> Any:
    from lemoncrow.gateway.cli.commands.review_eval import review_eval

    return CliRunner().invoke(review_eval, list(args), catch_exceptions=False)


def test_checked_in_gate_runs_in_report_mode_with_owner_approved_thresholds() -> None:
    config = load_gate_config(GATE_DIR)

    assert config.mode == "report"
    # Approved by the product owner on 2026-10-06, bound to the proposed values' digest.
    assert config.thresholds.approved
    assert (config.thresholds.approved_by, config.thresholds.approved_at) == ("Pankaj", "2026-10-06")
    assert config.thresholds.approved_values == dict(PROPOSED_THRESHOLDS)
    assert config.authorized_owners == ("Pankaj",)
    assert {
        "catch_rate_drop_max": config.thresholds.catch_rate_drop_max,
        "behavior_catch_rate_drop_max": config.thresholds.behavior_catch_rate_drop_max,
        "lost_catches_max": config.thresholds.lost_catches_max,
        "noise_rate_rise_max": config.thresholds.noise_rate_rise_max,
        "cost_per_catch_rise_max": config.thresholds.cost_per_catch_rise_max,
        "defect_catch_weight": config.thresholds.defect_catch_weight,
        "behavior_catch_weight": config.thresholds.behavior_catch_weight,
    } == dict(PROPOSED_THRESHOLDS)
    assert [row.label for row in config.spec.selections] == [
        "quick",
        "auto",
        "thorough",
        "deep",
        "custom:static-only",
    ]
    assert (GATE_DIR / "baseline.json").is_file()


def test_enforce_mode_needs_approved_thresholds(tmp_path: Path) -> None:
    pending = dict(yaml.safe_load((GATE_DIR / "gate.yaml").read_text(encoding="utf-8"))["thresholds"])
    pending["approval"] = {"status": "pending_product_owner_approval", "approved_by": None, "approved_at": None}
    with pytest.raises(GateError, match="requires product-owner-approved thresholds"):
        load_gate_config(_make_gate(tmp_path / "a", mode="enforce", thresholds=pending))

    approved = dict(yaml.safe_load((GATE_DIR / "gate.yaml").read_text(encoding="utf-8"))["thresholds"])
    approved["approval"] = {"status": "approved", "approved_by": None, "approved_at": None}
    with pytest.raises(GateError, match="approved_by and approved_at"):
        load_gate_config(_make_gate(tmp_path / "b", mode="enforce", thresholds=approved))

    # Approved without binding the values: unapproved until re-approved with a digest.
    approved["approval"] = {"status": "approved", "approved_by": "po@example", "approved_at": "2026-10-04"}
    with pytest.raises(GateError, match="records no thresholds_digest"):
        load_gate_config(_make_gate(tmp_path / "c", mode="enforce", thresholds=approved))

    values = load_gate_config(GATE_DIR).thresholds
    approved["approval"].update(thresholds=dict(values.values()), thresholds_digest=values.digest())
    config = load_gate_config(_make_gate(tmp_path / "d", mode="enforce", thresholds=approved))
    assert config.mode == "enforce" and config.thresholds.approved


def test_prompt_regression_is_blocked_reported_and_passes_after_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_gate(tmp_path)
    _baseline(root)

    # A prompt change: the old fixtures no longer answer, so nothing is measured.
    _revise_investigation_prompt(monkeypatch)
    _config, stale = _evaluate(root, mode="enforce")
    assert stale.status == "stale" and stale.blocking
    assert "prompt" in stale.changed_components
    assert {event["reason"] for event in stale.fixture_events} == {"prompt template (system prompt) changed"}

    # Re-recorded under the new prompt, the reviewer now misses a known catch.
    config = load_gate_config(root)
    corpus = load_corpus(config.corpus_path)
    recorded = record_corpus(
        corpus,
        config.spec,
        responder_for=_dropping_reviewer(config, "token-compare-timing"),
        recorded_with=RecordedWith("synthetic", config.spec.reviewer.driver, config.spec.reviewer.model),
    )
    assert recorded.ok

    _config, enforced = _evaluate(root, mode="enforce")
    assert enforced.status == "blocked" and enforced.blocking
    assert {"prompt", "fixtures"} <= set(enforced.changed_components)
    metrics = {breach.metric: breach for breach in enforced.breaches}
    assert metrics["catch_rate"].baseline == 1.0 and metrics["catch_rate"].current == 0.5
    assert metrics["lost_catches"].detail == "token-compare-timing#timing-compare"

    _config, reported = _evaluate(root, mode="report")
    assert reported.status == "blocked" and not reported.blocking

    assert _cli("gate", "--gate-dir", str(root), "--mode", "enforce").exit_code == 1
    report_mode = _cli("gate", "--gate-dir", str(root))
    assert report_mode.exit_code == 0
    assert "Gate I: BLOCKED (mode report; report only)" in report_mode.output
    assert "breach quick catch_rate: 1.0 -> 0.5" in report_mode.output

    with pytest.raises(GateError, match="never becomes the baseline"):
        update_baseline(config, enforced)
    with pytest.raises(GateError, match="not an authorized owner"):
        write_acceptance(config, enforced, owner="someone-else", reason="faster")
    with pytest.raises(GateError, match="needs a reason"):
        write_acceptance(config, enforced, owner=OWNER, reason="  ")

    path = write_acceptance(config, enforced, owner=OWNER, reason="New prompt halves latency; token case tracked.")
    record = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert record["owner"] == OWNER and record["reason"].startswith("New prompt")
    assert record["measured"]["baseline"]["quick"]["catch_rate"] == 1.0
    assert record["measured"]["current"]["quick"]["catch_rate"] == 0.5
    assert "prompt" in record["digest_set"]["changed"]
    assert record["digest_set"]["current"]["prompt"] != record["digest_set"]["baseline"]["prompt"]
    assert record["breaches"] == [breach.to_wire() for breach in enforced.breaches]

    _config, accepted = _evaluate(root, mode="enforce")
    assert accepted.status == "accepted" and not accepted.blocking
    assert accepted.accepted_by is not None and accepted.accepted_by.path == path.name
    assert _cli("gate", "--gate-dir", str(root), "--mode", "enforce").exit_code == 0

    update_baseline(config, accepted)
    baseline = json.loads(config.baseline_path.read_text(encoding="utf-8"))
    assert baseline["decision"] == "accepted" and baseline["acceptance"] == f"acceptances/{path.name}"
    assert _evaluate(root, mode="enforce")[1].status == "pass"


def test_preset_that_removes_an_entry_is_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _make_gate(tmp_path)
    _baseline(root)

    trimmed = tuple(row for row in capability_catalog.PRESET_SELECTIONS["quick"] if row.id != "side_effects.static")
    monkeypatch.setitem(capability_catalog.PRESET_SELECTIONS, "quick", trimmed)  # type: ignore[index]

    _config, enforced = _evaluate(root, mode="enforce")
    assert enforced.status == "blocked" and enforced.blocking
    assert "preset" in enforced.changed_components
    metrics = {breach.metric: breach for breach in enforced.breaches}
    assert metrics["behavior_catch_rate"].baseline == 1.0 and metrics["behavior_catch_rate"].current == 0.0
    assert "auth-env-bypass#reads-auth-bypass-env" in metrics["lost_catches"].detail
    assert _evaluate(root, mode="report")[1].blocking is False


def test_unchanged_numbers_with_a_changed_digest_need_a_recorded_gate_result(tmp_path: Path) -> None:
    root = _make_gate(tmp_path)
    _baseline(root)
    gate = yaml.safe_load((root / "gate.yaml").read_text(encoding="utf-8"))
    gate["replay"]["selections"] = [
        "quick",
        {"name": "static-only", "custom_from": "quick", "custom": ["side_effects.static"]},
    ]
    (root / "gate.yaml").write_text(yaml.safe_dump(gate, sort_keys=False), encoding="utf-8")

    config, result = _evaluate(root, mode="enforce")

    assert result.status == "pass_unrecorded" and result.blocking
    assert result.breaches == ()
    assert result.changed_components == ("replay",)
    update_baseline(config, result)
    assert _evaluate(root, mode="enforce")[1].status == "pass"


def test_tampered_or_unauthorized_acceptance_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _make_gate(tmp_path)
    _baseline(root)
    trimmed = tuple(row for row in capability_catalog.PRESET_SELECTIONS["quick"] if row.id != "side_effects.static")
    monkeypatch.setitem(capability_catalog.PRESET_SELECTIONS, "quick", trimmed)  # type: ignore[index]
    config, blocked = _evaluate(root)
    path = write_acceptance(config, blocked, owner=OWNER, reason="trade-off")
    record = yaml.safe_load(path.read_text(encoding="utf-8"))

    record["measured"]["current"]["quick"]["behavior_catch_rate"] = 1.0
    path.write_text(yaml.safe_dump(record), encoding="utf-8")
    _config, tampered = _evaluate(root, mode="enforce")
    assert tampered.status == "blocked"
    assert "the recorded current numbers differ from the measured ones" in tampered.acceptances[0].problems

    record = yaml.safe_load(write_acceptance(config, blocked, owner=OWNER, reason="x").read_text(encoding="utf-8"))
    record["owner"] = "intruder"
    for stale in config.acceptances_dir.glob("*.yaml"):
        stale.unlink()
    (config.acceptances_dir / "forged.yaml").write_text(yaml.safe_dump(record), encoding="utf-8")
    _config, forged = _evaluate(root, mode="enforce")
    assert forged.status == "blocked"
    assert "owner 'intruder' is not in authorized_owners" in forged.acceptances[0].problems


def test_no_acceptance_without_configured_owners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _make_gate(tmp_path, owners=())
    _baseline(root)
    trimmed = tuple(row for row in capability_catalog.PRESET_SELECTIONS["quick"] if row.id != "side_effects.static")
    monkeypatch.setitem(capability_catalog.PRESET_SELECTIONS, "quick", trimmed)  # type: ignore[index]

    result = _cli("accept", "--gate-dir", str(root), "--owner", OWNER, "--reason", "ok")

    assert result.exit_code == 1
    assert "no authorized_owners are configured" in result.output


def test_compare_flags_removed_selections_noise_and_cost_per_catch() -> None:
    thresholds = Thresholds(**dict(PROPOSED_THRESHOLDS))  # type: ignore[arg-type]
    spend = {
        "estimated_compute_minutes": 2.0,
        "model_calls": 0,
        "provider_api_calls": 0,
        "provider_usd": 0.0,
        "provider_usd_status": "known",
    }
    row = {
        "catch_rate": 0.5,
        "behavior_catch_rate": None,
        "noise_rate": 0.1,
        "caught": ["a#1"],
        "behavior_caught": [],
        "missed_behavior": [],
        "false_findings": ["b#x"],
        "cost_per_review": {"estimated_compute_minutes": 1.0, "model_calls": 0.0, "provider_usd": 0.0},
        "cost_total": spend,
    }
    baseline = {"selections": {"quick": row, "auto": row}}
    current = {
        "selections": {
            "quick": {
                **row,
                "noise_rate": 0.2,
                "false_findings": ["b#x", "c#y"],
                "cost_per_review": {"estimated_compute_minutes": 1.2, "model_calls": 0.5, "provider_usd": 3.0},
                "cost_total": {
                    **spend,
                    "estimated_compute_minutes": 2.4,
                    "model_calls": 1,
                    "provider_api_calls": 1,
                    "provider_usd": 3.0,
                },
            }
        }
    }

    breaches = {(item.selection, item.metric): item for item in compare(baseline, current, thresholds)}

    # Compute minutes per catch rose 20 %; model calls and dollars rose from zero per catch.
    assert set(breaches) == {
        ("auto", "selection"),
        ("quick", "noise_rate"),
        ("quick", "cost_per_catch.estimated_compute_minutes"),
        ("quick", "cost_per_catch.model_calls"),
        ("quick", "cost_per_catch.provider_usd"),
    }
    assert breaches[("quick", "cost_per_catch.estimated_compute_minutes")].detail == "rose 20.0%"
    assert breaches[("quick", "noise_rate")].detail == "c#y"
    assert breaches[("quick", "cost_per_catch.model_calls")].detail == "rose from zero"
    within = {"selections": {"quick": {**row, "noise_rate": 0.15}, "auto": row}}
    assert compare(baseline, within, thresholds) == []
    improved = {**row, "catch_rate": 1.0, "caught": ["a#1", "d#2"], "noise_rate": 0.05, "false_findings": []}
    assert compare(baseline, {"selections": {"quick": improved, "auto": improved}}, thresholds) == []


def test_cli_run_reports_measurements_and_writes_traceable_records(tmp_path: Path) -> None:
    root = _make_gate(tmp_path, selections=["quick", "thorough"])
    out = tmp_path / "report.json"

    result = _cli("run", "--gate-dir", str(root), "--selection", "quick", "--out", str(out))

    assert result.exit_code == 0, result.output
    assert "quick                catch 100% (2/2)" in result.output
    assert "ai.investigators" in result.output
    report = json.loads(out.read_text(encoding="utf-8"))
    assert list(report["selections"]) == ["quick"]
    assert {row["case_id"] for row in report["records"]} == {"token-compare-timing", "auth-env-bypass"}


def test_cli_run_exits_nonzero_on_stale_fixtures(tmp_path: Path) -> None:
    root = _make_gate(tmp_path)
    (root / "corpus" / "fixtures" / "token-compare-timing.json").unlink()

    result = _cli("run", "--gate-dir", str(root))

    assert result.exit_code == 1
    assert "missing finding_investigation: no fixtures were recorded for this case" in result.output


def test_cli_record_requires_an_explicit_mode_and_a_live_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_gate(tmp_path)
    attempts: list[str] = []
    monkeypatch.setattr(intelligence_runtime, "execute_structured", lambda **_kwargs: attempts.append("live"))

    neither = _cli("record", "--gate-dir", str(root))
    no_budget = _cli("record", "--gate-dir", str(root), "--live")
    synthetic_budget = _cli("record", "--gate-dir", str(root), "--synthetic", "--max-calls", "3")
    live = _cli("record", "--gate-dir", str(root), "--live", "--max-calls", "1")
    execute = _cli("record", "--gate-dir", str(root), "--synthetic", "--execute")

    assert neither.exit_code == 2 and "exactly one of --synthetic or --live" in neither.output
    assert no_budget.exit_code == 2 and "--max-calls budget cap" in no_budget.output
    assert synthetic_budget.exit_code == 2
    assert live.exit_code == 1 and "never runs under pytest" in live.output
    assert execute.exit_code == 1 and "local execution recording refused" in execute.output
    assert "never runs under pytest" in execute.output
    assert attempts == []


def test_cli_refuses_an_organization_corpus_without_opt_in(tmp_path: Path) -> None:
    root = _make_gate(tmp_path / "repo")
    organization = tmp_path / "acme-corpus"
    shutil.copytree(root / "corpus", organization)
    manifest = yaml.safe_load((organization / "corpus.yaml").read_text(encoding="utf-8"))
    manifest.update(
        visibility="organization",
        organization="acme",
        opt_in={"organization": "acme", "approved_by": "po", "approved_at": "2026-10-01", "reference": "DPA-1"},
    )
    (organization / "corpus.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    refused = _cli("run", "--gate-dir", str(root), "--corpus", str(organization))
    allowed = _cli("run", "--gate-dir", str(root), "--corpus", str(organization), "--organization-opt-in", "acme")

    assert refused.exit_code == 1 and "requires an explicit organization opt-in" in refused.output
    assert allowed.exit_code == 0, allowed.output
    assert "(organization, 2 cases)" in allowed.output

    import lemoncrow

    bundled = Path(lemoncrow.__file__).resolve().parent / "review-eval-acme.json"
    leak = _cli(
        "run",
        "--gate-dir",
        str(root),
        "--corpus",
        str(organization),
        "--organization-opt-in",
        "acme",
        "--out",
        str(bundled),
    )
    assert leak.exit_code == 1 and "must stay outside" in leak.output
    assert not bundled.exists()


# --------------------------------------------------------------------------- #
# P11-ESC: escalation thresholds and the rule-check ceiling are sweepable policy
# --------------------------------------------------------------------------- #


def test_policy_selections_sweep_escalation_thresholds_against_catch_and_cost(tmp_path: Path) -> None:
    root = _make_gate(
        tmp_path,
        cases=("migration-drop-column", "token-compare-timing"),
        selections=["auto", {"name": "no-escalation", "effort": "auto", "escalation": {"enabled": False}}],
    )
    config = load_gate_config(root)
    assert [row.label for row in config.spec.selections] == ["auto", "policy:no-escalation"]
    assert config.spec.selections[1].settings_document() == {"effort": "auto", "escalation": {"enabled": False}}

    report = replay_corpus(load_corpus(config.corpus_path, require_visibility="public"), config.spec)

    on, off = report.selections["auto"], report.selections["policy:no-escalation"]
    # Gate I reads both sides of the trade-off per swept value. The default
    # policy still plans hypothesis synthesis for the destructive migration,
    # but one deterministic candidate no longer spends a redundant synthesis
    # call, so measured model calls are equal while planned calls remain distinct.
    assert on["catch"]["rate"] == off["catch"]["rate"] == 1.0
    assert on["cost"]["total"]["model_calls"] == off["cost"]["total"]["model_calls"] == 2
    assert on["cost"]["total"]["planned_model_calls"] > off["cost"]["total"]["planned_model_calls"]
    hypotheses = {
        (run.selection, run.case_id): next(row.status for row in run.plan if row.id == "ai.hypotheses")
        for run in report.runs
    }
    # The destructive migration escalates under the default thresholds only.
    assert hypotheses[("auto", "migration-drop-column")] == "run"
    assert hypotheses[("policy:no-escalation", "migration-drop-column")] == "skipped"
    assert hypotheses[("auto", "token-compare-timing")] == "skipped"


def test_policy_selections_are_validated_and_selectable_from_the_cli(tmp_path: Path) -> None:
    for index, row in enumerate(
        (
            {"name": "bad-target", "effort": "auto", "escalation": {"to": "quick"}},
            {"name": "bad-ceiling", "effort": "auto", "rule_checks": {"max_model_calls": -1}},
            {"name": "not-swept", "effort": "auto", "budget": {"max_model_calls": 3}},
            {"name": "", "effort": "auto", "escalation": {"enabled": False}},
            {"name": "bad-effort", "effort": "custom", "escalation": {"enabled": False}},
        )
    ):
        with pytest.raises(GateError, match=r"replay\.selections"):
            load_gate_config(_make_gate(tmp_path / str(index), selections=["quick", row]))

    root = _make_gate(
        tmp_path / "cli",
        selections=["quick", {"name": "rule-ceiling", "effort": "quick", "rule_checks": {"max_model_calls": 2}}],
    )
    out = tmp_path / "report.json"
    result = _cli("run", "--gate-dir", str(root), "--selection", "rule-ceiling", "--out", str(out))
    assert result.exit_code == 0, result.output
    report = json.loads(out.read_text(encoding="utf-8"))
    assert list(report["selections"]) == ["policy:rule-ceiling"]


def test_escalation_and_rule_check_defaults_are_part_of_the_policy_default_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lemoncrow.pro.capabilities.review import escalation, rule_checks
    from lemoncrow.pro.capabilities.review.evaluation.digests import policy_default_component

    before = policy_default_component()
    assert before["detail"]["escalation_defaults"]["to"] == "thorough"
    assert before["detail"]["rule_check_defaults"] == {"enabled": True, "max_model_calls": 8}

    monkeypatch.setitem(escalation.ESCALATION_DEFAULTS, "to", "deep")  # type: ignore[index]
    raised = policy_default_component()
    monkeypatch.undo()
    monkeypatch.setitem(rule_checks.RULE_CHECK_DEFAULTS, "max_model_calls", 9)  # type: ignore[index]
    ceiling = policy_default_component()

    assert len({before["digest"], raised["digest"], ceiling["digest"]}) == 3

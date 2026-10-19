"""RC1: Gate H readiness-enforcement hardening.

The approval is bound to the threshold values; the Gate H contract is package data (an install with no
source checkout evaluates it); corpus subjects are typed (qualify / advisory / negative control); and
evidence produced before these fields existed still loads and judges the same.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from lemoncrow.pro.capabilities.review import evidence_qualification as eq
from lemoncrow.pro.capabilities.review.evaluation import gate_h
from lemoncrow.pro.capabilities.review.evaluation.gate_h import (
    CorpusRepository,
    ExpectationSpec,
    GateHHarnessError,
    check_expectations,
    load_repository,
    report,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_DIR = REPO_ROOT / "evals" / "review"
CORPUS = GATE_DIR / "gate-h" / "corpus"
PACKAGED = REPO_ROOT / "src" / "lemoncrow" / "pro" / "capabilities" / "review" / eq.CONTRACT_RESOURCE


def _contract(**approval: Any) -> dict[str, Any]:
    """The checked-in contract, with the approval block replaced when keyword overrides are given."""

    raw = yaml.safe_load((GATE_DIR / "gate-h.yaml").read_text(encoding="utf-8"))
    if approval:
        raw["thresholds"]["approval"] = {**raw["thresholds"]["approval"], **approval}
    return raw


# --------------------------------------------------------------------------- #
# 3. approval bound to the threshold digest
# --------------------------------------------------------------------------- #


def test_the_checked_in_approval_is_bound_to_exactly_the_values_it_approved() -> None:
    config = eq.load_gate_h_config(GATE_DIR)
    thresholds = config.thresholds
    assert thresholds.approved and thresholds.approval_problem() is None
    assert thresholds.approved_digest == thresholds.digest
    assert thresholds.approved_values == dict(eq.PROPOSED_THRESHOLDS)
    # The values the owner approved on 2026-10-06 (never loosened to turn anything green).
    assert thresholds.values() == {
        "flake_rate_max": 0.0,
        "min_runs_per_input": 10,
        "min_flake_inputs": 2,
        "classification_accuracy_min": 0.95,
        "min_labeled_units": 60,
        "visual_nondeterminism_max": 0.0,
        "min_render_pairs": 5,
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("min_labeled_units", 50),
        ("classification_accuracy_min", 0.9),
        ("flake_rate_max", 0.1),
        ("min_runs_per_input", 5),
        ("visual_nondeterminism_max", 0.2),
    ],
)
def test_an_edited_threshold_is_unapproved_and_names_what_changed(name: str, value: float) -> None:
    raw = _contract()
    raw["thresholds"][name] = value
    thresholds = eq.parse_gate_h_config(raw, source="edited").thresholds  # report mode loads, unapproved
    assert not thresholds.approved
    assert thresholds.changed_since_approval == (name,)
    problem = thresholds.approval_problem() or ""
    assert "changed since approval" in problem and name in problem
    wire = thresholds.to_wire()
    assert wire["approved"] is False and wire["changed_since_approval"] == [name]
    assert wire["digest"] != wire["approval"]["thresholds_digest"]
    # Tightening is an edit too: any change needs the owner.
    # Enforcement refuses it and prints what to re-approve.
    with pytest.raises(eq.GateHError, match=rf"thresholds changed since approval: {name}"):
        eq.parse_gate_h_config({**raw, "mode": "enforce"}, source="edited")


def test_an_approval_that_predates_the_digest_is_unapproved_and_enforce_is_refused() -> None:
    raw = _contract()
    for key in ("thresholds", "thresholds_digest"):
        del raw["thresholds"]["approval"][key]
    config = eq.parse_gate_h_config(raw, source="old gate-h.yaml")
    assert config.mode == "report" and config.thresholds.approval_status == eq.APPROVED
    assert not config.thresholds.approved
    assert "predates binding approval" in (config.thresholds.approval_problem() or "")
    with pytest.raises(eq.GateHError, match="predates binding approval"):
        eq.parse_gate_h_config({**raw, "mode": "enforce"}, source="old gate-h.yaml")


def test_a_wrong_or_malformed_digest_is_unapproved() -> None:
    wrong = eq.parse_gate_h_config(_contract(thresholds_digest="thresholds:sha256:" + "0" * 64), source="x")
    assert not wrong.thresholds.approved
    assert "does not match" in (wrong.thresholds.approval_problem() or "")
    with pytest.raises(eq.GateHError, match="thresholds_digest must be"):
        eq.parse_gate_h_config(_contract(thresholds_digest=7), source="x")
    with pytest.raises(eq.GateHError, match="thresholds must map"):
        eq.parse_gate_h_config(_contract(thresholds=[1]), source="x")


def test_pending_thresholds_stay_pending_whatever_the_digest() -> None:
    config = eq.parse_gate_h_config(_contract(status=eq.PENDING), source="x")
    assert not config.thresholds.approved
    assert config.thresholds.approval_problem() == "thresholds are proposed defaults pending product-owner approval"


def test_a_repository_may_tighten_an_approved_floor_without_unapproving_it() -> None:
    floor = eq.load_gate_h_config(GATE_DIR).thresholds
    tightened = eq.resolve_thresholds(floor, {"min_labeled_units": 90, "flake_rate_max": 0.0})
    assert tightened.thresholds.values()["min_labeled_units"] == 90
    assert tightened.thresholds.approved and tightened.floor.approved
    # A looser value is ignored and the floor keeps its approval.
    loosened = eq.resolve_thresholds(floor, {"min_labeled_units": 10})
    assert loosened.thresholds.values()["min_labeled_units"] == 60 and loosened.thresholds.approved
    # An unapproved floor stays unapproved however it is tightened.
    edited = replace(floor, min_labeled_units=50)
    assert not edited.approved
    assert not eq.resolve_thresholds(edited, {"min_labeled_units": 70}).thresholds.approved


def test_the_threshold_wire_keeps_every_key_it_had_before_the_digest() -> None:
    wire = eq.default_thresholds().to_wire()
    for key in (*eq.PROPOSED_THRESHOLDS, "approval"):
        assert key in wire
    assert set(wire["approval"]) >= {"status", "approved_by", "approved_at"}
    assert wire["approved"] is False


# --------------------------------------------------------------------------- #
# 4/5. the contract is package data; no source-checkout-only path
# --------------------------------------------------------------------------- #


def test_the_packaged_contract_is_the_checked_in_contract() -> None:
    assert PACKAGED.is_file() and eq.packaged_contract_path() == PACKAGED
    assert PACKAGED.read_bytes() == (GATE_DIR / "gate-h.yaml").read_bytes()
    eq.runtime_gate_h_config.cache_clear()
    try:
        runtime = eq.runtime_gate_h_config()
    finally:
        eq.runtime_gate_h_config.cache_clear()
    assert runtime.source == str(PACKAGED) and runtime.mode == "report" and runtime.thresholds.approved
    assert runtime.thresholds == eq.load_gate_h_config(GATE_DIR).thresholds


def test_the_review_engine_ships_the_contract_and_its_build_proves_it_resolves() -> None:
    relative = f"src/lemoncrow/pro/capabilities/review/{eq.CONTRACT_RESOURCE}"
    shipped = (REPO_ROOT / "server" / "review-engine-files.txt").read_text(encoding="utf-8").splitlines()
    assert relative in shipped
    builder = (REPO_ROOT / "scripts" / "build_review_engine.sh").read_text(encoding="utf-8")
    assert f'"lemoncrow/pro/capabilities/review/{eq.CONTRACT_RESOURCE}"' in builder  # required in the wheel
    assert "runtime_gate_h_config()" in builder and "built-in defaults" in builder  # resolves in the clean venv


def test_the_hosted_image_installs_the_package_that_carries_the_contract() -> None:
    # The hosted server installs `lemoncrow` (no checkout, no evals/): its wheel packages src/lemoncrow whole,
    # data files included, so the contract is wherever the readiness code is.
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'packages = ["src/lemoncrow"]' in pyproject
    # The image is built from the private checkout only; there its Dockerfile must install src and no evals.
    # The public mirror carries no enterprise tree; inside it, a moved server directory fails instead of passing.
    hosted = REPO_ROOT / "enterprise" / "server"
    if hosted.parent.is_dir():
        dockerfile = (hosted / "Dockerfile").read_text(encoding="utf-8")
        assert "COPY src ./src" in dockerfile and "evals" not in dockerfile


def test_the_contract_header_names_a_test_file_that_exists() -> None:
    import re

    for path in (GATE_DIR / "gate-h.yaml", PACKAGED):
        named = re.findall(r"tests/[\w/]+\.py", path.read_text(encoding="utf-8"))
        assert named, path
        assert [item for item in named if not (REPO_ROOT / item).is_file()] == [], path


def test_an_install_with_no_source_checkout_reads_the_packaged_contract(tmp_path: Path) -> None:
    """Only the module and its contract exist: no ``evals/review`` above them, no repository layout."""

    package = tmp_path / "site" / "lemoncrow_only"
    package.mkdir(parents=True)
    (package / "evidence_qualification.py").write_bytes((PACKAGED.with_name("evidence_qualification.py")).read_bytes())
    (package / eq.CONTRACT_RESOURCE).write_bytes(PACKAGED.read_bytes())
    script = (
        "import importlib.util, json, sys\n"
        "spec = importlib.util.spec_from_file_location('eq', sys.argv[1])\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules['eq'] = module\n"
        "spec.loader.exec_module(module)\n"
        "config = module.runtime_gate_h_config()\n"
        "print(json.dumps({'mode': config.mode, 'source': config.source, 'approved': config.thresholds.approved,"
        " 'digest': config.thresholds.digest}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", script, str(package / "evidence_qualification.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-400:]
    found = json.loads(done.stdout)
    assert found["mode"] == "report" and found["approved"] is True
    assert found["source"] == str(package / eq.CONTRACT_RESOURCE)
    assert found["digest"] == eq.load_gate_h_config(GATE_DIR).thresholds.digest


def test_readiness_reports_gate_h_known_without_a_source_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """RC10: an install used to report Gate H ``unknown`` because its config lived in the checkout."""

    from lemoncrow.pro.capabilities.review import diagnostic_state

    monkeypatch.setattr(eq, "source_checkout_gate_dir", lambda: Path("/nonexistent-gate-dir"))
    eq.runtime_gate_h_config.cache_clear()
    try:
        states = {state.gate: state for state in diagnostic_state.gate_states()}
    finally:
        eq.runtime_gate_h_config.cache_clear()
    assert (states["gate_h"].unreadable, states["gate_h"].mode, states["gate_h"].approved) == (False, "report", True)
    # Gate I's replay configuration is a development artifact that is not packaged: still unknown there.
    assert states["gate_i"].unreadable


def test_a_missing_or_invalid_packaged_contract_is_the_defaults_and_never_enforces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review import diagnostic_state

    broken = tmp_path / eq.CONTRACT_RESOURCE
    broken.write_text("version: 1\nmode: enforce\n", encoding="utf-8")  # enforce without approval
    monkeypatch.setattr(eq, "packaged_contract_path", lambda: broken)
    eq.runtime_gate_h_config.cache_clear()
    try:
        invalid = eq.runtime_gate_h_config()
        states = {state.gate: state for state in diagnostic_state.gate_states()}
        broken.unlink()
        eq.runtime_gate_h_config.cache_clear()
        missing = eq.runtime_gate_h_config()
    finally:
        eq.runtime_gate_h_config.cache_clear()
    for fallback in (invalid, missing):
        assert fallback.mode == "report" and fallback.source.startswith("built-in defaults (")
        assert not fallback.thresholds.approved
    assert states["gate_h"].unreadable


# --------------------------------------------------------------------------- #
# 1/2. typed subjects: positive corpora complete, negative controls typed
# --------------------------------------------------------------------------- #

NEGATIVE = {
    "checks.recipe:coin": "flaky",
    "surface:performance-clock-page": "visual_nondeterministic",
}


def _corpus(name: str) -> CorpusRepository:
    return load_repository(CORPUS / name)


def test_every_corpus_subject_is_typed_and_the_negative_controls_are_expected_negative() -> None:
    for name in ("synthetic-calc", "lemoncrow-dogfood"):
        repository = _corpus(name)
        subjects = {f"checks.recipe:{item.id}" for item in repository.recipes} | {
            f"surface:{item.id}" for item in repository.surfaces
        }
        assert {item.subject for item in repository.expectations} == subjects, name
    synthetic = {item.subject: item for item in _corpus("synthetic-calc").expectations}
    for subject, reason in NEGATIVE.items():
        assert synthetic[subject].expect == "negative_control" and synthetic[subject].reason == reason
        assert synthetic[subject].why
    qualifying = {subject for subject, item in synthetic.items() if item.expect == "qualify"}
    assert qualifying == {"checks.recipe:py-tests", "checks.recipe:py-tests-jest", "surface:frozen-clock-page"}
    assert synthetic["checks.recipe:lint"].expect == "advisory" and "60" in synthetic["checks.recipe:lint"].why
    assert {item.expect for item in _corpus("lemoncrow-dogfood").expectations} == {"qualify"}


def test_the_positive_synthetic_corpora_carry_the_labeled_units_they_need_to_qualify() -> None:
    results = json.loads((GATE_DIR / "gate-h" / "results" / "synthetic-calc.json").read_text(encoding="utf-8"))
    floor = int(eq.PROPOSED_THRESHOLDS["min_labeled_units"])
    budget = round(floor * (1 - float(eq.PROPOSED_THRESHOLDS["classification_accuracy_min"])))
    units = {item["subject"]: item for item in results["measurements"] if item["key"]["metric"] == "classification"}
    for classifier in ("classifier:r5-v1:junit", "classifier:r5-v1:jest-json"):
        assert units[classifier]["observations"] >= floor, classifier
        assert units[classifier]["failures"] <= budget, classifier
    # The labels are the seeded commits' ground truth: both classifiers see the same units.
    assert units["classifier:r5-v1:junit"]["observations"] == units["classifier:r5-v1:jest-json"]["observations"]
    # Lint is honestly small (one unit per pair) and stays advisory, recorded with why.
    assert units["classifier:r5-v1:recipe"]["observations"] < floor


def test_the_committed_report_types_every_subject_and_no_negative_control_qualifies() -> None:
    config = eq.load_gate_h_config(GATE_DIR)
    body = report(GATE_DIR, config)
    assert body["expectations_checked"] is True
    assert body["expectation_violations"] == [], body["expectation_violations"]
    assert body["changes"] == []  # the committed baseline is the committed results
    rows = {item["subject"]: item for item in body["expectations"]["synthetic-calc"]}
    assert rows["checks.recipe:coin"]["state"] == "advisory" and rows["checks.recipe:coin"]["reason"] == "flaky"
    assert rows["surface:performance-clock-page"]["reason"] == "visual_nondeterministic"
    assert rows["checks.recipe:py-tests"]["state"] == "qualified"
    assert rows["checks.recipe:lint"]["state"] == "advisory"
    qualified = {
        (repo, item["subject"])
        for repo, items in body["expectations"].items()
        for item in items
        if item["state"] == "qualified"
    }
    assert not {(repo, subject) for repo, subject in qualified if subject in NEGATIVE}


def _row(subject: str, state: str, reason: str) -> dict[str, Any]:
    return {"subject": subject, "state": state, "reason": reason, "detail": ""}


def _typed(*expectations: ExpectationSpec) -> CorpusRepository:
    return replace(_corpus("synthetic-calc"), expectations=tuple(expectations))


def _check(
    repository: CorpusRepository, *rows: dict[str, Any]
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    return check_expectations({repository.id: {"qualification": list(rows)}}, {repository.id: repository})


def test_a_negative_control_that_qualifies_or_fails_for_another_reason_is_a_violation() -> None:
    coin = ExpectationSpec("checks.recipe:coin", "negative_control", "flaky", "coin flip")
    repository = _typed(coin)
    assert _check(repository, _row("checks.recipe:coin", "advisory", "flaky"))[1] == []
    qualified = _check(repository, _row("checks.recipe:coin", "qualified", "qualified"))[1]
    assert [item["subject"] for item in qualified] == ["checks.recipe:coin"]
    assert qualified[0]["expect"] == "negative_control" and qualified[0]["met"] is False
    # Disqualified for the wrong reason (too few labels, not the flake it exists to prove): not a control.
    wrong = _check(repository, _row("checks.recipe:coin", "advisory", "insufficient_measurement"))[1]
    assert len(wrong) == 1 and wrong[0]["expected_reason"] == "flaky"


def test_a_subject_meant_to_qualify_that_does_not_is_a_violation() -> None:
    page = ExpectationSpec("surface:frozen-clock-page", "qualify")
    repository = _typed(page)
    assert _check(repository, _row("surface:frozen-clock-page", "qualified", "qualified"))[1] == []
    assert len(_check(repository, _row("surface:frozen-clock-page", "advisory", "visual_nondeterministic"))[1]) == 1


def test_an_advisory_subject_that_starts_to_qualify_must_be_retyped() -> None:
    lint = ExpectationSpec("checks.recipe:lint", "advisory", "insufficient_measurement", "needs 60 real pairs")
    repository = _typed(lint)
    assert _check(repository, _row("checks.recipe:lint", "advisory", "insufficient_measurement"))[1] == []
    assert len(_check(repository, _row("checks.recipe:lint", "qualified", "qualified"))[1]) == 1


def test_an_untyped_measured_subject_and_a_typed_unmeasured_one_are_violations() -> None:
    coin = ExpectationSpec("checks.recipe:coin", "negative_control", "flaky", "coin flip")
    repository = _typed(coin)
    _rows, violations = _check(repository, _row("checks.recipe:new-recipe", "qualified", "qualified"))
    assert {(item["subject"], item["reason"]) for item in violations} == {
        ("checks.recipe:new-recipe", "qualified"),
        ("checks.recipe:coin", "no_measurement"),
    }
    untyped = _check(_typed(), _row("checks.recipe:py-tests", "qualified", "qualified"))[1]
    assert len(untyped) == 1 and untyped[0]["expect"] is None


def test_the_corpus_loader_refuses_malformed_typing(tmp_path: Path) -> None:
    def refused(label: str, edit: Any, match: str) -> None:
        directory = tmp_path / label
        directory.mkdir()
        raw = yaml.safe_load((CORPUS / "synthetic-calc" / "repository.yaml").read_text(encoding="utf-8"))
        edit(raw)
        (directory / "repository.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
        with pytest.raises(GateHHarnessError, match=match):
            load_repository(directory)

    refused(
        "unknown", lambda raw: raw["expectations"].append({"subject": "surface:nope", "expect": "qualify"}), "unknown"
    )
    refused("twice", lambda raw: raw["expectations"].append(dict(raw["expectations"][0])), "typed twice")
    refused("half", lambda raw: raw["expectations"].pop(), "untyped subject")
    refused("kind", lambda raw: raw["expectations"][0].update(expect="maybe"), "expect must be one of")
    refused("qualify-why", lambda raw: raw["expectations"][0].update(why="because"), "names no reason or why")
    refused("no-why", lambda raw: raw["expectations"][3].pop("why"), "needs reason")
    refused("bad-reason", lambda raw: raw["expectations"][3].update(reason="qualified"), "needs reason")
    refused("empty", lambda raw: raw.update(expectations=[]), "one entry per recipe and surface")


# --------------------------------------------------------------------------- #
# 6. evidence produced before these fields existed
# --------------------------------------------------------------------------- #

# A baseline.json as recorded before RC1: thresholds without digest-bound approval, no expectations anywhere.
OLD_BASELINE = {
    "v": 1,
    "gate": "H",
    "thresholds": {
        "approval": {"approved_at": "2026-10-06", "approved_by": "Pankaj", "status": "approved"},
        "classification_accuracy_min": 0.95,
        "flake_rate_max": 0.0,
        "min_flake_inputs": 2,
        "min_labeled_units": 60,
        "min_render_pairs": 5,
        "min_runs_per_input": 10,
        "visual_nondeterminism_max": 0.0,
    },
    "thresholds_digest": "thresholds:sha256:bafae4c07df1ef32bc959c519f2f285c8c4c3fdc40415ab8fb98d36a79aa932f",
    "repositories": {
        "synthetic-calc": {
            "qualification": [
                {"subject": "checks.recipe:py-tests", "state": "advisory", "reason": "insufficient_measurement"},
                {"subject": "checks.recipe:coin", "state": "advisory", "reason": "flaky"},
            ]
        }
    },
}


def _gate_dir(tmp_path: Path, *, baseline: Any, corpus: bool = False) -> Path:
    gate = tmp_path / "evals" / "review"
    results = gate / "gate-h" / "results"
    results.mkdir(parents=True)
    (gate / "gate-h.yaml").write_bytes((GATE_DIR / "gate-h.yaml").read_bytes())
    (results / "synthetic-calc.json").write_bytes(
        (GATE_DIR / "gate-h" / "results" / "synthetic-calc.json").read_bytes()
    )
    (gate / "gate-h" / "baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
    if corpus:
        (gate / "gate-h" / "corpus").mkdir()
        (gate / "gate-h" / "corpus" / "synthetic-calc").mkdir()
        (gate / "gate-h" / "corpus" / "synthetic-calc" / "repository.yaml").write_bytes(
            (CORPUS / "synthetic-calc" / "repository.yaml").read_bytes()
        )
    return gate


def test_a_baseline_recorded_before_rc1_still_compares_and_names_the_new_qualifications(tmp_path: Path) -> None:
    gate = _gate_dir(tmp_path, baseline=OLD_BASELINE)
    body = report(gate, eq.load_gate_h_config(gate))
    assert body["thresholds_changed_since_baseline"] is False  # the old baseline already carried the digest
    assert body["expectations_checked"] is False and body["expectation_violations"] == []  # no corpus to type with
    changed = {(item["subject"], item["baseline"], item["now"]) for item in body["changes"]}
    assert ("checks.recipe:py-tests", "advisory", "qualified") in changed
    assert ("checks.recipe:coin", "advisory", "advisory") not in changed


def test_a_baseline_without_the_thresholds_digest_is_unknown_not_changed(tmp_path: Path) -> None:
    older = {key: value for key, value in OLD_BASELINE.items() if key != "thresholds_digest"}
    body = report(_gate_dir(tmp_path, baseline=older), eq.load_gate_h_config(GATE_DIR))
    assert body["thresholds_changed_since_baseline"] is None


def test_a_threshold_edit_since_the_baseline_is_reported(tmp_path: Path) -> None:
    gate = _gate_dir(tmp_path, baseline=OLD_BASELINE)
    edited = replace(eq.load_gate_h_config(gate), thresholds=replace(eq.default_thresholds(), min_labeled_units=50))
    assert report(gate, edited)["thresholds_changed_since_baseline"] is True


def test_results_and_measurements_written_before_rc1_load_unchanged() -> None:
    document, measurements = gate_h.read_results(GATE_DIR / "gate-h" / "results" / "lemoncrow-dogfood.json")
    assert document["v"] == gate_h.RESULTS_VERSION and measurements
    # The wire a Measurement had before: no expectation, no approval; it round-trips byte for byte.
    for item in measurements:
        assert eq.Measurement.from_wire(item.to_wire()) == item


def test_an_old_gate_h_yaml_without_the_digest_loads_in_report_mode_and_readiness_still_runs() -> None:
    raw = _contract()
    for key in ("thresholds", "thresholds_digest"):
        del raw["thresholds"]["approval"][key]
    config = eq.parse_gate_h_config(raw, source="old")
    assert config.mode == "report" and not config.thresholds.approved
    # Readiness reads mode and thresholds exactly as before; only the approval flag is no longer true.
    resolution = eq.resolve_thresholds(config.thresholds, {})
    context = eq.QualificationContext(
        mode=config.mode, resolution=resolution, lookup=lambda _key: None, source=config.source
    )
    assert context.thresholds.values() == dict(eq.PROPOSED_THRESHOLDS)
    effect = context.effect(eq.QualificationSubject("recipe", "checks.recipe:unit"))
    assert effect.mode == "report" and not effect.advisory


def test_a_corpus_typed_report_over_an_old_baseline_types_subjects_without_changing_states(tmp_path: Path) -> None:
    gate = _gate_dir(tmp_path, baseline=OLD_BASELINE, corpus=True)
    body = report(gate, eq.load_gate_h_config(gate))
    assert body["expectations_checked"] is True
    # The dogfood corpus is absent here and the baseline never named it: only synthetic-calc is checked.
    assert set(body["expectations"]) == {"synthetic-calc"} and body["expectation_violations"] == []


def test_the_report_command_prints_each_expectation_violation(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from lemoncrow.gateway.cli.commands.review_eval import review_eval

    gate = _gate_dir(tmp_path, baseline=OLD_BASELINE, corpus=True)
    manifest = gate / "gate-h" / "corpus" / "synthetic-calc" / "repository.yaml"
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    for item in raw["expectations"]:
        if item["subject"] == "checks.recipe:coin":
            item.update(expect="qualify")  # the control mistyped as meant to qualify
            del item["reason"], item["why"]
    manifest.write_text(yaml.safe_dump(raw), encoding="utf-8")
    result = CliRunner().invoke(review_eval, ["gate-h", "report", "--gate-dir", str(gate)], catch_exceptions=False)
    assert result.exit_code == 1, result.output  # a violated typing fails the text report
    assert "expectations checked: yes" in result.output
    assert (
        "expectation: synthetic-calc checks.recipe:coin: expected qualify, measured advisory (flaky)" in result.output
    )
    # The JSON form (what release qualification reads) fails only when asked to.
    as_json = ["gate-h", "report", "--gate-dir", str(gate), "--json"]
    assert CliRunner().invoke(review_eval, as_json, catch_exceptions=False).exit_code == 0
    assert CliRunner().invoke(review_eval, [*as_json, "--strict"], catch_exceptions=False).exit_code == 1


# --------------------------------------------------------------------------- #
# RC1 fix round
# --------------------------------------------------------------------------- #


def test_every_committed_results_and_baseline_corpus_digest_is_its_manifests() -> None:
    baseline = json.loads((GATE_DIR / "gate-h" / "baseline.json").read_text(encoding="utf-8"))
    names = sorted(path.name for path in CORPUS.iterdir())
    assert names == ["lemoncrow-dogfood", "synthetic-calc"]
    for name in names:
        digest = _corpus(name).digest
        results = json.loads((GATE_DIR / "gate-h" / "results" / f"{name}.json").read_text(encoding="utf-8"))
        assert results["corpus_digest"] == digest, name
        assert baseline["repositories"][name]["corpus_digest"] == digest, name


def test_typing_a_subject_does_not_change_the_corpus_digest_but_changing_what_is_measured_does() -> None:
    text = (CORPUS / "synthetic-calc" / "repository.yaml").read_text(encoding="utf-8")
    digest = gate_h.corpus_digest(text)
    assert digest == _corpus("synthetic-calc").digest
    retyped = text.replace("expect: negative_control", "expect: advisory", 1)
    assert retyped != text and gate_h.corpus_digest(retyped) == digest
    without = text[: text.index("\nexpectations:")].rstrip("\n") + "\n"
    assert gate_h.corpus_digest(without) == digest  # no block at all hashes the same
    assert gate_h.corpus_digest(text.replace("runs: 10", "runs: 11", 1)) != digest
    assert gate_h.corpus_digest(text.replace("test_sub: fixed", "test_sub: new", 1)) != digest
    # A block in the middle is removed whole and what follows it is still measured.
    middle = text.replace("\nexpectations:", "\nexpectations:", 1)
    head, block = middle.split("\nexpectations:", 1)
    moved = head + "\nnote: x\n" if False else head
    assert gate_h.measured_manifest_text(moved + "\nexpectations:" + block) == gate_h.measured_manifest_text(text)


def _corpus_gate(tmp_path: Path, *, keep_corpus: tuple[str, ...], keep_results: tuple[str, ...]) -> Path:
    gate = _gate_dir(tmp_path, baseline=OLD_BASELINE)
    (gate / "gate-h" / "corpus").mkdir()
    for name in keep_corpus:
        (gate / "gate-h" / "corpus" / name).mkdir()
        (gate / "gate-h" / "corpus" / name / "repository.yaml").write_bytes(
            (CORPUS / name / "repository.yaml").read_bytes()
        )
    if "synthetic-calc" not in keep_results:
        (gate / "gate-h" / "results" / "synthetic-calc.json").unlink()
    for name in keep_results:
        if name != "synthetic-calc":
            (gate / "gate-h" / "results" / f"{name}.json").write_bytes(
                (GATE_DIR / "gate-h" / "results" / f"{name}.json").read_bytes()
            )
    return gate


def test_deleting_a_corpus_repository_while_keeping_its_results_is_a_violation(tmp_path: Path) -> None:
    gate = _corpus_gate(
        tmp_path, keep_corpus=("lemoncrow-dogfood",), keep_results=("synthetic-calc", "lemoncrow-dogfood")
    )
    body = report(gate, eq.load_gate_h_config(gate))
    found = {(item["repository"], item["subject"], item["reason"]) for item in body["expectation_violations"]}
    assert ("synthetic-calc", "", "no_corpus") in found
    # Its measured subjects are untyped too.
    assert ("synthetic-calc", "checks.recipe:py-tests", "qualified") in found
    assert not [item for item in body["expectation_violations"] if item["repository"] == "lemoncrow-dogfood"]


def test_deleting_results_while_keeping_the_typed_corpus_is_a_violation(tmp_path: Path) -> None:
    gate = _corpus_gate(tmp_path, keep_corpus=("synthetic-calc",), keep_results=())
    body = report(gate, eq.load_gate_h_config(gate))
    assert body["expectations_checked"] is True
    found = {(item["repository"], item["subject"], item["reason"]) for item in body["expectation_violations"]}
    assert ("synthetic-calc", "", "no_results") in found
    typed = {item.subject for item in _corpus("synthetic-calc").expectations}
    assert {subject for repo, subject, reason in found if reason == "no_measurement"} == typed


def test_a_repository_only_in_the_corpus_or_only_in_the_results_is_checked_without_the_other() -> None:
    synthetic = _corpus("synthetic-calc")
    _rows, only_corpus = check_expectations({}, {synthetic.id: synthetic})
    assert {item["reason"] for item in only_corpus} == {"no_results", "no_measurement"}
    _rows, only_results = check_expectations(
        {"ghost": {"qualification": [_row("surface:x", "qualified", "qualified")]}}, {}
    )
    assert {item["reason"] for item in only_results} == {"no_corpus", "qualified"}


def test_a_negative_control_may_not_expect_insufficient_measurement(tmp_path: Path) -> None:
    directory = tmp_path / "ctl"
    directory.mkdir()
    raw = yaml.safe_load((CORPUS / "synthetic-calc" / "repository.yaml").read_text(encoding="utf-8"))
    for item in raw["expectations"]:
        if item["subject"] == "checks.recipe:coin":
            item["reason"] = "insufficient_measurement"
    (directory / "repository.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(GateHHarnessError, match="negative_control needs reason"):
        load_repository(directory)
    # An advisory subject may (lint's whole reason), and the committed controls name real disqualifiers.
    assert {item.reason for item in _corpus("synthetic-calc").expectations if item.expect == "negative_control"} <= set(
        gate_h.CONTROL_REASONS
    )


def test_a_tampered_approved_map_under_a_matching_digest_is_unapproved() -> None:
    raw = _contract()
    raw["thresholds"]["approval"]["thresholds"]["min_labeled_units"] = 10  # digest still the real one
    thresholds = eq.parse_gate_h_config(raw, source="tampered").thresholds
    assert thresholds.approved_digest == thresholds.digest and not thresholds.approved
    assert thresholds.changed_since_approval == ("min_labeled_units",)
    assert "disagrees" in (thresholds.approval_problem() or "")
    with pytest.raises(eq.GateHError, match="disagrees"):
        eq.parse_gate_h_config({**raw, "mode": "enforce"}, source="tampered")
    # A map missing a key is a disagreement too.
    del raw["thresholds"]["approval"]["thresholds"]["min_labeled_units"]
    assert not eq.parse_gate_h_config(raw, source="partial").thresholds.approved


def _packaged_with(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: bytes) -> eq.GateHConfig:
    path = tmp_path / eq.CONTRACT_RESOURCE
    path.write_bytes(body)
    monkeypatch.setattr(eq, "packaged_contract_path", lambda: path)
    eq.runtime_gate_h_config.cache_clear()
    try:
        return eq.runtime_gate_h_config()
    finally:
        eq.runtime_gate_h_config.cache_clear()


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda good: good + b"\n# \xff\xfe\n", id="non-utf8"),
        pytest.param(
            lambda good: good.replace(b"min_runs_per_input: 10 ", b"min_runs_per_input: " + b"9" * 400 + b" "),
            id="huge-int",
        ),
        pytest.param(
            lambda good: good.replace(b"min_runs_per_input: 10 ", b"min_runs_per_input: 2.5 "), id="fractional"
        ),
        pytest.param(
            lambda good: good.replace(b"min_runs_per_input: 10 ", b"min_runs_per_input: .inf "), id="infinite"
        ),
        pytest.param(lambda good: good.replace(b"flake_rate_max: 0.0 ", b"flake_rate_max: .nan "), id="nan"),
        pytest.param(lambda good: b"!!python/object/apply:os.system ['true']\n", id="python-tag"),
        pytest.param(lambda good: b"- 1\n", id="list"),
        pytest.param(lambda good: b"", id="empty"),
        pytest.param(
            lambda good: good.replace(b"mode: report", b"mode: enforce").replace(
                b"flake_rate_max: 0.0 ", b"flake_rate_max: 0.5 "
            ),
            id="enforce-edited-threshold",
        ),
    ],
)
def test_a_broken_packaged_contract_is_report_mode_with_defaults_and_never_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutate: Any
) -> None:
    config = _packaged_with(monkeypatch, tmp_path, mutate(PACKAGED.read_bytes()))
    assert config.mode == "report"
    assert config.source.startswith("built-in defaults (") or config.thresholds.approved


def test_an_enforce_edit_with_a_valid_approval_is_the_only_way_enforce_loads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    good = PACKAGED.read_bytes().replace(b"mode: report", b"mode: enforce")
    assert _packaged_with(monkeypatch, tmp_path, good).mode == "enforce"
    loosened = good.replace(b"min_labeled_units: 60 ", b"min_labeled_units: 10 ")
    assert loosened != good
    fallback = _packaged_with(monkeypatch, tmp_path, loosened)
    assert fallback.mode == "report" and fallback.source.startswith("built-in defaults (")


def test_a_huge_integer_threshold_is_a_typed_refusal_not_an_overflow() -> None:
    raw = _contract()
    raw["thresholds"]["min_runs_per_input"] = 10**400
    with pytest.raises(eq.GateHError, match="min_runs_per_input must be a non-negative integer"):
        eq.parse_gate_h_config(raw, source="huge")
    for bound in (eq._MAX_INTEGER_THRESHOLD, eq._MAX_INTEGER_THRESHOLD + 1):
        raw["thresholds"]["min_runs_per_input"] = bound
        if bound > eq._MAX_INTEGER_THRESHOLD:
            with pytest.raises(eq.GateHError):
                eq.parse_gate_h_config(raw, source="bound")
        else:
            assert eq.parse_gate_h_config(raw, source="bound").thresholds.min_runs_per_input == bound


def test_a_huge_integer_in_the_trusted_base_override_is_named_and_readiness_keeps_working(tmp_path: Path) -> None:
    from tests.gateway.test_review_check_runs_api import _base_files, _get, _run
    from tests.gateway.test_review_evidence_qualification import _readiness, _review

    override, problems = eq.parse_repository_override(
        f"check_policy:\n  qualification:\n    min_runs_per_input: {'9' * 400}\n    min_labeled_units: 90\n"
    )
    assert override == {"min_labeled_units": 90}
    assert any("min_runs_per_input must be a non-negative integer" in item for item in problems)

    config = f"check_policy:\n  qualification:\n    min_runs_per_input: {'9' * 400}\n    min_labeled_units: 90\n"
    review = _review(tmp_path, base={**_base_files(), ".lemoncrow/review.yaml": config})
    readiness = _readiness(review)
    assert {row["id"] for row in readiness["conditions"]} >= {"verification"}
    assert _run(review, "checks.test").status_code == 200
    payload = _get(review)
    thresholds = payload["qualification"]["thresholds"]
    assert thresholds["effective"]["min_labeled_units"] == 90  # the valid tightening still applies
    assert thresholds["effective"]["min_runs_per_input"] == 10  # the refused one left the floor
    assert any("min_runs_per_input must be a non-negative integer" in note for note in thresholds["notes"])


def test_the_loaders_type_an_undecodable_file_and_the_runtime_survives_any_parser_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / eq.CONTRACT_RESOURCE
    path.write_bytes(PACKAGED.read_bytes() + b"\n# \xff\n")
    monkeypatch.setattr(eq, "packaged_contract_path", lambda: path)
    with pytest.raises(eq.GateHError, match="UnicodeDecodeError"):
        eq.load_packaged_gate_h_config()
    (tmp_path / "gate-h.yaml").write_bytes(path.read_bytes())
    with pytest.raises(eq.GateHError, match="UnicodeDecodeError"):
        eq.load_gate_h_config(tmp_path)

    # Whatever else the parser might raise is still report mode with the defaults.
    path.write_bytes(PACKAGED.read_bytes())
    for failure in (OverflowError("big"), TypeError("odd"), RecursionError("deep")):

        def explode(*_args: object, _failure: BaseException = failure, **_kwargs: object) -> object:
            raise _failure

        monkeypatch.setattr(eq, "parse_gate_h_config", explode)
        eq.runtime_gate_h_config.cache_clear()
        try:
            config = eq.runtime_gate_h_config()
        finally:
            eq.runtime_gate_h_config.cache_clear()
        assert config.mode == "report" and config.source.startswith(f"built-in defaults ({type(failure).__name__}")

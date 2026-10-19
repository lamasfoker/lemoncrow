"""Gate H: behavior evidence quality -- qualification contract, readiness consumption, store/API (plan Gate H)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import evidence_qualification as eq
from lemoncrow.pro.capabilities.review.evidence_qualification import (
    APPROVED,
    PROPOSED_THRESHOLDS,
    GateHConfig,
    GateHError,
    Measurement,
    QualificationEffect,
    QualificationSubject,
    SubjectKey,
    classifier_key,
    default_thresholds,
    load_gate_h_config,
    load_measurement,
    merge_measurements,
    parse_gate_h_config,
    parse_repository_override,
    qualify,
    resolve_thresholds,
    store_measurements,
)
from lemoncrow.pro.capabilities.review.readiness import (
    BehaviorFacts,
    OrgCheckFact,
    OrgPolicyFacts,
    ReadinessInputs,
    VerificationFacts,
    _behavior_changes_judged,
    _browser_regression,
    _org_policy,
    _verification,
    built_in_policy_layer,
    conditions_digest,
    policy_generation,
    verification_gate,
)
from tests.gateway.test_review_check_runs_api import HEADERS, _get, _review, _run
from tests.gateway.test_review_hermetic import needs_bwrap

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV = "author_machine@host:sha256:" + "a" * 64
RECIPE_DIGEST = "recipe:sha256:" + "b" * 64
JUNIT = classifier_key("junit")
RUNS = int(PROPOSED_THRESHOLDS["min_runs_per_input"])
LABELED = int(PROPOSED_THRESHOLDS["min_labeled_units"])
PAIRS = int(PROPOSED_THRESHOLDS["min_render_pairs"])
ALLOWED_WRONG = round(LABELED * (1 - float(PROPOSED_THRESHOLDS["classification_accuracy_min"])))


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


def _approved(mode: str = "enforce") -> GateHConfig:
    thresholds = replace(default_thresholds(), approval_status=APPROVED, approved_by="po", approved_at="2026-10-04")
    return GateHConfig(mode=mode, thresholds=thresholds, source="test")


def _flake(failures: int = 0, *, inputs: int = 2, runs: int = RUNS, digest: str = RECIPE_DIGEST) -> Measurement:
    return Measurement(SubjectKey("flake", digest, ENV), "recipe:unit", inputs, failures, runs * inputs, runs)


def _accuracy(wrong: int = 0, *, labeled: int = LABELED, classifier: str = JUNIT) -> Measurement:
    return Measurement(SubjectKey("classification", classifier), classifier, labeled, wrong)


def _lookup(*measurements: Measurement) -> Any:
    found = {item.key: item for item in measurements}
    return found.get


RECIPE = QualificationSubject("recipe", "checks.recipe:unit", RECIPE_DIGEST, ENV, JUNIT)
SURFACE = QualificationSubject("surface", "visual.dom_a11y", "surface:sha256:" + "c" * 64, ENV)


def test_evidence_qualification_threshold_contract() -> None:
    """case: test_thresholds_load_from_the_versioned_gate_h_config_in_report_mode"""
    config = load_gate_h_config(REPO_ROOT / "evals" / "review")
    assert config.mode == "report"
    assert config.thresholds.values() == dict(PROPOSED_THRESHOLDS)
    assert config.thresholds.approved
    assert (config.thresholds.approved_by, config.thresholds.approved_at) == ("Pankaj", "2026-10-06")
    # The runtime switch readiness uses is the packaged copy of the same contract and is off.
    eq.runtime_gate_h_config.cache_clear()
    try:
        assert eq.runtime_gate_h_config().mode == "report"
        assert eq.runtime_gate_h_config().source.endswith(eq.CONTRACT_RESOURCE)
        assert eq.runtime_gate_h_config().thresholds.approved
    finally:
        eq.runtime_gate_h_config.cache_clear()
    "case: test_a_repository_override_round_trips_and_only_tightens_the_floor"
    content = "check_policy:\n  reruns: 1\n  qualification:\n    min_runs_per_input: 20\n    flake_rate_max: 0.0\n    classification_accuracy_min: 0.5\n    visual_nondeterminism_max: 0.25\n    min_render_pairs: 1\n"
    override, problems = parse_repository_override(content)
    assert problems == ()
    assert override == {
        "min_runs_per_input": 20,
        "flake_rate_max": 0.0,
        "classification_accuracy_min": 0.5,
        "visual_nondeterminism_max": 0.25,
        "min_render_pairs": 1,
    }
    resolved = resolve_thresholds(default_thresholds(), override)
    effective = resolved.thresholds
    assert effective.min_runs_per_input == 20 > RUNS
    assert effective.classification_accuracy_min == 0.95
    assert effective.visual_nondeterminism_max == 0.0 and effective.min_render_pairs == PAIRS
    assert len(resolved.notes) == 3 and all("would loosen the Gate H floor" in note for note in resolved.notes)
    assert resolved.to_wire()["override"] == override
    bad, errors = parse_repository_override("check_policy:\n  qualification:\n    flake_rate_max: 2\n    nope: 1\n")
    assert bad == {} and any("unknown keys: nope" in item for item in errors)
    assert any("flake_rate_max must be a number from 0 to 1" in item for item in errors)
    assert parse_repository_override("") == ({}, ())
    assert parse_repository_override("check_policy:\n  qualification: 3\n")[1] == (
        "check_policy.qualification must be a mapping",
    )
    "case: test_a_deterministic_measured_recipe_qualifies_and_a_flaky_one_is_advisory"
    thresholds = default_thresholds()
    assert qualify(RECIPE, _lookup(_flake(0), _accuracy(0)), thresholds).state == "qualified"
    flaky = qualify(RECIPE, _lookup(_flake(1), _accuracy(0)), thresholds)
    assert (flaky.state, flaky.reason) == ("advisory", "flaky")
    assert "1/2 input(s)" in flaky.detail
    rows = {("repo-a", item.key): item for item in (_flake(0), _accuracy(0))}
    verdict = eq.qualification(
        "repo-a", RECIPE, measurements=lambda repo, key: rows.get((repo, key)), thresholds=thresholds
    )
    assert verdict.qualified
    other = eq.qualification(
        "repo-b", RECIPE, measurements=lambda repo, key: rows.get((repo, key)), thresholds=thresholds
    )
    assert (other.state, other.reason) == ("unmeasured", "unmeasured")
    "case: test_bounds_are_semantics_at_and_past_each_minimum_and_maximum"
    thresholds = default_thresholds()
    assert qualify(RECIPE, _lookup(_flake(0, inputs=2, runs=RUNS), _accuracy()), thresholds).qualified
    assert (
        qualify(RECIPE, _lookup(_flake(0, inputs=1, runs=RUNS), _accuracy()), thresholds).reason
        == "insufficient_measurement"
    )
    assert (
        qualify(RECIPE, _lookup(_flake(0, inputs=2, runs=RUNS - 1), _accuracy()), thresholds).reason
        == "insufficient_measurement"
    )
    assert ALLOWED_WRONG == 3
    assert qualify(RECIPE, _lookup(_flake(), _accuracy(ALLOWED_WRONG)), thresholds).qualified
    inaccurate = qualify(RECIPE, _lookup(_flake(), _accuracy(ALLOWED_WRONG + 1)), thresholds)
    assert (inaccurate.state, inaccurate.reason) == ("advisory", "classification_inaccurate")
    assert (
        qualify(RECIPE, _lookup(_flake(), _accuracy(0, labeled=LABELED - 1)), thresholds).reason
        == "insufficient_measurement"
    )
    loose = replace(thresholds, flake_rate_max=0.25)
    assert qualify(RECIPE, _lookup(_flake(1, inputs=4), _accuracy()), loose).qualified
    assert qualify(RECIPE, _lookup(_flake(2, inputs=4), _accuracy()), loose).reason == "flaky"
    "case: test_unmeasured_unavailable_and_unidentified_are_never_qualified"
    thresholds = default_thresholds()
    missing = qualify(RECIPE, _lookup(_flake()), thresholds)
    assert (missing.state, missing.reason) == ("unmeasured", "unmeasured")
    unavailable = Measurement(SubjectKey("visual", SURFACE.digest, ENV), "s", 0, 0, unavailable="no browser")
    rendered = qualify(SURFACE, _lookup(unavailable), thresholds)
    assert (rendered.state, rendered.reason) == ("unmeasured", "measurement_unavailable")
    assert "no browser" in rendered.detail
    anonymous = qualify(replace(RECIPE, environment_class=""), _lookup(_flake(), _accuracy()), thresholds)
    assert (anonymous.state, anonymous.reason) == ("unmeasured", "unidentified")
    other = replace(RECIPE, id="checks.recipe:other", digest="recipe:sha256:" + "d" * 64)
    composite = QualificationSubject("recipe", "checks.test", components=(RECIPE, other))
    result = qualify(composite, _lookup(_flake(), _accuracy()), thresholds)
    assert result.state == "unmeasured" and result.detail.startswith("checks.recipe:other:")
    both = qualify(composite, _lookup(_flake(), _flake(digest=other.digest), _accuracy()), thresholds)
    assert both.qualified
    "case: test_visual_determinism_identical_renders_qualify_and_any_difference_disqualifies"
    thresholds = default_thresholds()
    key = SubjectKey("visual", SURFACE.digest, ENV)
    assert qualify(SURFACE, _lookup(Measurement(key, "s", PAIRS, 0)), thresholds).qualified
    differs = qualify(SURFACE, _lookup(Measurement(key, "s", PAIRS, 1)), thresholds)
    assert (differs.state, differs.reason) == ("advisory", "visual_nondeterministic")
    insufficient = qualify(SURFACE, _lookup(Measurement(key, "s", PAIRS - 1, 0)), thresholds)
    assert insufficient.reason == "insufficient_measurement"


def test_evidence_qualification_mode_contract() -> None:
    """case: test_the_mode_decides_whether_an_unqualified_result_may_gate"""
    verdict = qualify(RECIPE, _lookup(), default_thresholds())
    report, enforce = (QualificationEffect(verdict, "report"), QualificationEffect(verdict, "enforce"))
    assert (report.advisory, report.would_be_advisory, report.may_gate) == (False, True, True)
    assert (enforce.advisory, enforce.would_be_advisory, enforce.may_gate) == (True, True, False)
    qualified = QualificationEffect(qualify(RECIPE, _lookup(_flake(), _accuracy()), default_thresholds()), "enforce")
    assert qualified.may_gate and (not qualified.would_be_advisory)
    assert eq.may_gate(enforce.to_wire()) is False and eq.may_gate(report.to_wire()) is True
    assert eq.may_gate(None) is True
    "case: test_measurements_round_trip_on_the_wire_and_merge_across_corpora"
    item = replace(_accuracy(1), confusion={"fixed": {"fixed": 3, "unchanged": 1}}, cost_seconds=1.25)
    assert Measurement.from_wire(item.to_wire()) == replace(item, cost_seconds=1.25)
    with pytest.raises(ValueError):
        Measurement.from_wire({**item.to_wire(), "v": 99})
    with pytest.raises(ValueError):
        Measurement(SubjectKey("flake", "d"), "s", 1, 2)
    merged = merge_measurements([item, replace(_accuracy(0, labeled=10), confusion={"fixed": {"fixed": 2}}), _flake()])
    accuracy = next(row for row in merged if row.key.metric == "classification")
    assert (accuracy.observations, accuracy.failures) == (LABELED + 10, 1)
    assert accuracy.confusion == {"fixed": {"fixed": 5, "unchanged": 1}}
    unavailable = Measurement(_flake().key, "s", 0, 0, unavailable="no sandbox")
    assert merge_measurements([unavailable, _flake(1)])[0].failures == 1
    assert merge_measurements([unavailable, unavailable])[0].unavailable == "no sandbox"
    "case: test_report_mode_changes_no_readiness_condition_and_records_the_would_be_effect"
    plain = _wire(_verification(_inputs(_projection(None, ROWS, {}))))
    report = _wire(_verification(_inputs(_projection("report", ROWS, {"unit": False, "lint": True}))))
    assert conditions_digest(report) == conditions_digest(plain)
    assert [(row["state"], row["detail"]) for row in report] == [(row["state"], row["detail"]) for row in plain]
    assert report[0]["state"] == "fail"
    effect = report[0]["evidence_qualification"]
    assert effect["mode"] == "report" and effect["advisory"] == []
    assert [row["stable_key"] for row in effect["would_be_advisory"]] == ["checks.recipe:unit"]
    assert effect["would_be"]["state"] == "pass" and "advisory until Gate H" in effect["would_be"]["detail"]
    enforced = _wire(_verification(_inputs(_projection("enforce", ROWS, {"unit": False, "lint": True}))))
    assert (enforced[0]["state"], enforced[0]["detail"]) == (effect["would_be"]["state"], effect["would_be"]["detail"])
    "case: test_enforce_mode_an_unqualified_result_neither_satisfies_nor_blocks"
    blocked = _wire(_verification(_inputs(_projection("enforce", ROWS, {"unit": False, "lint": True}))))[0]
    assert blocked["state"] == "pass" and [ref["evidence_id"] for ref in blocked["evidence_refs"]] == ["ev-lint"]
    assert blocked["evidence_qualification"]["advisory"][0]["reason"] == "unmeasured"
    only = [_row("unit", "PASS")]
    advisory = _wire(_verification(_inputs(_projection("enforce", only, {"unit": False}))))[0]
    nothing = _wire(_verification(_inputs(_projection(None, [], {}))))[0]
    assert (advisory["state"], advisory["reason"], advisory["evidence_refs"]) == ("not_applicable", "advisory_only", [])
    assert nothing["state"] == "not_applicable"
    gate = verification_gate(_projection("enforce", only, {"unit": False}))
    assert gate["current"] == [] and [row["name"] for row in gate["advisory"]] == ["unit"]
    gating = _wire(_verification(_inputs(_projection("enforce", ROWS, {"unit": True, "lint": True}))))[0]
    assert gating["state"] == "fail" and "evidence_qualification" not in gating


def test_evidence_qualification_readiness_contract() -> None:
    """case: test_a_required_browser_scenario_with_only_an_advisory_result_stays_open"""
    browser = _row("checkout", "PASS", browser={"scenario": "checkout"}, required=True, kind="browser")
    passed = _wire(_browser_regression(_inputs(_projection("enforce", [browser], {"checkout": True}))))
    assert passed[0]["state"] == "pass"
    held = _wire(_browser_regression(_inputs(_projection("enforce", [browser], {"checkout": False}))))
    assert held[0]["state"] == "pending" and held[0]["evidence_refs"] == []
    failing = {**browser, "status": "FAIL"}
    not_failed = _wire(_browser_regression(_inputs(_projection("enforce", [failing], {"checkout": False}))))
    assert not_failed[0]["state"] == "pending"
    report = _wire(_browser_regression(_inputs(_projection("report", [failing], {"checkout": False}))))
    plain = _wire(_browser_regression(_inputs(_projection(None, [failing], {}))))
    assert conditions_digest(report) == conditions_digest(plain) and report[0]["state"] == "fail"
    assert report[0]["evidence_qualification"]["would_be"]["state"] == "pending"
    "case: test_behavior_targets_from_advisory_results_neither_block_nor_satisfy"
    projection = _projection(None, [], {})
    plain = _wire(_behavior_changes_judged(_inputs(projection, behavior=BehaviorFacts(open=2, blocked=0))))
    report = _wire(
        _behavior_changes_judged(
            _inputs(
                projection,
                behavior=BehaviorFacts(open=2, gate_h_mode="report", would_be={"open": 0, "blocked": 0, "advisory": 2}),
            )
        )
    )
    assert conditions_digest(report) == conditions_digest(plain) and report[0]["state"] == "pending"
    assert report[0]["evidence_qualification"]["would_be"]["state"] == "pass"
    enforced = _wire(
        _behavior_changes_judged(_inputs(projection, behavior=BehaviorFacts(advisory=2, gate_h_mode="enforce")))
    )
    assert enforced[0]["state"] == "pass" and "advisory until Gate H" in enforced[0]["detail"]
    "case: test_an_org_required_check_resting_on_advisory_evidence_is_not_satisfied"
    check = OrgCheckFact(stable_key="checks.recipe:unit", requirement_key="unit", satisfied=True, evidence_id="ev-unit")
    policy = OrgPolicyFacts(required_checks=(check,))
    rows = [_row("unit", "PASS")]
    enforce = _wire(_org_policy(_inputs(_projection("enforce", rows, {"unit": False}), org_policy=policy)))
    assert (enforce[0]["state"], enforce[0]["reason"]) == ("pending", "advisory_evidence")
    report = _wire(_org_policy(_inputs(_projection("report", rows, {"unit": False}), org_policy=policy)))
    plain = _wire(_org_policy(_inputs(_projection(None, rows, {}), org_policy=policy)))
    assert conditions_digest(report) == conditions_digest(plain) and report[0]["state"] == "pass"
    assert report[0]["evidence_qualification"]["would_be"]["reason"] == "advisory_evidence"
    qualified = _wire(_org_policy(_inputs(_projection("enforce", rows, {"unit": True}), org_policy=policy)))
    assert qualified[0]["state"] == "pass"


def test_enforce_needs_approved_thresholds_and_a_broken_file_never_enforces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = {"version": 1, "mode": "enforce", "thresholds": {"approval": {"status": "pending_product_owner_approval"}}}
    with pytest.raises(GateHError, match="requires product-owner-approved thresholds"):
        parse_gate_h_config(raw, source="gate-h.yaml")
    bound = {
        "status": APPROVED,
        "approved_by": "po",
        "approved_at": "2026-10-04",
        "thresholds_digest": eq.default_thresholds().digest,
    }
    approved = {"version": 1, "mode": "enforce", "thresholds": {"approval": bound}}
    assert parse_gate_h_config(approved, source="gate-h.yaml").mode == "enforce"
    for bad in ({"flake_rate_max": 1.5}, {"min_runs_per_input": 2.5}, {"classification_accuracy_min": -1}, {"x": 1}):
        with pytest.raises(GateHError):
            parse_gate_h_config({**approved, "thresholds": {**approved["thresholds"], **bad}}, source="g")
    # An invalid packaged contract at runtime is report mode with the defaults.
    (tmp_path / eq.CONTRACT_RESOURCE).write_text("version: 1\nmode: enforce\n", encoding="utf-8")
    monkeypatch.setattr(eq, "packaged_contract_path", lambda: tmp_path / eq.CONTRACT_RESOURCE)
    eq.runtime_gate_h_config.cache_clear()
    try:
        fallback = eq.runtime_gate_h_config()
        assert fallback.mode == "report" and fallback.source.startswith("built-in defaults (")
    finally:
        eq.runtime_gate_h_config.cache_clear()


def _row(name: str, status: str, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "kind": "verification",
        "status": status,
        "state": "current",
        "attestation_class": "lemoncrow_verification",
        "authority": "attested",
        "stable_key": f"checks.recipe:{name}",
        "evidence_id": f"ev-{name}",
        **extra,
    }


def _mark(mode: str, *, qualified: bool) -> dict[str, Any]:
    return {
        "state": "qualified" if qualified else "unmeasured",
        "reason": "qualified" if qualified else "unmeasured",
        "mode": mode,
        "advisory": mode == "enforce" and (not qualified),
        "would_be_advisory": not qualified,
    }


def _projection(mode: str | None, rows: list[dict[str, Any]], marks: dict[str, bool]) -> dict[str, Any]:
    checks = [
        {**row, "qualification": _mark(mode, qualified=marks[row["name"]])} if mode and row["name"] in marks else row
        for row in rows
    ]
    projection: dict[str, Any] = {"checks": checks, "browser": {}}
    if mode:
        projection["qualification"] = {"gate": "H", "mode": mode}
    return projection


def _inputs(projection: dict[str, Any], **facts: object) -> ReadinessInputs:
    return ReadinessInputs(
        review_id="review-1",
        snapshot_id="rr-1",
        snapshot_number=1,
        evaluated_at="2026-10-04T12:00:00Z",
        policy=policy_generation([built_in_policy_layer()]),
        facts={"verification": VerificationFacts(projection=projection, snapshot_number=1), **facts},
    )


def _wire(conditions: Any) -> list[dict[str, Any]]:
    return [item.to_wire() for item in conditions]


ROWS = [_row("unit", "FAIL"), _row("lint", "PASS")]


def test_measurement_rows_round_trip_per_repository_with_bounded_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    store, repository = (review.store, str(review.store.get_session(review.review_id).repo_root))
    written = store_measurements(
        store,
        review_id=review.review_id,
        revision_id=review.revision_id,
        repository=repository,
        measurements=[_flake()],
    )
    assert written == 1
    assert load_measurement(store, repository, _flake().key) == _flake()
    assert load_measurement(store, "another-repository", _flake().key) is None
    store_measurements(
        store,
        review_id=review.review_id,
        revision_id=review.revision_id,
        repository=repository,
        measurements=[_flake(1)],
    )
    found = load_measurement(store, repository, _flake().key)
    assert found is not None and found.failures == 1
    over = [_flake(digest=f"recipe:sha256:{index:064x}") for index in range(eq.MAX_IMPORT_MEASUREMENTS + 1)]
    with pytest.raises(ValueError, match="exceed the import limit"):
        store_measurements(
            store, review_id=review.review_id, revision_id=review.revision_id, repository=repository, measurements=over
        )
    at_bound = over[: eq.MAX_IMPORT_MEASUREMENTS]
    assert (
        store_measurements(
            store,
            review_id=review.review_id,
            revision_id=review.revision_id,
            repository=repository,
            measurements=at_bound,
        )
        == eq.MAX_IMPORT_MEASUREMENTS
    )
    monkeypatch.setattr(eq, "MAX_QUALIFICATION_ROWS", 3)
    keys = [_flake(digest=f"recipe:sha256:{index:064x}") for index in range(900, 904)]
    for item in keys:
        store_measurements(
            store,
            review_id=review.review_id,
            revision_id=review.revision_id,
            repository=repository,
            measurements=[item],
        )
    assert load_measurement(store, repository, keys[0].key) is None
    assert all(load_measurement(store, repository, item.key) is not None for item in keys[1:])


def _readiness(review: SimpleNamespace) -> dict[str, Any]:
    response = review.client.get(f"/api/reviews/{review.review_id}/readiness", headers=HEADERS)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _condition(readiness: dict[str, Any], condition_id: str) -> dict[str, Any]:
    return next(row for row in readiness["conditions"] if row["id"] == condition_id)


def _gate_h_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(eq, "annotate_verification", lambda projection, *args, **kwargs: projection)
    monkeypatch.setattr(eq, "partition_behavior", lambda *args, **kwargs: None)


@needs_bwrap
def test_local_api_report_mode_is_inert_and_enforce_mode_makes_unqualified_results_advisory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path)
    ran = _run(review, "checks.test")
    assert ran.status_code == 200, ran.text
    assert ran.json()["verdicts"] == {"checks.recipe:unit": "FAIL", "checks.test": "FAIL"}
    with monkeypatch.context() as off:
        _gate_h_off(off)
        before = _readiness(review)
    assert _condition(before, "verification")["state"] == "fail"
    monkeypatch.setattr(eq, "runtime_gate_h_config", lambda: eq.DEFAULT_GATE_H_CONFIG)
    report = _readiness(review)
    assert report["conditions_hash"] == before["conditions_hash"]
    assert [(row["id"], row["state"], row["detail"]) for row in report["conditions"]] == [
        (row["id"], row["state"], row["detail"]) for row in before["conditions"]
    ]
    verification = _condition(report, "verification")
    effect = verification["evidence_qualification"]
    assert effect["mode"] == "report"
    assert {row["stable_key"] for row in effect["would_be_advisory"]} == {"checks.recipe:unit", "checks.test"}
    assert effect["would_be"]["reason"] == "advisory_only"
    behavior = _condition(report, "behavior-changes-judged")
    assert behavior["state"] == _condition(before, "behavior-changes-judged")["state"] == "pending"
    assert behavior["evidence_qualification"]["would_be_advisory_targets"] >= 1
    payload = _get(review)
    verdict = next(row for row in payload["verdicts"] if row["stable_key"] == "checks.recipe:unit")
    mark = verdict["qualification"]
    assert (mark["state"], mark["reason"], mark["advisory"], mark["would_be_advisory"]) == (
        "unmeasured",
        "unmeasured",
        False,
        True,
    )
    assert next(row for row in payload["results"] if row["recipe"]["id"] == "unit")["qualification"] == mark
    assert payload["qualification"]["mode"] == "report" and payload["qualification"]["would_be_advisory"] == 2
    kind = next(row for row in payload["verdicts"] if row["stable_key"] == "checks.test")["qualification"]
    assert [item["id"] for item in kind["subject"]["components"]] == ["checks.recipe:unit"]
    subject = mark["subject"]
    assert subject["digest"] == payload["entry_digests"]["checks.recipe:unit"]
    assert subject["environment_class"].startswith("author_machine@host:sha256:")
    assert subject["classifier"] == JUNIT
    listing = review.client.get(f"/api/reviews/{review.review_id}/evidence-qualification", headers=HEADERS)
    assert listing.status_code == 200, listing.text
    assert [row["stable_key"] for row in listing.json()["subjects"]] == ["checks.recipe:unit", "checks.test"]
    monkeypatch.setattr(eq, "runtime_gate_h_config", lambda: _approved("enforce"))
    enforced = _readiness(review)
    verification = _condition(enforced, "verification")
    assert (verification["state"], verification["reason"]) == ("not_applicable", "advisory_only")
    assert verification["evidence_refs"] == []
    assert {row["stable_key"] for row in verification["evidence_qualification"]["advisory"]} == {
        "checks.recipe:unit",
        "checks.test",
    }
    assert verification["detail"] == effect["would_be"]["detail"]
    assert enforced["conditions_hash"] != before["conditions_hash"]
    assert _condition(enforced, "behavior-changes-judged")["evidence_qualification"]["advisory_targets"] >= 1
    store, session = (review.store, review.store.get_session(review.review_id))
    qualified = [
        Measurement(SubjectKey("flake", subject["digest"], subject["environment_class"]), "unit", 2, 0, 2 * RUNS, RUNS),
        Measurement(SubjectKey("classification", JUNIT), JUNIT, LABELED, 0),
    ]
    store_measurements(
        store,
        review_id=review.review_id,
        revision_id=review.revision_id,
        repository=str(session.repo_root),
        measurements=qualified,
    )
    gating = _condition(_readiness(review), "verification")
    assert gating["state"] == "fail" and "evidence_qualification" not in gating
    store_measurements(
        store,
        review_id=review.review_id,
        revision_id=review.revision_id,
        repository=str(session.repo_root),
        measurements=[replace(qualified[0], failures=1)],
    )
    flaky = _condition(_readiness(review), "verification")
    assert flaky["state"] == "not_applicable"
    assert flaky["evidence_qualification"]["advisory"][0]["reason"] == "flaky"

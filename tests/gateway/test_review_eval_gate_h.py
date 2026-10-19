"""Gate H measurement harness: flake rate, classification accuracy, visual determinism and the CLI."""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from lemoncrow.pro.capabilities.review import evidence_qualification as eq
from lemoncrow.pro.capabilities.review.differential import TestCase, TestReport, classify_recipe
from lemoncrow.pro.capabilities.review.evaluation import gate_h
from lemoncrow.pro.capabilities.review.evaluation.gate_h import (
    GateHHarnessError,
    MaterializedRepository,
    RunOutcome,
    SurfaceSpec,
    load_repository,
    materialize,
    measure_classification,
    measure_flake,
    measure_visual,
    run_repository,
)
from lemoncrow.pro.capabilities.review.evidence_qualification import (
    QualificationSubject,
    SubjectKey,
    classifier_key,
    default_thresholds,
    qualify,
)
from lemoncrow.pro.capabilities.review.recipes import CheckRecipe, parse_config_recipes
from lemoncrow.pro.capabilities.review.render_driver import PageCapture, RenderEnvironment, chrome_executable
from tests.gateway.test_review_check_runs_api import (
    CALC_BASE,
    CALC_HEAD,
    CONFIG,
    FMT,
    HEADERS,
    RUN_TESTS,
    TESTS_BASE,
    TESTS_HEAD,
    _get,
    _review,
    _run,
)
from tests.gateway.test_review_hermetic import needs_bwrap

REPO = Path(__file__).resolve().parents[2]
GATE_DIR = REPO / "evals" / "review"
ENV = "author_machine@host:sha256:" + "e" * 64
JUNIT = classifier_key("junit")
# The proposed floors (gate-h.yaml): measurements here are sized to them.
RUNS = int(eq.PROPOSED_THRESHOLDS["min_runs_per_input"])
LABELED = int(eq.PROPOSED_THRESHOLDS["min_labeled_units"])
needs_chrome = pytest.mark.skipif(not chrome_executable(), reason="no non-snap Chrome/Chromium on this host")


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


def _recipe(yaml_rows: str) -> CheckRecipe:
    recipes, errors = parse_config_recipes("check_recipes:\n" + yaml_rows)
    assert errors == ()
    return recipes[0]


UNIT = _recipe("  - id: unit\n    kind: test\n    command: python3 run_tests.py {report}\n    report: junit\n")


def _report(statuses: dict[str, str]) -> TestReport:
    return TestReport("junit", tuple(TestCase(f"tests.t::{name}", status) for name, status in statuses.items()))  # type: ignore[arg-type]


class ScriptedExecutor:
    """Per-commit scripted outcomes, consumed in order (the last repeats)."""

    name = "scripted"

    def __init__(self, script: dict[str, Sequence[dict[str, str] | None]]) -> None:
        self.script = {key: list(value) for key, value in script.items()}
        self.calls: list[str] = []

    def run(self, recipe: CheckRecipe, repository: Any, commit: str) -> RunOutcome:
        self.calls.append(commit)
        queue = self.script[commit]
        statuses = queue.pop(0) if len(queue) > 1 else queue[0]
        if statuses is None:
            return RunOutcome("not_run", None, "", "author_machine", "", 0.01, "sandbox unavailable")
        failed = any(value == "failed" for value in statuses.values())
        return RunOutcome(
            "failed" if failed else "passed",
            _report(statuses),
            "envfp:sha256:" + "1" * 64,
            "author_machine",
            "host:sha256:" + "e" * 64,
            0.25,
        )


def _repo(*commits: str) -> MaterializedRepository:
    return MaterializedRepository(Path("/nonexistent"), {name: name * 8 for name in commits})


# --------------------------------------------------------------------------- #
# (a) flake rate
# --------------------------------------------------------------------------- #


def test_a_random_recipe_is_measured_flaky_and_a_deterministic_one_qualifies() -> None:
    steady = {"test_a": "passed", "test_b": "failed"}
    executor = ScriptedExecutor({"base": [steady], "head": [steady]})
    measured, detail = measure_flake(
        UNIT, ["base", "head"], runs=RUNS, executor=executor, repository=_repo("base", "head")
    )
    assert (measured.observations, measured.failures, measured.runs, measured.min_runs_per_observation) == (
        2,
        0,
        2 * RUNS,
        RUNS,
    )
    assert measured.key == SubjectKey("flake", UNIT.digest, ENV)
    assert detail["base"]["disagree"] is False and measured.cost_seconds == pytest.approx(0.25 * 2 * RUNS)
    accuracy = eq.Measurement(SubjectKey("classification", JUNIT), JUNIT, LABELED, 0)
    subject = QualificationSubject("recipe", "checks.recipe:unit", UNIT.digest, ENV, JUNIT)
    lookup = {item.key: item for item in (measured, accuracy)}.get
    assert qualify(subject, lookup, default_thresholds()).state == "qualified"

    # Head: a test that passes and fails at random on one fixed input.
    coin = [{"test_a": "passed", "test_coin": "passed"}, {"test_a": "passed", "test_coin": "failed"}]
    flaky_executor = ScriptedExecutor({"base": [steady], "head": coin * 3})
    flaky, detail = measure_flake(
        UNIT, ["base", "head"], runs=RUNS, executor=flaky_executor, repository=_repo("base", "head")
    )
    assert (flaky.observations, flaky.failures) == (2, 1) and detail["head"]["unstable_tests"] == ["tests.t::test_coin"]
    verdict = qualify(subject, {item.key: item for item in (flaky, accuracy)}.get, default_thresholds())
    assert (verdict.state, verdict.reason) == ("advisory", "flaky")
    effect = eq.QualificationEffect(verdict, "enforce")
    assert effect.advisory and not effect.may_gate


def test_flake_bounds_and_unexecuted_runs_are_unmeasured_never_passes() -> None:
    steady = {"test_a": "passed"}
    executor = ScriptedExecutor({"base": [steady]})
    at_cap, _ = measure_flake(
        UNIT, ["base"], runs=gate_h.MAX_RUNS_PER_INPUT, executor=executor, repository=_repo("base")
    )
    assert at_cap.runs == gate_h.MAX_RUNS_PER_INPUT
    with pytest.raises(GateHHarnessError, match="runs must be from 2"):
        measure_flake(UNIT, ["base"], runs=gate_h.MAX_RUNS_PER_INPUT + 1, executor=executor, repository=_repo("base"))
    with pytest.raises(GateHHarnessError, match="exceeds the limit"):
        measure_flake(
            UNIT, ["base"] * (gate_h.MAX_FLAKE_INPUTS + 1), runs=2, executor=executor, repository=_repo("base")
        )
    nothing = ScriptedExecutor({"base": [None]})
    unmeasured, _ = measure_flake(UNIT, ["base"], runs=3, executor=nothing, repository=_repo("base"))
    assert unmeasured.observations == 0 and unmeasured.unavailable == "sandbox unavailable"
    subject = QualificationSubject("recipe", "checks.recipe:unit", UNIT.digest, "author_machine@x", JUNIT)
    assert qualify(subject, {unmeasured.key: unmeasured}.get, default_thresholds()).state == "unmeasured"


# --------------------------------------------------------------------------- #
# (b) classification accuracy against labeled cases
# --------------------------------------------------------------------------- #

CORPUS = """\
version: 1
id: scripted
executor: sandbox
runs: 3
reruns: 1
repository:
  kind: synthetic
  commits:
    - id: base
      files: {README.md: "x\\n"}
    - id: head
      parent: base
      files: {README.md: "y\\n"}
recipes:
  - id: unit
    kind: test
    command: python3 run_tests.py {report}
    report: junit
cases:
  - id: seeded
    recipe: unit
    base: base
    head: head
    default: unchanged
    labels:
      test_broken: introduced_failure
      test_old: pre_existing_failure
      test_repaired: fixed
      test_added: new
      "@recipe": pre_existing_failure
"""


def _corpus(tmp_path: Path, text: str = CORPUS) -> Any:
    folder = tmp_path / "corpus" / "repo"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "repository.yaml").write_text(text, encoding="utf-8")
    return load_repository(folder)


BASE_UNITS = {
    "test_ok_1": "passed",
    "test_ok_2": "passed",
    "test_broken": "passed",
    "test_old": "failed",
    "test_repaired": "failed",
}
HEAD_UNITS = {**BASE_UNITS, "test_broken": "failed", "test_repaired": "passed", "test_added": "passed"}


def test_r5_matches_labeled_ground_truth_and_a_misclassifying_stub_is_disqualified(tmp_path: Path) -> None:
    spec = _corpus(tmp_path)
    executor = ScriptedExecutor({"base": [BASE_UNITS], "head": [HEAD_UNITS]})
    measured, details = measure_classification(spec, executor=executor, repository=_repo("base", "head"))
    (accuracy,) = measured
    assert accuracy.key == SubjectKey("classification", JUNIT)
    assert (accuracy.observations, accuracy.failures) == (7, 0)  # six tests and the recipe unit
    assert accuracy.confusion["introduced_failure"] == {"introduced_failure": 1}
    assert accuracy.confusion["unchanged"] == {"unchanged": 2}
    # R5's attempt policy: a failing side is rerun (reruns: 1).
    assert details[0]["head_attempts"] == ["failed", "failed"] and details[0]["base_attempts"] == ["failed", "failed"]

    def stub(recipe: CheckRecipe, attempts: Any, **kwargs: Any) -> Any:
        real = classify_recipe(recipe, attempts, **kwargs)
        # Calls every introduced failure pre-existing and every new test unchanged.
        swapped = {"introduced_failure": "pre_existing_failure", "new": "unchanged"}
        tests = [replace(unit, klass=swapped.get(unit.klass, unit.klass)) for unit in real.tests]
        return replace(real, tests=tests)

    executor = ScriptedExecutor({"base": [BASE_UNITS], "head": [HEAD_UNITS]})
    (wrong,), details = measure_classification(
        spec, executor=executor, repository=_repo("base", "head"), classifier=stub
    )
    assert (wrong.observations, wrong.failures) == (7, 2)
    assert wrong.confusion["introduced_failure"] == {"pre_existing_failure": 1}
    assert {row["unit"].rpartition("::")[2] for row in details[0]["wrong"]} == {"test_broken", "test_added"}
    flake = eq.Measurement(SubjectKey("flake", "recipe:sha256:x", ENV), "unit", 2, 0, 2 * RUNS, RUNS)
    subject = QualificationSubject("recipe", "checks.recipe:unit", "recipe:sha256:x", ENV, JUNIT)
    thresholds = replace(default_thresholds(), min_labeled_units=7)
    assert qualify(subject, {flake.key: flake, accuracy.key: accuracy}.get, thresholds).qualified
    verdict = qualify(subject, {flake.key: flake, wrong.key: wrong}.get, thresholds)
    assert (verdict.state, verdict.reason) == ("advisory", "classification_inaccurate")
    assert "71.4%" in verdict.detail


def test_an_unreported_labeled_unit_counts_as_absent_and_bounds_are_refused(tmp_path: Path) -> None:
    text = CORPUS.replace("      test_added: new\n", "      test_added: new\n      test_ghost: removed\n")
    spec = _corpus(tmp_path, text)
    executor = ScriptedExecutor({"base": [BASE_UNITS], "head": [HEAD_UNITS]})
    (accuracy,), _details = measure_classification(spec, executor=executor, repository=_repo("base", "head"))
    assert accuracy.failures == 1 and accuracy.confusion["removed"] == {gate_h.ABSENT: 1}
    too_many = "".join(f"      test_{index}: unchanged\n" for index in range(gate_h.MAX_LABELS_PER_CASE + 1))
    with pytest.raises(GateHHarnessError, match="exceeds the limit"):
        _corpus(tmp_path, CORPUS.replace("      test_old: pre_existing_failure\n", too_many))
    with pytest.raises(GateHHarnessError, match="unknown class"):
        _corpus(tmp_path, CORPUS.replace("test_old: pre_existing_failure", "test_old: broken"))
    with pytest.raises(GateHHarnessError, match="runs must be an integer"):
        _corpus(tmp_path, CORPUS.replace("runs: 3", f"runs: {gate_h.MAX_RUNS_PER_INPUT + 1}"))


# --------------------------------------------------------------------------- #
# (c) visual determinism
# --------------------------------------------------------------------------- #


class FakeDriver:
    def __init__(self, *, available: bool = True, timestamp: bool = False) -> None:
        self.available, self.timestamp, self.renders = available, timestamp, 0

    def environment(self) -> RenderEnvironment:
        if not self.available:
            return RenderEnvironment(False, "rendering diffs need a non-snap Chrome/Chromium on this host")
        return RenderEnvironment(True, browser="FakeChrome 1.0", fonts_digest="fonts:1")

    def capture(self, site: Path, cells: Any, controls: Any, *, spa_fallback: bool, timeout: float) -> list[Any]:
        self.renders += 1
        stamp = f"rendered {self.renders}" if self.timestamp else "rendered"
        return [PageCapture(cell.id, "captured", dom_lines=("h1 Orders", f"p {stamp}")) for cell in cells]


def _site(tmp_path: Path) -> MaterializedRepository:
    corpus = _corpus(
        tmp_path,
        CORPUS.replace(
            'files: {README.md: "x\\n"}', 'files: {README.md: "x\\n", site/index.html: "<h1>Orders</h1>\\n"}'
        ),
    )
    return materialize(corpus, tmp_path / "work", source_root=REPO)


PAIRS = int(eq.PROPOSED_THRESHOLDS["min_render_pairs"])
SURFACE = SurfaceSpec("orders", "visual.dom_a11y", "base", {"static_root": "site", "routes": ["/"]}, PAIRS)


def test_identical_renders_qualify_a_timestamp_disqualifies_and_no_renderer_is_unmeasured(tmp_path: Path) -> None:
    repository = _site(tmp_path)
    thresholds = default_thresholds()
    same, _ = measure_visual(SURFACE, repository=repository, driver=FakeDriver(), scratch=tmp_path, image_digest="img")
    assert (same.observations, same.failures, same.runs) == (PAIRS, 0, 2 * PAIRS)
    subject = QualificationSubject("surface", "orders", same.key.digest, same.key.environment_class)
    assert same.key.environment_class == "author_machine@img"
    assert qualify(subject, {same.key: same}.get, thresholds).qualified

    stamped, detail = measure_visual(
        SURFACE, repository=repository, driver=FakeDriver(timestamp=True), scratch=tmp_path, image_digest="img"
    )
    assert (stamped.observations, stamped.failures) == (PAIRS, PAIRS) and detail["pairs"][0]["differs"] == ["dom"]
    verdict = qualify(subject, {stamped.key: stamped}.get, thresholds)
    assert (verdict.state, verdict.reason) == ("advisory", "visual_nondeterministic")

    missing, _ = measure_visual(
        SURFACE, repository=repository, driver=FakeDriver(available=False), scratch=tmp_path, image_digest="img"
    )
    assert missing.observations == 0 and "Chrome" in missing.unavailable
    # Nothing measured is recorded as such, never as a pass.
    gone = qualify(
        QualificationSubject("surface", "orders", missing.key.digest, "author_machine@img"),
        {missing.key: missing}.get,
        thresholds,
    )
    assert gone.state == "unmeasured"
    with pytest.raises(GateHHarnessError, match="pairs must be"):
        measure_visual(
            replace(SURFACE, pairs=gate_h.MAX_RENDER_PAIRS + 1),
            repository=repository,
            driver=FakeDriver(),
            scratch=tmp_path,
        )


def test_the_surface_digest_is_what_an_r7_render_diff_record_states(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.render_diff import parse_render_config

    config = parse_render_config(SURFACE.render)
    wire = eq.render_subject_wire(
        "visual.dom_a11y",
        config.digest,
        browser="FakeChrome 1.0",
        fonts_digest="fonts:1",
        sandbox="bubblewrap",
        executor_class="author_machine",
        image_digest="img",
    )
    measured, _ = measure_visual(
        SURFACE, repository=_site(tmp_path), driver=FakeDriver(), scratch=tmp_path, image_digest="img"
    )
    assert (measured.key.digest, measured.key.environment_class) == (wire["digest"], wire["environment_class"])
    subject = eq.render_qualification_subject({"entry": "visual.dom_a11y", "qualification_subject": wire})
    assert subject is not None and subject.keys() == (measured.key,)


@needs_bwrap
@needs_chrome
@pytest.mark.parametrize(
    ("surface_index", "expected_failures", "expect_dom_diff"),
    [(0, 0, False), (1, 1, True)],
    ids=("frozen", "performance-now"),
)
def test_a_real_browser_frozen_page_is_deterministic_and_performance_now_is_not(
    tmp_path: Path,
    surface_index: int,
    expected_failures: int,
    expect_dom_diff: bool,
) -> None:
    from lemoncrow.pro.capabilities.review.render_driver import ChromeCdpDriver

    spec = load_repository(GATE_DIR / "gate-h" / "corpus" / "synthetic-calc")
    repository = materialize(spec, tmp_path / "work", source_root=REPO)
    surface = replace(spec.surfaces[surface_index], pairs=1)
    measured, detail = measure_visual(
        surface,
        repository=repository,
        driver=ChromeCdpDriver(),
        scratch=tmp_path,
    )
    assert (measured.observations, measured.failures) == (1, expected_failures), (measured.unavailable, detail["pairs"])
    assert ("dom" in detail["pairs"][0]["differs"]) is expect_dom_diff


# --------------------------------------------------------------------------- #
# end to end: measured on the sandbox, keyed like the product, consumed by readiness
# --------------------------------------------------------------------------- #


def _fixture_corpus(tmp_path: Path) -> Any:
    def block(text: str) -> str:
        return "\n".join("          " + line if line else "" for line in text.splitlines()) + "\n"

    files = {"calc.py": CALC_BASE, "tests/test_calc.py": TESTS_BASE, "run_tests.py": RUN_TESTS, "fmt.py": FMT}
    head = {"calc.py": CALC_HEAD, "tests/test_calc.py": TESTS_HEAD}
    recipes = yaml.safe_load(CONFIG)["check_recipes"]
    text = f"version: 1\nid: fixture\nexecutor: sandbox\nruns: {RUNS}\nreruns: 1\nrepository:\n  kind: synthetic\n  commits:\n" "    - id: base\n      files:\n" + "".join(
        f"        {path}: |\n{block(content)}" for path, content in files.items()
    ) + "    - id: head\n      parent: base\n      files:\n" + "".join(
        f"        {path}: |\n{block(content)}" for path, content in head.items()
    ) + yaml.safe_dump(
        {"recipes": recipes[:1], "flake": {"unit": ["base", "head"]}}, sort_keys=False
    )
    return _corpus(tmp_path, text)


@needs_bwrap
def test_sandbox_measurements_are_keyed_like_product_results_and_qualify_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path / "review")
    assert _run(review, "checks.test").status_code == 200
    spec = _fixture_corpus(tmp_path / "corpus")
    document = run_repository(spec, source_root=REPO, parts=("flake",))
    (flake,) = [eq.Measurement.from_wire(item) for item in document["measurements"]]
    assert (flake.observations, flake.failures, flake.runs) == (2, 0, 2 * RUNS)
    # Same recipe digest (inputs pinned from the trusted base) and environment class as the product's verdict.
    payload = _get(review)
    mark = next(row for row in payload["verdicts"] if row["stable_key"] == "checks.recipe:unit")["qualification"]
    assert flake.key.digest == mark["subject"]["digest"] == payload["entry_digests"]["checks.recipe:unit"]
    assert flake.key.environment_class == mark["subject"]["environment_class"]

    # Imported through the CLI into the Review's repository, they qualify the result in enforce mode.
    results = tmp_path / "results.json"
    accuracy = eq.Measurement(SubjectKey("classification", JUNIT), JUNIT, LABELED, 0)
    results.write_text(
        json.dumps({**document, "measurements": [*document["measurements"], accuracy.to_wire()]}), encoding="utf-8"
    )
    imported = _cli(
        "import",
        "--gate-dir",
        str(GATE_DIR),
        "--review",
        review.review_id,
        "--results",
        str(results),
        "--store-root",
        str(review.store_root),
    )
    assert imported.exit_code == 0, imported.output
    assert "imported 2 measurement(s)" in imported.output
    approved = replace(default_thresholds(), approval_status=eq.APPROVED, approved_by="po", approved_at="2026-10-04")
    monkeypatch.setattr(eq, "runtime_gate_h_config", lambda: eq.GateHConfig(mode="enforce", thresholds=approved))
    readiness = review.client.get(f"/api/reviews/{review.review_id}/readiness", headers=HEADERS).json()
    verification = next(row for row in readiness["conditions"] if row["id"] == "verification")
    assert verification["state"] == "fail" and "evidence_qualification" not in verification


# --------------------------------------------------------------------------- #
# CLI: record and report over versioned results
# --------------------------------------------------------------------------- #


def _cli(*args: str) -> Any:
    from lemoncrow.gateway.cli.commands.review_eval import review_eval

    return CliRunner().invoke(review_eval, ["gate-h", *args], catch_exceptions=False)


def test_record_writes_a_baseline_and_report_compares_against_it(tmp_path: Path) -> None:
    gate = tmp_path / "evals" / "review"
    gate.mkdir(parents=True)
    shutil.copy(GATE_DIR / "gate-h.yaml", gate / "gate-h.yaml")
    flake = eq.Measurement(SubjectKey("flake", UNIT.digest, ENV), "recipe:unit", 2, 0, 2 * RUNS, RUNS)
    accuracy = eq.Measurement(SubjectKey("classification", JUNIT), JUNIT, LABELED + 5, 1)
    document = {
        "v": 1,
        "repository": "demo",
        "recipes": [{"id": "unit", "digest": UNIT.digest, "report": "junit"}],
        "measurements": [flake.to_wire(), accuracy.to_wire()],
        "cost": {"executions": 10, "seconds": 2.5},
    }
    results = gate / "gate-h" / "results"
    results.mkdir(parents=True)
    (results / "demo.json").write_text(json.dumps(document), encoding="utf-8")
    recorded = _cli("record", "--gate-dir", str(gate))
    assert recorded.exit_code == 0, recorded.output
    baseline = json.loads((gate / "gate-h" / "baseline.json").read_text(encoding="utf-8"))
    assert baseline["repositories"]["demo"]["qualification"] == [
        {
            "subject": "checks.recipe:unit",
            "state": "qualified",
            "reason": "qualified",
            "detail": "measured within the Gate H thresholds",
        }
    ]
    assert baseline["thresholds"]["approval"]["status"] == "approved"
    # A flakier re-measurement shows up as a change against the baseline.
    flaky = replace(flake, failures=1)
    (results / "demo.json").write_text(
        json.dumps({**document, "measurements": [flaky.to_wire(), accuracy.to_wire()]}), encoding="utf-8"
    )
    reported = _cli("report", "--gate-dir", str(gate), "--json")
    assert reported.exit_code == 0, reported.output
    body = json.loads(reported.output)
    assert body["mode"] == "report"
    assert body["changes"] == [
        {"repository": "demo", "subject": "checks.recipe:unit", "baseline": "qualified", "now": "advisory"}
    ]


@dataclass(frozen=True)
class _Repository:
    id: str


def test_results_dir_measures_and_reports_outside_the_committed_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # G11-RELEASE measures into a scratch directory: `run --results-dir` writes there and
    # `report --results-dir` reads there, against the committed baseline; the gate-h.yaml
    # results directory is never written.
    gate = tmp_path / "evals" / "review"
    gate.mkdir(parents=True)
    shutil.copy(GATE_DIR / "gate-h.yaml", gate / "gate-h.yaml")
    flake = eq.Measurement(SubjectKey("flake", UNIT.digest, ENV), "recipe:unit", 2, 0, 2 * RUNS, RUNS)
    document = {
        "v": 1,
        "repository": "demo",
        "recipes": [{"id": "unit", "digest": UNIT.digest, "report": "junit"}],
        "measurements": [flake.to_wire()],
        "cost": {"executions": 1, "seconds": 0.1},
        "executor": "host",
    }
    monkeypatch.setattr(gate_h, "load_corpus", lambda root, config, only=(): [_Repository("demo")])
    monkeypatch.setattr(gate_h, "run_repository", lambda repository, **_: document)
    scratch = tmp_path / "scratch"
    measured = _cli("run", "--gate-dir", str(gate), "--results-dir", str(scratch))
    assert measured.exit_code == 0, measured.output
    assert json.loads((scratch / "demo.json").read_text(encoding="utf-8"))["repository"] == "demo"
    assert not (gate / "gate-h" / "results").exists()

    reported = _cli("report", "--gate-dir", str(gate), "--results-dir", str(scratch), "--json")
    assert reported.exit_code == 0, reported.output
    body = json.loads(reported.output)
    assert set(body["repositories"]) == {"demo"}
    assert body["baseline"] == str(gate / "gate-h" / "baseline.json")
    # Without the option, report reads the (empty) configured results directory.
    default = json.loads(_cli("report", "--gate-dir", str(gate), "--json").output)
    assert default["repositories"] == {}


def test_the_versioned_corpus_loads_within_its_bounds() -> None:
    config = gate_h.load_gate(GATE_DIR)
    corpus = gate_h.load_corpus(GATE_DIR, config)
    ids = {item.id for item in corpus}
    assert {"synthetic-calc", "lemoncrow-dogfood"} <= ids
    dogfood = next(item for item in corpus if item.id == "lemoncrow-dogfood")
    assert dogfood.executor == "host" and dict(dogfood.source_revs)["head"].startswith("189074ae9")

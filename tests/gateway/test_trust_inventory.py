"""Trust primitive inventory: each of the twelve primitives the trust work builds on is on ``main``.

The authority map (``docs/architecture/review-authority-map.md``) names one authority per
Review concern. These tests keep it honest. Each primitive's entry point must be defined in
its module and referenced by a production caller (an import alone is not wiring). Each one
must also map to entries in the capability status index, which states its present-tense
support, default and debt; this file adds no second inventory. Every module the map names
must exist.
"""

from __future__ import annotations

import ast
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REVIEW = ROOT / "src" / "lemoncrow" / "pro" / "capabilities" / "review"
PACKAGE = "lemoncrow.pro.capabilities.review"
ADR = ROOT / "docs" / "architecture" / "review-authority-map.md"
STATUS_INDEX = ROOT / "docs" / "status" / "capabilities.toml"


@dataclass(frozen=True, slots=True)
class Anchor:
    """``module.symbol`` (module relative to the Review package) and the production file that uses it."""

    module: str
    symbol: str
    caller: str


@dataclass(frozen=True, slots=True)
class Primitive:
    name: str
    anchors: tuple[Anchor, ...]
    capabilities: tuple[str, ...]


PRIMITIVES: tuple[Primitive, ...] = (
    Primitive(
        "revision reconciliation",
        (Anchor("revisions", "reconcile", "sources/local.py"),),
        ("review-local",),
    ),
    Primitive(
        "typed evidence and checks",
        (
            Anchor("evidence_adapters", "admit", "check_ingest_api.py"),
            Anchor("check_ingest", "record_check_ingest", "check_ingest_api.py"),
        ),
        ("review-check-runs", "review-premerge-checks"),
    ),
    Primitive(
        "evidence freshness",
        (Anchor("freshness", "assess_freshness", "api.py"),),
        ("gate-f",),
    ),
    Primitive(
        "trusted-base policy generation",
        (
            Anchor("readiness", "policy_generation", "api.py"),
            Anchor("trusted_base", "resolve_trusted_base_document", "api.py"),
        ),
        ("gate-f", "review-local"),
    ),
    Primitive(
        "readiness",
        (Anchor("readiness", "DEFAULT_CONDITIONS", "api.py"),),
        ("review-local", "review-landing"),
    ),
    Primitive(
        "run plan",
        (Anchor("run_plan", "rebuild_run_plan", "run_plan_service.py"),),
        ("review-run-plan",),
    ),
    Primitive(
        "impact selection and carry",
        (
            Anchor("test_impact", "build_impact_map", "run_plan_service.py"),
            Anchor("incremental", "explain_carry", "check_runs.py"),
        ),
        ("review-test-impact", "review-check-runs"),
    ),
    Primitive(
        "signed Review Record",
        (Anchor("record_signing", "sign_review_record", "review_record_api.py"),),
        ("review-record-signing",),
    ),
    Primitive(
        "model identity and independence",
        (
            Anchor("independence", "independence_condition", "readiness.py"),
            Anchor("model_identity", "ModelIdentity", "ai_review.py"),
        ),
        ("review-intelligence-auto", "review-auto-approval"),
    ),
    Primitive(
        "track record",
        (Anchor("track_record", "track_record_assessment", "run_plan_service.py"),),
        ("review-author-track-record",),
    ),
    Primitive(
        "change-set readiness",
        (Anchor("change_sets", "readiness_condition", "api.py"),),
        ("review-change-sets",),
    ),
    Primitive(
        "deployment outcome facts",
        (Anchor("outcomes.deployment_outcomes", "apply_observations", "outcomes/local_deployments.py"),),
        ("review-deployments", "production-observation-local", "production-observation-hosted"),
    ),
)

_IDS = [primitive.name for primitive in PRIMITIVES]


def _module_path(module: str) -> Path:
    return REVIEW / (module.replace(".", "/") + ".py")


def _defines(tree: ast.Module, symbol: str) -> bool:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol:
            return True
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == symbol for t in node.targets):
            return True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == symbol:
            return True
    return False


def _is_module(source: str, level: int, module: str) -> bool:
    """Whether an import source names ``module`` (absolute, or relative by its trailing path)."""

    if level == 0:
        return source == f"{PACKAGE}.{module}"
    return source == module or source.endswith(f".{module}") or source == module.rsplit(".", 1)[-1]


def _uses(caller: Path, anchor: Anchor) -> bool:
    """Whether ``caller`` binds ``anchor.symbol`` (or its module) by import and then reads it."""

    tree = ast.parse(caller.read_text(encoding="utf-8"))
    parent, _, leaf = f"{PACKAGE}.{anchor.module}".rpartition(".")
    names: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            source = node.module or ""
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name == anchor.symbol and _is_module(source, node.level, anchor.module):
                    names.add(bound)
                elif alias.name == leaf and node.level == 0 and source == parent:
                    modules.add(bound)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == f"{PACKAGE}.{anchor.module}" and alias.asname:
                    modules.add(alias.asname)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names:
            return True
        if (
            isinstance(node, ast.Attribute)
            and node.attr == anchor.symbol
            and isinstance(node.value, ast.Name)
            and node.value.id in modules
        ):
            return True
    return False


def test_the_inventory_covers_the_twelve_trust_primitives() -> None:
    assert len(PRIMITIVES) == 12
    assert len(set(_IDS)) == 12


@pytest.mark.parametrize("primitive", PRIMITIVES, ids=_IDS)
def test_each_primitive_is_defined_and_wired_into_a_production_caller(primitive: Primitive) -> None:
    for anchor in primitive.anchors:
        module = _module_path(anchor.module)
        assert module.is_file(), f"{primitive.name}: {module} is missing"
        assert _defines(
            ast.parse(module.read_text(encoding="utf-8")), anchor.symbol
        ), f"{primitive.name}: {anchor.module}.{anchor.symbol} is not defined"
        caller = REVIEW / anchor.caller
        assert caller.is_file(), f"{primitive.name}: caller {caller} is missing"
        assert _uses(caller, anchor), f"{primitive.name}: {anchor.caller} never uses {anchor.module}.{anchor.symbol}"


def test_the_wiring_check_rejects_an_import_that_is_never_read(tmp_path: Path) -> None:
    anchor = Anchor("freshness", "assess_freshness", "unused.py")
    unused = tmp_path / "unused.py"
    unused.write_text(f"from {PACKAGE}.freshness import assess_freshness\n", encoding="utf-8")
    assert not _uses(unused, anchor)
    called = tmp_path / "called.py"
    called.write_text(f"from {PACKAGE} import freshness as f\n\nf.assess_freshness()\n", encoding="utf-8")
    assert _uses(called, anchor)
    other = tmp_path / "other.py"
    other.write_text(f"from {PACKAGE}.readiness import assess_freshness\n\nassess_freshness()\n", encoding="utf-8")
    assert not _uses(other, anchor)


def test_the_authority_map_names_only_modules_that_exist_and_every_primitive_module() -> None:
    text = ADR.read_text(encoding="utf-8")
    named = set(re.findall(r"`((?:[a-z_]+/)*[a-z_]+\.py)`", text))
    missing = sorted(
        path for path in named if not (REVIEW / path).is_file() and not (REVIEW / "outcomes" / path).is_file()
    )
    assert not missing, f"the authority map names modules that do not exist: {missing}"
    for primitive in PRIMITIVES:
        for anchor in primitive.anchors:
            leaf = anchor.module.rsplit(".", 1)[-1] + ".py"
            assert leaf in named, f"the authority map does not name {leaf} ({primitive.name})"


def test_each_primitive_maps_to_supported_entries_in_the_status_index() -> None:
    if not STATUS_INDEX.is_file():
        pytest.skip("the capability status index is kept in the private checkout only")
    capabilities = {row["id"]: row for row in tomllib.loads(STATUS_INDEX.read_text(encoding="utf-8"))["capability"]}
    for primitive in PRIMITIVES:
        for capability in primitive.capabilities:
            assert capability in capabilities, f"{primitive.name}: {capability} is not in the status index"
            state = capabilities[capability]["state"]
            assert state not in {"unsupported", "blocked"}, f"{primitive.name}: {capability} is {state}, not on main"

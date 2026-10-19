"""The Gate I digest set: what a measurement was taken against.

Each component is ``{"digest": sha256, "detail": ...}`` so a gate result can
name exactly which of these changed since the baseline:

* ``model`` -- the replay reviewer (driver/model) and every identity the
  fixtures were recorded with;
* ``prompt`` -- prompt/schema versions, the hypothesis and investigation system
  prompts, the three output schemas, every system prompt observed during the
  replay (covers stages the static probe cannot reach, e.g. dedupe) and the
  candidate-generation markers that decide what the hypothesis prompt offers;
* ``policy_default`` -- built-in Review-policy defaults: default run settings,
  proposed budget defaults, per-cost-class minute estimates, the default
  finding threshold, severity normalization, criticality depth floors, the
  deep-review escalation thresholds and the rule-check ceiling;
* ``preset`` -- the Quick/Auto/Thorough/Deep selections, the capability
  catalog, AI call estimates and AI effort profiles;
* ``corpus`` / ``fixtures`` -- the cases and the recorded model outputs;
* ``replay`` -- the selections replayed and the matching rules.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict
from typing import Any, Final

from lemoncrow.pro.capabilities.review.evaluation.corpus import Corpus, digest
from lemoncrow.pro.capabilities.review.evaluation.fixtures import CaseFixtures, schema_digest, text_digest

DIGEST_COMPONENTS: Final[tuple[str, ...]] = (
    "model",
    "prompt",
    "policy_default",
    "preset",
    "corpus",
    "fixtures",
    "replay",
)
#: Bump when matching or metric semantics change (a ``replay`` component change).
#: 2: the gate view carries behavior catches and cost totals (compute minutes,
#: model calls, dollars: cost per catch); unknown compute minutes stay unknown,
#: and provider costs in another currency are unknown, never dollars.
HARNESS_VERSION: Final[int] = 3


def _component(detail: Any) -> dict[str, Any]:
    return {"digest": digest(detail), "detail": detail}


def prompt_component(observed_systems: Iterable[tuple[str, str]] = ()) -> dict[str, Any]:
    from lemoncrow.pro.capabilities.review import ai_review

    probe = ai_review.ReviewHypothesis(
        id="probe",
        category="correctness",
        target_ids=("probe",),
        failure_mode="",
        investigation_rationale="",
        priority_rationale="",
        evidence_needed="",
    )
    hypothesis_system, _ = ai_review._hypothesis_prompt((), {}, (), 1)
    investigation_system, _ = ai_review._investigation_prompt(probe, {}, (), ())
    return _component(
        {
            "prompt_version": ai_review.PROMPT_VERSION,
            "schema_version": ai_review.SCHEMA_VERSION,
            "systems": {
                "hypothesis_generation": text_digest(hypothesis_system),
                "finding_investigation": text_digest(investigation_system),
            },
            "schemas": {
                "hypothesis_generation": schema_digest(ai_review.HYPOTHESIS_SCHEMA),
                "finding_investigation": schema_digest(ai_review.INVESTIGATION_SCHEMA),
                "finding_dedupe": schema_digest(ai_review.DEDUPE_SCHEMA),
            },
            "observed_systems": sorted(f"{stage}:{value}" for stage, value in set(observed_systems)),
            # Candidate generation decides what the hypothesis prompt offers the model.
            "candidate_markers": {
                category: [pattern.pattern for _marker, pattern in patterns]
                for category, patterns in ai_review._MARKER_PATTERNS.items()
            },
        }
    )


def policy_default_component() -> dict[str, Any]:
    from lemoncrow.pro.capabilities.review import capability_catalog, criticality
    from lemoncrow.pro.capabilities.review.escalation import ESCALATION_DEFAULTS, escalation_document
    from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallBudget
    from lemoncrow.pro.capabilities.review.rule_checks import RULE_CHECK_DEFAULTS
    from lemoncrow.pro.capabilities.review.run_plan import PROPOSED_BUDGET_DEFAULTS, EffectiveRunSettings

    return _component(
        {
            "run_settings": EffectiveRunSettings().digest_document(),
            "proposed_budget_defaults": dict(PROPOSED_BUDGET_DEFAULTS),
            "minutes_by_cost": dict(capability_catalog._MINUTES_BY_COST),
            "static_minimum_minutes": capability_catalog.STATIC_MINIMUM_MINUTES,
            "model_call_budget": asdict(ModelCallBudget()),
            "finding_threshold": criticality.DEFAULT_FINDING_THRESHOLD,
            "ai_category_severity": dict(criticality._AI_CATEGORY_SEVERITY),
            "behavior_severity": dict(criticality._BEHAVIOR_SEVERITY),
            "criticality_min_effort": dict(criticality.CRITICALITY_MIN_EFFORT),
            # P11-ESC: deep-review escalation thresholds and the rule-check ceiling.
            "escalation_defaults": escalation_document(ESCALATION_DEFAULTS),
            "rule_check_defaults": dict(RULE_CHECK_DEFAULTS),
        }
    )


def preset_component() -> dict[str, Any]:
    from lemoncrow.pro.capabilities.review import ai_review, capability_catalog

    return _component(
        {
            "presets": {
                name: [asdict(row) for row in rows] for name, rows in capability_catalog.PRESET_SELECTIONS.items()
            },
            "catalog": {key: asdict(entry) for key, entry in sorted(capability_catalog.STATIC_CATALOG.items())},
            "ai_investigator_calls": dict(capability_catalog.AI_INVESTIGATOR_CALLS),
            "ai_effort_profiles": {name: asdict(profile) for name, profile in ai_review._EFFORT_PROFILES.items()},
        }
    )


def compute_digest_set(
    *,
    corpus: Corpus,
    reviewer: Mapping[str, str],
    selections: Iterable[Mapping[str, Any]],
    fixtures: Mapping[str, CaseFixtures | None],
    observed_systems: Iterable[tuple[str, str]] = (),
) -> dict[str, Any]:
    identities = sorted(
        {f"{driver}/{model}" for item in fixtures.values() if item is not None for driver, model in item.identities()}
    )
    components = {
        "model": _component({"reviewer": dict(reviewer), "fixture_identities": identities}),
        "prompt": prompt_component(observed_systems),
        "policy_default": policy_default_component(),
        "preset": preset_component(),
        "corpus": _component({"id": corpus.id, "cases": corpus.cases_digest()}),
        "fixtures": _component({"fixtures": corpus.fixtures_digest()}),
        "replay": _component(
            {
                "harness_version": HARNESS_VERSION,
                "selections": [dict(row) for row in selections],
                "line_tolerance": corpus.line_tolerance,
            }
        ),
    }
    return {"digest": digest({name: components[name]["digest"] for name in DIGEST_COMPONENTS}), **components}


def changed_components(baseline: Mapping[str, Any] | None, current: Mapping[str, Any]) -> list[str]:
    if baseline is None:
        return list(DIGEST_COMPONENTS)
    out = []
    for name in DIGEST_COMPONENTS:
        before = baseline.get(name)
        after = current.get(name)
        before_digest = before.get("digest") if isinstance(before, Mapping) else None
        after_digest = after.get("digest") if isinstance(after, Mapping) else None
        if before_digest != after_digest:
            out.append(name)
    return out


__all__ = [
    "DIGEST_COMPONENTS",
    "HARNESS_VERSION",
    "changed_components",
    "compute_digest_set",
    "policy_default_component",
    "preset_component",
    "prompt_component",
]

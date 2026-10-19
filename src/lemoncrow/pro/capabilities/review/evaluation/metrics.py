"""Per-selection metrics, each aggregate linked to the case records it counts.

* **catch rate** -- matched expected defects / expected defects (recall);
* **noise rate** -- surfaced findings matching no expectation / surfaced
  findings (1 - precision); on a clean case every surfaced finding is false;
* **behavior catch rate** -- matched expected behavior deltas (side effects,
  verification weakening, ...) / expected ones; the rest are *missed*;
* **cost per Review** -- planned compute minutes of the entries the plan runs,
  split by locus (customer: local, worker, the Snapshot-holding host in local
  mode; managed: LemonCrow sandboxes), plus model calls, tokens and provider
  dollars where the provider reported them, split provider-API vs local model
  (CLI subscriptions, Ollama). Unknown amounts are counted, never invented; a
  cost reported in another currency (or with none) is unknown, never summed as
  dollars;
* **cost totals** -- the selection's planned compute minutes, model calls and
  provider dollars over the corpus, with their known-ness, which Gate I
  divides by weighted catches (cost per catch). A planned row without a
  finite, non-negative minute estimate makes compute unknown, never zero;
* **time** -- measured wall time and in-process compute; reported, not gated.

Per catalog entry, each selection reports where it ran, what it caught, the
false findings it produced and what it cost, so each depth's contents can be
calibrated against measurement.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from lemoncrow.pro.capabilities.review.evaluation.corpus import EvalCase, ExpectedBehavior, ExpectedFinding
from lemoncrow.pro.capabilities.review.evaluation.fixtures import ENTRY_BY_STAGE
from lemoncrow.pro.capabilities.review.evaluation.matching import (
    BehaviorMatch,
    FindingMatch,
    match_behavior,
    match_findings,
)
from lemoncrow.pro.capabilities.review.evaluation.pipeline import CaseRun, ObservedBehavior, ObservedFinding, Selection

CUSTOMER_LOCI: Final[frozenset[str]] = frozenset({"local", "worker", "review_host"})
MANAGED_LOCI: Final[frozenset[str]] = frozenset({"managed"})
PROVIDER_API_DRIVERS: Final[frozenset[str]] = frozenset({"openai", "litellm"})
#: The only currency provider costs are summed in; any other is unknown, never converted.
COST_CURRENCY: Final[str] = "USD"
#: Known-ness of a selection's provider dollars (see :func:`provider_usd_status`).
USD_STATUSES: Final[tuple[str, ...]] = ("known", "unreported", "partial", "other_currency")
_PLACES: Final[int] = 6


def _round(value: float) -> float:
    return round(value, _PLACES)


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else _round(numerator / denominator)


def provider_usd_status(*, provider_api_calls: int, unreported_calls: int, other_currency_calls: int) -> str:
    """Whether a selection's provider dollars are known.

    ``known``: every provider-API call reported a USD cost (or there was none);
    ``unreported``: no provider-API call reported any cost (the scripted
    reviewer, a provider without cost reporting); ``partial``: some did, some
    did not; ``other_currency``: some cost came in another currency or with no
    currency. Only ``known`` dollars are ever compared.
    """

    if other_currency_calls:
        return "other_currency"
    if not unreported_calls:
        return "known"
    return "unreported" if unreported_calls >= provider_api_calls else "partial"


@dataclass(frozen=True, slots=True)
class CaseCost:
    estimated_customer_minutes: float
    estimated_managed_minutes: float
    planned_model_calls: int
    provider_api_calls: int
    local_model_calls: int
    input_tokens: int
    output_tokens: int
    token_unknown_calls: int
    provider_usd: float
    usd_unknown_calls: int
    usd_other_currency_calls: int
    measured_compute_minutes: float
    wall_seconds: float
    entry_minutes: Mapping[str, float]
    entry_calls: Mapping[str, int]
    #: Planned rows whose minute estimate is not a finite non-negative number: compute is unknown, never zero.
    minutes_unknown_rows: int = 0

    @property
    def model_calls(self) -> int:
        return self.provider_api_calls + self.local_model_calls


def case_cost(run: CaseRun) -> CaseCost:
    customer = managed = 0.0
    planned_calls = minutes_unknown = 0
    entry_minutes: dict[str, float] = {}
    for row in run.plan:
        if row.status != "run":
            continue
        planned_calls += row.estimated_model_calls
        entry_minutes[row.id] = row.estimated_minutes
        if not math.isfinite(row.estimated_minutes) or row.estimated_minutes < 0:
            minutes_unknown += 1
        elif row.executor in MANAGED_LOCI:
            managed += row.estimated_minutes
        else:
            customer += row.estimated_minutes
    api = local = 0
    tokens_in = tokens_out = token_unknown = usd_unknown = usd_other_currency = 0
    usd = 0.0
    entry_calls: dict[str, int] = {}
    for call in run.model_calls:
        entry = ENTRY_BY_STAGE.get(call.stage, "ai.investigators")
        entry_calls[entry] = entry_calls.get(entry, 0) + 1
        if call.driver in PROVIDER_API_DRIVERS:
            api += 1
            if call.usage.provider_cost is None:
                usd_unknown += 1
            elif (call.usage.currency or "").upper() != COST_CURRENCY:
                usd_other_currency += 1  # never converted, never summed as dollars
            else:
                usd += call.usage.provider_cost
        else:
            local += 1
        if call.usage.input_tokens is None or call.usage.output_tokens is None:
            token_unknown += 1
        else:
            tokens_in += call.usage.input_tokens
            tokens_out += call.usage.output_tokens
    return CaseCost(
        estimated_customer_minutes=customer,
        estimated_managed_minutes=managed,
        planned_model_calls=planned_calls,
        provider_api_calls=api,
        local_model_calls=local,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        token_unknown_calls=token_unknown,
        provider_usd=usd,
        usd_unknown_calls=usd_unknown,
        usd_other_currency_calls=usd_other_currency,
        measured_compute_minutes=run.measured_compute_minutes,
        wall_seconds=run.wall_seconds,
        entry_minutes=entry_minutes,
        entry_calls=entry_calls,
        minutes_unknown_rows=minutes_unknown,
    )


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    case: EvalCase
    run: CaseRun
    matches: tuple[FindingMatch, ...]
    missed: tuple[ExpectedFinding, ...]
    false_findings: tuple[ObservedFinding, ...]
    behavior_matches: tuple[BehaviorMatch, ...]
    missed_behavior: tuple[ExpectedBehavior, ...]
    unexpected_behavior: tuple[ObservedBehavior, ...]
    cost: CaseCost

    def to_wire(self) -> dict[str, Any]:
        return {
            "case_id": self.case.id,
            "kind": self.case.kind,
            "caught": [
                {"expected": item.expected.id, "finding": item.finding.ref, "line_gap": item.line_gap}
                for item in self.matches
            ],
            "missed": [item.id for item in self.missed],
            "false_findings": [item.ref for item in self.false_findings],
            "behavior_caught": [
                {"expected": item.expected.id, "observed": item.observed.ref} for item in self.behavior_matches
            ],
            "missed_behavior": [item.id for item in self.missed_behavior],
            "unexpected_behavior": [item.ref for item in self.unexpected_behavior],
        }


def evaluate_case(case: EvalCase, run: CaseRun, *, tolerance: int) -> CaseOutcome:
    matches, missed, unmatched = match_findings(case.expected_findings, run.findings, tolerance=tolerance)
    applicable_behavior = tuple(
        item for item in case.expected_behavior if not item.selections or run.selection in item.selections
    )
    behavior, missed_behavior, unexpected = match_behavior(applicable_behavior, run.behavior)
    return CaseOutcome(
        case=case,
        run=run,
        matches=tuple(matches),
        missed=tuple(missed),
        false_findings=tuple(unmatched),
        behavior_matches=tuple(behavior),
        missed_behavior=tuple(missed_behavior),
        unexpected_behavior=tuple(unexpected),
        cost=case_cost(run),
    )


def _ref(case_id: str, item: str) -> str:
    return f"{case_id}#{item}"


def aggregate(selection: Selection, outcomes: Sequence[CaseOutcome]) -> dict[str, Any]:
    """One selection's metrics; every list names the case (and record) it came from."""

    reviews = len(outcomes)
    caught = [
        {
            "case": item.case.id,
            "expected": match.expected.id,
            "finding": match.finding.ref,
            "line_gap": match.line_gap,
            "severity_expected": match.expected.severity,
            "severity_observed": match.finding.severity,
        }
        for item in outcomes
        for match in item.matches
    ]
    missed = [{"case": item.case.id, "expected": exp.id} for item in outcomes for exp in item.missed]
    expected_total = sum(len(item.case.expected_findings) for item in outcomes)
    surfaced = sum(1 for item in outcomes for finding in item.run.findings if finding.surfaced)
    folded = sum(1 for item in outcomes for finding in item.run.findings if not finding.surfaced)
    false = [
        {"case": item.case.id, "finding": finding.ref, "path": finding.path, "category": finding.category}
        for item in outcomes
        for finding in item.false_findings
    ]
    behavior_expected = sum(len(item.behavior_matches) + len(item.missed_behavior) for item in outcomes)
    behavior_caught = [
        {"case": item.case.id, "expected": match.expected.id, "observed": match.observed.ref}
        for item in outcomes
        for match in item.behavior_matches
    ]
    behavior_missed = [{"case": item.case.id, "expected": exp.id} for item in outcomes for exp in item.missed_behavior]
    unexpected = [
        {"case": item.case.id, "observed": delta.ref, "kind": delta.kind, "subject": delta.subject}
        for item in outcomes
        for delta in item.unexpected_behavior
    ]
    compared = [match for item in outcomes for match in item.matches if match.severity_agrees is not None]
    clean = [item.case.id for item in outcomes if item.case.kind == "clean"]
    flagged = [item.case.id for item in outcomes if item.case.kind == "clean" and item.false_findings]
    incomplete = [
        {
            "case": item.case.id,
            "fixture_events": [event.to_wire() for event in item.run.fixture_events],
            "errors": list(item.run.errors),
        }
        for item in outcomes
        if not item.run.complete
    ]
    costs = [item.cost for item in outcomes]

    def total(name: str) -> float:
        return float(sum(getattr(cost, name) for cost in costs))

    usd_unknown = int(total("usd_unknown_calls"))
    usd_other_currency = int(total("usd_other_currency_calls"))
    token_unknown = int(total("token_unknown_calls"))
    minutes_unknown = int(total("minutes_unknown_rows"))
    per = max(1, reviews)

    def minutes(value: float) -> float | None:
        """Planned minutes, or ``None`` when any planned row had no usable estimate (never a partial sum)."""

        return None if minutes_unknown else _round(value)

    customer_minutes, managed_minutes = total("estimated_customer_minutes"), total("estimated_managed_minutes")
    cost = {
        "reviews": reviews,
        "total": {
            "estimated_compute_minutes": minutes(customer_minutes + managed_minutes),
            "estimated_customer_minutes": minutes(customer_minutes),
            "estimated_managed_minutes": minutes(managed_minutes),
            "planned_model_calls": int(total("planned_model_calls")),
            "model_calls": int(total("provider_api_calls") + total("local_model_calls")),
            "provider_api_calls": int(total("provider_api_calls")),
            "local_model_calls": int(total("local_model_calls")),
            "input_tokens": int(total("input_tokens")),
            "output_tokens": int(total("output_tokens")),
            "provider_usd_known": _round(total("provider_usd")),
        },
        "per_review": {
            "estimated_compute_minutes": minutes((customer_minutes + managed_minutes) / per),
            "estimated_customer_minutes": minutes(customer_minutes / per),
            "estimated_managed_minutes": minutes(managed_minutes / per),
            "model_calls": _round((total("provider_api_calls") + total("local_model_calls")) / per),
            "provider_api_calls": _round(total("provider_api_calls") / per),
            "local_model_calls": _round(total("local_model_calls") / per),
            "input_tokens": None if token_unknown else _round(total("input_tokens") / per),
            "output_tokens": None if token_unknown else _round(total("output_tokens") / per),
            "provider_usd": None if usd_unknown or usd_other_currency else _round(total("provider_usd") / per),
        },
        "unknown": {
            "token_calls": token_unknown,
            "provider_usd_calls": usd_unknown,
            "provider_usd_other_currency_calls": usd_other_currency,
            "provider_usd_status": provider_usd_status(
                provider_api_calls=int(total("provider_api_calls")),
                unreported_calls=usd_unknown,
                other_currency_calls=usd_other_currency,
            ),
            "compute_minute_rows": minutes_unknown,
            "note": (
                "calls whose provider reported no tokens or cost, or a cost in another currency, and planned rows "
                "with no usable minute estimate; never estimated"
            ),
        },
        "cases": {
            item.case.id: {
                "estimated_customer_minutes": _round(item.cost.estimated_customer_minutes),
                "estimated_managed_minutes": _round(item.cost.estimated_managed_minutes),
                "model_calls": item.cost.model_calls,
                "planned_model_calls": item.cost.planned_model_calls,
            }
            for item in outcomes
        },
    }
    entries: dict[str, dict[str, Any]] = {}

    def entry(entry_id: str) -> dict[str, Any]:
        return entries.setdefault(
            entry_id,
            {
                "run_cases": [],
                "executed_cases": [],
                "estimate_only_cases": [],
                "catches": [],
                "false_findings": [],
                "behavior_caught": [],
                "model_calls": 0,
                "estimated_minutes": 0.0,
            },
        )

    for item in outcomes:
        for row in item.run.plan:
            if row.status != "run":
                continue
            bucket = entry(row.id)
            bucket["run_cases"].append(item.case.id)
            bucket["estimated_minutes"] = _round(bucket["estimated_minutes"] + row.estimated_minutes)
            if row.executed:
                bucket["executed_cases"].append(item.case.id)
            elif row.note.startswith("no hermetic executor"):
                bucket["estimate_only_cases"].append(item.case.id)
        for entry_id, calls in item.cost.entry_calls.items():
            entry(entry_id)["model_calls"] += calls
        for match in item.matches:
            entry(match.finding.entry)["catches"].append(_ref(item.case.id, match.expected.id))
        for finding in item.false_findings:
            entry(finding.entry)["false_findings"].append(_ref(item.case.id, finding.ref))
        for behavior_match in item.behavior_matches:
            entry(behavior_match.observed.entry)["behavior_caught"].append(
                _ref(item.case.id, behavior_match.expected.id)
            )
    return {
        "selection": selection.to_wire(),
        "cases": [item.case.id for item in outcomes],
        "complete": not incomplete,
        "incomplete": incomplete,
        "catch": {
            "expected": expected_total,
            "caught": caught,
            "missed": missed,
            "rate": _rate(len(caught), expected_total),
        },
        "noise": {
            "surfaced_findings": surfaced,
            "folded_findings": folded,
            "false": false,
            "rate": _rate(len(false), surfaced) if surfaced else 0.0,
            "clean_cases": clean,
            "clean_cases_flagged": flagged,
        },
        "behavior": {
            "expected": behavior_expected,
            "caught": behavior_caught,
            "missed": behavior_missed,
            "rate": _rate(len(behavior_caught), behavior_expected),
            "unexpected": unexpected,
        },
        "severity_agreement": {
            "compared": len(compared),
            "agreeing": sum(1 for match in compared if match.severity_agrees),
            "rate": _rate(sum(1 for match in compared if match.severity_agrees), len(compared)),
        },
        "cost": cost,
        "time": {
            "wall_seconds_total": round(total("wall_seconds"), 3),
            "wall_seconds_per_review": round(total("wall_seconds") / per, 3),
            "measured_compute_minutes_total": _round(total("measured_compute_minutes")),
            "note": "measured on this machine; reported, never gated",
        },
        "entries": {key: entries[key] for key in sorted(entries)},
    }


def gate_view(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """The deterministic numbers Gate I compares (no ids, no wall time)."""

    catch = metrics["catch"]
    noise = metrics["noise"]
    behavior = metrics["behavior"]
    per_review = metrics["cost"]["per_review"]
    totals = metrics["cost"]["total"]
    status = metrics["cost"]["unknown"]["provider_usd_status"]
    return {
        "catch_rate": catch["rate"],
        "behavior_catch_rate": behavior["rate"],
        "noise_rate": noise["rate"],
        "caught": sorted(_ref(row["case"], row["expected"]) for row in catch["caught"]),
        "missed": sorted(_ref(row["case"], row["expected"]) for row in catch["missed"]),
        "behavior_caught": sorted(_ref(row["case"], row["expected"]) for row in behavior["caught"]),
        "false_findings": sorted(_ref(row["case"], row["finding"]) for row in noise["false"]),
        "missed_behavior": sorted(_ref(row["case"], row["expected"]) for row in behavior["missed"]),
        "cost_per_review": {
            key: per_review[key]
            for key in (
                "estimated_compute_minutes",
                "estimated_customer_minutes",
                "estimated_managed_minutes",
                "model_calls",
                "provider_api_calls",
                "local_model_calls",
                "input_tokens",
                "output_tokens",
                "provider_usd",
            )
        },
        # Totals over the corpus: what Gate I divides by weighted catches (cost per catch).
        # ``None`` (and a non-known dollar status) means unknown, never zero.
        "cost_total": {
            "estimated_compute_minutes": totals["estimated_compute_minutes"],
            "model_calls": totals["model_calls"],
            "provider_api_calls": totals["provider_api_calls"],
            "provider_usd": totals["provider_usd_known"] if status == "known" else None,
            "provider_usd_status": status,
        },
        "cases": sorted(metrics["cases"]),
        "incomplete_cases": sorted(row["case"] for row in metrics["incomplete"]),
    }


__all__ = [
    "COST_CURRENCY",
    "CUSTOMER_LOCI",
    "MANAGED_LOCI",
    "PROVIDER_API_DRIVERS",
    "USD_STATUSES",
    "CaseCost",
    "CaseOutcome",
    "aggregate",
    "case_cost",
    "evaluate_case",
    "gate_view",
    "provider_usd_status",
]

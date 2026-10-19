"""Gate I: block a model, prompt, policy-default or preset change that regresses Review.

The gate state lives in the repository (reviewable in the pull request that
changes it), under one directory (default ``evals/review/``)::

    gate.yaml          # mode, corpus, replay selections, thresholds, owners
    baseline.json      # the last recorded gate result (snapshot + digest set)
    acceptances/*.yaml # explicit owner acceptances of measured trade-offs

``evaluate_gate`` replays the corpus (CI mode) and compares the deterministic
snapshot with the baseline:

* ``stale``       -- a fixture is stale or missing, so nothing can be measured;
* ``no_baseline`` -- nothing recorded yet;
* ``blocked``     -- a threshold is breached and no valid acceptance covers it;
* ``accepted``    -- breached, but an acceptance record names an authorized
  owner, a reason, this exact baseline and current snapshot and the same
  measured numbers and breaches;
* ``pass_unrecorded`` -- within thresholds, but the measured snapshot (or its
  digest set: model, prompt, policy default, preset, corpus, fixtures, replay)
  differs from the baseline; ``--update-baseline`` records a gate result for it;
* ``pass``        -- identical to the recorded baseline.

Per selection the thresholds are: no catch-rate or behavior-catch-rate drop, no
lost catch, noise rise within ``noise_rate_rise_max``, and cost per weighted
catch (:class:`CatchCost`: planned compute minutes, model calls and, when
known, provider dollars) rising by at most ``cost_per_catch_rise_max``.
Absolute cost per Review is reported beside the result and never gates by
itself (owner decision 2026-10-05; it replaced a flat cost-per-Review rule).
The thresholds' approval is bound to their values by a digest: an edited
threshold or weight counts as unapproved until the product owner re-approves.

In ``report`` mode every status is printed and nothing blocks. In ``enforce``
mode anything but ``pass``/``accepted`` fails. Thresholds are configuration;
their defaults are *proposed* and pending product-owner approval, and
``gate.yaml`` cannot select ``enforce`` until they are approved (an explicit
``--mode enforce`` on the command line can, and says so).

A regressed snapshot can never become the baseline without an acceptance
record, whatever the mode: report mode never silently moves the baseline down.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

import yaml

from lemoncrow.pro.capabilities.review.evaluation.corpus import digest
from lemoncrow.pro.capabilities.review.evaluation.digests import changed_components
from lemoncrow.pro.capabilities.review.evaluation.harness import EvalReport, ReplaySpec
from lemoncrow.pro.capabilities.review.evaluation.metrics import USD_STATUSES
from lemoncrow.pro.capabilities.review.evaluation.pipeline import Reviewer, Selection

GateMode = Literal["report", "enforce"]
GateStatus = Literal["pass", "pass_unrecorded", "accepted", "blocked", "stale", "no_baseline"]

GATE_CONFIG: Final[str] = "gate.yaml"
GATE_DIR_RELATIVE: Final[str] = "evals/review"
APPROVED: Final[str] = "approved"
PENDING: Final[str] = "pending_product_owner_approval"
PASSING: Final[frozenset[str]] = frozenset({"pass", "accepted"})
_EPSILON: Final[float] = 1e-9
_PLACES: Final[int] = 6
INFINITE: Final[str] = "infinite"

#: Proposed defaults, pending product-owner approval (plan: Execution decisions).
PROPOSED_THRESHOLDS: Final[Mapping[str, float | int]] = {
    # Any drop in a selection's catch rate on the corpus blocks.
    "catch_rate_drop_max": 0.0,
    # Any drop in the behavior-delta (side effect, weakening) catch rate blocks.
    "behavior_catch_rate_drop_max": 0.0,
    # Any previously caught defect or behavior delta that is now missed blocks,
    # even when a new catch keeps the rate level.
    "lost_catches_max": 0,
    # Noise (false share of surfaced findings) may rise by at most 5 points.
    "noise_rate_rise_max": 0.05,
    # Cost (planned compute minutes, model calls, provider dollars where known)
    # per weighted catch may rise by at most 10 % relative; a rise from zero blocks.
    "cost_per_catch_rise_max": 0.10,
    # Weighted catches = defect catches x defect weight + behavior catches x behavior weight.
    "defect_catch_weight": 1.0,
    "behavior_catch_weight": 1.0,
}
#: Threshold keys that no longer exist; a gate.yaml that still sets one is refused, never reinterpreted.
REMOVED_THRESHOLDS: Final[Mapping[str, str]] = {
    "cost_per_review_rise_max": (
        "was removed (owner decision 2026-10-05): Gate I no longer gates a flat cost-per-Review rise; it gates "
        "cost per weighted catch. Delete it, set thresholds.cost_per_catch_rise_max (and "
        "defect_catch_weight / behavior_catch_weight), and have the product owner approve the thresholds again"
    ),
}
#: Absolute cost per Review: reported beside the gate result, never gated by itself.
COST_METRICS: Final[tuple[str, ...]] = ("estimated_compute_minutes", "model_calls", "provider_usd")
#: Cost units the cost-per-catch rule compares, each against the same threshold.
CATCH_COST_UNITS: Final[tuple[str, ...]] = ("estimated_compute_minutes", "model_calls", "provider_usd")


class GateError(ValueError):
    pass


def thresholds_digest(values: Mapping[str, Any]) -> str:
    """The digest an approval records: every threshold and weight, as numbers (``0.1`` == ``0.10``, ``0`` == ``0.0``)."""

    return digest({name: float(values[name]) for name in sorted(PROPOSED_THRESHOLDS)})


def _same_number(recorded: Any, value: float) -> bool:
    return (
        isinstance(recorded, (int, float))
        and not isinstance(recorded, bool)
        and math.isfinite(recorded)
        and float(recorded) == float(value)
    )


@dataclass(frozen=True, slots=True)
class Thresholds:
    catch_rate_drop_max: float
    behavior_catch_rate_drop_max: float
    lost_catches_max: int
    noise_rate_rise_max: float
    cost_per_catch_rise_max: float
    defect_catch_weight: float
    behavior_catch_weight: float
    approval_status: str = PENDING
    approved_by: str = ""
    approved_at: str = ""
    #: :func:`thresholds_digest` of the values the owner approved; approval holds only while it matches.
    approved_digest: str = ""
    #: The approved values themselves, to name what changed since approval.
    approved_values: Mapping[str, Any] | None = None

    def values(self) -> dict[str, float | int]:
        return {name: getattr(self, name) for name in PROPOSED_THRESHOLDS}

    def digest(self) -> str:
        return thresholds_digest(self.values())

    @property
    def approved(self) -> bool:
        return self.approval_status == APPROVED and bool(self.approved_digest) and self.approved_digest == self.digest()

    @property
    def changed_since_approval(self) -> tuple[str, ...]:
        """Thresholds or weights whose value differs from the approved one (empty unless approval is broken)."""

        if self.approval_status != APPROVED or self.approved or self.approved_values is None:
            return ()
        recorded = self.approved_values
        return tuple(name for name, value in self.values().items() if not _same_number(recorded.get(name), value))

    def approval_problem(self) -> str | None:
        """Why these thresholds are not approved, or ``None`` when they are."""

        if self.approved:
            return None
        if self.approval_status != APPROVED:
            return "thresholds are proposed defaults pending product-owner approval"
        if not self.approved_digest:
            return (
                "the threshold approval records no thresholds_digest (it predates binding approval to the values), "
                "so the thresholds count as unapproved"
            )
        changed = self.changed_since_approval
        if changed:
            recorded = self.approved_values or {}
            current = self.values()
            detail = ", ".join(f"{name} (approved {recorded.get(name)!r}, now {current[name]!r})" for name in changed)
            return f"thresholds changed since approval: {detail}; they count as unapproved"
        return "thresholds.approval.thresholds_digest does not match the thresholds, so they count as unapproved"

    def to_wire(self) -> dict[str, Any]:
        return {
            "approval": {
                "status": self.approval_status,
                "approved_by": self.approved_by or None,
                "approved_at": self.approved_at or None,
                "thresholds": None if self.approved_values is None else dict(self.approved_values),
                "thresholds_digest": self.approved_digest or None,
            },
            "approved": self.approved,
            "changed_since_approval": list(self.changed_since_approval),
            "digest": self.digest(),
            "catch_rate_drop_max": self.catch_rate_drop_max,
            "behavior_catch_rate_drop_max": self.behavior_catch_rate_drop_max,
            "lost_catches_max": self.lost_catches_max,
            "noise_rate_rise_max": self.noise_rate_rise_max,
            "cost_per_catch_rise_max": self.cost_per_catch_rise_max,
            "defect_catch_weight": self.defect_catch_weight,
            "behavior_catch_weight": self.behavior_catch_weight,
        }


@dataclass(frozen=True, slots=True)
class GateConfig:
    root: Path
    mode: GateMode
    corpus_path: Path
    spec: ReplaySpec
    thresholds: Thresholds
    authorized_owners: tuple[str, ...]
    baseline_path: Path
    acceptances_dir: Path


def _number(raw: Mapping[str, Any], key: str, problems: list[str], *, integer: bool = False) -> float:
    value = raw.get(key, PROPOSED_THRESHOLDS[key])
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        problems.append(f"thresholds.{key} must be a non-negative number")
        return float(PROPOSED_THRESHOLDS[key])
    if integer and int(value) != value:
        problems.append(f"thresholds.{key} must be an integer")
    return float(value)


def _weight(raw: Mapping[str, Any], key: str, problems: list[str]) -> float:
    value = raw.get(key, PROPOSED_THRESHOLDS[key])
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        problems.append(f"thresholds.{key} must be a positive number")
        return float(PROPOSED_THRESHOLDS[key])
    return float(value)


def _selections(raw: object, problems: list[str]) -> tuple[Selection, ...]:
    if not isinstance(raw, list) or not raw:
        problems.append("replay.selections must list depths and custom selections")
        return ()
    out: list[Selection] = []
    for row in raw:
        try:
            if isinstance(row, str):
                out.append(Selection.preset(row))
            elif isinstance(row, Mapping) and "custom" not in row and "effort" in row:
                # P11-ESC: a depth with swept policy data (escalation thresholds, rule-check ceiling).
                out.append(
                    Selection.swept(
                        str(row.get("name") or ""),
                        str(row.get("effort") or ""),
                        {key: value for key, value in row.items() if key not in {"name", "effort"}},
                    )
                )
            elif isinstance(row, Mapping):
                entries = row.get("custom")
                out.append(
                    Selection.custom(
                        str(row.get("name") or ""),
                        str(row.get("custom_from") or "auto"),
                        [str(item) for item in entries] if isinstance(entries, list) else [],
                    )
                )
            else:
                problems.append(f"replay.selections: unsupported entry {row!r}")
        except ValueError as exc:
            problems.append(f"replay.selections: {exc}")
    labels = [row.label for row in out]
    if len(labels) != len(set(labels)):
        problems.append("replay.selections must not repeat a selection")
    return tuple(out)


def load_gate_config(root: Path) -> GateConfig:
    path = root / GATE_CONFIG
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise GateError(f"{path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise GateError(f"{path} must be a mapping")
    problems: list[str] = []
    if raw.get("version") != 1:
        problems.append("version must be 1")
    mode = raw.get("mode")
    if mode not in {"report", "enforce"}:
        problems.append("mode must be report or enforce")
        mode = "report"
    replay = raw.get("replay") if isinstance(raw.get("replay"), Mapping) else {}
    assert isinstance(replay, Mapping)
    reviewer = replay.get("reviewer") if isinstance(replay.get("reviewer"), Mapping) else {}
    assert isinstance(reviewer, Mapping)
    driver = str(reviewer.get("driver") or "")
    model = str(reviewer.get("model") or "")
    if not driver:
        problems.append("replay.reviewer.driver is required")
    selections = _selections(replay.get("selections"), problems)
    thresholds_raw = raw.get("thresholds") if isinstance(raw.get("thresholds"), Mapping) else {}
    assert isinstance(thresholds_raw, Mapping)
    problems.extend(f"thresholds.{key} {reason}" for key, reason in REMOVED_THRESHOLDS.items() if key in thresholds_raw)
    approval = thresholds_raw.get("approval") if isinstance(thresholds_raw.get("approval"), Mapping) else {}
    assert isinstance(approval, Mapping)
    status = str(approval.get("status") or PENDING)
    if status not in {APPROVED, PENDING}:
        problems.append(f"thresholds.approval.status must be {APPROVED} or {PENDING}")
    approved_by = str(approval.get("approved_by") or "")
    approved_at = str(approval.get("approved_at") or "")
    if status == APPROVED and (not approved_by or not approved_at):
        problems.append("approved thresholds must name approved_by and approved_at")
    approved_digest = approval.get("thresholds_digest")
    if approved_digest is not None and not isinstance(approved_digest, str):
        problems.append("thresholds.approval.thresholds_digest must be the approved values' digest or null")
    approved_values = approval.get("thresholds")
    if approved_values is not None and not isinstance(approved_values, Mapping):
        problems.append("thresholds.approval.thresholds must map each threshold to its approved value, or be null")
    thresholds = Thresholds(
        catch_rate_drop_max=_number(thresholds_raw, "catch_rate_drop_max", problems),
        behavior_catch_rate_drop_max=_number(thresholds_raw, "behavior_catch_rate_drop_max", problems),
        lost_catches_max=int(_number(thresholds_raw, "lost_catches_max", problems, integer=True)),
        noise_rate_rise_max=_number(thresholds_raw, "noise_rate_rise_max", problems),
        cost_per_catch_rise_max=_number(thresholds_raw, "cost_per_catch_rise_max", problems),
        defect_catch_weight=_weight(thresholds_raw, "defect_catch_weight", problems),
        behavior_catch_weight=_weight(thresholds_raw, "behavior_catch_weight", problems),
        approval_status=status,
        approved_by=approved_by,
        approved_at=approved_at,
        approved_digest=approved_digest if isinstance(approved_digest, str) else "",
        approved_values=dict(approved_values) if isinstance(approved_values, Mapping) else None,
    )
    if mode == "enforce" and not thresholds.approved:
        if status == APPROVED:
            problems.append(
                f"mode: enforce requires approved thresholds, but {thresholds.approval_problem()}. Have the product "
                "owner re-approve them: set thresholds.approval.thresholds to "
                f"{json.dumps(thresholds.values(), sort_keys=True)} and thresholds.approval.thresholds_digest to "
                f"{thresholds.digest()}, with approved_by and approved_at (or keep mode: report)"
            )
        else:
            problems.append(
                "mode: enforce requires product-owner-approved thresholds; keep mode: report until they are approved "
                "(an explicit --mode enforce on the command line still previews enforcement)"
            )
    owners = raw.get("authorized_owners") or []
    if not isinstance(owners, list) or not all(isinstance(item, str) and item.strip() for item in owners):
        problems.append("authorized_owners must list owner names")
        owners = []
    corpus = str(raw.get("corpus") or "")
    if not corpus:
        problems.append("corpus is required")
    if problems:
        raise GateError(f"{path} is invalid:\n  - " + "\n  - ".join(problems))
    return GateConfig(
        root=root,
        mode=cast(GateMode, mode),
        corpus_path=(root / corpus).resolve(),
        spec=ReplaySpec(reviewer=Reviewer(driver=driver, model=model), selections=selections),
        thresholds=thresholds,
        authorized_owners=tuple(item.strip() for item in owners),
        baseline_path=root / str(raw.get("baseline") or "baseline.json"),
        acceptances_dir=root / str(raw.get("acceptances") or "acceptances"),
    )


def find_gate_root(start: Path) -> Path:
    """The nearest ``evals/review`` directory holding a ``gate.yaml``, walking up from *start*."""

    for directory in (start.resolve(), *start.resolve().parents):
        candidate = directory / GATE_DIR_RELATIVE
        if (candidate / GATE_CONFIG).is_file():
            return candidate
    raise GateError(f"no {GATE_DIR_RELATIVE}/{GATE_CONFIG} found above {start}; pass --gate-dir")


# --------------------------------------------------------------------------- #
# comparison
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Breach:
    selection: str
    metric: str
    baseline: Any
    current: Any
    threshold: Any
    detail: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "selection": self.selection,
            "metric": self.metric,
            "baseline": self.baseline,
            "current": self.current,
            "threshold": self.threshold,
            "detail": self.detail,
        }


def _finite(value: object) -> float | None:
    """*value* as a float when it is a finite number (never a bool), else ``None``."""

    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _per_catch(cost: float, weighted_catches: float) -> float:
    """Cost per weighted catch: 0 when nothing was spent, infinite when something was spent on no catch."""

    if cost <= 0:
        return 0.0
    if weighted_catches <= 0:
        return math.inf
    return cost / weighted_catches


def _wire_number(value: float | None) -> float | str | None:
    if value is None:
        return None
    return INFINITE if math.isinf(value) else round(value, _PLACES)


@dataclass(frozen=True, slots=True)
class CatchCost:
    """One side's cost per weighted catch, for one selection.

    Weighted catches are defect catches times ``defect_catch_weight`` plus
    behavior catches times ``behavior_catch_weight``. Cost is the corpus total
    in planned compute minutes, in model calls and in provider dollars. A
    ``None`` cost is unknown and fails closed, never zero: compute minutes when
    a planned row had no usable estimate, model calls when the snapshot has no
    count, dollars unless every provider-API call reported a USD cost.
    """

    defect_catches: int
    behavior_catches: int
    weighted_catches: float
    compute_minutes: float | None
    model_calls: float | None
    provider_usd: float | None
    provider_usd_status: str

    def cost(self, unit: str) -> float | None:
        if unit == "estimated_compute_minutes":
            return self.compute_minutes
        return self.model_calls if unit == "model_calls" else self.provider_usd

    def state(self, unit: str) -> str:
        """How *unit* is known on this side, for breach details."""

        if unit == "provider_usd":
            return self.provider_usd_status
        return "unknown" if self.cost(unit) is None else "known"

    def per_catch(self, unit: str) -> float | None:
        cost = self.cost(unit)
        return None if cost is None else _per_catch(cost, self.weighted_catches)

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "defect_catches": self.defect_catches,
            "behavior_catches": self.behavior_catches,
            "weighted_catches": round(self.weighted_catches, _PLACES),
        }
        for unit in CATCH_COST_UNITS:
            cost = self.cost(unit)
            out[unit] = None if cost is None else round(cost, _PLACES)
            out[f"{unit}_per_catch"] = _wire_number(self.per_catch(unit))
        out["provider_usd_status"] = self.provider_usd_status
        return out


def _cost_value(raw: Any) -> float | None:
    value = _finite(raw)
    return None if value is None or value < 0 else value


def catch_cost(row: Mapping[str, Any], thresholds: Thresholds) -> CatchCost | None:
    """*row*'s cost per catch, or ``None`` when the snapshot predates cost per catch."""

    caught, behavior, spend = row.get("caught"), row.get("behavior_caught"), row.get("cost_total")
    if not isinstance(caught, list) or not isinstance(behavior, list) or not isinstance(spend, Mapping):
        return None
    status = str(spend.get("provider_usd_status") or "")
    usd = _cost_value(spend.get("provider_usd"))
    known = status == "known" and usd is not None
    return CatchCost(
        defect_catches=len(caught),
        behavior_catches=len(behavior),
        weighted_catches=thresholds.defect_catch_weight * len(caught)
        + thresholds.behavior_catch_weight * len(behavior),
        compute_minutes=_cost_value(spend.get("estimated_compute_minutes")),
        model_calls=_cost_value(spend.get("model_calls")),
        provider_usd=usd if known else None,
        # An unrecognized or inconsistent status is unknown: it fails closed below.
        provider_usd_status=status if status in USD_STATUSES and (known or status != "known") else "unknown",
    )


def cost_per_catch_breach(before: CatchCost, after: CatchCost, unit: str, limit: float) -> str | None:
    """Why *after*'s cost per catch in *unit* breaches the rule against *before*, or ``None``.

    * unknown cost on either side: blocks, never counted as zero;
    * nothing spent: passes;
    * cost with no catch (infinite per catch): blocks, unless the baseline also
      caught nothing and the cost did not rise;
    * the baseline spent nothing (zero per catch, zero catches included): any
      cost blocks as a rise from zero;
    * the baseline spent on no catch (infinite per catch): any catch is an improvement;
    * otherwise the relative rise in cost per catch must stay within *limit*.
    """

    old_cost, new_cost = before.cost(unit), after.cost(unit)
    if old_cost is None or new_cost is None:
        return (
            f"unknown cost (baseline {before.state(unit)}, current {after.state(unit)}): "
            "fails closed, never counted as zero"
        )
    old, new = _per_catch(old_cost, before.weighted_catches), _per_catch(new_cost, after.weighted_catches)
    if new == 0.0:
        return None
    if math.isinf(new):
        if before.weighted_catches <= 0 and new_cost <= old_cost + _EPSILON:
            return None
        return "cost with no catches"
    if old == 0.0:
        return "rose from zero"
    if math.isinf(old):
        return None
    rise = (new - old) / old
    return f"rose {rise:.1%}" if rise > limit + _EPSILON else None


def _breach_value(side: CatchCost, unit: str) -> float | str | None:
    return side.state(unit) if side.cost(unit) is None else _wire_number(side.per_catch(unit))


def _cost_per_catch_breaches(label: str, before: CatchCost, after: CatchCost, limit: float) -> list[Breach]:
    out: list[Breach] = []
    for unit in CATCH_COST_UNITS:
        if unit == "provider_usd" and before.provider_usd_status == after.provider_usd_status == "unreported":
            # Neither side reported dollars at all; compute minutes and model calls carry the rule.
            # lc-debt: a per-call price change on a provider that reports no cost is invisible here;
            # upgrade by recording a provider price table with the fixtures and gating priced dollars.
            continue
        detail = cost_per_catch_breach(before, after, unit, limit)
        if detail:
            out.append(
                Breach(
                    label,
                    f"cost_per_catch.{unit}",
                    _breach_value(before, unit),
                    _breach_value(after, unit),
                    limit,
                    detail,
                )
            )
    return out


def compare(baseline: Mapping[str, Any], current: Mapping[str, Any], thresholds: Thresholds) -> list[Breach]:
    """Every threshold breach of *current* against *baseline* (gate snapshots)."""

    breaches: list[Breach] = []
    before_all = baseline.get("selections") or {}
    after_all = current.get("selections") or {}
    for label in sorted(before_all):
        before = before_all[label]
        after = after_all.get(label)
        if after is None:
            breaches.append(Breach(label, "selection", "measured", "not measured", "present", "selection removed"))
            continue
        for metric, limit in (
            ("catch_rate", thresholds.catch_rate_drop_max),
            ("behavior_catch_rate", thresholds.behavior_catch_rate_drop_max),
        ):
            old, new = before.get(metric), after.get(metric)
            if old is None:
                continue
            if new is None or old - new > limit + _EPSILON:
                breaches.append(Breach(label, metric, old, new, limit, "dropped"))
        lost = sorted(set(before.get("caught") or ()) - set(after.get("caught") or ())) + sorted(
            set(after.get("missed_behavior") or ()) - set(before.get("missed_behavior") or ())
        )
        if len(lost) > thresholds.lost_catches_max:
            breaches.append(Breach(label, "lost_catches", 0, len(lost), thresholds.lost_catches_max, ", ".join(lost)))
        old_noise, new_noise = before.get("noise_rate") or 0.0, after.get("noise_rate") or 0.0
        if new_noise - old_noise > thresholds.noise_rate_rise_max + _EPSILON:
            added = sorted(set(after.get("false_findings") or ()) - set(before.get("false_findings") or ()))
            breaches.append(
                Breach(label, "noise_rate", old_noise, new_noise, thresholds.noise_rate_rise_max, ", ".join(added))
            )
        old_cost, new_cost = catch_cost(before, thresholds), catch_cost(after, thresholds)
        if old_cost is None or new_cost is None:
            side = "the baseline" if old_cost is None else "the current snapshot"
            breaches.append(
                Breach(
                    label,
                    "cost_per_catch",
                    "measured" if old_cost is not None else "not measured",
                    "measured" if new_cost is not None else "not measured",
                    thresholds.cost_per_catch_rise_max,
                    f"{side} predates cost per catch (no behavior_caught / cost_total); fails closed",
                )
            )
        else:
            breaches.extend(_cost_per_catch_breaches(label, old_cost, new_cost, thresholds.cost_per_catch_rise_max))
    return breaches


@dataclass(frozen=True, slots=True)
class CostChange:
    """An absolute cost-per-Review change: reported beside the gate result, never a breach."""

    selection: str
    metric: str
    baseline: float | None
    current: float | None
    detail: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "selection": self.selection,
            "metric": self.metric,
            "baseline": self.baseline,
            "current": self.current,
            "gated": False,
            "detail": self.detail,
        }


def cost_per_review_changes(baseline: Mapping[str, Any], current: Mapping[str, Any]) -> list[CostChange]:
    """Every changed absolute cost per Review, per selection measured on both sides (reported, not gated)."""

    out: list[CostChange] = []
    before_all = baseline.get("selections") or {}
    after_all = current.get("selections") or {}
    for label in sorted(set(before_all) & set(after_all)):
        old_costs = before_all[label].get("cost_per_review") or {}
        new_costs = after_all[label].get("cost_per_review") or {}
        for metric in COST_METRICS:
            old_value, new_value = old_costs.get(metric), new_costs.get(metric)
            if old_value == new_value:
                continue
            old, new = _finite(old_value), _finite(new_value)
            if old is None or new is None:
                change = "unknown on one side"
            elif old <= 0:
                change = "rose from zero"
            else:
                ratio = (new - old) / old
                change = f"{'rose' if ratio > 0 else 'fell'} {abs(ratio):.1%}"
            out.append(CostChange(label, metric, old_value, new_value, f"{change} (reported, not gated)"))
    return out


# --------------------------------------------------------------------------- #
# baseline and acceptances
# --------------------------------------------------------------------------- #


def load_baseline(config: GateConfig) -> dict[str, Any] | None:
    if not config.baseline_path.is_file():
        return None
    try:
        raw = json.loads(config.baseline_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GateError(f"{config.baseline_path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("kind") != "review_eval_gate_baseline":
        raise GateError(f"{config.baseline_path} is not a Gate I baseline")
    snapshot = raw.get("snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("snapshot_digest") != raw.get("snapshot_digest"):
        raise GateError(f"{config.baseline_path}: snapshot digest does not match its snapshot")
    return raw


def _measured(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(snapshot.get("selections") or {}, sort_keys=True))  # type: ignore[no-any-return]


def _component_digests(snapshot: Mapping[str, Any]) -> dict[str, str]:
    digest_set = snapshot.get("digest_set") or {}
    return {
        name: str(value.get("digest"))
        for name, value in sorted(digest_set.items())
        if isinstance(value, Mapping) and "digest" in value
    }


@dataclass(frozen=True, slots=True)
class AcceptanceCheck:
    path: str
    valid: bool
    problems: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {"path": self.path, "valid": self.valid, "problems": list(self.problems)}


def check_acceptances(
    config: GateConfig,
    *,
    baseline: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    breaches: Sequence[Breach],
) -> list[AcceptanceCheck]:
    """Acceptance records that target this exact baseline and snapshot, validated."""

    out: list[AcceptanceCheck] = []
    if not config.acceptances_dir.is_dir():
        return out
    for path in sorted(config.acceptances_dir.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            out.append(AcceptanceCheck(path.name, False, (f"unreadable: {exc}",)))
            continue
        if not isinstance(raw, Mapping) or raw.get("kind") != "review_eval_gate_acceptance":
            continue
        if raw.get("current_snapshot_digest") != snapshot.get("snapshot_digest"):
            continue
        problems: list[str] = []
        if raw.get("baseline_snapshot_digest") != baseline.get("snapshot_digest"):
            problems.append("accepted against a different baseline")
        owner = str(raw.get("owner") or "").strip()
        if owner not in config.authorized_owners:
            problems.append(f"owner {owner!r} is not in authorized_owners")
        if not str(raw.get("reason") or "").strip():
            problems.append("no reason recorded")
        measured = raw.get("measured") if isinstance(raw.get("measured"), Mapping) else {}
        assert isinstance(measured, Mapping)
        if measured.get("current") != _measured(snapshot):
            problems.append("the recorded current numbers differ from the measured ones")
        if measured.get("baseline") != _measured(baseline.get("snapshot") or {}):
            problems.append("the recorded baseline numbers differ from the baseline")
        recorded_breaches = raw.get("breaches")
        if recorded_breaches != [breach.to_wire() for breach in breaches]:
            problems.append("the recorded breaches differ from the measured ones")
        out.append(AcceptanceCheck(path.name, not problems, tuple(problems)))
    return out


@dataclass(frozen=True, slots=True)
class GateResult:
    mode: GateMode
    status: GateStatus
    thresholds: Thresholds
    snapshot: Mapping[str, Any]
    baseline: Mapping[str, Any] | None
    breaches: tuple[Breach, ...] = ()
    changed_components: tuple[str, ...] = ()
    acceptances: tuple[AcceptanceCheck, ...] = ()
    fixture_events: tuple[Mapping[str, Any], ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)
    #: Absolute cost-per-Review changes against the baseline: reported, never gated by themselves.
    cost_changes: tuple[CostChange, ...] = ()

    @property
    def blocking(self) -> bool:
        return self.mode == "enforce" and self.status not in PASSING

    @property
    def accepted_by(self) -> AcceptanceCheck | None:
        return next((item for item in self.acceptances if item.valid), None)

    def cost_per_catch(self) -> dict[str, dict[str, Any]]:
        """Per selection: baseline and current cost per weighted catch, and the relative rise per unit."""

        base_rows = ((self.baseline or {}).get("snapshot") or {}).get("selections") or {}
        out: dict[str, dict[str, Any]] = {}
        for label, row in (self.snapshot.get("selections") or {}).items():
            current = catch_cost(row, self.thresholds)
            base_row = base_rows.get(label)
            before = None if base_row is None else catch_cost(base_row, self.thresholds)
            view: dict[str, Any] = {
                "baseline": None if before is None else before.to_wire(),
                "current": None if current is None else current.to_wire(),
            }
            for unit in CATCH_COST_UNITS:
                old = None if before is None else before.per_catch(unit)
                new = None if current is None else current.per_catch(unit)
                rise = None
                if old is not None and new is not None and 0 < old < math.inf and new < math.inf:
                    rise = round((new - old) / old, _PLACES)
                view[f"{unit}_per_catch_rise"] = rise
            out[label] = view
        return out

    def to_wire(self) -> dict[str, Any]:
        return {
            "gate": "Gate I — review-system regression gate",
            "mode": self.mode,
            "status": self.status,
            "blocking": self.blocking,
            "thresholds": self.thresholds.to_wire(),
            "changed_components": list(self.changed_components),
            "breaches": [breach.to_wire() for breach in self.breaches],
            "acceptances": [item.to_wire() for item in self.acceptances],
            "fixture_events": [dict(item) for item in self.fixture_events],
            "snapshot_digest": self.snapshot.get("snapshot_digest"),
            "baseline_snapshot_digest": None if self.baseline is None else self.baseline.get("snapshot_digest"),
            "selections": {
                label: {
                    "catch_rate": row.get("catch_rate"),
                    "behavior_catch_rate": row.get("behavior_catch_rate"),
                    "noise_rate": row.get("noise_rate"),
                    "cost_per_review": row.get("cost_per_review"),
                }
                for label, row in (self.snapshot.get("selections") or {}).items()
            },
            "cost_per_catch": self.cost_per_catch(),
            "cost_per_review_changes": [item.to_wire() for item in self.cost_changes],
            "notes": list(self.notes),
        }


def _dollar_notes(base_snapshot: Mapping[str, Any], snapshot: Mapping[str, Any]) -> list[str]:
    before_all = base_snapshot.get("selections") or {}
    unreported = [
        label
        for label, row in sorted((snapshot.get("selections") or {}).items())
        if label in before_all
        and ((before_all[label].get("cost_total") or {}).get("provider_usd_status"))
        == ((row.get("cost_total") or {}).get("provider_usd_status"))
        == "unreported"
    ]
    if not unreported:
        return []
    return [
        f"provider dollars were reported on neither side for {', '.join(unreported)}; "
        "compute minutes and model calls carry cost per catch there"
    ]


def evaluate_gate(config: GateConfig, report: EvalReport, *, mode: GateMode | None = None) -> GateResult:
    effective: GateMode = mode or config.mode
    snapshot = report.snapshot()
    baseline = load_baseline(config)
    notes: list[str] = []
    approval_problem = config.thresholds.approval_problem()
    if approval_problem:
        notes.append(approval_problem)
    if effective == "enforce" and config.mode != "enforce":
        notes.append("enforce mode was selected explicitly on the command line")
    base_snapshot = None if baseline is None else baseline.get("snapshot")
    changed = tuple(
        changed_components(None if base_snapshot is None else base_snapshot.get("digest_set"), snapshot["digest_set"])
    )
    if not report.complete:
        return GateResult(
            mode=effective,
            status="stale",
            thresholds=config.thresholds,
            snapshot=snapshot,
            baseline=baseline,
            changed_components=changed,
            fixture_events=tuple(report.fixture_events()),
            notes=(
                *notes,
                "stale or missing fixtures were never reused; re-record them (lc dev eval review record) to measure",
            ),
        )
    if baseline is None or base_snapshot is None:
        return GateResult(
            mode=effective,
            status="no_baseline",
            thresholds=config.thresholds,
            snapshot=snapshot,
            baseline=None,
            changed_components=changed,
            notes=(*notes, "no baseline recorded; run lc dev eval review gate --update-baseline"),
        )
    breaches = tuple(compare(base_snapshot, snapshot, config.thresholds))
    cost_changes = tuple(cost_per_review_changes(base_snapshot, snapshot))
    notes.extend(_dollar_notes(base_snapshot, snapshot))
    if breaches:
        checks = tuple(check_acceptances(config, baseline=baseline, snapshot=snapshot, breaches=breaches))
        status: GateStatus = "accepted" if any(item.valid for item in checks) else "blocked"
        return GateResult(
            mode=effective,
            status=status,
            thresholds=config.thresholds,
            snapshot=snapshot,
            baseline=baseline,
            breaches=breaches,
            changed_components=changed,
            acceptances=checks,
            notes=tuple(notes),
            cost_changes=cost_changes,
        )
    same = baseline.get("snapshot_digest") == snapshot.get("snapshot_digest")
    return GateResult(
        mode=effective,
        status="pass" if same else "pass_unrecorded",
        thresholds=config.thresholds,
        snapshot=snapshot,
        baseline=baseline,
        changed_components=changed,
        notes=tuple(notes) if same else (*notes, "within thresholds; record it with --update-baseline"),
        cost_changes=cost_changes,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def write_acceptance(
    config: GateConfig,
    result: GateResult,
    *,
    owner: str,
    reason: str,
    recorded_by: str = "",
    now: str | None = None,
) -> Path:
    """Record an authorized owner's explicit acceptance of the measured trade-off."""

    owner = owner.strip()
    reason = reason.strip()
    if result.status != "blocked" or result.baseline is None:
        raise GateError(f"nothing to accept: the gate status is {result.status}")
    if not config.authorized_owners:
        raise GateError("no authorized_owners are configured in gate.yaml (pending product-owner approval)")
    if owner not in config.authorized_owners:
        raise GateError(f"{owner!r} is not an authorized owner ({', '.join(config.authorized_owners)})")
    if not reason:
        raise GateError("an acceptance needs a reason")
    stamp = now or _now()
    baseline_snapshot = result.baseline.get("snapshot") or {}
    document = {
        "version": 1,
        "kind": "review_eval_gate_acceptance",
        "gate": "Gate I — review-system regression gate",
        "accepted_at": stamp,
        "owner": owner,
        "recorded_by": recorded_by,
        "reason": reason,
        "mode": result.mode,
        "thresholds": config.thresholds.to_wire(),
        "baseline_snapshot_digest": result.baseline.get("snapshot_digest"),
        "current_snapshot_digest": result.snapshot.get("snapshot_digest"),
        "digest_set": {
            "changed": list(result.changed_components),
            "baseline": _component_digests(baseline_snapshot),
            "current": _component_digests(result.snapshot),
        },
        "breaches": [breach.to_wire() for breach in result.breaches],
        "measured": {"baseline": _measured(baseline_snapshot), "current": _measured(result.snapshot)},
    }
    config.acceptances_dir.mkdir(parents=True, exist_ok=True)
    compact = stamp.replace("-", "").replace(":", "").replace("+0000", "Z")
    stem = f"{compact[:15]}-{str(result.snapshot.get('snapshot_digest'))[:12]}"
    path = config.acceptances_dir / f"{stem}.yaml"
    suffix = 1
    while path.exists():  # never overwrite another recorded acceptance
        suffix += 1
        path = config.acceptances_dir / f"{stem}-{suffix}.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def update_baseline(config: GateConfig, result: GateResult, *, now: str | None = None) -> Path:
    """Record this gate result as the baseline; a regression needs a valid acceptance first."""

    if result.status not in {"pass_unrecorded", "accepted", "no_baseline", "pass"}:
        raise GateError(
            f"the gate status is {result.status}; a regressed or unmeasurable result never becomes the baseline "
            "(record an acceptance first, or re-record stale fixtures)"
        )
    acceptance = result.accepted_by
    decision = {"no_baseline": "initial", "accepted": "accepted"}.get(result.status, "passed")
    document = {
        "version": 1,
        "kind": "review_eval_gate_baseline",
        "decision": decision,
        "acceptance": None if acceptance is None else f"{config.acceptances_dir.name}/{acceptance.path}",
        "previous_snapshot_digest": None if result.baseline is None else result.baseline.get("snapshot_digest"),
        "recorded_at": now or _now(),
        "snapshot_digest": result.snapshot.get("snapshot_digest"),
        "snapshot": dict(result.snapshot),
    }
    config.baseline_path.write_text(
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return config.baseline_path


__all__ = [
    "APPROVED",
    "CATCH_COST_UNITS",
    "COST_METRICS",
    "GATE_CONFIG",
    "GATE_DIR_RELATIVE",
    "INFINITE",
    "PENDING",
    "PROPOSED_THRESHOLDS",
    "REMOVED_THRESHOLDS",
    "AcceptanceCheck",
    "Breach",
    "CatchCost",
    "CostChange",
    "GateConfig",
    "GateError",
    "GateResult",
    "Thresholds",
    "catch_cost",
    "check_acceptances",
    "compare",
    "cost_per_catch_breach",
    "cost_per_review_changes",
    "evaluate_gate",
    "find_gate_root",
    "load_baseline",
    "load_gate_config",
    "thresholds_digest",
    "update_baseline",
    "write_acceptance",
]

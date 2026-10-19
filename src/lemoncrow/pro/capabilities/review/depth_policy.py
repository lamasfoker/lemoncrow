"""Depth ladder defaults per path and per-Review depth requests (Phase 11 P11-ESC items 10 and 11).

R1 already resolves the depth (``effort``) per organization, team, repository
and trusted-base layer with an explicit origin, and path-scoped ``min_effort``
floors (directly or compiled from criticality). This module adds:

* **path defaults** (``review.path_effort``): the starting depth for changes
  under given paths, per layer, with the rule's origin. Unlike a floor, a
  default is not required work: the budget may still degrade it, and it can
  name a lower depth than the layer's (a docs-only Change runs Quick). For a
  Change, each changed path takes the last matching rule over the layers in
  precedence order (else the layer depth) and the Change runs at the deepest
  of those. Floors still apply on top.
* **request explanations**: a per-Review request (``Run deeper``, ``Run
  specific…``) may raise depth and never lowers a floor. When the effective
  depth differs from the request, the response names why -- the strictest
  matching ``min_effort`` floor and its origin, escalation, or the budget.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from lemoncrow.pro.capabilities.review.capability_catalog import PRESET_RANK, PRESETS, paths_match

_MAX_RULES: Final[int] = 64


@dataclass(frozen=True, slots=True)
class PathEffortRule:
    """``{paths, effort}``: the default depth for changes under *paths* (``origin``: ``SettingOrigin``)."""

    effort: str
    paths: tuple[str, ...]
    origin: Any

    def to_document(self) -> dict[str, Any]:
        return {"effort": self.effort, "paths": list(self.paths)}

    def to_wire(self) -> dict[str, Any]:
        return {**self.to_document(), "origin": self.origin.to_wire()}


def parse_path_effort(raw: object, origin: Any, errors: list[str]) -> tuple[PathEffortRule, ...]:
    if not isinstance(raw, list):
        errors.append("path_effort must be a list of {paths, effort} rules")
        return ()
    rules: list[PathEffortRule] = []
    for index, item in enumerate(raw[:_MAX_RULES]):
        where = f"path_effort[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{where} must be an object with paths and effort")
            continue
        unknown = sorted(str(key) for key in set(item) - {"paths", "effort"})
        if unknown:
            errors.append(f"{where} has unknown field {unknown[0]!r}")
        effort = item.get("effort")
        if effort not in PRESETS:
            errors.append(f"{where}.effort must be quick, auto, thorough or deep")
            continue
        paths_raw = item.get("paths")
        if isinstance(paths_raw, str):
            paths_raw = [paths_raw]
        if not isinstance(paths_raw, list) or not paths_raw:
            errors.append(f"{where}.paths must be a non-empty list of path globs")
            continue
        valid = [
            value.strip() for value in paths_raw[:64] if isinstance(value, str) and value.strip() and len(value) <= 512
        ]
        if len(valid) != len(paths_raw[:64]) or len(paths_raw) > 64:
            errors.append(f"{where}.paths entries must be at most 64 non-empty strings")
        paths = tuple(dict.fromkeys(valid))
        if not paths:
            continue
        rules.append(PathEffortRule(str(effort), paths, origin))
    if len(raw) > _MAX_RULES:
        errors.append(f"path_effort exceeds {_MAX_RULES} rules")
    return tuple(rules)


@dataclass(frozen=True, slots=True)
class PathDefault:
    """The depth a preset selection starts from for one Change, and why."""

    depth: str
    #: The path rule that set ``depth`` (``None``: the layer depth did).
    rule: PathEffortRule | None = None
    #: Changed paths that rule matched.
    paths: tuple[str, ...] = ()

    def selection_wire(self) -> dict[str, str]:
        """Display keys for ``RunPlan.selection`` (only when a path rule decided)."""

        if self.rule is None:
            return {}
        return {
            "path_default": self.depth,
            "path_default_paths": ", ".join(self.rule.paths),
            "path_default_origin": str(self.rule.origin.source or self.rule.origin.layer),
        }


def path_default_depth(rules: Sequence[PathEffortRule], effort: str, scope: Sequence[str]) -> PathDefault:
    """Per changed path the last matching rule (else *effort*); the Change runs at the deepest."""

    if not rules or not scope:
        return PathDefault(effort)
    decided: list[tuple[str, PathEffortRule | None]] = [
        (path, next((row for row in reversed(rules) if paths_match(row.paths, (path,))), None))
        for path in dict.fromkeys(scope)
    ]

    def depth_of(rule: PathEffortRule | None) -> str:
        return rule.effort if rule is not None else effort

    top = max(PRESET_RANK[depth_of(rule)] for _path, rule in decided)
    at_top = [(path, rule) for path, rule in decided if PRESET_RANK[depth_of(rule)] == top]
    if any(rule is None for _path, rule in at_top):
        # A path no rule matched already runs at this depth: the layer depth decided.
        return PathDefault(PRESETS[top])
    winner = at_top[0][1]
    return PathDefault(PRESETS[top], winner, tuple(path for path, rule in at_top if rule is winner))


def strictest_floor(floors: Sequence[Any], scope: Sequence[str], *, above: str = "quick") -> Any | None:
    """The deepest ``min_effort`` floor matching *scope* that is deeper than *above*."""

    matching = [
        floor
        for floor in floors
        if floor.kind == "min_effort"
        and floor.value in PRESET_RANK
        and PRESET_RANK[floor.value] > PRESET_RANK.get(above, 0)
        and paths_match(floor.paths, scope)
    ]
    if not matching:
        return None
    return max(matching, key=lambda floor: PRESET_RANK[floor.value])


def explain_depth_request(
    requested: str,
    plan: Any,
    floors: Sequence[Any],
    scope: Sequence[str],
    *,
    entry_id: str = "ai.investigators",
) -> dict[str, Any]:
    """How the effective plan answered a per-Review depth request for *entry_id*.

    ``state``: ``honored``, ``raised`` (a floor, escalation or the plan already
    runs deeper; a request never lowers depth), ``limited_by_budget`` or
    ``refused`` (the entry does not run). ``reason`` names the floor and origin.
    """

    entry = plan.entry(entry_id)
    base = {"entry": entry_id, "requested": requested}
    if entry is None or entry.status != "run":
        why = entry.reason if entry is not None else f"{entry_id} is not part of the effective run plan"
        return {**base, "effective": None, "state": "refused", "reason": why}
    effective = str(entry.depth)
    if effective == requested:
        return {**base, "effective": effective, "state": "honored", "reason": ""}
    if PRESET_RANK.get(effective, 0) > PRESET_RANK.get(requested, 0):
        floor = strictest_floor(floors, scope, above=requested)
        if floor is not None and PRESET_RANK[floor.value] >= PRESET_RANK[effective]:
            where = ", ".join(floor.paths) if floor.paths else "every path"
            reason = (
                f"raised to {effective}: a per-Review request cannot go below the min_effort floor "
                f"{floor.value} for {where} ({floor.origin.source or floor.origin.layer})"
            )
        elif entry.origin == "escalation" or entry.escalation:
            reason = f"raised to {effective}: {entry.reason}"
        else:
            reason = (
                f"raised to {effective}: the effective run plan already runs {entry_id} at {effective}; "
                "a per-Review request only adds"
            )
        return {**base, "effective": effective, "state": "raised", "reason": reason}
    state = "limited_by_budget" if entry.budget_reduced_from else "limited"
    return {**base, "effective": effective, "state": state, "reason": entry.reason}


__all__ = [
    "PathDefault",
    "PathEffortRule",
    "explain_depth_request",
    "parse_path_effort",
    "path_default_depth",
    "strictest_floor",
]

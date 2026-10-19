"""Deterministic Change Tour projection over the Review's authoritative targets.

A Tour is navigation, not state. It never records visits, review marks or
annotations; every step points back to exact existing Review targets. Optional
model output may tighten a title/explanation but cannot add targets or facts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

TOUR_SCHEMA_VERSION = 1
TOUR_PROMPT_VERSION = 1
TOUR_MAX_SYNTHESIS_STEPS = 8

TOUR_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "step_id": {"type": "string"},
                    "title": {"type": "string"},
                    "why": {"type": "string"},
                    "target_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["step_id", "title", "why", "target_ids"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["steps"],
    "additionalProperties": False,
}

TOUR_SYSTEM_PROMPT = """You refine a deterministic code-review Change Tour.

You are given existing Tour steps derived from an immutable Review Snapshot.
For a step, make the title and why-this-matters explanation concise and useful
to a human reviewer. Use only the supplied step and target facts. Do not invent
targets, evidence, test results, architecture relationships, author intent, or
approval recommendations. Do not claim that a target has been reviewed.

Return only JSON with:
{"steps": [{"step_id": "...", "title": "...", "why": "...", "target_ids": ["..."]}]}
Omit a step when the deterministic wording is already sufficient.
"""


def _text(value: Any, limit: int = 280) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(".,;:") + "…"


def _values(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, (list, tuple)) else []


def _target_value(target: Any, name: str, default: Any = None) -> Any:
    if isinstance(target, Mapping):
        return target.get(name, default)
    return getattr(target, name, default)


def _verification(target: Any) -> tuple[int, int]:
    value = _target_value(target, "verification", {})
    if isinstance(value, Mapping):
        return int(value.get("fail") or value.get("fail_count") or 0), int(
            value.get("unknown") or value.get("unknown_count") or 0
        )
    return int(getattr(value, "fail_count", 0) or 0), int(getattr(value, "unknown_count", 0) or 0)


def _signal_text(item: Mapping[str, Any], targets: Sequence[Any]) -> str:
    parts = [
        str(item.get("title") or ""),
        str(item.get("before") or ""),
        str(item.get("after") or ""),
        str(item.get("rationale") or ""),
    ]
    for target in targets:
        parts.extend(
            [
                str(_target_value(target, "path", "")),
                str(_target_value(target, "symbol", "")),
                " ".join(str(value) for value in (_target_value(target, "reasons", ()) or ())),
            ]
        )
    return " ".join(parts).casefold()


def _deterministic_signals(
    item: Mapping[str, Any],
    targets: Sequence[Any],
    *,
    changed: set[str],
    added: set[str],
    reopened: bool,
    decision_refs: Sequence[str],
) -> list[str]:
    signals: list[str] = []
    if reopened:
        signals.append("reopened_judgment")
    if decision_refs:
        signals.append("human_decision")
    if any(_verification(target)[0] > 0 for target in targets):
        signals.append("verification_failed")
    elif any(_verification(target)[1] > 0 for target in targets):
        signals.append("verification_unknown")
    ids = {str(_target_value(target, "target_id", "")) for target in targets}
    if ids & (changed | added):
        signals.append("snapshot_delta")
    if item.get("kind") == "behavior" or item.get("before") or item.get("after"):
        signals.append("behavior_contract")
    text = _signal_text(item, targets)
    if any(token in text for token in ("auth", "permission", "security", "trust", "credential", "token", "tenant")):
        signals.append("security_trust_boundary")
    if any(token in text for token in ("persist", "storage", "database", "schema", "state", "migration", "cache")):
        signals.append("state_persistence")
    if any(token in text for token in ("api", "public", "contract", "protocol", "wire", "cli", "endpoint")):
        signals.append("public_contract")
    return list(dict.fromkeys(signals))


def _why(item: Mapping[str, Any], signals: Sequence[str]) -> tuple[str, bool]:
    after = _text(item.get("after"))
    before = _text(item.get("before"))
    rationale = _text(item.get("rationale"))
    concerns = _values(item.get("concerns"))
    if before and after:
        return f"{before} → {after}", False
    if after:
        return after, False
    if rationale and item.get("rationale_origin") != "none":
        return rationale, False
    if concerns:
        reason = _text(concerns[0].get("reason") or concerns[0].get("scope"))
        if reason:
            return reason, False
    if "verification_failed" in signals:
        return "Recorded verification for this part of the change is failing.", False
    if "reopened_judgment" in signals:
        return "A prior human judgment was reopened by this Snapshot.", False
    return "Review the changed implementation and its observable effect.", True


def build_change_tour(projection: Mapping[str, Any], targets: Sequence[Any]) -> dict[str, Any]:
    """Build an ordered Tour without creating any new Review state."""

    target_by_id = {
        str(_target_value(target, "target_id", "")): target
        for target in targets
        if str(_target_value(target, "target_id", ""))
    }
    claims = {str(row.get("id") or ""): row for row in _values(projection.get("claims"))}
    decisions = _values(projection.get("decisions"))
    raw_delta = projection.get("delta")
    delta: Mapping[str, Any] = raw_delta if isinstance(raw_delta, Mapping) else {}
    changed = {str(value) for value in (delta.get("changed") or ())}
    added = {str(value) for value in (delta.get("added") or ())}
    reopened_decisions = {str(value) for value in (delta.get("reopened_decisions") or ())}

    steps: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in _values(projection.get("items")):
        semantic_key = str(item.get("key") or item.get("id") or "")
        if not semantic_key:
            continue
        step_id = f"tour:{semantic_key}"
        if step_id in seen_ids:
            step_id = f"tour:{semantic_key}:{item.get('id') or len(steps)}"
        seen_ids.add(step_id)

        declared_ids = list(
            dict.fromkeys(
                str(value)
                for value in (
                    *tuple(item.get("primary_target_ids") or ()),
                    *tuple(item.get("related_target_ids") or ()),
                )
                if str(value)
            )
        )
        current_ids = [target_id for target_id in declared_ids if target_id in target_by_id]
        stale_ids = [target_id for target_id in declared_ids if target_id not in target_by_id]
        if not current_ids and not stale_ids:
            continue
        current_targets = [target_by_id[target_id] for target_id in current_ids]

        decision_refs = [
            str(row.get("key") or "")
            for row in decisions
            if set(str(value) for value in (row.get("target_ids") or ())) & set(current_ids)
        ]
        reopened = bool(set(decision_refs) & reopened_decisions)
        signals = _deterministic_signals(
            item,
            current_targets,
            changed=changed,
            added=added,
            reopened=reopened,
            decision_refs=decision_refs,
        )
        mechanical = str(item.get("kind") or "") == "mechanical" or (
            bool(current_targets)
            and all(str(_target_value(target, "structural_level", "")) == "mechanical" for target in current_targets)
        )
        consequential_mechanical = bool(
            {"verification_failed", "reopened_judgment", "human_decision", "security_trust_boundary"} & set(signals)
        )
        if mechanical and not consequential_mechanical:
            continue

        evidence_refs: list[dict[str, Any]] = []
        for claim_id in item.get("claim_ids") or ():
            claim = claims.get(str(claim_id))
            if claim is None:
                continue
            for evidence in _values(claim.get("evidence")):
                evidence_refs.append(
                    {
                        "claim_id": str(claim_id),
                        "key": str(evidence.get("key") or ""),
                        "title": str(evidence.get("title") or evidence.get("key") or ""),
                        "status": str(evidence.get("status") or ""),
                        "state": str(evidence.get("state") or ""),
                        "snapshot_number": evidence.get("snapshot_number"),
                    }
                )

        why, synthesis_needed = _why(item, signals)
        first = current_targets[0] if current_targets else None
        changed_ids = set(current_ids) & changed
        added_ids = set(current_ids) & added
        change_type = "reopened" if reopened else "added" if added_ids else "changed" if changed_ids else "current"
        position = item.get("position")
        position_key = int(position) if isinstance(position, int) else 1 << 30
        first_path = str(_target_value(first, "path", "")) if first is not None else ""
        first_line = int(_target_value(first, "start_line", 0) or 0) if first is not None else 0
        priority = (
            0 if reopened else 1,
            0 if "verification_failed" in signals else 1,
            0 if decision_refs else 1,
            0 if item.get("kind") == "behavior" else 1,
            0 if {"public_contract", "security_trust_boundary", "state_persistence"} & set(signals) else 1,
            0 if change_type in {"added", "changed"} else 1,
            position_key,
            first_path,
            first_line,
            step_id,
        )
        steps.append(
            {
                "id": step_id,
                "semantic_key": semantic_key,
                "title": _text(item.get("title") or first_path or semantic_key, 120),
                "why": why,
                "why_origin": "deterministic",
                "target_ids": current_ids,
                "stale_target_ids": stale_ids,
                "change_type": change_type,
                "signals": signals,
                "claim_ids": [str(value) for value in (item.get("claim_ids") or ())],
                "decision_refs": [value for value in decision_refs if value],
                "evidence_refs": evidence_refs,
                "stale": bool(item.get("stale"))
                or bool(stale_ids)
                or any(row.get("state") == "stale" for row in evidence_refs),
                "synthesis_needed": synthesis_needed,
                "implementation": (
                    {
                        "target_id": str(_target_value(first, "target_id", "")),
                        "path": first_path,
                        "symbol": str(_target_value(first, "symbol", "")),
                        "start_line": first_line,
                        "end_line": int(_target_value(first, "end_line", 0) or 0),
                    }
                    if first is not None
                    else None
                ),
                "_order": priority,
            }
        )

    steps.sort(key=lambda row: tuple(row["_order"]))
    for index, step in enumerate(steps):
        step.pop("_order", None)
        step["position"] = index
    return {"schema_version": TOUR_SCHEMA_VERSION, "steps": steps, "step_count": len(steps)}


def tour_synthesis_context(tour: Mapping[str, Any]) -> dict[str, Any]:
    """Bounded semantic input for optional wording refinement.

    Display ordering is deliberately excluded from durable identity: visiting,
    marking or reprioritizing existing targets must not re-spend for the same
    semantic wording gaps.
    """

    candidates = sorted(
        (row for row in _values(tour.get("steps")) if row.get("synthesis_needed")),
        key=lambda row: str(row.get("id") or ""),
    )
    rows = []
    for row in candidates[:TOUR_MAX_SYNTHESIS_STEPS]:
        rows.append(
            {
                "step_id": str(row.get("id") or ""),
                "title": str(row.get("title") or ""),
                "why": str(row.get("why") or ""),
                "target_ids": sorted(str(value) for value in (row.get("target_ids") or ())),
                "change_type": str(row.get("change_type") or ""),
                "signals": sorted(str(value) for value in (row.get("signals") or ())),
                "implementation": row.get("implementation"),
            }
        )
    return {
        "steps": rows,
        "skipped_step_ids": sorted(str(row.get("id") or "") for row in candidates[TOUR_MAX_SYNTHESIS_STEPS:]),
    }


def tour_messages(context: Mapping[str, Any]) -> tuple[str, str]:
    import json

    return TOUR_SYSTEM_PROMPT, json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def validate_tour_synthesis(raw: Mapping[str, Any], tour: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Accept only refinements that stay inside an existing exact Tour step."""

    known = {str(row.get("id") or ""): row for row in _values(tour.get("steps"))}
    validated: dict[str, dict[str, Any]] = {}
    for row in _values(raw.get("steps")):
        step_id = str(row.get("step_id") or "")
        step = known.get(step_id)
        if step is None:
            continue
        allowed = {str(value) for value in (step.get("target_ids") or ())}
        target_ids = [str(value) for value in (row.get("target_ids") or ())]
        if not target_ids or not set(target_ids).issubset(allowed):
            continue
        title = _text(row.get("title"), 120)
        why = _text(row.get("why"), 360)
        if not title or not why:
            continue
        validated[step_id] = {"title": title, "why": why, "target_ids": target_ids}
    return validated


def apply_tour_synthesis(tour: Mapping[str, Any], raw: Mapping[str, Any]) -> dict[str, Any]:
    validated = validate_tour_synthesis(raw, tour)
    steps: list[dict[str, Any]] = []
    for row in _values(tour.get("steps")):
        step = dict(row)
        refined = validated.get(str(step.get("id") or ""))
        if refined is not None:
            step["title"] = refined["title"]
            step["why"] = refined["why"]
            step["why_origin"] = "inferred"
            step["synthesis_needed"] = False
        steps.append(step)
    return {**dict(tour), "steps": steps}


__all__ = [
    "TOUR_JSON_SCHEMA",
    "TOUR_MAX_SYNTHESIS_STEPS",
    "TOUR_PROMPT_VERSION",
    "TOUR_SCHEMA_VERSION",
    "apply_tour_synthesis",
    "build_change_tour",
    "tour_messages",
    "tour_synthesis_context",
    "validate_tour_synthesis",
]

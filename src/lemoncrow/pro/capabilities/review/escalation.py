"""Deep-review escalation inside the depth ladder (Phase 11 P11-ESC items 6 and 12).

Deep contextual review -- deeper AI investigators plus hypothesis synthesis --
is reserved for changes that deterministic signals mark as consequential:
auth/access control, business logic, cross-service contracts,
concurrency/state and critical architecture. Escalation is not a second
control. It lives inside the Auto rung of the one depth ladder and only raises
what that rung already plans: ``ai.investigators`` to the escalation depth and
the escalate-gated ``ai.hypotheses`` from skipped to run. Quick never escalates
(it is the explicit cheap choice); a rung at or above the target has nothing
to raise.

Trigger classes, each deterministic and each shown with its paths and the
policy origin behind it:

* ``criticality`` -- a changed path whose triage criticality meets the threshold;
* ``static_inventory`` -- the R3 side-effect inventory of a changed file gained
  or lost a fact in a consequential category (permissions, drops, migrations,
  scheduled jobs, data flows);
* ``changed_contracts`` -- a cross-service contract changed: a route or
  outbound-host fact, or a schema file (proto, GraphQL, OpenAPI, ...);
* ``security_chains`` -- a P11-FED security chain at or above the severity threshold;
* ``explicit_policy`` -- a changed path matches ``escalation.paths``;
* ``author_track_record`` -- the authoring agent/model's track record is poor
  (``track_record.py``; agents only, never a human author).

Escalation changes only which AI entries run and at what depth. It never
touches severity, presentation or priority, and never lowers a floor or a
per-Review request. ``escalation.disabled_paths`` turns deep review off for
paths (shown with the reason, never silent). ``escalation.max_model_calls`` is
its own ceiling: escalation that does not fit is shown Limited by budget.

Every threshold is policy data (``review.escalation``), so ``lc dev eval review``
can sweep it and Gate I can measure catch rate against cost.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal, cast

from lemoncrow.pro.capabilities.review.capability_catalog import PRESET_RANK, PRESETS

TriggerClass = Literal[
    "criticality", "static_inventory", "changed_contracts", "security_chains", "explicit_policy", "author_track_record"
]
TRIGGER_CLASSES: Final[tuple[TriggerClass, ...]] = (
    "criticality",
    "static_inventory",
    "changed_contracts",
    "security_chains",
    "explicit_policy",
    "author_track_record",
)
EscalationState = Literal["escalated", "none", "disabled", "suppressed", "not_applicable", "limited_by_budget"]
ESCALATION_TARGETS: Final[tuple[str, ...]] = ("thorough", "deep")
INVESTIGATORS: Final[str] = "ai.investigators"
HYPOTHESES: Final[str] = "ai.hypotheses"
ESCALATED_ENTRIES: Final[tuple[str, ...]] = (INVESTIGATORS, HYPOTHESES)
#: The reason ``build_run_plan`` gives an escalate-gated entry without a risk signal.
AUTO_GATED_REASON: Final[str] = "Auto escalates only on a risk signal; none was found"

CRITICALITY_LEVELS: Final[tuple[str, ...]] = ("critical", "high")
_CRITICALITY_RANK: Final[Mapping[str, int]] = {"high": 2, "critical": 3}
CHAIN_SEVERITIES: Final[tuple[str, ...]] = ("p0", "p1", "p2")
_SEVERITY_RANK: Final[Mapping[str, int]] = {"suggestion": 0, "p2": 1, "p1": 2, "p0": 3}

#: Built-in escalation policy. Each key is inherited unless a settings layer
#: overrides it (``review.escalation.<key>``), with that layer as its origin.
ESCALATION_DEFAULTS: Final[Mapping[str, Any]] = {
    "enabled": True,
    "to": "thorough",
    # No ceiling of its own unless policy sets one; the per-Review budget still applies.
    "max_model_calls": None,
    "criticality": "high",
    "security_chains": "p1",
    "static_inventory": ("permission", "db_drop", "migration", "scheduled_job", "data_flow"),
    "changed_contracts": ("route", "outbound_host"),
    "contract_paths": (
        "*.proto",
        "*.graphql",
        "*.graphqls",
        "*.gql",
        "*.avsc",
        "*.thrift",
        "*.wsdl",
        "*openapi*.yaml",
        "*openapi*.yml",
        "*openapi*.json",
        "*swagger*.yaml",
        "*swagger*.yml",
        "*swagger*.json",
    ),
    "paths": (),
    "disabled_paths": (),
    # P11 item 16: a poor agent/model track record escalates (agents only).
    "author_track_record": True,
}
_LIST_KEYS: Final[frozenset[str]] = frozenset(
    {"static_inventory", "changed_contracts", "contract_paths", "paths", "disabled_paths"}
)

# lc-debt: escalation facts inventory at most 200 changed files (each capped at 1 MiB by
# the inventory itself); files past the bound escalate conservatively instead of being
# read. Upgrade path: reuse the side_effects.static register once it is computed before planning.
MAX_INVENTORY_PATHS: Final[int] = 200
#: Fact rows kept per side-effect category (every category stays represented).
MAX_PATHS_PER_CATEGORY: Final[int] = 16
MAX_FACT_ROWS: Final[int] = 256
MAX_NOTE_CHARS: Final[int] = 400
_MAX_GLOBS: Final[int] = 64
_SHOWN_PATHS: Final[int] = 8


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #


def _globs(raw: object, field_name: str, errors: list[str]) -> tuple[str, ...]:
    if raw is None or raw is False:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        errors.append(f"{field_name} must be a list of path globs")
        return ()
    out: list[str] = []
    for item in raw[:_MAX_GLOBS]:
        if not isinstance(item, str) or not item.strip() or len(item) > 512:
            errors.append(f"{field_name} entries must be non-empty strings")
            continue
        out.append(item.strip())
    if len(raw) > _MAX_GLOBS:
        errors.append(f"{field_name} exceeds {_MAX_GLOBS} entries")
    return tuple(dict.fromkeys(out))


def parse_escalation(raw: object, errors: list[str]) -> dict[str, Any]:
    """The explicit keys of one layer's ``escalation:`` section; problems go to ``errors``."""

    from lemoncrow.pro.capabilities.review.contracts import SIDE_EFFECT_CATEGORIES

    # YAML 1.1 reads a bare ``escalation: off`` as False.
    if isinstance(raw, bool):
        return {"enabled": raw}
    if raw == "off":
        return {"enabled": False}
    if not isinstance(raw, Mapping):
        errors.append("escalation must be a mapping")
        return {}
    out: dict[str, Any] = {}
    for key in sorted(str(item) for item in set(raw) - set(ESCALATION_DEFAULTS)):
        errors.append(f"unknown escalation setting {key!r}")
    if "enabled" in raw:
        if isinstance(raw["enabled"], bool):
            out["enabled"] = raw["enabled"]
        else:
            errors.append("escalation.enabled must be true or false")
    if "author_track_record" in raw:
        value = raw["author_track_record"]
        if isinstance(value, bool) or value in {"on", "off"}:
            out["author_track_record"] = value if isinstance(value, bool) else value == "on"
        else:
            errors.append("escalation.author_track_record must be true, false, on or off")
    if "to" in raw:
        if raw["to"] in ESCALATION_TARGETS:
            out["to"] = str(raw["to"])
        else:
            errors.append("escalation.to must be thorough or deep")
    if "max_model_calls" in raw:
        value = raw["max_model_calls"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            errors.append("escalation.max_model_calls must be a whole number of at least 0")
        else:
            out["max_model_calls"] = int(value)
    for key, allowed in (("criticality", CRITICALITY_LEVELS), ("security_chains", CHAIN_SEVERITIES)):
        if key not in raw:
            continue
        value = "off" if raw[key] is False else raw[key]
        if value in (*allowed, "off"):
            out[key] = str(value)
        else:
            errors.append(f"escalation.{key} must be {', '.join(allowed)} or off")
    for key in ("static_inventory", "changed_contracts"):
        if key not in raw:
            continue
        value = raw[key]
        if value is None or value is False or value == "off":
            out[key] = ()
            continue
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            errors.append(f"escalation.{key} must be a list of side-effect categories or off")
            continue
        unknown = [item for item in value if item not in SIDE_EFFECT_CATEGORIES]
        if unknown:
            errors.append(f"escalation.{key}: unknown side-effect category {unknown[0]!r}")
        out[key] = tuple(dict.fromkeys(item for item in value if item in SIDE_EFFECT_CATEGORIES))
    for key in ("contract_paths", "paths", "disabled_paths"):
        if key in raw:
            out[key] = _globs(raw[key], f"escalation.{key}", errors)
    return out


def escalation_document(values: Mapping[str, Any]) -> dict[str, Any]:
    """``review.escalation``-shaped document; parsing it yields the same values."""

    return {key: list(value) if key in _LIST_KEYS else value for key, value in values.items()}


@dataclass(frozen=True, slots=True)
class EscalationPolicy:
    """Effective escalation policy: built-in defaults overlaid with explicit layer values.

    ``origins`` maps each explicit key to the settings layer that set it (a
    ``run_plan.SettingOrigin``); every other key inherits the built-in default.
    """

    explicit: Mapping[str, Any] = field(default_factory=dict)
    origins: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: Any) -> EscalationPolicy:
        explicit = dict(getattr(settings, "escalation", None) or {})
        return cls(explicit, {key: settings.origin(f"escalation.{key}") for key in explicit})

    def value(self, key: str) -> Any:
        return self.explicit.get(key, ESCALATION_DEFAULTS[key])

    def origin_label(self, key: str) -> str:
        origin = self.origins.get(key)
        if origin is None:
            return "built-in default"
        return str(origin.source or origin.layer)

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in ESCALATION_DEFAULTS:
            value = self.value(key)
            origin = self.origins.get(key)
            out[key] = {
                "effective_value": list(value) if key in _LIST_KEYS else value,
                "mode": "override" if key in self.explicit else "inherit",
                "origin": (
                    origin.to_wire()
                    if origin is not None
                    else {"layer": "built_in_default", "source": "built_in_default"}
                ),
            }
        return out


# --------------------------------------------------------------------------- #
# facts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EscalationFacts:
    """Deterministic signals observed when a Snapshot's plan is first built.

    Frozen on the plan (``signals.escalation``) like the AI candidate count, so
    a persisted plan keeps its decision while marks and evidence change.
    """

    #: ``(path, criticality, origin)`` for changed paths at high or critical.
    criticality: tuple[tuple[str, str, str], ...] = ()
    #: ``(category, path, name)`` for side-effect facts a changed file gained or lost.
    side_effects: tuple[tuple[str, str, str], ...] = ()
    #: ``(severity, path, rule id)`` for current P11-FED security chains.
    security_chains: tuple[tuple[str, str, str], ...] = ()
    #: In-scope changed files past :data:`MAX_INVENTORY_PATHS` (not inventoried).
    not_inventoried: int = 0
    #: ``(agent/model, metric, detail)`` for each poor metric of the authoring
    #: agent's track record (P11 item 16). Never present for a human author.
    author_track_record: tuple[tuple[str, str, str], ...] = ()
    #: Why the authoring agent's record is incomplete (its bounded window was exceeded);
    #: shown as a note, never a trigger -- unknown is not poor.
    track_record_unknown: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "criticality": [list(row) for row in self.criticality],
            "side_effects": [list(row) for row in self.side_effects],
            "security_chains": [list(row) for row in self.security_chains],
            "not_inventoried": self.not_inventoried,
            # Only when an agent's record is poor: a plan without it keeps its digest.
            **(
                {"author_track_record": [list(row) for row in self.author_track_record]}
                if self.author_track_record
                else {}
            ),
            **({"track_record_unknown": self.track_record_unknown} if self.track_record_unknown else {}),
        }

    @classmethod
    def from_wire(cls, raw: object) -> EscalationFacts | None:
        if not isinstance(raw, Mapping):
            return None

        def rows(key: str) -> tuple[tuple[str, str, str], ...]:
            value = raw.get(key)
            out: list[tuple[str, str, str]] = []
            for row in value if isinstance(value, list) else ():
                if isinstance(row, list | tuple) and len(row) == 3:
                    out.append((str(row[0]), str(row[1]), str(row[2])))
            return tuple(out[:MAX_FACT_ROWS])

        count = raw.get("not_inventoried")
        return cls(
            criticality=rows("criticality"),
            side_effects=rows("side_effects"),
            security_chains=rows("security_chains"),
            not_inventoried=max(0, count) if isinstance(count, int) and not isinstance(count, bool) else 0,
            author_track_record=rows("author_track_record"),
            track_record_unknown=str(raw.get("track_record_unknown") or "")[:MAX_NOTE_CHARS],
        )


def collect_escalation_facts(
    paths: Sequence[str],
    *,
    read_base: Callable[[str], str | None],
    read_head: Callable[[str], str | None],
    classify: Callable[[str], tuple[str, str]] | None = None,
    chains: Iterable[tuple[str, str, str]] = (),
) -> EscalationFacts:
    """Observe the signals for *paths* (the whole Change).

    ``classify`` returns ``(criticality, origin)`` for a path (triage policy);
    ``chains`` are ``(severity, path, rule id)`` rows of current security chains.
    The R3 inventory compares each in-scope changed file's base and head facts.
    """

    from lemoncrow.pro.capabilities.review.side_effects import file_facts, inventory_scope

    unique = tuple(dict.fromkeys(path for path in paths if path))
    criticality: list[tuple[str, str, str]] = []
    if classify is not None:
        for path in unique:
            level, origin = classify(path)
            if level in _CRITICALITY_RANK:
                criticality.append((path, level, origin))
    scoped = [path for path in unique if inventory_scope(path)]
    inventoried = scoped[:MAX_INVENTORY_PATHS]
    by_category: dict[str, list[tuple[str, str, str]]] = {}

    def facts_of(path: str, text: str | None) -> set[tuple[str, str, str]]:
        # A fact's value is part of what changed: a migration's content digest, a
        # permission level, a job schedule. Unreadable (None): no facts on that side.
        if text is None:
            return set()
        return {(fact.category, fact.name, fact.value) for fact in file_facts(path, text)}

    for path in inventoried:
        before = facts_of(path, read_base(path))
        after = facts_of(path, read_head(path))
        seen: set[str] = set()
        for category, name, _value in sorted(before ^ after):
            if category in seen:
                continue
            seen.add(category)
            bucket = by_category.setdefault(category, [])
            if len(bucket) < MAX_PATHS_PER_CATEGORY:
                bucket.append((category, path, name))
    side_effects = [row for category in sorted(by_category) for row in by_category[category]]
    return EscalationFacts(
        criticality=tuple(criticality[:MAX_FACT_ROWS]),
        side_effects=tuple(side_effects[:MAX_FACT_ROWS]),
        security_chains=tuple(sorted(set(chains))[:MAX_FACT_ROWS]),
        not_inventoried=len(scoped) - len(inventoried),
    )


# --------------------------------------------------------------------------- #
# decision
# --------------------------------------------------------------------------- #


def _matches(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _shown(paths: Sequence[str]) -> str:
    head = ", ".join(paths[:3])
    return head + (f" and {len(paths) - 3} more" if len(paths) > 3 else "")


@dataclass(frozen=True, slots=True)
class EscalationTrigger:
    """One fired (or suppressed) trigger. Empty ``paths``: not attributable to a path."""

    trigger_class: TriggerClass
    label: str
    paths: tuple[str, ...]
    origin: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "class": self.trigger_class,
            "label": self.label,
            "paths": list(self.paths[:_SHOWN_PATHS]),
            "path_count": len(self.paths),
            "origin": self.origin,
        }


def escalation_triggers(
    facts: EscalationFacts, policy: EscalationPolicy, scope: Sequence[str]
) -> tuple[EscalationTrigger, ...]:
    """Every trigger the facts satisfy under *policy*, before path enablement."""

    out: list[EscalationTrigger] = []
    level = str(policy.value("criticality"))
    if level in _CRITICALITY_RANK:
        rows = [row for row in facts.criticality if _CRITICALITY_RANK.get(row[1], 0) >= _CRITICALITY_RANK[level]]
        if rows:
            sources = ", ".join(sorted({row[2] for row in rows if row[2]}))
            out.append(
                EscalationTrigger(
                    "criticality",
                    f"path criticality {level} or higher: {_shown([row[0] for row in rows])}",
                    tuple(dict.fromkeys(row[0] for row in rows)),
                    policy.origin_label("criticality") + (f"; criticality from {sources}" if sources else ""),
                )
            )
    inventory = set(policy.value("static_inventory"))
    if inventory:
        rows = [row for row in facts.side_effects if row[0] in inventory]
        if rows:
            categories = ", ".join(sorted({row[0].replace("_", " ") for row in rows}))
            out.append(
                EscalationTrigger(
                    "static_inventory",
                    f"side-effect inventory changed ({categories}) in {_shown([row[1] for row in rows])}",
                    tuple(dict.fromkeys(row[1] for row in rows)),
                    policy.origin_label("static_inventory"),
                )
            )
        if facts.not_inventoried:
            # Conservative: files past the inventory bound may hold the signal.
            out.append(
                EscalationTrigger(
                    "static_inventory",
                    f"{facts.not_inventoried} changed file(s) past the {MAX_INVENTORY_PATHS}-file inventory bound "
                    "were not inventoried",
                    (),
                    policy.origin_label("static_inventory"),
                )
            )
    contracts = set(policy.value("changed_contracts"))
    contract_rows = [row for row in facts.side_effects if row[0] in contracts]
    schemas = [path for path in scope if _matches(path, tuple(policy.value("contract_paths")))]
    if contract_rows or schemas:
        parts = []
        if contract_rows:
            parts.append(", ".join(sorted({row[0].replace("_", " ") for row in contract_rows})) + " changed")
        if schemas:
            parts.append(f"contract schema changed ({_shown(schemas)})")
        origin_keys = [key for key, hit in (("changed_contracts", contract_rows), ("contract_paths", schemas)) if hit]
        out.append(
            EscalationTrigger(
                "changed_contracts",
                "cross-service contract: " + "; ".join(parts),
                tuple(dict.fromkeys([*(row[1] for row in contract_rows), *schemas])),
                "; ".join(dict.fromkeys(policy.origin_label(key) for key in origin_keys)),
            )
        )
    threshold = str(policy.value("security_chains"))
    if threshold in _SEVERITY_RANK:
        chains = [row for row in facts.security_chains if _SEVERITY_RANK.get(row[0], 0) >= _SEVERITY_RANK[threshold]]
        if chains:
            rules = ", ".join(sorted({row[2] for row in chains}))
            out.append(
                EscalationTrigger(
                    "security_chains",
                    f"security chain at {threshold} or higher ({rules}) in {_shown([row[1] for row in chains])}",
                    tuple(dict.fromkeys(row[1] for row in chains)),
                    policy.origin_label("security_chains"),
                )
            )
    explicit = [path for path in scope if _matches(path, tuple(policy.value("paths")))]
    if explicit:
        out.append(
            EscalationTrigger(
                "explicit_policy",
                f"policy escalates {_shown(explicit)}",
                tuple(explicit),
                policy.origin_label("paths"),
            )
        )
    if policy.value("author_track_record") and facts.author_track_record:
        record = facts.author_track_record
        agents = ", ".join(dict.fromkeys(row[0] for row in record))
        out.append(
            EscalationTrigger(
                "author_track_record",
                f"author track record of {agents}: "
                + "; ".join(row[2] for row in record[:4])
                + (f"; and {len(record) - 4} more" if len(record) > 4 else ""),
                (),
                policy.origin_label("author_track_record"),
            )
        )
    return tuple(out)


@dataclass(frozen=True, slots=True)
class EscalationDecision:
    """What escalation decided for one plan (display: ``RunPlan.escalation``)."""

    state: EscalationState
    target: str
    reason: str
    triggers: tuple[EscalationTrigger, ...] = ()
    suppressed: tuple[tuple[EscalationTrigger, str], ...] = ()
    ceiling: int | None = None
    ceiling_origin: str = ""
    target_origin: str = ""
    entries: tuple[str, ...] = ()
    #: Coverage gaps the reviewer should know about; they never escalate.
    notes: tuple[str, ...] = ()

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(trigger.label for trigger in self.triggers)

    def to_wire(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "to": self.target,
            "to_origin": self.target_origin,
            "reason": self.reason,
            "triggers": [trigger.to_wire() for trigger in self.triggers],
            "suppressed": [{**trigger.to_wire(), "reason": why} for trigger, why in self.suppressed],
            "ceiling": {"max_model_calls": self.ceiling, "origin": self.ceiling_origin},
            "entries": list(self.entries),
            **({"notes": list(self.notes)} if self.notes else {}),
        }


def decide_escalation(facts: EscalationFacts, policy: EscalationPolicy, scope: Sequence[str]) -> EscalationDecision:
    """Which triggers fire for this Change, after per-path enablement."""

    target = str(policy.value("to"))
    ceiling = policy.value("max_model_calls")
    common: dict[str, Any] = {
        "target": target,
        "ceiling": None if ceiling is None else int(ceiling),
        "ceiling_origin": policy.origin_label("max_model_calls"),
        "target_origin": policy.origin_label("to"),
    }
    if not policy.value("enabled"):
        return EscalationDecision(
            state="disabled",
            reason=f"deep review escalation is turned off ({policy.origin_label('enabled')})",
            **common,
        )
    if facts.track_record_unknown and policy.value("author_track_record"):
        common["notes"] = (facts.track_record_unknown,)
    disabled = tuple(policy.value("disabled_paths"))
    fired: list[EscalationTrigger] = []
    suppressed: list[tuple[EscalationTrigger, str]] = []
    for trigger in escalation_triggers(facts, policy, scope):
        attributed = trigger.paths or tuple(scope)
        kept = tuple(path for path in attributed if not _matches(path, disabled))
        if kept or not attributed:
            fired.append(replace(trigger, paths=kept if trigger.paths else ()))
        else:
            suppressed.append(
                (
                    trigger,
                    f"deep review is disabled for {_shown(attributed)} ({policy.origin_label('disabled_paths')})",
                )
            )
    if fired:
        return EscalationDecision(
            state="escalated",
            reason=f"escalated to {target}: " + "; ".join(trigger.label for trigger in fired),
            triggers=tuple(fired),
            suppressed=tuple(suppressed),
            **common,
        )
    if suppressed:
        return EscalationDecision(
            state="suppressed",
            reason="not escalated: " + "; ".join(why for _trigger, why in suppressed),
            suppressed=tuple(suppressed),
            **common,
        )
    return EscalationDecision(state="none", reason="no deterministic escalation signal", **common)


# --------------------------------------------------------------------------- #
# plan registration
# --------------------------------------------------------------------------- #


def _entry_wire(
    decision: EscalationDecision, state: str, *, from_depth: str = "", to_depth: str = ""
) -> dict[str, Any]:
    return {
        "state": state,
        "from": from_depth,
        "to": to_depth,
        "triggers": [trigger.to_wire() for trigger in decision.triggers],
    }


def apply_escalation(drafts: Mapping[str, Any], decision: EscalationDecision, *, base: str) -> EscalationDecision:
    """Raise the Auto rung's AI entries for a fired decision, within the escalation ceiling.

    Mutates ``depth``/``status``/``reason``/``origin``/``escalation`` (and
    ``budget_reduced_from``) of ``ai.investigators`` and the escalate-gated
    ``ai.hypotheses`` drafts. Runs after statuses and dependencies resolve and
    before the fast lane and the budget.
    """

    if decision.state != "escalated":
        return decision
    if base == "quick":
        return replace(decision, state="not_applicable", reason="Quick does not escalate; " + decision.reason)
    target = decision.target
    ceiling = decision.ceiling
    ceiling_note = f"the deep-review ceiling allows {ceiling} model call(s) ({decision.ceiling_origin})"
    spent = 0
    touched: list[str] = []
    investigators = drafts.get(INVESTIGATORS)
    if (
        investigators is not None
        and investigators.status == "run"
        and PRESET_RANK[investigators.depth] < PRESET_RANK[target]
    ):
        original = cast(str, investigators.depth)
        steps = PRESETS[PRESET_RANK[original] + 1 : PRESET_RANK[target] + 1]
        chosen = ""
        for depth in reversed(steps):
            extra = investigators.catalog.model_calls(depth) - investigators.catalog.model_calls(original)
            if ceiling is None or spent + extra <= ceiling:
                chosen, spent = depth, spent + extra
                break
        if investigators.origin not in {"policy_floor", "manual"}:
            investigators.origin = "escalation"
        touched.append(INVESTIGATORS)
        if not chosen:
            investigators.budget_reduced_from = target
            investigators.reason = f"Limited by budget: escalation to {target} does not fit; {ceiling_note}"
            investigators.escalation = _entry_wire(decision, "limited_by_budget", from_depth=original, to_depth=target)
        else:
            investigators.depth = chosen
            if chosen != target:
                investigators.budget_reduced_from = target
                investigators.reason = (
                    f"Limited by budget: escalated to {chosen}, not {target}; {ceiling_note}. {decision.reason}"
                )
            else:
                investigators.reason = decision.reason
            investigators.escalation = _entry_wire(
                decision,
                "escalated" if chosen == target else "limited_by_budget",
                from_depth=original,
                to_depth=chosen,
            )
    running = investigators is not None and investigators.status == "run"
    hypotheses = drafts.get(HYPOTHESES)
    if (
        running
        and hypotheses is not None
        and hypotheses.status == "skipped"
        and hypotheses.escalate
        and hypotheses.reason == AUTO_GATED_REASON
    ):
        extra = hypotheses.catalog.model_calls(hypotheses.depth)
        if hypotheses.origin not in {"policy_floor", "manual"}:
            hypotheses.origin = "escalation"
        touched.append(HYPOTHESES)
        if ceiling is None or spent + extra <= ceiling:
            spent += extra
            hypotheses.status, hypotheses.reason = "run", decision.reason
            hypotheses.escalation = _entry_wire(decision, "escalated")
        else:
            hypotheses.status = "limited_by_budget"
            hypotheses.reason = f"Limited by budget: escalation does not fit; {ceiling_note}"
            hypotheses.escalation = _entry_wire(decision, "limited_by_budget")
    if not touched:
        return replace(
            decision,
            state="not_applicable",
            reason=f"nothing to raise: the {base} rung already runs at or above {target}, or runs no AI review; "
            + decision.reason,
        )
    return replace(decision, entries=tuple(touched))


def finalize_escalation(drafts: Mapping[str, Any], decision: EscalationDecision | None) -> EscalationDecision | None:
    """After the budget: an escalated entry the budget reduced or limited is shown so."""

    if decision is None or decision.state != "escalated":
        return decision
    limited = False
    for entry_id in decision.entries:
        draft = drafts.get(entry_id)
        if draft is None or draft.escalation is None:
            continue
        if draft.status == "limited_by_budget" or draft.budget_reduced_from:
            limited = True
            draft.escalation = {**draft.escalation, "state": "limited_by_budget"}
    if not limited:
        return decision
    return replace(decision, state="limited_by_budget", reason="Limited by budget: " + decision.reason)


__all__ = [
    "AUTO_GATED_REASON",
    "CHAIN_SEVERITIES",
    "CRITICALITY_LEVELS",
    "ESCALATED_ENTRIES",
    "ESCALATION_DEFAULTS",
    "ESCALATION_TARGETS",
    "MAX_INVENTORY_PATHS",
    "TRIGGER_CLASSES",
    "EscalationDecision",
    "EscalationFacts",
    "EscalationPolicy",
    "EscalationTrigger",
    "apply_escalation",
    "collect_escalation_facts",
    "decide_escalation",
    "escalation_document",
    "escalation_triggers",
    "finalize_escalation",
    "parse_escalation",
]

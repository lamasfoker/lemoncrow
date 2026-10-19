"""Runtime budgets: how much retrieval/latency/token spend one call may use.

Three named depths cover the workloads in the runtime plan:

* ``fast``   — lookup/autocomplete: one retrieval round, no expansion.
* ``normal`` — bug localization / understanding: bounded adaptive expansion.
* ``deep``   — audit / refactor / long-horizon: more rounds and a larger budget.

Agents normally do not choose a depth; :func:`infer_budget` derives one from
the tool and its arguments.  An explicit ``budget`` argument (``"fast"`` etc.)
or explicit numeric fields always win, and every field is clamped.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

BudgetDepth = Literal["fast", "normal", "deep"]


@dataclass(frozen=True, slots=True)
class RuntimeBudget:
    depth: BudgetDepth = "normal"
    max_rounds: int = 1
    max_expansion_tokens: int = 800
    latency_target_ms: int = 1000
    max_prompt_tokens: int | None = None
    max_cost_usd: float | None = None
    inferred: bool = True
    reason: str = "default"

    def to_payload(self) -> dict[str, Any]:
        return {
            "depth": self.depth,
            "max_rounds": self.max_rounds,
            "max_expansion_tokens": self.max_expansion_tokens,
            "latency_target_ms": self.latency_target_ms,
            "max_prompt_tokens": self.max_prompt_tokens,
            "max_cost_usd": self.max_cost_usd,
            "inferred": self.inferred,
            "reason": self.reason,
        }


PROFILES: dict[BudgetDepth, RuntimeBudget] = {
    "fast": RuntimeBudget(depth="fast", max_rounds=0, max_expansion_tokens=0, latency_target_ms=200),
    "normal": RuntimeBudget(depth="normal", max_rounds=1, max_expansion_tokens=800, latency_target_ms=1000),
    "deep": RuntimeBudget(depth="deep", max_rounds=3, max_expansion_tokens=4000, latency_target_ms=5000),
}

_DEEP_TERMS = re.compile(
    r"\b(audit|security|vulnerab\w*|refactor|migrat\w*|all (callers|usages|references)|every|across|"
    r"architecture|end[- ]to[- ]end|whole|entire)\b",
    re.IGNORECASE,
)
_IDENTIFIER = re.compile(r"^[A-Za-z_][\w.:/-]*$")


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def budget_for(depth: str | None) -> RuntimeBudget:
    key = (depth or "").strip().lower()
    if key == "fast":
        return PROFILES["fast"]
    if key == "deep":
        return PROFILES["deep"]
    return PROFILES["normal"]


def infer_budget(tool: str, arguments: Mapping[str, Any] | None = None) -> RuntimeBudget:
    """Choose a budget for one call. Explicit caller intent always wins."""
    args = arguments or {}
    explicit = args.get("budget")
    if isinstance(explicit, str) and explicit.strip().lower() in PROFILES:
        return replace(budget_for(explicit), inferred=False, reason="explicit")
    if isinstance(explicit, Mapping):
        base = budget_for(str(explicit.get("depth") or "normal"))
        return replace(
            base,
            max_rounds=_clamp(explicit.get("max_rounds"), 0, 5, base.max_rounds),
            max_expansion_tokens=_clamp(explicit.get("max_expansion_tokens"), 0, 16000, base.max_expansion_tokens),
            latency_target_ms=_clamp(explicit.get("latency_target_ms"), 0, 30000, base.latency_target_ms),
            inferred=False,
            reason="explicit",
        )

    query = str(args.get("query") or args.get("q") or "").strip()
    if tool in {"read", "edit", "bash", "grep", "blame"}:
        return replace(PROFILES["fast"], reason="non_retrieval_tool")
    if not query:
        return replace(PROFILES["normal"], reason="no_query")
    words = query.split()
    if _DEEP_TERMS.search(query):
        return replace(PROFILES["deep"], reason="deep_intent_terms")
    if len(words) == 1 and _IDENTIFIER.match(query):
        return replace(PROFILES["fast"], reason="single_identifier")
    if len(words) >= 12:
        return replace(PROFILES["deep"], reason="long_question")
    return replace(PROFILES["normal"], reason="natural_language_query")


__all__ = ["PROFILES", "BudgetDepth", "RuntimeBudget", "budget_for", "infer_budget"]

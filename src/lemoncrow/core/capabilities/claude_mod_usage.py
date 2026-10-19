"""Read exact Claude Code Mod usage measurements.

The Claude Mod is measurement-only: it never changes prompts, tools, models, or
reasoning effort. ``turn.step`` gives exact request token/cache/model usage and
``session.usage`` gives Claude Code's cumulative session accounting.

Actual usage can be measured. The counterfactual "what would this have cost
without LemonCrow?" remains modeled unless a paired baseline run exists.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ClaudeModBucket:
    kind: str
    agent_id: str | None
    steps: int
    turns: int
    duration_ms: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    models: dict[str, int] = field(default_factory=dict)
    model_usage: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass(frozen=True)
class ClaudeModUsage:
    session_id: str
    started_at_ms: int
    updated_at_ms: int
    main: ClaudeModBucket
    subagents: dict[str, ClaudeModBucket]
    total_steps: int
    total_turns: int
    total_duration_ms: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    models: dict[str, int]
    session_usage: dict[str, Any]
    measured_cost_usd: float | None

    @property
    def subagent_turns(self) -> int:
        return sum(bucket.turns for bucket in self.subagents.values())

    @property
    def subagent_steps(self) -> int:
        return sum(bucket.steps for bucket in self.subagents.values())

    @property
    def subagent_tokens(self) -> int:
        return sum(bucket.total_tokens for bucket in self.subagents.values())

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens


def measurement_path(root: Path, session_id: str) -> Path:
    return root / f"claude-mod-usage-{session_id}.json"


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _numeric_usd(value: Any) -> float | None:
    """Best-effort extraction across Claude Code's evolving cost shape."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return max(0.0, float(value))
    if isinstance(value, str):
        try:
            return max(0.0, float(value.strip().replace("$", "").replace(",", "")))
        except ValueError:
            return None
    if not isinstance(value, Mapping):
        return None
    for key in (
        "total_cost_usd",
        "totalCostUsd",
        "cost_usd",
        "costUsd",
        "total_usd",
        "totalUsd",
        "usd",
        "total",
        "amount_usd",
        "amountUsd",
    ):
        if key in value:
            parsed = _numeric_usd(value[key])
            if parsed is not None:
                return parsed
    return None


def session_usage_cost_usd(session_usage: Mapping[str, Any] | dict[str, Any]) -> float | None:
    """Extract Claude Code's cumulative host-reported cost from session.usage()."""
    return _numeric_usd(session_usage.get("cost"))


def _bucket(raw: Any, *, default_kind: str, default_agent_id: str | None = None) -> ClaudeModBucket:
    data = raw if isinstance(raw, Mapping) else {}
    raw_models = data.get("models")
    models = {str(k): _nonnegative_int(v) for k, v in raw_models.items()} if isinstance(raw_models, Mapping) else {}
    raw_model_usage = data.get("model_usage")
    model_usage: dict[str, dict[str, int]] = {}
    if isinstance(raw_model_usage, Mapping):
        for model, usage in raw_model_usage.items():
            if not isinstance(usage, Mapping):
                continue
            model_usage[str(model)] = {
                "steps": _nonnegative_int(usage.get("steps")),
                "input_tokens": _nonnegative_int(usage.get("input_tokens")),
                "output_tokens": _nonnegative_int(usage.get("output_tokens")),
                "cache_read_tokens": _nonnegative_int(
                    usage.get("cache_read_tokens", usage.get("cache_read_input_tokens"))
                ),
                "cache_write_tokens": _nonnegative_int(
                    usage.get("cache_write_tokens", usage.get("cache_creation_input_tokens"))
                ),
            }

    agent_raw = data.get("agent_id", default_agent_id)
    agent_id = str(agent_raw).strip() if agent_raw else None
    return ClaudeModBucket(
        kind=str(data.get("kind") or default_kind),
        agent_id=agent_id,
        steps=_nonnegative_int(data.get("steps")),
        turns=_nonnegative_int(data.get("turns")),
        duration_ms=_nonnegative_int(data.get("duration_ms")),
        input_tokens=_nonnegative_int(data.get("input_tokens")),
        output_tokens=_nonnegative_int(data.get("output_tokens")),
        cache_read_tokens=_nonnegative_int(data.get("cache_read_tokens", data.get("cache_read_input_tokens"))),
        cache_write_tokens=_nonnegative_int(data.get("cache_write_tokens", data.get("cache_creation_input_tokens"))),
        models=models,
        model_usage=model_usage,
    )


def load_claude_mod_usage(root: Path, session_id: str) -> ClaudeModUsage | None:
    """Load one measurement snapshot, returning None on absence/corruption."""
    if not session_id:
        return None
    path = measurement_path(root, session_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, Mapping):
        return None
    if int(raw.get("schema_version") or 0) != 1 or raw.get("source") != "claude_mod":
        return None
    if str(raw.get("session_id") or "") != session_id:
        return None

    totals = raw.get("totals")
    totals = totals if isinstance(totals, Mapping) else {}
    main = _bucket(raw.get("main"), default_kind="main")
    subagents: dict[str, ClaudeModBucket] = {}
    raw_subagents = raw.get("subagents")
    if isinstance(raw_subagents, Mapping):
        for agent_id, value in raw_subagents.items():
            key = str(agent_id)
            subagents[key] = _bucket(value, default_kind="subagent", default_agent_id=key)

    raw_models = raw.get("models")
    models = {str(k): _nonnegative_int(v) for k, v in raw_models.items()} if isinstance(raw_models, Mapping) else {}
    session_usage = raw.get("session_usage")
    session_usage = dict(session_usage) if isinstance(session_usage, Mapping) else {}

    return ClaudeModUsage(
        session_id=session_id,
        started_at_ms=_nonnegative_int(raw.get("started_at_ms")),
        updated_at_ms=_nonnegative_int(raw.get("updated_at_ms")),
        main=main,
        subagents=subagents,
        total_steps=_nonnegative_int(totals.get("steps")),
        total_turns=_nonnegative_int(totals.get("turns")),
        total_duration_ms=_nonnegative_int(totals.get("duration_ms")),
        input_tokens=_nonnegative_int(totals.get("input_tokens")),
        output_tokens=_nonnegative_int(totals.get("output_tokens")),
        cache_read_tokens=_nonnegative_int(totals.get("cache_read_input_tokens", totals.get("cache_read_tokens"))),
        cache_write_tokens=_nonnegative_int(
            totals.get("cache_creation_input_tokens", totals.get("cache_write_tokens"))
        ),
        models=models,
        session_usage=session_usage,
        measured_cost_usd=session_usage_cost_usd(session_usage),
    )


def bucket_cost_breakdown_usd(bucket: ClaudeModBucket) -> dict[str, float]:
    """Price exact per-model tokens for parent/subagent attribution."""
    from lemoncrow.core.capabilities.pricing import usage_cost_breakdown_usd
    from lemoncrow.core.capabilities.savings_summary import resolve_model_id

    result = {"input": 0.0, "cache_read": 0.0, "cache_write": 0.0, "output": 0.0}
    for model, usage in bucket.model_usage.items():
        part = usage_cost_breakdown_usd(
            resolve_model_id(model),
            input_tokens=_nonnegative_int(usage.get("input_tokens")),
            output_tokens=_nonnegative_int(usage.get("output_tokens")),
            cache_read_tokens=_nonnegative_int(usage.get("cache_read_tokens")),
            cache_write_tokens=_nonnegative_int(usage.get("cache_write_tokens")),
        )
        for key in result:
            result[key] += float(part.get(key) or 0.0)
        result["output"] += float(part.get("thinking") or 0.0)
    return {key: round(value, 6) for key, value in result.items()}


def bucket_cost_usd(bucket: ClaudeModBucket) -> float:
    return round(sum(bucket_cost_breakdown_usd(bucket).values()), 6)


__all__ = [
    "ClaudeModBucket",
    "ClaudeModUsage",
    "bucket_cost_breakdown_usd",
    "bucket_cost_usd",
    "load_claude_mod_usage",
    "measurement_path",
    "session_usage_cost_usd",
]

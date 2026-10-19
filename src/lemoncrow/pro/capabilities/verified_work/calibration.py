"""Paired shadow calibration for Verified Local Work savings.

Learning/shadow mode naturally supplies the control arm: LemonCrow attempts a
local repair in isolation while the host still sees the original verifier
failure and repairs it normally. Claude Mods measure the host usage between that
failure and the same verifier command later passing. This module pairs the two
observations without storing source text or prompts.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import statistics
import tempfile
import threading
import time
from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.verified_work.models import RepairKind, VerifiedWorkOpportunity
from lemoncrow.core.foundation.paths import default_store_root
from lemoncrow.pro.capabilities.model_setup.transport import active_store_root

_SCHEMA_VERSION = 1
_FILE_NAME = "verified_work_calibration.json"
_MAX_LOCAL = 2000
_MAX_BASELINE = 2000
_MAX_RECENT_PAIRS_PER_CLASS = 100
_LOCK = threading.Lock()


def _root(root: Path | None = None) -> Path:
    return Path(root or active_store_root() or default_store_root()).expanduser()


def calibration_path(root: Path | None = None) -> Path:
    return _root(root) / _FILE_NAME


def command_digest(command: str) -> str:
    normalized = " ".join(command.strip().split())
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _LOCK:
        handle = lock_path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                api: Any = msvcrt
                api.locking(handle.fileno(), api.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    api = msvcrt
                    api.locking(handle.fileno(), api.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": _SCHEMA_VERSION, "local": [], "baseline": []}
    if not isinstance(raw, dict):
        return {"schema_version": _SCHEMA_VERSION, "local": [], "baseline": []}
    return {
        "schema_version": _SCHEMA_VERSION,
        "local": raw.get("local") if isinstance(raw.get("local"), list) else [],
        "baseline": raw.get("baseline") if isinstance(raw.get("baseline"), list) else [],
    }


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _append(root: Path | None, section: str, row: dict[str, Any], *, limit: int) -> None:
    path = calibration_path(root)
    with _locked(path):
        raw = _read(path)
        rows = raw[section]
        assert isinstance(rows, list)
        rows.append(row)
        if len(rows) > limit:
            del rows[: len(rows) - limit]
        _write(path, raw)


def record_local_observation(
    opportunity: VerifiedWorkOpportunity,
    *,
    model: str,
    verified: bool,
    promoted: bool,
    latency_ms: int,
    input_tokens: int,
    output_tokens: int,
    provider_cost_usd: float | None,
    mode: str,
    root: Path | None = None,
) -> None:
    _append(
        root,
        "local",
        {
            "at_ms": int(time.time() * 1000),
            "session_id": opportunity.session_id,
            "command_digest": command_digest(opportunity.command),
            "diagnostic_digest": opportunity.diagnostic_digest,
            "kind": opportunity.kind,
            "model": model,
            "verified": bool(verified),
            "promoted": bool(promoted),
            "mode": mode,
            "latency_ms": max(0, int(latency_ms)),
            "input_tokens": max(0, int(input_tokens)),
            "output_tokens": max(0, int(output_tokens)),
            "provider_cost_usd": max(0.0, float(provider_cost_usd or 0.0)),
        },
        limit=_MAX_LOCAL,
    )


def _num(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _float(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _usage_cost(raw: Any) -> float | None:
    from lemoncrow.core.capabilities.claude_mod_usage import session_usage_cost_usd

    return session_usage_cost_usd(raw if isinstance(raw, Mapping) else {})


def _model_usage_delta(start: Any, end: Any) -> dict[str, dict[str, int]]:
    start_map = _mapping(start)
    end_map = _mapping(end)
    result: dict[str, dict[str, int]] = {}
    keys = set(str(key) for key in start_map) | set(str(key) for key in end_map)
    for model in keys:
        before = _mapping(start_map.get(model))
        after = _mapping(end_map.get(model))
        row = {
            "input_tokens": max(0, _num(after.get("input_tokens")) - _num(before.get("input_tokens"))),
            "output_tokens": max(0, _num(after.get("output_tokens")) - _num(before.get("output_tokens"))),
            "cache_read_tokens": max(
                0,
                _num(after.get("cache_read_input_tokens", after.get("cache_read_tokens")))
                - _num(before.get("cache_read_input_tokens", before.get("cache_read_tokens"))),
            ),
            "cache_write_tokens": max(
                0,
                _num(after.get("cache_creation_input_tokens", after.get("cache_write_tokens")))
                - _num(before.get("cache_creation_input_tokens", before.get("cache_write_tokens"))),
            ),
        }
        if any(row.values()):
            result[model] = row
    return result


def _price_model_usage(model_usage: Mapping[str, Mapping[str, int]]) -> float:
    from lemoncrow.core.capabilities.pricing import usage_cost_breakdown_usd
    from lemoncrow.core.capabilities.savings_summary import resolve_model_id

    total = 0.0
    for model, usage in model_usage.items():
        part = usage_cost_breakdown_usd(
            resolve_model_id(model),
            input_tokens=_num(usage.get("input_tokens")),
            output_tokens=_num(usage.get("output_tokens")),
            cache_read_tokens=_num(usage.get("cache_read_tokens")),
            cache_write_tokens=_num(usage.get("cache_write_tokens")),
        )
        total += sum(float(value or 0.0) for value in part.values())
    return round(total, 6)


def record_host_baseline(payload: Mapping[str, Any], *, root: Path | None = None) -> dict[str, Any]:
    """Persist one measured host repair interval emitted by the Claude Mod."""
    command = str(payload.get("command") or "")
    start = _mapping(payload.get("start"))
    end = _mapping(payload.get("end"))
    start_total_steps = _num(start.get("total_steps"))
    end_total_steps = _num(end.get("total_steps"))
    start_agent_steps = _num(start.get("agent_steps"))
    end_agent_steps = _num(end.get("agent_steps"))
    total_steps = max(0, end_total_steps - start_total_steps)
    agent_steps = max(0, end_agent_steps - start_agent_steps)
    total_turns = max(0, _num(end.get("total_turns")) - _num(start.get("total_turns")))
    agent_turns = max(0, _num(end.get("agent_turns")) - _num(start.get("agent_turns")))
    model_usage = _model_usage_delta(start.get("model_usage"), end.get("model_usage"))

    start_cost = _usage_cost(start.get("session_usage"))
    end_cost = _usage_cost(end.get("session_usage"))
    measured_cost = (
        max(0.0, end_cost - start_cost)
        if start_cost is not None and end_cost is not None and end_cost >= start_cost
        else None
    )
    priced_cost = _price_model_usage(model_usage)
    clean = total_steps > 0 and total_steps == agent_steps
    row = {
        "at_ms": _num(payload.get("ended_at_ms")) or int(time.time() * 1000),
        "started_at_ms": _num(payload.get("started_at_ms")),
        "session_id": str(payload.get("session_id") or ""),
        "host": str(payload.get("host") or "claude"),
        "agent_id": str(payload.get("agent_id") or "") or None,
        "command_digest": command_digest(command),
        "clean": clean,
        "host_steps": agent_steps,
        "host_turns": agent_turns or total_turns,
        "total_steps": total_steps,
        "model_usage": model_usage,
        "measured_cost_usd": round(measured_cost, 6) if measured_cost is not None else None,
        "priced_cost_usd": priced_cost,
        "cost_source": "claude_session_usage" if measured_cost is not None else "priced_measured_tokens",
    }
    _append(root, "baseline", row, limit=_MAX_BASELINE)
    return row


@dataclass(frozen=True, slots=True)
class PairedCalibration:
    model: str
    kind: RepairKind
    pairs: int
    median_host_cost_usd: float
    median_local_cost_usd: float
    median_net_savings_usd: float
    median_host_turns: float
    median_host_steps: float
    qualified: bool


def _pair_records(raw: Mapping[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    local_rows = (
        [row for row in raw.get("local", []) if isinstance(row, dict)] if isinstance(raw.get("local"), list) else []
    )
    baseline_rows = (
        [row for row in raw.get("baseline", []) if isinstance(row, dict)]
        if isinstance(raw.get("baseline"), list)
        else []
    )
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in local_rows:
        if row.get("mode") != "shadow" or not row.get("verified"):
            continue
        key = (str(row.get("session_id") or ""), str(row.get("command_digest") or ""))
        by_key[key].append(row)
    for rows in by_key.values():
        rows.sort(key=lambda row: _num(row.get("at_ms")))

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    used: set[int] = set()
    for baseline in sorted(baseline_rows, key=lambda row: _num(row.get("at_ms"))):
        if not baseline.get("clean"):
            continue
        key = (str(baseline.get("session_id") or ""), str(baseline.get("command_digest") or ""))
        candidates = by_key.get(key, [])
        if not candidates:
            continue
        start = _num(baseline.get("started_at_ms"))
        end = _num(baseline.get("at_ms"))
        best_index = -1
        best_distance = 10**18
        for index, local in enumerate(candidates):
            marker = id(local)
            if marker in used:
                continue
            at = _num(local.get("at_ms"))
            # Local shadow work can finish before or shortly after the host does.
            if at < max(0, start - 60_000) or at > end + 180_000:
                continue
            distance = min(abs(at - start), abs(at - end))
            if distance < best_distance:
                best_distance = distance
                best_index = index
        if best_index >= 0:
            local = candidates[best_index]
            used.add(id(local))
            pairs.append((local, baseline))
    return pairs


def summarize_calibration(*, root: Path | None = None, min_pairs: int = 10) -> dict[str, Any]:
    raw = _read(calibration_path(root))
    pairs = _pair_records(raw)
    grouped: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for local, baseline in pairs:
        grouped[(str(local.get("model") or ""), str(local.get("kind") or ""))].append((local, baseline))

    rows: list[dict[str, Any]] = []
    calibrated_total = 0.0
    qualified_promotions = 0
    all_local = (
        [row for row in raw.get("local", []) if isinstance(row, dict)] if isinstance(raw.get("local"), list) else []
    )
    for (model, kind), group in sorted(grouped.items()):
        # Qualification is deliberately recent, not lifetime-weighted. A host
        # model/price change or a local model regression must be able to demote
        # Auto even after a long history of profitable repairs. Keep at least
        # 2x the qualification floor for stability, bounded to the persisted
        # recent evidence budget.
        recent_limit = min(_MAX_RECENT_PAIRS_PER_CLASS, max(min_pairs * 2, min_pairs))
        group = sorted(group, key=lambda pair: _num(pair[1].get("at_ms")))[-recent_limit:]
        host_costs = [
            _float(
                base.get("measured_cost_usd")
                if base.get("measured_cost_usd") is not None
                else base.get("priced_cost_usd")
            )
            for _, base in group
        ]
        local_costs = [_float(local.get("provider_cost_usd")) for local, _ in group]
        # Net means net: expensive local inference is allowed to make a sample
        # negative. Clamping losses to zero would systematically overstate
        # savings and could keep an uneconomic class qualified.
        nets = [host - local for host, local in zip(host_costs, local_costs, strict=True)]
        host_turns = [_float(base.get("host_turns")) for _, base in group]
        host_steps = [_float(base.get("host_steps")) for _, base in group]
        median_net = statistics.median(nets) if nets else 0.0
        qualified = len(group) >= min_pairs and median_net > 0.0
        median_host = statistics.median(host_costs) if host_costs else 0.0
        promoted_rows = [
            row
            for row in all_local
            if row.get("model") == model
            and row.get("kind") == kind
            and row.get("mode") == "enforce"
            and row.get("promoted")
        ]
        promoted = len(promoted_rows)
        calibrated_for_promotions = (
            sum(median_host - _float(row.get("provider_cost_usd")) for row in promoted_rows) if qualified else 0.0
        )
        if qualified:
            calibrated_total += calibrated_for_promotions
            qualified_promotions += promoted
        rows.append(
            {
                "model": model,
                "kind": kind,
                "pairs": len(group),
                "recent_window": recent_limit,
                "median_host_cost_usd": round(median_host, 6),
                "median_local_cost_usd": round(statistics.median(local_costs), 6) if local_costs else 0.0,
                "median_net_savings_usd": round(median_net, 6),
                "median_host_turns": round(statistics.median(host_turns), 2) if host_turns else 0.0,
                "median_host_steps": round(statistics.median(host_steps), 2) if host_steps else 0.0,
                "qualified": qualified,
                "promoted_repairs": promoted,
                "calibrated_savings_usd": round(calibrated_for_promotions, 6) if qualified else None,
            }
        )
    clean_baselines = sum(1 for row in raw.get("baseline", []) if isinstance(row, dict) and row.get("clean"))
    return {
        "schema_version": _SCHEMA_VERSION,
        "source": "paired_shadow_host_usage",
        "pairs": len(pairs),
        "clean_baselines": clean_baselines,
        "min_pairs": min_pairs,
        "by_model_kind": rows,
        "qualified_promotions": qualified_promotions,
        "calibrated_net_savings_usd": round(calibrated_total, 6) if any(row["qualified"] for row in rows) else None,
        "status": "qualified" if any(row["qualified"] for row in rows) else "calibrating",
    }


def session_calibrated_savings(
    session_id: str,
    *,
    root: Path | None = None,
    min_pairs: int = 20,
) -> dict[str, Any]:
    """Calibrated Local Savings attributable to one host session.

    Only promoted enforce rows whose model+kind economics are currently
    qualified contribute dollar savings. Explicit/manual promotions outside a
    qualified class remain visible in the counts but never receive a dollar
    claim. The returned value is therefore safe to show beside, not inside, the
    older modelled savings total.
    """
    sid = str(session_id or "").strip()
    if not sid:
        return {
            "promoted_repairs": 0,
            "qualified_repairs": 0,
            "unqualified_repairs": 0,
            "local_cost_usd": 0.0,
            "calibrated_net_savings_usd": None,
            "provenance": "paired_shadow_host_usage",
        }

    raw = _read(calibration_path(root))
    summary = summarize_calibration(root=root, min_pairs=min_pairs)
    qualified: dict[tuple[str, str], float] = {}
    for row in summary.get("by_model_kind", []):
        if not isinstance(row, dict) or not row.get("qualified"):
            continue
        qualified[(str(row.get("model") or ""), str(row.get("kind") or ""))] = _float(row.get("median_host_cost_usd"))

    promoted_rows = (
        [
            row
            for row in raw.get("local", [])
            if isinstance(row, dict)
            and str(row.get("session_id") or "") == sid
            and row.get("mode") == "enforce"
            and bool(row.get("promoted"))
        ]
        if isinstance(raw.get("local"), list)
        else []
    )

    local_cost = sum(_float(row.get("provider_cost_usd")) for row in promoted_rows)
    calibrated = 0.0
    qualified_count = 0
    for row in promoted_rows:
        key = (str(row.get("model") or ""), str(row.get("kind") or ""))
        host_cost = qualified.get(key)
        if host_cost is None:
            continue
        qualified_count += 1
        calibrated += host_cost - _float(row.get("provider_cost_usd"))

    promoted = len(promoted_rows)
    return {
        "promoted_repairs": promoted,
        "qualified_repairs": qualified_count,
        "unqualified_repairs": promoted - qualified_count,
        "local_cost_usd": round(local_cost, 6),
        "calibrated_net_savings_usd": round(calibrated, 6) if qualified_count else None,
        "provenance": "paired_shadow_host_usage",
    }


def calibrated_model_kind(
    model: str,
    kind: RepairKind,
    *,
    root: Path | None = None,
    min_pairs: int = 20,
) -> dict[str, Any] | None:
    """Return one paired-economics row, or None when it has never been observed."""
    summary = summarize_calibration(root=root, min_pairs=min_pairs)
    for row in summary.get("by_model_kind", []):
        if isinstance(row, dict) and row.get("model") == model and row.get("kind") == kind:
            return row
    return None


__all__ = [
    "calibrated_model_kind",
    "calibration_path",
    "command_digest",
    "record_host_baseline",
    "record_local_observation",
    "session_calibrated_savings",
    "summarize_calibration",
]

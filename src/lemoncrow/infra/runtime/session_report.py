"""Per-session cost and savings report.

Pure-computation module: reads a serialised run snapshot (the dict produced
by ``RunLedger.snapshot()``) plus optional flat-file companions and returns a
``SessionReport`` dataclass ready for rendering.

Usage::

    # from a live ledger
    from lemoncrow.infra.runtime.session_report import build_report_from_ledger, render_text
    report = build_report_from_ledger(ledger, root)
    print(render_text(report))

    # from a persisted run file
    from lemoncrow.infra.runtime.session_report import load_report, render_text
    report = load_report(session_id, root)
    print(render_text(report))
"""

from __future__ import annotations

import dataclasses
import json
import stat
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lemoncrow.core.capabilities.savings_summary import _fmt_tok, _fmt_usd, read_session_end_carry

if TYPE_CHECKING:
    from lemoncrow.infra.runtime.run_ledger import RunLedger


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

_VENDOR_PREFIXES: dict[str, str] = {
    "claude": "Anthropic",
    "gpt": "OpenAI",
    "o1": "OpenAI",
    "o3": "OpenAI",
    "o4": "OpenAI",
    "gemini": "Google",
    "mistral": "Mistral",
}


def _model_vendor(model: str) -> str:
    lower = model.lower()
    for prefix, vendor in _VENDOR_PREFIXES.items():
        if lower.startswith(prefix):
            return vendor
    return "Unknown"


def _derive_vendor(models: dict[str, int]) -> str:
    vendors = {_model_vendor(m) for m in models}
    vendors.discard("Unknown")
    if not vendors:
        return "Unknown"
    if len(vendors) == 1:
        return next(iter(vendors))
    return "mixed"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
    except (ValueError, TypeError):
        return None


def _live_savings_path(root: Path) -> Path:
    return root / "live_savings_events.jsonl"


@lru_cache(maxsize=4)
def _live_savings_index(path_str: str, _mtime_ns: int, _size: int) -> dict[str, tuple[int, float, float]]:
    """Index ``live_savings_events.jsonl`` by session in ONE parse.

    Maps ``session_id -> (compact_event_count, compact_saved_usd,
    total_saved_usd)``. Keyed on the file's ``(mtime_ns, size)`` so appends
    invalidate the cache. Profiling showed the previous per-session
    full-file scans (build_report → _read_compact_savings re-parsing every
    line once PER SESSION) costing 25M+ json.loads per /v1/insights call.
    """
    try:
        text = Path(path_str).read_text(encoding="utf-8")
    except OSError:
        return {}
    index: dict[str, list[float]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = ev.get("session_id")
        if not sid:
            continue
        saved = float(ev.get("cost_saved_usd") or 0.0)
        bucket = index.setdefault(str(sid), [0, 0.0, 0.0])
        bucket[2] += saved
        if ev.get("lever") == "session_compaction":
            bucket[0] += 1
            bucket[1] += saved
    return {sid: (int(b[0]), b[1], b[2]) for sid, b in index.items()}


def _live_savings_for_session(session_id: str, root: Path) -> tuple[int, float, float]:
    """(compact_count, compact_saved_usd, total_saved_usd) for one session."""
    path = _live_savings_path(root)
    try:
        st = path.stat()
    except OSError:
        return 0, 0.0, 0.0
    return _live_savings_index(str(path), st.st_mtime_ns, st.st_size).get(session_id, (0, 0.0, 0.0))


# --------------------------------------------------------------------------- #
# Per-token cost estimation                                                    #
# --------------------------------------------------------------------------- #


def _cost_breakdown_from_calls(
    calls: list[dict[str, Any]],
) -> tuple[int, float, int, float, int, float, int, float]:
    """Return (in_tok, in_cost, out_tok, out_cost, cr_tok, cr_cost, cw_tok, cw_cost)."""
    from lemoncrow.core.capabilities.pricing import get_model_pricing

    total_in_tok = total_out_tok = total_cr_tok = total_cw_tok = 0
    total_in_cost = total_out_cost = total_cr_cost = total_cw_cost = 0.0

    for call in calls:
        model = str(call.get("model") or "claude-haiku-4-5")
        pricing = get_model_pricing(model)
        in_tok = int(call.get("input_tokens") or 0)
        out_tok = int(call.get("output_tokens") or 0)
        cr_tok = int(call.get("cache_read_tokens") or 0)
        cw_tok = int(call.get("cache_write_tokens") or 0)

        total_in_tok += in_tok
        total_out_tok += out_tok
        total_cr_tok += cr_tok
        total_cw_tok += cw_tok

        total_in_cost += pricing.cost_usd(input_tokens=in_tok)
        total_out_cost += pricing.cost_usd(output_tokens=out_tok)
        total_cr_cost += pricing.cost_usd(cache_read_tokens=cr_tok)
        if cw_tok:
            total_cw_cost += pricing.cost_usd(cache_write_tokens=cw_tok)

    return (
        total_in_tok,
        round(total_in_cost, 6),
        total_out_tok,
        round(total_out_cost, 6),
        total_cr_tok,
        round(total_cr_cost, 6),
        total_cw_tok,
        round(total_cw_cost, 6),
    )


# --------------------------------------------------------------------------- #
# Compact savings                                                              #
# --------------------------------------------------------------------------- #


def _read_compact_savings(session_id: str, root: Path) -> tuple[int, float]:
    """Read compact savings from ``live_savings_events.jsonl`` for *session_id*.

    Returns ``(event_count, total_cost_saved_usd)``.
    """
    count, compact_saved, _ = _live_savings_for_session(session_id, root)
    return count, round(compact_saved, 6)


def _read_context_compression_savings(session_id: str, root: Path) -> tuple[int, float, list[dict[str, Any]]]:
    """Read per-tool savings for *session_id* from the host sidecar.

    Source: ``sessions/<session_id>/savings.jsonl`` — written keyed by the
    host session id (Claude UUID, Codex id, etc.). Rows that predate the
    pre-priced ``cost_saved_usd`` field are priced here at the model captured
    at write time. This is what the statusline reads, so the frontend's
    session-detail ``total_lemoncrow_savings_usd`` matches the live figure.

    Returns ``(call_count, total_cost_saved_usd, rows)`` where rows are the
    synthesised context_savings shape
    (tool, tokens_saved, calls_saved, model, cost_saved_usd, at).
    """
    return _read_host_sidecar_savings(session_id, root)


def _read_host_sidecar_savings(session_id: str, root: Path) -> tuple[int, float, list[dict[str, Any]]]:
    """Aggregate savings from ``sessions/<session_id>/savings.jsonl``.

    Each sidecar row is priced through
    :func:`savings_summary._price_savings_row` — the single rule the statusline,
    stop hook, ``lc runtime savings`` CLI, dashboard, and web Savings page all use
    — so this session-detail total reconciles exactly with the live figure.
    Synthesises context_savings-shaped rows so downstream renderers can treat
    both sources uniformly.
    """
    from lemoncrow.core.capabilities.savings_summary import _price_savings_row
    from lemoncrow.core.foundation.paths import find_session_dir, flat_session_dir

    session_root = find_session_dir(root, session_id) or flat_session_dir(root, session_id)
    if session_root is None:
        return 0, 0.0, []
    sidecar = session_root / "savings.jsonl"
    if not sidecar.is_file():
        return 0, 0.0, []

    count = 0
    total_saved = 0.0
    rows: list[dict[str, Any]] = []
    try:
        for line in sidecar.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            priced_tokens, usd, calls, calls_usd, unpriced = _price_savings_row(ev)
            tokens = priced_tokens + unpriced
            cost = usd + calls_usd
            if tokens <= 0 and calls <= 0 and cost <= 0.0:
                continue
            count += 1
            total_saved += cost
            rows.append(
                {
                    "at": ev.get("ts"),
                    "tool": ev.get("tool"),
                    "model": str(ev.get("model") or "").strip(),
                    "tokens_saved": tokens,
                    "calls_saved": calls,
                    "cost_saved_usd": round(cost, 6),
                    "rid": ev.get("rid"),
                }
            )
    except OSError:
        return 0, 0.0, []
    return count, round(total_saved, 6), rows


# The dashboard totals every session's sidecar per load. The file is
# append-only and its rows are priced at read time, so a total is reused while
# the file state and the pricing table are both unchanged.
_SIDECAR_TOTAL_MEMO_MAX = 4096
_sidecar_total_memo: OrderedDict[tuple[str, int, int, int], tuple[object, float]] = OrderedDict()
_sidecar_total_lock = threading.Lock()


def _host_sidecar_total(session_id: str, root: Path) -> float:
    """``_read_host_sidecar_savings(session_id, root)[1]``, memoized per file state."""
    from lemoncrow.core.capabilities.pricing import _load_pricing_table
    from lemoncrow.core.foundation.paths import find_session_dir, flat_session_dir

    session_root = find_session_dir(root, session_id) or flat_session_dir(root, session_id)
    if session_root is None:
        return 0.0
    try:
        st = (session_root / "savings.jsonl").stat()
    except OSError:
        return 0.0
    if not stat.S_ISREG(st.st_mode):
        return 0.0
    key = (str(session_root / "savings.jsonl"), st.st_ino, st.st_mtime_ns, st.st_size)
    pricing_table = _load_pricing_table()
    with _sidecar_total_lock:
        hit = _sidecar_total_memo.get(key)
        if hit is not None and hit[0] is pricing_table:
            _sidecar_total_memo.move_to_end(key)
            return hit[1]
    _, total, _ = _read_host_sidecar_savings(session_id, root)
    with _sidecar_total_lock:
        _sidecar_total_memo[key] = (pricing_table, total)
        _sidecar_total_memo.move_to_end(key)
        if len(_sidecar_total_memo) > _SIDECAR_TOTAL_MEMO_MAX:
            _sidecar_total_memo.popitem(last=False)
    return total


def read_total_savings_from_events(session_id: str, root: Path) -> float:
    """Total LemonCrow savings for *session_id* across all sources.

    Aggregates three streams so trace-only sessions surface the same
    figure the live statusline displays:

      1. ``live_savings_events.jsonl`` (routing/compaction savings keyed by
         internal LemonCrow session id)
      2. ``sessions/<session_id>/savings.jsonl`` (host sidecar keyed by
         host UUID — the source the statusline reads)
      3. Persisted context-carry credit, frozen at the session's last Stop
         event (``read_session_end_carry``) -- the SAME value the statusline
         and ``lc session stats``/``report`` fold into their totals
         (see that function's docstring). Without this, a session with a
         large carry credit would show materially less savings here than
         in the CLI/statusline for the identical session id. Sessions that
         haven't Stopped yet have no persisted snapshot; carry for those is
         simply omitted here rather than re-derived live, since a live
         re-derive needs Claude-transcript-specific machinery this
         host-agnostic reader intentionally doesn't depend on.

    The first source matches when the trace was recorded with an internal
    LemonCrow id; the second matches when the trace UUID is the host id that
    the MCP server wrote at the time.
    """
    # 1. live_savings_events.jsonl (single shared parse, indexed by session)
    _, _, total = _live_savings_for_session(session_id, root)

    # 2. host sidecar savings (priced per-row by the helper)
    total += _host_sidecar_total(session_id, root)

    # 3. persisted carry credit, if this session has Stopped at least once
    persisted_carry = read_session_end_carry(session_id, root)
    if persisted_carry is not None:
        total += persisted_carry[0]

    return round(total, 6)


# --------------------------------------------------------------------------- #
# Routing savings                                                              #
# --------------------------------------------------------------------------- #


def _read_routing_savings(events: list[dict[str, Any]]) -> tuple[int, float, int, int]:
    """Extract routing savings from ledger events.

    Returns ``(downtiered_turns, total_saved_usd)``.
    A turn is "downtiered" when ``cost_saved_usd > 0``.
    """
    downtiered = 0
    total_saved = 0.0
    lesson_applications = 0
    cost_cap_fired = 0
    for ev in events:
        if ev.get("kind") != "model_recommendation":
            continue
        payload = ev.get("payload") or ev
        if payload.get("configured") is False:
            continue
        saved = float(payload.get("cost_saved_usd") or 0.0)
        if payload.get("applied_lessons"):
            lesson_applications += 1
        if payload.get("cost_cap_triggered"):
            cost_cap_fired += 1
        if saved > 0:
            downtiered += 1
            total_saved += saved
    return downtiered, round(total_saved, 6), lesson_applications, cost_cap_fired


# --------------------------------------------------------------------------- #
# Data model                                                                   #
# --------------------------------------------------------------------------- #


@dataclass
class SessionReport:
    session_id: str
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: float
    active_duration_seconds: float
    vendor: str
    agent_settings: dict[str, Any]
    skills: list[str]
    telemetry: dict[str, Any]
    models_used: dict[str, int]
    started_model: str | None
    total_turns: int
    tool_call_count: int

    # Costs
    input_token_cost_usd: float
    cache_write_cost_usd: float
    cache_read_cost_usd: float
    output_token_cost_usd: float
    total_cost_usd: float

    # Tokens
    input_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    output_tokens: int

    # LemonCrow savings
    routing_downtiered_turns: int
    routing_savings_usd: float
    compact_events: int
    compact_savings_estimate_usd: float
    total_lemoncrow_savings_usd: float

    # Sources
    raw_artifact_ids: list[str]

    # Top tools by estimated cost
    top_tools_by_cost: list[tuple[str, int, float]]
    workflow_step: str = ""
    review_decision: str = ""
    task_progress_task_id: str = ""
    completed_tasks: int = 0
    remaining_tasks: int = 0
    routing_lesson_applications: int = 0
    cost_cap_fired_turns: int = 0
    context_compression_savings_usd: float = 0.0
    context_compression_tool_calls: int = 0
    tool_savings: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    # Canonical Read/Carry/Output components -- sourced from the shared
    # compute_savings_summary in build_report, never reimplemented here.
    # Carry credit is a recurring re-read credit across future turns (not a
    # one-time bucket), same convention as SavingsSummary.carry_usd.
    read_saved_usd: float = 0.0
    output_saved_usd: float = 0.0
    carry_usd: float = 0.0
    carry_tokens: int = 0
    # True when the ledger recorded no total (total_cost_usd == 0) and
    # total_cost_usd was backfilled from the pricing-derived bucket sum --
    # i.e. the total is an estimate, not a recorded figure.
    cost_estimated: bool = False
    # Claude Mods can provide host-owned per-request usage plus Claude's own
    # cumulative session cost. These fields keep actual measurement separate
    # from LemonCrow's counterfactual savings estimate.
    usage_measurement_source: str = "ledger"
    actual_cost_source: str = "ledger"
    measured_request_steps: int = 0
    measured_total_turns: int = 0
    measured_main_turns: int = 0
    measured_subagent_count: int = 0
    measured_subagent_turns: int = 0
    main_attributed_cost_usd: float = 0.0
    subagent_attributed_cost_usd: float = 0.0
    main_usage: dict[str, int] = dataclasses.field(default_factory=dict)
    subagent_usage: dict[str, int] = dataclasses.field(default_factory=dict)
    savings_estimated: bool = True

    @property
    def is_running(self) -> bool:
        return self.ended_at is None


# --------------------------------------------------------------------------- #
# Build                                                                        #
# --------------------------------------------------------------------------- #


def build_report(snapshot: dict[str, Any], root: Path, *, include_carry_credit: bool = False) -> SessionReport:
    """Build a ``SessionReport`` from a run-ledger *snapshot* dict.

    ``include_carry_credit`` gates the one EXPENSIVE component: carry credit
    requires per-session transcript access (glob + parse), unlike
    read/output savings which are cheap sidecar-file reads. Defaults to
    ``False`` so bulk callers that build a report per session in a loop
    (``insights.build_insights``, the ``/v1/sessions`` list endpoint) don't
    pay a per-session transcript scan multiplied by session count -- exactly
    the class of regression those callers' own caching comments already
    guard against. Pass ``True`` only for single/few-session detail views
    (``lc session report <id>``, the dashboard's small recent-runs list).
    """
    from lemoncrow.infra.runtime.cost_tracker import CostTracker

    session_id = str(snapshot.get("session_id") or "")
    status = str(snapshot.get("status") or "running")

    created_at = _parse_dt(snapshot.get("created_at")) or datetime.now(UTC)
    updated_at = _parse_dt(snapshot.get("updated_at")) or created_at
    ended_at: datetime | None = None if status == "running" else updated_at

    # --- events ---
    raw_events: list[dict[str, Any]] = snapshot.get("events") or []

    # Duration from first/last event timestamps
    event_times: list[datetime] = []
    for ev in raw_events:
        ts = _parse_dt(str(ev.get("at") or ""))
        if ts:
            event_times.append(ts)
    first_ts = event_times[0] if event_times else created_at
    last_ts = event_times[-1] if event_times else updated_at
    duration = max(0.0, (last_ts - first_ts).total_seconds())

    # Writers may append events to run.json after status flips away from
    # "running" without bumping updated_at (e.g. hook-driven ledger appends),
    # leaving the recorded end (updated_at) before the first event — an
    # impossible timeline (ended_at < started_at with a positive duration).
    # Trust the ledger's own end stamp and clamp the window/duration down to
    # it rather than lifting ended_at to the last event, which would make a
    # stale session sort as if it had just been active (see the /v1/sessions
    # last-activity sort and its regression test).
    if ended_at is not None and ended_at < first_ts:
        first_ts = min(first_ts, created_at, ended_at)
        last_ts = ended_at
        duration = max(0.0, (ended_at - first_ts).total_seconds())

    # --- active duration ---
    # Sum only the time chunks where the agent is working (from User -> Response)
    active_duration = 0.0
    current_start: datetime | None = None
    for ev in raw_events:
        ts = _parse_dt(str(ev.get("at") or ""))
        if not ts:
            continue
        kind = str(ev.get("kind") or "")
        if kind == "user_message":
            current_start = ts
        else:
            if current_start:
                chunk = (ts - current_start).total_seconds()
                if 0 < chunk < 3600:  # ignore massive gaps/clock drift
                    active_duration += chunk
                current_start = ts
            else:
                current_start = ts

    if active_duration <= 0:
        active_duration = duration

    # --- cost calls ---
    cost_data = snapshot.get("cost") or {}
    calls: list[dict[str, Any]] = cost_data.get("calls") or []
    total_cost = float(cost_data.get("total_cost_usd") or 0.0)
    snapshot_cost_status = str(cost_data.get("status") or snapshot.get("cost_status") or "").strip()
    snapshot_cost_source = str(cost_data.get("source") or "").strip()
    snapshot_usage_source = str(
        cost_data.get("usage_source") or (snapshot.get("telemetry") or {}).get("usage_measurement_source") or ""
    ).strip()

    # models_used: group calls by model
    models_used: dict[str, int] = {}
    for call in calls:
        m = str(call.get("model") or "unknown")
        models_used[m] = models_used.get(m, 0) + 1
    started_model = None
    if calls:
        started_model = str(calls[0].get("model") or "").strip() or None
    total_turns = len(calls)

    vendor = _derive_vendor(models_used)

    # tool_call_count from snapshot or event count
    tool_call_count = int(snapshot.get("tool_call_count") or 0)
    if not tool_call_count:
        tool_call_count = sum(1 for e in raw_events if e.get("kind") == "tool_call")

    # --- per-token cost breakdown ---
    (
        in_tok,
        in_cost,
        out_tok,
        out_cost,
        cr_tok,
        cr_cost,
        cw_tok,
        cw_cost,
    ) = _cost_breakdown_from_calls(calls)

    # Use total_cost from snapshot as ground truth; attribute remainder to input if needed
    cost_sum = in_cost + out_cost + cr_cost + cw_cost
    cost_estimated = snapshot_cost_status == "estimated"
    if cost_sum > 0 and total_cost > 0:
        ratio = total_cost / cost_sum
        in_cost = round(in_cost * ratio, 6)
        out_cost = round(out_cost * ratio, 6)
        cr_cost = round(cr_cost * ratio, 6)
        cw_cost = round(cw_cost * ratio, 6)
    elif cost_sum > 0:
        # Per-call token data exists but the ledger never recorded a total
        # (total_cost_usd == 0). Rendering non-zero bucket rows above
        # "Total: $0.00" is inconsistent -- use the buckets' own sum, and
        # flag the total as estimated (the buckets are pricing-derived).
        total_cost = round(cost_sum, 6)
        cost_estimated = True

    # --- Claude Mod exact usage measurement ---
    # Prefer host-owned request/token/cache measurements over the RunLedger when
    # available. The Mod is observational only, so this cannot perturb the
    # session it is measuring. Claude's own cumulative cost is authoritative
    # when exposed; parent/subagent cost split is attributed from exact per-model
    # token buckets and scaled to that host total.
    usage_measurement_source = snapshot_usage_source or "ledger"
    if snapshot_cost_source:
        actual_cost_source = snapshot_cost_source
    elif snapshot_cost_status == "unavailable":
        actual_cost_source = "unavailable"
    else:
        actual_cost_source = "priced" if cost_estimated else "ledger"
    measured_request_steps = 0
    measured_total_turns = 0
    measured_main_turns = 0
    measured_subagent_count = 0
    measured_subagent_turns = 0
    main_attributed_cost_usd = 0.0
    subagent_attributed_cost_usd = 0.0
    main_usage: dict[str, int] = {}
    subagent_usage: dict[str, int] = {}
    try:
        from lemoncrow.core.capabilities.claude_mod_usage import (
            bucket_cost_breakdown_usd,
            bucket_cost_usd,
            load_claude_mod_usage,
        )

        mod_usage = load_claude_mod_usage(root, session_id)
    except Exception:
        mod_usage = None
    if mod_usage is not None and mod_usage.total_steps > 0:
        usage_measurement_source = "claude_mod"
        measured_request_steps = mod_usage.total_steps
        measured_total_turns = mod_usage.total_turns
        measured_main_turns = mod_usage.main.turns
        measured_subagent_count = len(mod_usage.subagents)
        measured_subagent_turns = mod_usage.subagent_turns
        in_tok = mod_usage.input_tokens
        out_tok = mod_usage.output_tokens
        cr_tok = mod_usage.cache_read_tokens
        cw_tok = mod_usage.cache_write_tokens
        main_usage = {
            "input_tokens": mod_usage.main.input_tokens,
            "cache_write_tokens": mod_usage.main.cache_write_tokens,
            "cache_read_tokens": mod_usage.main.cache_read_tokens,
            "output_tokens": mod_usage.main.output_tokens,
        }
        subagent_usage = {
            "input_tokens": sum(item.input_tokens for item in mod_usage.subagents.values()),
            "cache_write_tokens": sum(item.cache_write_tokens for item in mod_usage.subagents.values()),
            "cache_read_tokens": sum(item.cache_read_tokens for item in mod_usage.subagents.values()),
            "output_tokens": sum(item.output_tokens for item in mod_usage.subagents.values()),
        }

        breakdown = {"input": 0.0, "cache_read": 0.0, "cache_write": 0.0, "output": 0.0}
        for bucket in (mod_usage.main, *mod_usage.subagents.values()):
            part = bucket_cost_breakdown_usd(bucket)
            for key in breakdown:
                breakdown[key] += float(part.get(key) or 0.0)
        in_cost = round(breakdown["input"], 6)
        cr_cost = round(breakdown["cache_read"], 6)
        cw_cost = round(breakdown["cache_write"], 6)
        out_cost = round(breakdown["output"], 6)
        priced_total = in_cost + cr_cost + cw_cost + out_cost
        measured_total = mod_usage.measured_cost_usd
        if measured_total is not None:
            total_cost = round(measured_total, 6)
            actual_cost_source = "claude_mod"
            cost_estimated = False
            if priced_total > 0:
                scale = total_cost / priced_total
                in_cost = round(in_cost * scale, 6)
                cr_cost = round(cr_cost * scale, 6)
                cw_cost = round(cw_cost * scale, 6)
                out_cost = round(out_cost * scale, 6)
        elif priced_total > 0:
            total_cost = round(priced_total, 6)
            actual_cost_source = "claude_mod_tokens_priced"
            cost_estimated = True

        main_raw = bucket_cost_usd(mod_usage.main)
        sub_raw = sum(bucket_cost_usd(item) for item in mod_usage.subagents.values())
        attributed_raw = main_raw + sub_raw
        attribution_scale = total_cost / attributed_raw if attributed_raw > 0 and total_cost > 0 else 1.0
        main_attributed_cost_usd = round(main_raw * attribution_scale, 6)
        subagent_attributed_cost_usd = round(sub_raw * attribution_scale, 6)

    # --- routing savings ---
    routing_downtiered, routing_saved, lesson_applications, cost_cap_fired = _read_routing_savings(raw_events)

    # --- compact savings ---
    compact_count, compact_saved = _read_compact_savings(session_id, root)

    # --- context compression savings (per-tool read/grep/search/shell) ---
    compression_count, compression_saved, compression_rows = _read_context_compression_savings(session_id, root)

    # --- read/output/carry components (canonical Read/Carry/Output/Routing
    # model) -- sourced directly from the shared per-lever sidecar readers
    # (same ones compute_savings_summary uses) so carry/read/output logic is
    # never reimplemented here. KNOWN DUPLICATION (follow-up): compression_saved
    # above independently re-scans the same sessions/<id>/savings.jsonl sidecar
    # through the same _price_savings_row rule, rather than this file sourcing
    # everything from one place. read_saved_usd/output_saved_usd are
    # informational subcomponents already folded inside compression_saved --
    # do not add them into total_saved below, that would double-count (mirrors
    # SavingsSummary.saved_usd/total_saved_usd); carry_usd is the one component
    # genuinely absent before this change, so it IS added (when computed).
    from lemoncrow.core.capabilities.savings_summary import (
        _read_session_output_style,
        _read_session_read_savings,
    )

    _, read_saved_usd_raw = _read_session_read_savings(session_id, root)
    _, output_saved_usd_raw = _read_session_output_style(session_id, root)
    read_saved_usd = round(float(read_saved_usd_raw), 6)
    output_saved_usd = round(float(output_saved_usd_raw), 6)

    carry_usd = 0.0
    carry_tokens = 0
    if include_carry_credit:
        from lemoncrow.core.capabilities.savings_summary import compute_savings_summary

        summary = compute_savings_summary(session_id, lemoncrow_root=root)
        carry_usd = round(float(summary.carry_usd), 6)
        carry_tokens = int(summary.carry_tokens)

    total_saved = round(routing_saved + compact_saved + compression_saved + carry_usd, 6)

    # --- per-tool breakdown ---
    top_tools = CostTracker.per_tool_cost_breakdown(raw_events)

    agent_settings = dict(snapshot.get("agent_settings") or {})
    skills = list(snapshot.get("skills") or [])
    telemetry = dict(snapshot.get("telemetry") or {})
    raw_artifact_ids = list(snapshot.get("raw_artifact_ids") or [])
    workflow_state = dict(snapshot.get("workflow_state") or {})
    plan_review = dict(snapshot.get("plan_review") or {})
    task_progress = dict(snapshot.get("task_progress") or {})

    return SessionReport(
        session_id=session_id,
        started_at=first_ts,
        ended_at=ended_at,
        duration_seconds=duration,
        active_duration_seconds=active_duration,
        vendor=vendor,
        agent_settings=agent_settings,
        skills=skills,
        telemetry=telemetry,
        raw_artifact_ids=raw_artifact_ids,
        models_used=models_used,
        started_model=started_model,
        total_turns=total_turns,
        tool_call_count=tool_call_count,
        workflow_step=str(workflow_state.get("workflow_step") or ""),
        review_decision=str(plan_review.get("review_decision") or ""),
        task_progress_task_id=str(task_progress.get("task_id") or ""),
        completed_tasks=int(task_progress.get("completed_tasks") or 0),
        remaining_tasks=int(task_progress.get("remaining_tasks") or 0),
        input_token_cost_usd=in_cost,
        cache_write_cost_usd=cw_cost,
        cache_read_cost_usd=cr_cost,
        output_token_cost_usd=out_cost,
        total_cost_usd=total_cost,
        input_tokens=in_tok,
        cache_write_tokens=cw_tok,
        cache_read_tokens=cr_tok,
        output_tokens=out_tok,
        routing_downtiered_turns=routing_downtiered,
        routing_savings_usd=routing_saved,
        routing_lesson_applications=lesson_applications,
        cost_cap_fired_turns=cost_cap_fired,
        compact_events=compact_count,
        compact_savings_estimate_usd=compact_saved,
        context_compression_savings_usd=compression_saved,
        context_compression_tool_calls=compression_count,
        tool_savings=compression_rows,
        total_lemoncrow_savings_usd=total_saved,
        read_saved_usd=read_saved_usd,
        output_saved_usd=output_saved_usd,
        carry_usd=carry_usd,
        carry_tokens=carry_tokens,
        cost_estimated=cost_estimated,
        usage_measurement_source=usage_measurement_source,
        actual_cost_source=actual_cost_source,
        measured_request_steps=measured_request_steps,
        measured_total_turns=measured_total_turns,
        measured_main_turns=measured_main_turns,
        measured_subagent_count=measured_subagent_count,
        measured_subagent_turns=measured_subagent_turns,
        main_attributed_cost_usd=main_attributed_cost_usd,
        subagent_attributed_cost_usd=subagent_attributed_cost_usd,
        main_usage=main_usage,
        subagent_usage=subagent_usage,
        savings_estimated=True,
        top_tools_by_cost=top_tools[:5],
    )


def build_report_from_ledger(ledger: RunLedger, root: Path, *, include_carry_credit: bool = False) -> SessionReport:
    """Build a ``SessionReport`` directly from a live ``RunLedger``."""
    return build_report(ledger.snapshot(), root, include_carry_credit=include_carry_credit)


def load_report(session_id: str, root: Path, *, include_carry_credit: bool = False) -> SessionReport | None:
    """Load a logical session, including its older date partitions."""
    for path, snapshot in list_run_snapshots(root, session_id=session_id):
        if str(snapshot.get("session_id") or snapshot.get("run_id") or path.parent.name) == session_id:
            return build_report(snapshot, root, include_carry_credit=include_carry_credit)
    return None


def _merge_partition_rows(snapshots: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    """Union snapshots, preserving repeated identical rows within one partition."""
    rows: dict[tuple[str, int], dict[str, Any]] = {}
    for snapshot in reversed(snapshots):  # newest values win for stable source ids
        counts: dict[str, int] = {}
        source = (snapshot.get("cost") or {}).get("calls", []) if field == "calls" else snapshot.get(field, [])
        for row in source or []:
            if not isinstance(row, dict):
                continue
            source_id = row.get("source_id") or row.get("id")
            key = f"id:{source_id}" if source_id else json.dumps(row, sort_keys=True)
            occurrence = counts.get(key, 0)
            counts[key] = occurrence + 1
            rows[(key, occurrence)] = row
    return sorted(rows.values(), key=lambda row: str(row.get("at") or ""))


@lru_cache(maxsize=2048)
def _read_run_partition(path: str, mtime_ns: int, size: int, ctime_ns: int) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("run snapshot must be an object")
    return payload


def list_run_snapshots(
    root: Path, *, since: datetime | None = None, session_id: str | None = None
) -> list[tuple[Path, dict[str, Any]]]:
    """Read each logical session once, merging calls across date partitions.

    File mtime selects active sessions, never individual partitions: an older
    partition can contain unique calls from a session that resumed this week.
    """
    groups: dict[str, list[tuple[Path, dict[str, Any], float]]] = {}
    for path in (root / "sessions").glob("**/run.json"):
        if session_id is not None and path.parent.name != session_id:
            continue
        try:
            info = path.stat()
            snapshot = _read_run_partition(str(path), info.st_mtime_ns, info.st_size, info.st_ctime_ns)
        except (OSError, ValueError, TypeError):
            continue
        sid = str(snapshot.get("session_id") or snapshot.get("run_id") or path.parent.name)
        groups.setdefault(sid, []).append((path, snapshot, info.st_mtime))
    result: list[tuple[float, Path, dict[str, Any]]] = []
    for partitions in groups.values():
        partitions.sort(key=lambda item: (item[2], str(item[0])), reverse=True)
        path, latest, mtime = partitions[0]
        if since is not None and mtime < since.timestamp():
            continue
        snapshots = [item[1] for item in partitions]
        merged = dict(latest)
        if len(snapshots) > 1:
            calls = _merge_partition_rows(snapshots, "calls")
            merged["events"] = _merge_partition_rows(snapshots, "events")
            cost = dict(latest.get("cost") or {})
            cost["calls"] = calls
            if calls and all(call.get("cost_usd") is not None for call in calls):
                cost["total_cost_usd"] = round(sum(float(call["cost_usd"]) for call in calls), 6)
            else:
                cost["total_cost_usd"] = max(
                    float((snap.get("cost") or {}).get("total_cost_usd") or 0) for snap in snapshots
                )
            merged["cost"] = cost
            starts = [str(snap["created_at"]) for snap in snapshots if snap.get("created_at")]
            if starts:
                merged["created_at"] = min(starts)
        result.append((mtime, path, merged))
    result.sort(key=lambda item: (item[0], str(item[1])), reverse=True)
    return [(path, snapshot) for _, path, snapshot in result]


def list_run_files(root: Path, *, since: datetime | None = None) -> list[Path]:
    """Return one newest run file per session, before any caller paginates."""
    return [path for path, _ in list_run_snapshots(root, since=since)]


# --------------------------------------------------------------------------- #
# Renderers                                                                    #
# --------------------------------------------------------------------------- #


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s}s"
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h {m}m"


ReportLine = tuple[str, str]
"""``(tag, text)``: tag ``heading``, ``total``, ``dim`` or ``""``; the CLI styles by tag."""

_LABEL_W = 26
_VALUE_W = 12


def report_title(report: SessionReport) -> tuple[str, str]:
    """``("Session 1a2b3c", "2h 14m · claude")``: a report's name and its one-line detail."""
    sid_short = report.session_id[:6] if report.session_id else "?"
    duration = "running" if report.is_running else _fmt_duration(report.duration_seconds)
    return f"Session {sid_short}", " · ".join(part for part in (duration, report.vendor) if part)


def report_lines(report: SessionReport) -> list[ReportLine]:
    """A session report's body, line by line, each tagged for styling."""
    lines: list[ReportLine] = []

    def add(text: str = "", tag: str = "") -> None:
        lines.append((tag, text))

    def row(label: str, value: str, note: str = "", tag: str = "") -> None:
        text = f"    {label:<{_LABEL_W}}{value:>{_VALUE_W}}"
        add(f"{text}  {note}" if note else text, tag)

    def fact(label: str, value: str) -> None:
        add(f"    {label:<{_LABEL_W - 6}}{value}")

    def plural(count: int, noun: str) -> str:
        return f"{count} {noun}{'' if count == 1 else 's'}"

    started = report.started_at.astimezone()
    fact("Started:", f"{started:%a} {started.day} {started:%b %Y, %H:%M}")
    models = sorted(report.models_used.items(), key=lambda item: -item[1])
    fact("Models used:", ", ".join(f"{model} ({plural(count, 'turn')})" for model, count in models) or "none")
    if report.usage_measurement_source == "claude_mod":
        fact("Measured requests:", str(report.measured_request_steps))
        fact("Main turns:", str(report.measured_main_turns))
        if report.measured_subagent_count:
            fact("Subagents:", f"{report.measured_subagent_count} · {plural(report.measured_subagent_turns, 'turn')}")
    else:
        fact("Turns:", str(report.total_turns))
    fact("Tool calls:", str(report.tool_call_count))
    if report.workflow_step or report.review_decision or report.task_progress_task_id:
        fact("Workflow step:", report.workflow_step or "n/a")
        if report.review_decision:
            fact("Review decision:", report.review_decision)
        if report.task_progress_task_id:
            fact(
                "Task progress:",
                f"{report.task_progress_task_id} ({report.completed_tasks} done/{report.remaining_tasks} remaining)",
            )

    add()
    add("  Cost", "heading")
    if report.usage_measurement_source == "unavailable":
        fact("Usage:", "unavailable from host")
    else:
        buckets = [("Input tokens:", report.input_tokens, report.input_token_cost_usd)]
        if report.cache_write_tokens or report.cache_write_cost_usd:
            buckets.append(("Cache writes:", report.cache_write_tokens, report.cache_write_cost_usd))
        buckets.append(("Cache reads:", report.cache_read_tokens, report.cache_read_cost_usd))
        buckets.append(("Output tokens:", report.output_tokens, report.output_token_cost_usd))
        for label, tokens, cost in buckets:
            add(f"    {label:<{_LABEL_W - 10}}{_fmt_tok(tokens):>10}{_fmt_usd(cost):>{_VALUE_W}}")
    if report.actual_cost_source == "unavailable":
        fact("Cost:", "unavailable from host")
    else:
        cost_label = (
            "Priced total:"
            if report.actual_cost_source == "priced"
            else "Measured total:" if report.actual_cost_source in {"claude_mod", "host_reported"} else "Total:"
        )
        row(cost_label, _fmt_usd(report.total_cost_usd), tag="total")
    if report.usage_measurement_source == "claude_mod" and report.measured_subagent_count:
        attributed = report.main_attributed_cost_usd + report.subagent_attributed_cost_usd
        share = 100.0 * report.subagent_attributed_cost_usd / attributed if attributed > 0 else 0.0
        row("Subagent share:", f"{share:.1f}%")

    # Savings are a counterfactual estimate. The Mod makes actual usage exact,
    # but there is still no simultaneous no-LemonCrow version of the same run.
    add()
    add("  Estimated LemonCrow savings", "heading")
    if report.context_compression_savings_usd > 0 or report.context_compression_tool_calls > 0:
        calls = plural(report.context_compression_tool_calls, "tool call")
        row("Context compression:", _fmt_usd(report.context_compression_savings_usd), f"({calls})")
        # read/output are informational subcomponents already folded inside
        # context compression (see build_report's KNOWN DUPLICATION note) --
        # indent them so the top-level items visibly sum to the total.
        if report.read_saved_usd > 0:
            row("  of which read:", _fmt_usd(report.read_saved_usd), tag="dim")
        if report.output_saved_usd > 0:
            row("  of which output:", _fmt_usd(report.output_saved_usd), tag="dim")
    if report.carry_usd > 0:
        row("Carry credit:", _fmt_usd(report.carry_usd), f"({_fmt_tok(report.carry_tokens)} tok)")
    if report.routing_downtiered_turns:
        turns = plural(report.routing_downtiered_turns, "turn")
        row("Routing recommendations:", _fmt_usd(report.routing_savings_usd), f"({turns} downtiered)")
    else:
        row("Routing recommendations:", "none", tag="dim")
    if report.routing_lesson_applications:
        row("Active lessons applied:", str(report.routing_lesson_applications), "recommendations")
    if report.cost_cap_fired_turns:
        row("Cost cap fired:", str(report.cost_cap_fired_turns), "turns")
    if report.compact_events:
        events = plural(report.compact_events, "event")
        row("Compaction:", f"~{_fmt_usd(report.compact_savings_estimate_usd)}", f"({events}, avoided resend cost)")
    else:
        row("Compaction:", "none", tag="dim")
    row("Estimated saved:", _fmt_usd(report.total_lemoncrow_savings_usd), tag="total")
    baseline = report.total_cost_usd + report.total_lemoncrow_savings_usd
    if baseline > 0:
        row("Estimated reduction:", f"{100.0 * report.total_lemoncrow_savings_usd / baseline:.1f}%")

    if report.top_tools_by_cost:
        add()
        add("  Costliest tools", "heading")
        for tool_name, count, cost in report.top_tools_by_cost:
            add(f"    {tool_name:<{_LABEL_W - 10}}{plural(count, 'call'):>10}{_fmt_usd(cost):>{_VALUE_W}}")
    return lines


def render_text(report: SessionReport, *, no_color: bool = False) -> str:
    """Render a human-readable session report as plain text."""
    name, detail = report_title(report)
    return "\n".join([f"{name}  {detail}", "", *(text for _tag, text in report_lines(report))])


def render_json(report: SessionReport) -> str:
    """Render report as JSON (datetimes serialised as ISO-8601 strings)."""

    def _default(obj: object) -> str:
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serialisable")

    return json.dumps(dataclasses.asdict(report), default=_default, indent=2)


__all__ = [
    "SessionReport",
    "build_report",
    "build_report_from_ledger",
    "list_run_files",
    "load_report",
    "render_json",
    "render_text",
    "report_lines",
    "report_title",
]

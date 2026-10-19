"""Offline replay of runtime traces: rebuild calls, re-rank, and score.

A call is the union of every layer's record for one ``lt_`` trace id
(client, server, runtime).  Retrieval calls carry the ranked candidate set
and features, so any :class:`Ranker` can be replayed against exactly what the
production ranker saw.

Relevance labels are implicit and observable: a candidate is relevant when
the agent touched its path (read/edit/grep/...) in the next
``label_window`` calls of the same session.  Only traces with at least one
label are scored.  Metrics: MRR@k, recall@k, hit@1, plus per-call
improvements/regressions of reciprocal rank and mean sufficiency.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from lemoncrow.core.capabilities.context_engine import (
    Candidate,
    ContextQuery,
    Ranker,
    assess_sufficiency,
)

_LAYER_ORDER = ("client", "server", "runtime")
_RETRIEVAL_TOOLS = frozenset({"code_search"})


@dataclass(slots=True)
class TraceCall:
    """One tool call assembled from every layer that recorded it."""

    trace_id: str
    tool: str
    started_at: str
    session: str | None
    arguments: dict[str, Any]
    layers: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def retrieval(self) -> dict[str, Any] | None:
        for layer in ("server", "runtime", "client"):
            record = self.layers.get(layer)
            retrieval = record.get("retrieval") if record else None
            if isinstance(retrieval, dict):
                return retrieval
        return None

    @property
    def decisions(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for layer in _LAYER_ORDER:
            out.extend(self.layers.get(layer, {}).get("decisions") or [])
        return out

    @property
    def duration_ms(self) -> int:
        for layer in _LAYER_ORDER:
            value = (self.layers.get(layer, {}).get("execution") or {}).get("duration_ms")
            if isinstance(value, int):
                return value
        return 0

    @property
    def outcome(self) -> dict[str, Any]:
        for layer in _LAYER_ORDER:
            value = self.layers.get(layer, {}).get("outcome")
            if value:
                return dict(value)
        return {}


def _session_key(record: Mapping[str, Any]) -> str | None:
    value = record.get("session_id")
    return value if isinstance(value, str) and value else None


def assemble_calls(records: Iterable[Mapping[str, Any]]) -> list[TraceCall]:
    """Merge per-layer records by trace id; order calls by start time."""
    calls: dict[str, TraceCall] = {}
    for record in records:
        trace_id = str(record.get("trace_id") or "")
        if not trace_id:
            continue
        writer = str(record.get("writer") or "runtime")
        started = str(record.get("started_at") or "")
        arguments = (record.get("request") or {}).get("arguments") or {}
        call = calls.get(trace_id)
        if call is None:
            call = calls[trace_id] = TraceCall(
                trace_id=trace_id,
                tool=str(record.get("tool") or ""),
                started_at=started,
                session=_session_key(record) if writer == "client" else None,
                arguments=dict(arguments) if isinstance(arguments, Mapping) else {},
            )
        # Keep the first record per layer (retries write one per attempt).
        call.layers.setdefault(writer, dict(record))
        if started and (not call.started_at or started < call.started_at):
            call.started_at = started
        if writer == "client":
            call.session = _session_key(record) or call.session
            if isinstance(arguments, Mapping) and arguments:
                call.arguments = dict(arguments)
        elif call.session is None and "client" not in call.layers:
            call.session = _session_key(record)
    return sorted(calls.values(), key=lambda c: (c.started_at, c.trace_id))


def _string_leaves(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _string_leaves(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _string_leaves(item)


def _touched(call: TraceCall, candidate_paths: Sequence[str]) -> set[str]:
    leaves = [leaf for leaf in _string_leaves(call.arguments) if leaf]
    return {path for path in candidate_paths if any(path in leaf for leaf in leaves)}


@dataclass(frozen=True, slots=True)
class ReplayCase:
    trace_id: str
    query: str
    limit: int
    scopes: tuple[str, ...]
    candidates: tuple[Candidate, ...]
    production: tuple[str, ...]
    relevant: frozenset[str]


def build_cases(calls: Sequence[TraceCall], *, label_window: int = 8) -> list[ReplayCase]:
    """Retrieval calls with raw candidates, labeled by later touches."""
    by_session: dict[str | None, list[TraceCall]] = {}
    for call in calls:
        by_session.setdefault(call.session, []).append(call)
    cases: list[ReplayCase] = []
    for session_calls in by_session.values():
        for index, call in enumerate(session_calls):
            retrieval = call.retrieval
            if call.tool not in _RETRIEVAL_TOOLS or not retrieval:
                continue
            query = retrieval.get("query")
            rows = retrieval.get("candidates") or []
            if not isinstance(query, str) or not rows or not isinstance(rows[0].get("path"), str):
                continue  # redacted trace: not replayable
            candidates = tuple(Candidate.from_trace(row) for row in rows if isinstance(row, Mapping))
            paths = [c.path for c in candidates]
            relevant: set[str] = set()
            for later in session_calls[index + 1 : index + 1 + label_window]:
                if later.tool in _RETRIEVAL_TOOLS:
                    continue
                relevant |= _touched(later, paths)
            extra = retrieval.get("extra") or {}
            scopes = tuple(str(s) for s in extra.get("scopes") or () if isinstance(s, str))
            selected = tuple(str(p) for p in retrieval.get("selected") or () if isinstance(p, str))
            limit = int(retrieval.get("limit") or len(selected) or 8)
            cases.append(
                ReplayCase(
                    trace_id=call.trace_id,
                    query=query,
                    limit=max(1, limit),
                    scopes=scopes,
                    candidates=candidates,
                    production=selected,
                    relevant=frozenset(relevant),
                )
            )
    return cases


def _reciprocal_rank(ranked: Sequence[str], relevant: frozenset[str], k: int) -> float:
    for index, path in enumerate(ranked[:k]):
        if path in relevant:
            return 1.0 / (index + 1)
    return 0.0


@dataclass(slots=True)
class RankerScore:
    name: str
    evaluated: int = 0
    mrr: float = 0.0
    recall: float = 0.0
    hit_at_1: float = 0.0
    sufficiency: float = 0.0

    def to_payload(self) -> dict[str, Any]:
        return {
            "ranker": self.name,
            "evaluated": self.evaluated,
            "mrr": round(self.mrr, 4),
            "recall_at_k": round(self.recall, 4),
            "hit_at_1": round(self.hit_at_1, 4),
            "mean_sufficiency": round(self.sufficiency, 4),
        }


@dataclass(slots=True)
class ReplayReport:
    cases_total: int
    cases_labeled: int
    baseline: RankerScore
    candidate: RankerScore
    improvements: int = 0
    regressions: int = 0
    unchanged: int = 0
    top1_changed: int = 0
    examples: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "cases_total": self.cases_total,
            "cases_labeled": self.cases_labeled,
            "baseline": self.baseline.to_payload(),
            "candidate": self.candidate.to_payload(),
            "improvements": self.improvements,
            "regressions": self.regressions,
            "unchanged": self.unchanged,
            "top1_changed": self.top1_changed,
            "examples": self.examples,
        }


def replay_rankers(
    cases: Sequence[ReplayCase],
    baseline: Ranker,
    candidate: Ranker,
    *,
    max_examples: int = 10,
) -> ReplayReport:
    """Score two rankers on the same recorded candidate sets."""
    labeled = [case for case in cases if case.relevant]
    report = ReplayReport(
        cases_total=len(cases),
        cases_labeled=len(labeled),
        baseline=RankerScore(baseline.name),
        candidate=RankerScore(candidate.name),
    )
    for case in cases:
        query = ContextQuery(text=case.query, limit=case.limit, scopes=case.scopes)
        base_ranked = baseline.rank(query, case.candidates)
        cand_ranked = candidate.rank(query, case.candidates)
        base_paths = [c.path for c in base_ranked]
        cand_paths = [c.path for c in cand_ranked]
        if base_paths[:1] != cand_paths[:1]:
            report.top1_changed += 1
        if not case.relevant:
            continue
        k = case.limit
        for score, ranked, paths in (
            (report.baseline, base_ranked, base_paths),
            (report.candidate, cand_ranked, cand_paths),
        ):
            score.evaluated += 1
            score.mrr += _reciprocal_rank(paths, case.relevant, k)
            score.recall += len(case.relevant & set(paths[:k])) / len(case.relevant)
            score.hit_at_1 += 1.0 if paths[:1] and paths[0] in case.relevant else 0.0
            score.sufficiency += assess_sufficiency(query, ranked[:k]).score
        base_rr = _reciprocal_rank(base_paths, case.relevant, k)
        cand_rr = _reciprocal_rank(cand_paths, case.relevant, k)
        if cand_rr > base_rr:
            report.improvements += 1
        elif cand_rr < base_rr:
            report.regressions += 1
        else:
            report.unchanged += 1
        if cand_rr != base_rr and len(report.examples) < max_examples:
            report.examples.append(
                {
                    "trace_id": case.trace_id,
                    "query": case.query[:120],
                    "baseline_rr": round(base_rr, 4),
                    "candidate_rr": round(cand_rr, 4),
                    "relevant": sorted(case.relevant)[:5],
                }
            )
    for score in (report.baseline, report.candidate):
        if score.evaluated:
            score.mrr /= score.evaluated
            score.recall /= score.evaluated
            score.hit_at_1 /= score.evaluated
            score.sufficiency /= score.evaluated
    return report


def verify_production_parity(cases: Sequence[ReplayCase], ranker: Ranker) -> tuple[int, int]:
    """How many recorded rankings ``ranker`` reproduces exactly (sanity check).

    Only cases where the production ranker's name matched are meaningful; a
    mismatch means the recorded candidate set or features are insufficient to
    replay, which would invalidate a comparison.
    """
    matched = 0
    for case in cases:
        query = ContextQuery(text=case.query, limit=case.limit, scopes=case.scopes)
        ranked = [c.path for c in ranker.rank(query, case.candidates)][: case.limit]
        # Production selection may also hold post-ranking additions (accelerator
        # hits, bounded expansion); compare order over the paths both share.
        shared = set(ranked) & set(case.production)
        if [p for p in case.production if p in shared] == [p for p in ranked if p in shared]:
            matched += 1
    return matched, len(cases)


def compare_calls(a: TraceCall, b: TraceCall) -> dict[str, Any]:
    """Structured diff of two calls (request, selection, decisions, outcome)."""

    def selected(call: TraceCall) -> list[Any]:
        retrieval = call.retrieval or {}
        return list(retrieval.get("selected") or [])

    def kinds(call: TraceCall) -> list[str]:
        return [str(d.get("kind")) for d in call.decisions]

    sel_a, sel_b = selected(a), selected(b)
    return {
        "a": a.trace_id,
        "b": b.trace_id,
        "tool": [a.tool, b.tool],
        "arguments_equal": a.arguments == b.arguments,
        "arguments": [a.arguments, b.arguments] if a.arguments != b.arguments else a.arguments,
        "selected_only_a": [p for p in sel_a if p not in sel_b],
        "selected_only_b": [p for p in sel_b if p not in sel_a],
        "selected_order_equal": sel_a == sel_b,
        "decisions": [kinds(a), kinds(b)],
        "duration_ms": [a.duration_ms, b.duration_ms],
        "outcome": [a.outcome, b.outcome],
    }


__all__ = [
    "RankerScore",
    "ReplayCase",
    "ReplayReport",
    "TraceCall",
    "assemble_calls",
    "build_cases",
    "compare_calls",
    "replay_rankers",
    "verify_production_parity",
]

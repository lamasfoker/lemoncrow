from __future__ import annotations

from typing import Any

from lemoncrow.core.capabilities.context_engine import Candidate, ContextQuery, FeatureWeightedRanker, HeuristicRanker
from lemoncrow.core.capabilities.trace_replay import (
    assemble_calls,
    build_cases,
    compare_calls,
    replay_rankers,
    verify_production_parity,
)


def _client(tid: str, tool: str, at: str, args: dict[str, Any], session: str = "s1") -> dict[str, Any]:
    return {
        "trace_id": tid,
        "writer": "client",
        "tool": tool,
        "started_at": at,
        "session_id": session,
        "request": {"arguments": args},
        "execution": {"duration_ms": 5},
        "outcome": {"is_error": False},
    }


def _server(tid: str, at: str, query: str, rows: list[tuple[str, float, float]], limit: int = 3) -> dict[str, Any]:
    candidates = [
        {"path": p, "score": s, "rank": i, "features": {"has_definitions": d}} for i, (p, s, d) in enumerate(rows)
    ]
    ordered = HeuristicRanker().rank(ContextQuery(query, limit), [Candidate.from_trace(c) for c in candidates])
    return {
        "trace_id": tid,
        "writer": "server",
        "tool": "code_search",
        "started_at": at,
        "session_id": "host-session",
        "request": {"arguments": {"query": query}},
        "decisions": [{"kind": "context.sufficiency"}],
        "retrieval": {
            "query": query,
            "ranker": "heuristic",
            "limit": limit,
            "candidates": candidates,
            "selected": [c.path for c in ordered[:limit]],
        },
        "execution": {"duration_ms": 7},
        "outcome": {"is_error": False},
    }


def _records() -> list[dict[str, Any]]:
    rows = [("src/util.py", 1.0, 1.0), ("src/review/auth.py", 0.9, 0.0), ("docs/x.md", 0.8, 0.0)]
    return [
        _client("lt_0000000000000001", "code_search", "2026-09-23T10:00:00", {"query": "review auth"}),
        _server("lt_0000000000000001", "2026-09-23T10:00:00.1", "review auth", rows),
        _client("lt_0000000000000002", "read", "2026-09-23T10:00:05", {"files": ["src/review/auth.py:L1-L20"]}),
        _client("lt_0000000000000003", "bash", "2026-09-23T10:00:09", {"command": "ls"}, session="s2"),
    ]


def test_layers_merge_by_trace_id_and_session_comes_from_the_client() -> None:
    calls = assemble_calls(_records())
    assert [c.trace_id for c in calls] == ["lt_0000000000000001", "lt_0000000000000002", "lt_0000000000000003"]
    first = calls[0]
    assert sorted(first.layers) == ["client", "server"] and first.session == "s1"
    assert first.retrieval is not None and first.decisions == [{"kind": "context.sufficiency"}]


def test_cases_are_labeled_by_later_touches_in_the_same_session() -> None:
    cases = build_cases(assemble_calls(_records()))
    assert len(cases) == 1
    assert cases[0].relevant == frozenset({"src/review/auth.py"})
    assert cases[0].production[0] == "src/util.py"  # definitions-first


def test_replay_scores_both_rankers_on_identical_candidates() -> None:
    cases = build_cases(assemble_calls(_records()))
    report = replay_rankers(cases, HeuristicRanker(), FeatureWeightedRanker())
    assert report.cases_labeled == 1
    assert report.baseline.mrr == 0.5  # relevant file ranked second
    assert report.candidate.mrr == 1.0  # term coverage lifts it to first
    assert (report.improvements, report.regressions) == (1, 0)
    assert verify_production_parity(cases, HeuristicRanker()) == (1, 1)


def test_redacted_traces_are_not_replayable() -> None:
    records = _records()
    records[1]["retrieval"]["query"] = {"redacted_sha256": "x"}
    assert build_cases(assemble_calls(records)) == []


def test_compare_calls_reports_selection_and_argument_differences() -> None:
    calls = assemble_calls(_records())
    diff = compare_calls(calls[0], calls[1])
    assert diff["tool"] == ["code_search", "read"] and diff["arguments_equal"] is False
    assert diff["selected_only_a"] == ["src/util.py", "src/review/auth.py", "docs/x.md"]

from __future__ import annotations

import fnmatch
import random

import pytest

from lemoncrow.core.capabilities.context_engine import (
    Candidate,
    ContextEngine,
    ContextQuery,
    ExpansionAction,
    FeatureWeightedRanker,
    HeuristicRanker,
    assess_sufficiency,
    get_ranker,
)
from lemoncrow.core.capabilities.context_engine.engine import merge_preserving_head
from lemoncrow.core.foundation.runtime_budget import PROFILES, infer_budget
from lemoncrow.core.foundation.runtime_trace import open_trace


def _legacy_order(query: str, scopes: tuple[str, ...], rows: list[tuple[str, float, bool]]) -> list[str]:
    """Verbatim copy of the pre-R3 server ordering (IndexQuery.search)."""
    ranked = sorted(rows, key=lambda r: (not r[2], r[0] != query and r[0].rsplit("/", 1)[-1] != query, -r[1], r[0]))
    if scopes:
        preferred = [
            r
            for r in ranked
            if any(
                s == "." or r[0] == s or r[0].startswith(s.rstrip("/") + "/") or fnmatch.fnmatchcase(r[0], s)
                for s in scopes
            )
        ][:2]
        reserved = {r[0] for r in preferred}
        ranked = [*preferred, *(r for r in ranked if r[0] not in reserved)]
    return [r[0] for r in ranked]


def test_context_engine_ranker_contract() -> None:
    """case: test_heuristic_ranker_is_byte_identical_to_the_legacy_server_order"""
    rng = random.Random(1234)
    dirs = ["src", "src/core", "tests", "docs", "lib/x"]
    names = ["a.py", "b.py", "engine.py", "auth.go", "README.md", "query"]
    divergences = 0
    for _ in range(3000):
        rows = []
        seen: set[str] = set()
        for _ in range(rng.randint(0, 25)):
            path = f"{rng.choice(dirs)}/{rng.choice(names)}" if rng.random() > 0.1 else rng.choice(names)
            if path in seen:
                continue
            seen.add(path)
            rows.append((path, rng.choice([0.0, 0.5, 1.0, rng.random()]), rng.random() < 0.3))
        query = rng.choice(["engine.py", "src/a.py", "query", "authorize_review", ""])
        scopes = tuple(rng.sample(["src", "tests/*", ".", "lib/x/auth.go", "nope"], rng.randint(0, 2)))
        candidates = [Candidate(path=p, score=s, features={"has_definitions": 1.0 if d else 0.0}) for p, s, d in rows]
        got = [c.path for c in HeuristicRanker().rank(ContextQuery(query, 10, scopes), candidates)]
        if got != _legacy_order(query, scopes, rows):
            divergences += 1
    assert divergences == 0
    "case: test_ranker_registry_names_and_unknown"
    assert get_ranker().name == "heuristic"
    assert get_ranker("feature-v1").name == "feature-v1"
    with pytest.raises(KeyError):
        get_ranker("nope")
    "case: test_feature_ranker_rewards_query_term_coverage"
    query = ContextQuery("where is review authorization enforced", 5)
    candidates = [
        Candidate("src/util/strings.py", 1.0),
        Candidate("src/review/authorization.py", 0.9, symbols=("authorize_review",)),
    ]
    assert FeatureWeightedRanker().rank(query, candidates)[0].path == "src/review/authorization.py"
    "case: test_sufficiency_flags_undefined_query_identifier"
    query = ContextQuery("where is authorize_review called", 5)
    ranked = [Candidate("src/review/routes.py", 2.0), Candidate("src/review/jwks.py", 1.0)]
    s = assess_sufficiency(query, ranked)
    assert not s.sufficient
    assert [(m.kind, m.target) for m in s.missing] == [("definition", "authorize_review")]
    defined = [Candidate("src/review/auth.py", 3.0, symbols=("authorize_review",)), *ranked]
    assert assess_sufficiency(query, defined).components["symbol_coverage"] == 1.0
    "case: test_sufficiency_empty_is_zero"
    s = assess_sufficiency(ContextQuery("anything"), [])
    assert s.score == 0.0 and (not s.sufficient) and (s.missing[0].kind == "candidates")


def test_context_engine_execution_contract() -> None:
    """case: test_engine_shadow_proposes_but_never_changes_the_answer"""
    calls, expanders = _expander_calls()
    engine = ContextEngine(expanders=expanders, mode="shadow")
    head = [Candidate(f"src/f{i}.py", 1.0 - i / 10) for i in range(3)]
    result = engine.resolve(ContextQuery("where is authorize_review", 3), head, ranked=True)
    assert [c.path for c in result.candidates] == [c.path for c in head]
    assert result.proposed == [ExpansionAction("follow_symbol", "authorize_review")]
    assert result.taken == [] and calls == [] and (result.stop_reason == "shadow")
    assert {e.kind for e in result.decisions} == {"context.sufficiency", "context.expand", "context.stop"}
    "case: test_engine_enforce_follows_symbol_inside_limit_and_keeps_head"
    calls, expanders = _expander_calls()
    engine = ContextEngine(expanders=expanders, mode="enforce")
    head = [Candidate(f"src/f{i}.py", 1.0 - i / 10) for i in range(3)]
    result = engine.resolve(ContextQuery("where is authorize_review", 3), head, PROFILES["normal"], ranked=True)
    paths = [c.path for c in result.candidates]
    assert len(paths) == 3 and paths[:2] == ["src/f0.py", "src/f1.py"]
    assert "src/review/auth.py" in paths
    assert result.sufficiency.components["symbol_coverage"] == 1.0
    assert len(calls) == 1 and result.rounds == 1
    "case: test_engine_respects_zero_round_budget"
    calls, expanders = _expander_calls()
    engine = ContextEngine(expanders=expanders, mode="enforce")
    result = engine.resolve(
        ContextQuery("where is authorize_review", 3), [Candidate("a.py", 1.0)], PROFILES["fast"], ranked=True
    )
    assert calls == [] and result.stop_reason == "round_budget"
    "case: test_merge_preserving_head_never_exceeds_limit"
    head = [Candidate(f"h{i}", 1.0) for i in range(4)]
    added = [Candidate("x", 0.1), Candidate("y", 0.1), Candidate("z", 0.1)]
    merged = merge_preserving_head(head, added, limit=4)
    assert [c.path for c in merged] == ["h0", "h1", "x", "y"]
    assert [c.path for c in merge_preserving_head(head[:2], added, limit=4)] == ["h0", "h1", "x", "y"]
    assert [c.path for c in merge_preserving_head(head, [Candidate("h0", 1.0)], limit=4)] == [c.path for c in head]


def test_context_engine_budget_contract() -> None:
    """case: test_infer_budget"""
    for tool, args, depth, reason in [
        ("code_search", {"query": "ToolCallRuntime"}, "fast", "single_identifier"),
        ("code_search", {"query": "how does retry work"}, "normal", "natural_language_query"),
        ("code_search", {"query": "security audit of review auth"}, "deep", "deep_intent_terms"),
        ("read", {"path": "x"}, "fast", "non_retrieval_tool"),
        ("code_search", {"query": "x", "budget": "deep"}, "deep", "explicit"),
    ]:
        budget = infer_budget(tool, args)
        assert (budget.depth, budget.reason) == (depth, reason)
    "case: test_explicit_budget_mapping_is_clamped"
    budget = infer_budget("code_search", {"query": "q", "budget": {"depth": "fast", "max_rounds": 99}})
    assert budget.max_rounds == 5 and (not budget.inferred)


def _expander_calls() -> tuple[list[ExpansionAction], dict[str, object]]:
    calls: list[ExpansionAction] = []

    def follow(action: ExpansionAction, _query: ContextQuery) -> list[Candidate]:
        calls.append(action)
        return [Candidate("src/review/auth.py", 0.1, symbols=(action.target,))]

    return (calls, {"follow_symbol": follow})


def test_engine_records_decisions_on_the_active_trace(tmp_path) -> None:
    from lemoncrow.core.foundation.runtime_trace import TraceStore

    store = TraceStore(tmp_path)
    with open_trace(trace_id=None, writer="runtime", tool="code_search", mode="raw", store=store) as trace:
        ContextEngine(mode="shadow").resolve(ContextQuery("where is authorize_review", 3), [Candidate("a.py", 1.0)])
    kinds = [d["kind"] for d in trace.decisions]
    assert kinds[0] == "context.sufficiency" and kinds[-1] == "context.stop"

"""Swappable rankers.

``heuristic`` reproduces the production server-index ordering exactly
(definitions first, then exact path/basename matches, then score, then path;
explicit scopes reserve two leading slots).  Any other ranker is a candidate
that is evaluated offline by trace replay before it can serve traffic.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

from lemoncrow.core.capabilities.context_engine.candidates import Candidate, ContextQuery, candidate_terms


@runtime_checkable
class Ranker(Protocol):
    name: str

    def rank(self, query: ContextQuery, candidates: Sequence[Candidate]) -> list[Candidate]: ...


def _in_scope(path: str, scopes: Sequence[str]) -> bool:
    return any(
        scope == "." or path == scope or path.startswith(scope.rstrip("/") + "/") or fnmatch.fnmatchcase(path, scope)
        for scope in scopes
    )


def _reserve_scoped(ranked: list[Candidate], scopes: Sequence[str]) -> list[Candidate]:
    if not scopes:
        return ranked
    preferred = [c for c in ranked if _in_scope(c.path, scopes)][:2]
    reserved = {c.path for c in preferred}
    return [*preferred, *(c for c in ranked if c.path not in reserved)]


class HeuristicRanker:
    """Production ordering of the server index (byte-for-byte)."""

    name = "heuristic"

    def rank(self, query: ContextQuery, candidates: Sequence[Candidate]) -> list[Candidate]:
        text = query.text
        ranked = sorted(
            candidates,
            key=lambda c: (
                not bool(c.feature("has_definitions")),
                c.path != text and c.path.rsplit("/", 1)[-1] != text,
                -c.score,
                c.path,
            ),
        )
        return _reserve_scoped(ranked, query.scopes)


class FeatureWeightedRanker:
    """Linear re-scoring over recorded features — a replay/eval candidate.

    Adds query-term coverage of path and symbols, so natural-language queries
    are less dominated by the definitions-first rule.
    """

    name = "feature-v1"

    def __init__(
        self,
        *,
        w_score: float = 1.0,
        w_definitions: float = 0.6,
        w_exact: float = 1.5,
        w_terms: float = 0.8,
        w_related: float = -0.2,
    ) -> None:
        self.w_score = w_score
        self.w_definitions = w_definitions
        self.w_exact = w_exact
        self.w_terms = w_terms
        self.w_related = w_related

    def _value(self, query: ContextQuery, terms: tuple[str, ...], candidate: Candidate, top_score: float) -> float:
        path = candidate.path.lower()
        symbols = {s.lower() for s in candidate.symbols}
        coverage = sum(1 for t in terms if t in path or any(t in s for s in symbols)) / len(terms) if terms else 0.0
        exact = candidate.path == query.text or candidate.path.rsplit("/", 1)[-1] == query.text
        normalized = candidate.score / top_score if top_score > 0 else 0.0
        return (
            self.w_score * normalized
            + self.w_definitions * candidate.feature("has_definitions")
            + self.w_exact * float(exact)
            + self.w_terms * coverage
            + self.w_related * candidate.feature("related")
        )

    def rank(self, query: ContextQuery, candidates: Sequence[Candidate]) -> list[Candidate]:
        terms = candidate_terms(query.text)
        top = max((c.score for c in candidates), default=0.0)
        ranked = sorted(candidates, key=lambda c: (-self._value(query, terms, c, top), c.path))
        return _reserve_scoped(ranked, query.scopes)


_RANKERS: dict[str, Callable[[], Ranker]] = {
    HeuristicRanker.name: HeuristicRanker,
    FeatureWeightedRanker.name: FeatureWeightedRanker,
}


def ranker_names() -> tuple[str, ...]:
    return tuple(_RANKERS)


def get_ranker(name: str | None = None) -> Ranker:
    factory = _RANKERS.get((name or HeuristicRanker.name).strip().lower())
    if factory is None:
        raise KeyError(f"unknown ranker {name!r}; known: {', '.join(_RANKERS)}")
    return factory()


__all__ = ["FeatureWeightedRanker", "HeuristicRanker", "Ranker", "get_ranker", "ranker_names"]

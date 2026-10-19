"""Evidence-based sufficiency: is the ranked context enough to act on?

No model call.  The score combines observable signals:

    S = 0.35·ranking_confidence + 0.30·query_term_coverage + 0.20·symbol_coverage
      + 0.10·reference_closure + 0.05·implementation_test_pairing - 0.20·ambiguity

clamped to [0, 1].  ``missing_signals`` name what further retrieval would
supply (an undefined query identifier, relations of an ambiguous head), which
is what the engine's bounded expansion follows.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from lemoncrow.core.capabilities.context_engine.candidates import Candidate, ContextQuery, candidate_terms

SUFFICIENCY_THRESHOLD = 0.6
_TOP_K = 3
_IDENT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:[A-Z_][A-Za-z0-9_]*|\(\)))\b|\b([a-z]+_[a-z0-9_]+)\b")
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_[^/]+$|_test\.[a-z]+$|\.spec\.[a-z]+$")

SignalKind = Literal["definition", "relations", "candidates"]


@dataclass(frozen=True, slots=True)
class MissingSignal:
    kind: SignalKind
    target: str = ""

    def to_payload(self) -> dict[str, str]:
        return {"kind": self.kind, "target": self.target}


@dataclass(frozen=True, slots=True)
class Sufficiency:
    score: float
    sufficient: bool
    components: dict[str, float]
    missing: tuple[MissingSignal, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "sufficient": self.sufficient,
            "components": self.components,
            "missing": [m.to_payload() for m in self.missing],
        }


def query_identifiers(text: str) -> tuple[str, ...]:
    """Code-shaped identifiers in a query (CamelCase, snake_case, call())."""
    out: list[str] = []
    for match in _IDENT.finditer(text or ""):
        name = (match.group(1) or match.group(2) or "").removesuffix("()")
        is_code = "_" in name or any(ch.isupper() for ch in name[1:]) or (match.group(1) or "").endswith("()")
        if name and is_code and name not in out:
            out.append(name)
    if not out and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", (text or "").strip()):
        out.append(text.strip().rsplit(".", 1)[-1])
    return tuple(out[:8])


def _ranking_confidence(ranked: Sequence[Candidate]) -> float:
    if not ranked:
        return 0.0
    if len(ranked) == 1:
        return 0.7
    first, second = ranked[0].score, ranked[1].score
    if first <= 0:
        return 0.3
    margin = max(0.0, min(1.0, (first - second) / abs(first)))
    return round(0.4 + 0.6 * margin, 6)


def _ambiguity(ranked: Sequence[Candidate]) -> float:
    head = ranked[:5]
    if len(head) < 3 or head[0].score <= 0:
        return 0.0
    close = sum(1 for c in head[1:] if head[0].score - c.score <= 0.1 * abs(head[0].score))
    return round(close / (len(head) - 1), 6)


def assess_sufficiency(query: ContextQuery, ranked: Sequence[Candidate]) -> Sufficiency:
    if not ranked:
        return Sufficiency(
            score=0.0,
            sufficient=False,
            components={"candidates": 0.0},
            missing=(MissingSignal("candidates"),),
        )
    top = list(ranked[:_TOP_K])
    terms = candidate_terms(query.text)
    haystack = [(c.path.lower(), {s.lower() for s in c.symbols}) for c in top]
    covered = [t for t in terms if any(t in path or any(t in s for s in syms) for path, syms in haystack)]
    term_coverage = len(covered) / len(terms) if terms else 1.0

    identifiers = query_identifiers(query.text)
    defined = {s for c in ranked[: max(_TOP_K, 5)] for s in c.symbols}
    missing_defs = [name for name in identifiers if name not in defined]
    symbol_coverage = 1.0 - (len(missing_defs) / len(identifiers)) if identifiers else 1.0

    closure = sum(1 for c in top if c.feature("related") > 0 or c.feature("relation_count") > 0) / len(top)
    has_test = any(_TEST_PATH.search(c.path) for c in ranked[:8])
    has_impl = any(not _TEST_PATH.search(c.path) for c in top)
    pairing = 1.0 if has_test and has_impl else 0.0
    confidence = _ranking_confidence(ranked)
    ambiguity = _ambiguity(ranked)

    raw = (
        0.35 * confidence
        + 0.30 * term_coverage
        + 0.20 * symbol_coverage
        + 0.10 * closure
        + 0.05 * pairing
        - 0.20 * ambiguity
    )
    score = round(max(0.0, min(1.0, raw)), 6)
    missing: list[MissingSignal] = [MissingSignal("definition", name) for name in missing_defs]
    if ambiguity >= 0.5 and closure == 0.0:
        missing.append(MissingSignal("relations", ranked[0].path))
    return Sufficiency(
        score=score,
        sufficient=score >= SUFFICIENCY_THRESHOLD and not missing_defs,
        components={
            "ranking_confidence": confidence,
            "query_term_coverage": round(term_coverage, 6),
            "symbol_coverage": round(symbol_coverage, 6),
            "reference_closure": round(closure, 6),
            "implementation_test_pairing": pairing,
            "ambiguity": ambiguity,
        },
        missing=tuple(missing),
    )


__all__ = ["SUFFICIENCY_THRESHOLD", "MissingSignal", "Sufficiency", "assess_sufficiency", "query_identifiers"]

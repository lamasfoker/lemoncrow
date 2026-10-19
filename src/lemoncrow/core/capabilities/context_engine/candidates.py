"""Retrieval candidates and queries, independent of any index implementation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from lemoncrow.core.foundation.runtime_trace import TraceCandidate

_TERM_SPLIT = re.compile(r"[^A-Za-z0-9_]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_STOP = frozenset(
    {
        "the", "and", "for", "with", "where", "what", "how", "why", "when", "which", "who",
        "does", "did", "this", "that", "from", "into", "are", "was", "not", "all", "any",
        "find", "show", "get", "set", "use", "used", "uses", "code", "file", "files", "function",
        "class", "method", "implemented", "implementation", "defined", "handle", "handled",
    }
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class ContextQuery:
    text: str
    limit: int = 10
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Candidate:
    """One ranked unit of evidence (a file, optionally a line range).

    ``features`` are numeric ranking signals; ``symbols`` are definition names
    the index attributes to this candidate (used for symbol coverage).
    """

    path: str
    score: float
    channel: str = ""
    start_line: int | None = None
    end_line: int | None = None
    symbols: tuple[str, ...] = ()
    features: Mapping[str, float] = field(default_factory=dict)
    payload: Any = None

    def feature(self, name: str, default: float = 0.0) -> float:
        try:
            return float(self.features.get(name, default))
        except (TypeError, ValueError):
            return default

    def to_trace(self, rank: int) -> TraceCandidate:
        features = dict(self.features)
        return TraceCandidate(
            path=self.path,
            score=self.score,
            rank=rank,
            start_line=self.start_line,
            end_line=self.end_line,
            features=features,
            symbols=self.symbols,
        )

    @classmethod
    def from_trace(cls, row: Mapping[str, Any]) -> Candidate:
        raw_features = row.get("features")
        features = (
            {str(k): float(v) for k, v in raw_features.items() if isinstance(v, int | float)}
            if isinstance(raw_features, Mapping)
            else {}
        )
        return cls(
            path=str(row.get("path") or ""),
            score=float(row.get("score") or 0.0),
            start_line=row.get("start_line") if isinstance(row.get("start_line"), int) else None,
            end_line=row.get("end_line") if isinstance(row.get("end_line"), int) else None,
            symbols=tuple(str(s) for s in row.get("symbols") or () if isinstance(s, str)),
            features=features,
        )


def candidate_terms(text: str) -> tuple[str, ...]:
    """Distinct lowercase content terms of a query (camel/snake aware)."""
    out: list[str] = []
    for token in _TERM_SPLIT.split(text or ""):
        if not token:
            continue
        parts = [token, *_CAMEL.sub(" ", token).replace("_", " ").split()]
        for part in parts:
            lowered = part.lower()
            if len(lowered) >= 3 and lowered not in _STOP and not lowered.isdigit() and lowered not in out:
                out.append(lowered)
    return tuple(out[:16])


__all__ = ["Candidate", "ContextQuery", "candidate_terms"]

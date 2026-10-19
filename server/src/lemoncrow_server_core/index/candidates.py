"""Server index hits <-> context-engine candidates, and trace recording."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from lemoncrow.core.capabilities.context_engine import Candidate
from lemoncrow.core.foundation.runtime_trace import active_trace

if TYPE_CHECKING:
    from .query import QueryHit

__all__ = ["definition_names", "hit_candidate", "record_ranked_candidates"]

_AUX = re.compile(r"(^|/)(tests?|docs?|examples?|benchmarks?|fixtures?)(/|$)", re.IGNORECASE)
_TRACE_CANDIDATES = 32


def definition_names(detail: Mapping[str, Any]) -> tuple[str, ...]:
    definitions = detail.get("definitions")
    if not isinstance(definitions, list):
        return ()
    names: list[str] = []
    for item in definitions:
        if isinstance(item, Mapping):
            name = str(item.get("name") or "")
            if name and name not in names:
                names.append(name)
    return tuple(names[:16])


def _first_range(detail: Mapping[str, Any]) -> tuple[int | None, int | None]:
    definitions = detail.get("definitions")
    if isinstance(definitions, list):
        for item in definitions:
            if not isinstance(item, Mapping):
                continue
            start = item.get("start") if isinstance(item.get("start"), int) else item.get("line")
            end = item.get("end")
            if isinstance(start, int):
                return start, end if isinstance(end, int) and end >= start else None
    return None, None


def hit_candidate(hit: QueryHit, *, query: str) -> Candidate:
    """Project one index hit to a ranker candidate; ``payload`` keeps the hit."""
    detail = hit.detail
    symbols = definition_names(detail)
    start, end = _first_range(detail)
    exact = hit.path == query or hit.path.rsplit("/", 1)[-1] == query
    return Candidate(
        path=hit.path,
        score=hit.score,
        channel="relation" if detail.get("related_via") else ("symbol" if symbols else "lexical"),
        start_line=start,
        end_line=end,
        symbols=symbols,
        features={
            "has_definitions": 1.0 if detail.get("definitions") else 0.0,
            "exact_path": 1.0 if exact else 0.0,
            "related": 1.0 if detail.get("related_via") else 0.0,
            "auxiliary_path": 1.0 if _AUX.search(hit.path) else 0.0,
            "definition_count": float(len(symbols)),
            "line_count": float(hit.line_count),
        },
        payload=hit,
    )


def record_ranked_candidates(
    *, query: str, ordered: Sequence[Candidate], limit: int, ranker: str, scopes: Sequence[str] = ()
) -> None:
    """Attach the ranked candidate set to the active runtime trace (if any)."""
    trace = active_trace()
    # First retrieval of a call wins: expansion sub-queries must not replace
    # the candidate set the ranker actually ordered for the agent's query.
    if trace is None or trace.retrieval is not None:
        return
    trace.record_retrieval(
        query=query,
        candidates=[c.to_trace(rank) for rank, c in enumerate(ordered[:_TRACE_CANDIDATES])],
        selected=[c.path for c in ordered[:limit]],
        channel="server_index",
        ranker=ranker,
        limit=limit,
        extra={"scopes": list(scopes)} if scopes else None,
    )

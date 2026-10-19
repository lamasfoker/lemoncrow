"""Bounded, revision-scoped soft caching for the thin client.

This is deliberately *not* a local code index. The client never derives
symbols, embeddings, graph edges or rankings. It remembers only complete
server responses that the server has explicitly marked reusable for the exact
session/view/revision/tool/arguments tuple. Every cache hit still goes to the
server with the opaque validator first, so authentication, authorization,
audit and accounting remain server-side; the server can then answer "reuse
what you already have" without materializing the workspace or executing the
index query again.

The budget is an eviction ceiling, not a reservation. By default it is 10% of
currently available memory, bounded by the process/container headroom and a
4 GiB automatic ceiling. A 16 GiB machine with 16 GiB genuinely available
therefore gets about 1.6 GiB of potential cache, but a fresh process consumes
almost none of it until useful responses arrive.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from .memory_budget import available_memory_bytes, cache_budget_bytes

_MIB: Final[int] = 1024 * 1024

__all__ = [
    "CACHEABLE_TOOLS",
    "CacheKey",
    "CacheStats",
    "CachedResult",
    "ResolutionCache",
    "available_memory_bytes",
    "cache_budget_bytes",
    "make_cache_key",
]

_MAX_ENTRIES: Final[int] = 16_384
_MAX_SINGLE_ENTRY: Final[int] = 64 * _MIB

#: Only deterministic, revision-bound code-resolution surfaces participate.
#: Dynamic/user-state tools (memory, web_fetch, verify, context, etc.) remain
#: uncached even if their current implementation happens to be deterministic.
CACHEABLE_TOOLS: Final[frozenset[str]] = frozenset({"code_search", "read", "relations", "search"})


@dataclass(frozen=True, slots=True)
class CacheKey:
    session_id: str
    view_id: str
    view_revision: int
    tool: str
    arguments_digest: str


@dataclass(frozen=True, slots=True)
class CachedResult:
    payload: Mapping[str, Any]
    validator: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class CacheStats:
    budget_bytes: int
    bytes_used: int
    entries: int
    hits: int
    misses: int
    evictions: int


class ResolutionCache:
    """Memory-only LRU of server-validated tool responses."""

    __slots__ = ("_budget", "_bytes", "_entries", "_evictions", "_hits", "_items", "_misses")

    def __init__(self, budget_bytes: int, *, max_entries: int = _MAX_ENTRIES) -> None:
        self._budget = max(0, int(budget_bytes))
        self._entries = max(1, int(max_entries))
        self._items: OrderedDict[CacheKey, CachedResult] = OrderedDict()
        self._bytes = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    @property
    def enabled(self) -> bool:
        return self._budget > 0

    @property
    def stats(self) -> CacheStats:
        return CacheStats(
            budget_bytes=self._budget,
            bytes_used=self._bytes,
            entries=len(self._items),
            hits=self._hits,
            misses=self._misses,
            evictions=self._evictions,
        )

    def get(self, key: CacheKey | None) -> CachedResult | None:
        if key is None or not self.enabled:
            return None
        entry = self._items.get(key)
        if entry is None:
            self._misses += 1
            return None
        self._items.move_to_end(key)
        self._hits += 1
        return entry

    def put(self, key: CacheKey | None, payload: Mapping[str, Any], validator: str) -> None:
        if key is None or not self.enabled or not validator:
            return
        snapshot: dict[str, Any] = {str(name): value for name, value in payload.items()}
        size = _wire_size(snapshot)
        if size <= 0:
            return
        single_limit = min(_MAX_SINGLE_ENTRY, max(1, self._budget // 4))
        if size > single_limit:
            return
        previous = self._items.pop(key, None)
        if previous is not None:
            self._bytes -= previous.size_bytes
        self._items[key] = CachedResult(payload=snapshot, validator=validator, size_bytes=size)
        self._bytes += size
        self._trim()

    def prune_scope(self, session_id: str, view_id: str, view_revision: int) -> None:
        """Drop entries that cannot possibly match the client's current view."""
        stale = [
            key
            for key in self._items
            if key.session_id != session_id or key.view_id != view_id or key.view_revision != view_revision
        ]
        for key in stale:
            self._drop(key, eviction=False)

    def clear(self) -> None:
        self._items.clear()
        self._bytes = 0

    def _trim(self) -> None:
        while self._items and (self._bytes > self._budget or len(self._items) > self._entries):
            key = next(iter(self._items))
            self._drop(key, eviction=True)

    def _drop(self, key: CacheKey, *, eviction: bool) -> None:
        entry = self._items.pop(key, None)
        if entry is None:
            return
        self._bytes = max(0, self._bytes - entry.size_bytes)
        if eviction:
            self._evictions += 1


def make_cache_key(
    *,
    session_id: str,
    view_id: str,
    view_revision: int,
    tool: str,
    arguments: Mapping[str, Any],
) -> CacheKey | None:
    if tool not in CACHEABLE_TOOLS or not session_id or not view_id:
        return None
    try:
        canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return None
    digest = hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()
    return CacheKey(
        session_id=session_id,
        view_id=view_id,
        view_revision=view_revision,
        tool=tool,
        arguments_digest=digest,
    )


def _wire_size(payload: Mapping[str, Any]) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        return 0

"""Semantic retrieval seam: the port is public, the implementation is private.

Public server core owns the :class:`~.contracts.SemanticIndex` contract, the
readiness/degradation wiring that consumes it, and
:class:`UnavailableSemanticIndex`. Semantic unit derivation, the
generation/overlay ANN and query-time semantic scoring are private advanced
code intelligence supplied by the private server distribution
(``lemoncrow_server.semantic``).

Without it, every semantic layer reports ``IndexLayerState.UNAVAILABLE`` and a
``semantic`` query returns no hits flagged
``semantic:semantic_engine_unavailable``: an explicit degraded answer, never an
empty answer that claims to be complete. Lexical, symbol and relations queries
are unaffected.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING, Any, Final

from .contracts import IndexLayerState, IndexLayerStatus, SemanticIndex, SemanticSearchResult

if TYPE_CHECKING:
    from .layers import LayerSources

__all__ = [
    "SEMANTIC_UNAVAILABLE_REASON",
    "UnavailableSemanticIndex",
    "create_semantic_index",
    "private_semantic_available",
]

#: The one module public code names; the private distribution implements it.
_PRIVATE_SEMANTIC_MODULE: Final[str] = "lemoncrow_server.semantic"
SEMANTIC_UNAVAILABLE_REASON: Final[str] = "semantic_engine_unavailable"

_UNAVAILABLE: Final[IndexLayerStatus] = IndexLayerStatus(
    state=IndexLayerState.UNAVAILABLE,
    reason=SEMANTIC_UNAVAILABLE_REASON,
    covered_paths=0,
    at_view_revision=0,
)


class UnavailableSemanticIndex:
    """The semantic layer of a server without the private semantic engine.

    Stateless and never ready: builds are no-ops, searches return nothing, and
    the status names why so degradation reports say "unavailable", not "cold".
    """

    __slots__ = ()

    def status(self, org_id: str, view_id: str) -> IndexLayerStatus:
        _ = org_id, view_id
        return _UNAVAILABLE

    def note_revision(self, org_id: str, view_id: str, view_revision: int) -> None:
        _ = org_id, view_id, view_revision

    def refresh(
        self,
        org_id: str,
        view_id: str,
        *,
        budget: int | None = None,
        wait: bool = False,
        cancel: Callable[[], bool] | None = None,
        defer_cold: bool = False,
    ) -> IndexLayerStatus:
        _ = org_id, view_id, budget, wait, cancel, defer_cold
        return _UNAVAILABLE

    def forget(self, org_id: str, view_id: str) -> None:
        _ = org_id, view_id

    def forget_view(self, view_id: str) -> None:
        _ = view_id

    def mark_ready(self, org_id: str, view_id: str, *, view_revision: int, covered_paths: int) -> None:
        _ = org_id, view_id, view_revision, covered_paths

    def search(self, org_id: str, view_id: str, query: str, limit: int) -> SemanticSearchResult:
        _ = org_id, view_id, query, limit
        return SemanticSearchResult((), 0, 0)

    def scan(
        self, org_id: str, view_id: str, digests: Sequence[str], query: str, limit: int
    ) -> tuple[tuple[str, float], ...]:
        _ = org_id, view_id, digests, query, limit
        return ()

    def export_generation(self, org_id: str, view_id: str) -> Mapping[str, Any] | None:
        _ = org_id, view_id
        return None

    def restore_generation(self, org_id: str, view_id: str, payload: Mapping[str, Any]) -> bool:
        _ = org_id, view_id, payload
        return False

    def release_idle(self) -> None:
        return None


def _private_semantic_module() -> ModuleType | None:
    try:
        return import_module(_PRIVATE_SEMANTIC_MODULE)
    except ModuleNotFoundError as exc:
        # Only the private distribution being absent means "unavailable"; a
        # broken private install must fail loudly, not degrade silently.
        if exc.name in {"lemoncrow_server", _PRIVATE_SEMANTIC_MODULE}:
            return None
        raise


def private_semantic_available() -> bool:
    """Whether the private semantic engine is installed in this interpreter."""
    return _private_semantic_module() is not None


def create_semantic_index(sources: LayerSources, *, inline_rebuild_budget: int) -> SemanticIndex:
    """The private semantic layer when installed, else :class:`UnavailableSemanticIndex`."""
    module = _private_semantic_module()
    if module is None:
        return UnavailableSemanticIndex()
    index: SemanticIndex = module.create_semantic_index(sources, inline_rebuild_budget=inline_rebuild_budget)
    return index

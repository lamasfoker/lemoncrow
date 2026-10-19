"""Public contracts and compatibility surface for code intelligence.

Advanced implementation modules are supplied only by the private enterprise
runtime. This public package intentionally contains neutral DTOs, rendering and
evidence helpers. Importing it never requires the private provider.
"""

from __future__ import annotations

from pkgutil import extend_path
from typing import Any

# In the private monorepo / installed Pro product the enterprise distribution
# contributes advanced modules to this package path. OSS-only installs have only
# the public directory and remain fully importable.
__path__ = extend_path(__path__, __name__)

from lemoncrow.pro.capabilities.code_context.budget import BudgetPacker
from lemoncrow.pro.capabilities.code_context.evidence_resolution import (
    EvidenceResolutionAction,
    EvidenceResolutionMode,
    EvidenceResolutionProposal,
    normalize_evidence_resolution_mode,
    propose_evidence_resolution,
)
from lemoncrow.pro.capabilities.code_context.evidence_state import (
    EvidenceState,
    EvidenceStatus,
    evaluate_explore_evidence,
)
from lemoncrow.pro.capabilities.code_context.models import (
    ContextPack,
    IndexStats,
    SymbolRecord,
    TextMatch,
)


def __getattr__(name: str) -> Any:
    """Private compatibility exports without making OSS import the engine."""

    if name in {"CodeContextEngine", "CodeRetriever"}:
        from lemoncrow.core.capabilities.code_intelligence_provider import (
            advanced_code_intelligence_helper,
        )

        return advanced_code_intelligence_helper("engine_class")
    if name == "RetrievalCache":
        from lemoncrow.core.capabilities.code_intelligence_provider import (
            advanced_code_intelligence_helper,
        )

        return advanced_code_intelligence_helper("retrieval_cache_class")
    raise AttributeError(name)


__all__ = [
    "BudgetPacker",
    "ContextPack",
    "EvidenceResolutionAction",
    "EvidenceResolutionMode",
    "EvidenceResolutionProposal",
    "EvidenceState",
    "EvidenceStatus",
    "IndexStats",
    "SymbolRecord",
    "TextMatch",
    "evaluate_explore_evidence",
    "normalize_evidence_resolution_mode",
    "propose_evidence_resolution",
]

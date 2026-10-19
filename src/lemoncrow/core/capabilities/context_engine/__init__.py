"""Context engine: candidates -> ranker -> sufficiency -> bounded expansion.

The public surface is deliberately small (:class:`ContextEngine.resolve`); the
model-facing ``code_search`` contract (inline best source, pointers, related
symbols, candidate files) is unchanged.  What changes underneath is that
ranking is a swappable :class:`Ranker`, every answer carries a
:class:`Sufficiency` assessment, and expansion is driven by missing signals
within a :class:`~lemoncrow.core.foundation.runtime_budget.RuntimeBudget`.
"""

from lemoncrow.core.capabilities.context_engine.candidates import Candidate, ContextQuery, candidate_terms
from lemoncrow.core.capabilities.context_engine.engine import (
    ContextEngine,
    ContextResult,
    Expander,
    ExpansionAction,
    context_engine_mode,
)
from lemoncrow.core.capabilities.context_engine.ranking import (
    FeatureWeightedRanker,
    HeuristicRanker,
    Ranker,
    get_ranker,
    ranker_names,
)
from lemoncrow.core.capabilities.context_engine.sufficiency import (
    SUFFICIENCY_THRESHOLD,
    MissingSignal,
    Sufficiency,
    assess_sufficiency,
)

__all__ = [
    "SUFFICIENCY_THRESHOLD",
    "Candidate",
    "ContextEngine",
    "ContextQuery",
    "ContextResult",
    "Expander",
    "ExpansionAction",
    "FeatureWeightedRanker",
    "HeuristicRanker",
    "MissingSignal",
    "Ranker",
    "Sufficiency",
    "assess_sufficiency",
    "candidate_terms",
    "context_engine_mode",
    "get_ranker",
    "ranker_names",
]

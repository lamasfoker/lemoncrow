"""Review analytics: policy effectiveness, review quality and cost (Phase 13 items 1-8, P13-ANALYTICS).

Read-only and advisory. Nothing in readiness, landing, auto-approval, priority
or the Inbox imports this package, and every store access goes through
:class:`~.facts.ReadOnlyStore`, so no analytics value can become a merge gate.
The evaluation harness (item 9) is :mod:`..evaluation` (P13-EVAL).
"""

from lemoncrow.pro.capabilities.review.analytics.report import AnalyticsReport
from lemoncrow.pro.capabilities.review.analytics.scope import (
    AnalyticsQueryError,
    AnalyticsScope,
    parse_drill_key,
    parse_scope,
)
from lemoncrow.pro.capabilities.review.analytics.service import RepositorySource, Viewer, analyze

__all__ = [
    "AnalyticsQueryError",
    "AnalyticsReport",
    "AnalyticsScope",
    "RepositorySource",
    "Viewer",
    "analyze",
    "parse_drill_key",
    "parse_scope",
]

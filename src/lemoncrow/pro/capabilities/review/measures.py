"""Evidence-backed review measures (spec §13), derived on read.

Nothing here is stored: every number comes from records the Review already
keeps (guidance rows, decision events, the bounded synthesis cache), so the
measures cannot drift from what the reviewer saw and need no retention of
their own. They record baselines; no threshold is invented here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.decisions import DecisionState
    from lemoncrow.pro.capabilities.review.guidance import GuidanceView
    from lemoncrow.pro.capabilities.review.session_models import ReviewDecisionEvent, ReviewIntelligenceRun


def _host(producer: str) -> str:
    return producer.split(":", 1)[0] or "other"


def _payload(row: ReviewIntelligenceRun) -> Mapping[str, Any]:
    try:
        value = json.loads(row.result_json or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, Mapping) else {}


def review_measures(
    *,
    target_count: int,
    author_explained: int,
    guidance: GuidanceView,
    decision_states: Sequence[DecisionState],
    decision_events: Sequence[ReviewDecisionEvent],
    syntheses: Sequence[ReviewIntelligenceRun],
) -> dict[str, Any]:
    """Guidance coverage by adapter, decision churn, and Brief intelligence spend."""

    adapters: dict[str, dict[str, int]] = {}
    for record in guidance.records:
        bucket = adapters.setdefault(_host(record.producer_host), {"records": 0, "conflicts": 0})
        bucket["records"] += 1
    for conflict in guidance.conflicts:
        for host in {_host(definition.producer) for definition in conflict.definitions}:
            adapters.setdefault(host, {"records": 0, "conflicts": 0})["conflicts"] += 1

    active = [state for state in decision_states if not state.withdrawn]
    settled = [row for row in syntheses if row.status in ("ready", "failed")]
    ready = [row for row in settled if row.status == "ready"]
    newest_ready = max(ready, key=lambda row: (row.created_at, row.id), default=None)
    explained = (
        {
            target_id
            for row in _payload(newest_ready).get("explanations") or ()
            if isinstance(row, Mapping)
            for target_id in row.get("target_ids") or ()
        }
        if newest_ready is not None
        else set()
    )
    return {
        "guidance": {
            "snapshot_has_guidance": bool(guidance.records),
            "target_coverage": {"explained": author_explained, "targets": target_count},
            "by_adapter": adapters,
        },
        "decisions": {
            "decisions": len(active),
            "accepted": sum(1 for state in active if state.accepted_option),
            "reconfirmed": sum(1 for event in decision_events if event.action == "reconfirm"),
            "needs_reconsideration": sum(1 for state in active if state.validity == "needs_reconsideration"),
        },
        "synthesis": {
            # Durable shared-runtime rows only; cache hits do not create duplicate rows/calls.
            "calls": sum(max(0, int(row.model_calls)) for row in settled),
            "ready": len(ready),
            "failed": len(settled) - len(ready),
            "reused_items": 0,
            "explained_targets": len(explained),
        },
    }


__all__ = ["review_measures"]

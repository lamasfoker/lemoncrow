"""Waiting time by owner and reviewer load, from the viewer's own Inbox rows (Phase 13 items 1-2).

The input is the P9-INBOX projection the viewer already reads, so nothing here
can show a Review the viewer cannot open. What it never does is score or rank
people:

* waiting time is grouped by the *side* that owns the next step -- reviewers,
  the author, an authoring agent, checks, or a decision -- never by person;
* team load is shown for the viewer's own teams (all teams only for a viewer
  who manages every repository in scope), as counts and waiting times, never
  per member;
* the only per-person figures are the viewer's own ("to review", "waiting on
  you").
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from lemoncrow.pro.capabilities.review.analytics.facts import SourceRef
from lemoncrow.pro.capabilities.review.analytics.report import DrillIndex, percentile
from lemoncrow.pro.capabilities.review.freshness import parse_timestamp

#: Workflow state -> the side that owns the next step.
OWNER_SIDES: Final[Mapping[str, str]] = {
    "needs_review": "reviewers",
    "waiting_on_reviewer": "reviewers",
    "waiting_on_author": "author",
    "waiting_on_checks": "checks",
    "needs_decision": "decision",
}
SIDE_LABELS: Final[Mapping[str, str]] = {
    "reviewers": "Reviewers",
    "author": "Author",
    "agent": "Authoring agent",
    "checks": "Checks",
    "decision": "A decision",
}


def _since(wire: Mapping[str, Any]) -> str:
    """When the current wait began: the oldest outstanding reason, else the attention clock, else the last update."""

    next_action = wire.get("next_action")
    reasons = next_action.get("reasons") if isinstance(next_action, Mapping) else None
    stamps = [
        str(reason.get("since") or "")
        for reason in reasons or ()
        if isinstance(reason, Mapping) and parse_timestamp(reason.get("since")) is not None
    ]
    if stamps:
        return min(stamps, key=lambda value: parse_timestamp(value) or datetime.max)
    attention = wire.get("attention")
    waiting = attention.get("waiting_since") if isinstance(attention, Mapping) else ""
    if parse_timestamp(waiting) is not None:
        return str(waiting)
    return str(wire.get("updated_at") or wire.get("created_at") or "")


def _hours(since: str, now: datetime) -> float:
    instant = parse_timestamp(since)
    return 0.0 if instant is None else max(0.0, (now - instant).total_seconds() / 3600.0)


def _side(row: Any) -> str:
    side = OWNER_SIDES.get(str(row.workflow_state), "")
    workflow = row.wire.get("workflow") if isinstance(row.wire, Mapping) else None
    if side == "author" and isinstance(workflow, Mapping) and workflow.get("rule") == "agent_turn":
        return "agent"
    return side


def reviewer_load(
    rows: Sequence[Any],
    *,
    index: DrillIndex,
    now: datetime,
    viewer_teams: frozenset[str],
    all_teams_visible: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """``(waiting_by_owner, reviewer_load)`` over open Inbox rows, each row drillable."""

    for side, label in SIDE_LABELS.items():
        index.touch(f"waiting.{side}", label=label)
    index.touch("load.waiting_on_reviewers", label="Open Reviews waiting on reviewers")
    index.touch("load.mine.to_review", label="To review (you)")
    index.touch("load.mine.waiting_on_me", label="Waiting on you")
    for team in sorted(viewer_teams):
        index.touch(f"load.team.{team}", label=team)
    for row in rows:
        if row.status != "open":
            continue
        wire = row.wire if isinstance(row.wire, Mapping) else {}
        since = _since(wire)
        hours = _hours(since, now)
        title = str(wire.get("title") or "")
        ref = SourceRef(
            row.review_id, row.repo_id, "review", row.review_id, str(wire.get("revision_id") or ""), since, title, hours
        )
        side = _side(row)
        if side:
            index.add(f"waiting.{side}", ref)
        if side == "reviewers":
            index.add("load.waiting_on_reviewers", ref)
            for team in sorted(row.team_keys):
                if all_teams_visible or team in viewer_teams:
                    index.add(f"load.team.{team}", ref, label=team)
        attention = wire.get("attention")
        mine_since = attention.get("waiting_since") if isinstance(attention, Mapping) else ""
        mine = SourceRef(
            row.review_id,
            row.repo_id,
            "review",
            row.review_id,
            ref.revision_id,
            str(mine_since or since),
            title,
            _hours(str(mine_since or since), now),
        )
        if "to_review" in row.views:
            index.add("load.mine.to_review", mine)
        if "waiting_on_me" in row.views:
            index.add("load.mine.waiting_on_me", mine)

    def timed(key: str, **extra: Any) -> dict[str, Any]:
        values = [ref.value for ref in index.refs(key)]
        return index.row(
            key,
            **extra,
            median_hours=percentile(values, 0.5),
            max_hours=None if not values else round(max(values), 3),
        )

    waiting = [timed(f"waiting.{side}", name=side) for side in SIDE_LABELS]
    load = {
        "waiting_on_reviewers": timed("load.waiting_on_reviewers"),
        "by_team": sorted(
            (timed(key, name=key[len("load.team.") :]) for key in index.prefixed("load.team.")),
            key=lambda row: (-row["count"], row["key"]),
        ),
        "teams_shown": "all" if all_teams_visible else "yours",
        "mine": {
            "to_review": timed("load.mine.to_review"),
            "waiting_on_me": timed("load.mine.waiting_on_me"),
        },
        "individuals": "not_reported",
        "basis": (
            "Open Reviews in scope, from your Inbox: waiting time by the side that owns the next step and "
            "by requested team. No figure about any other person is computed."
        ),
    }
    return waiting, load


__all__ = ["OWNER_SIDES", "SIDE_LABELS", "reviewer_load"]

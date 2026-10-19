"""Revert and Fix forward from production outcomes (P15-OBS item 6).

Both are Phase 12 controlled actions (kinds ``revert`` and ``fix_forward``):
prepared, previewed and started through ``/reviews/{id}/actions`` by an
explicit human, routed exact authoring session first (P12-FIX), then the
preferred agent, then a copied prompt. Their subjects are this module's
production outcomes: a failed or rolled-back production deployment, a
contradicted prediction, and (advisory) a linked incident.

The result is a **new** Change, never a Snapshot of the merged one, linked
both ways with ``change`` relationships (``reverted_by``/``reverts``,
``fixed_forward_by``/``fixes_forward``):

* automatically, when a later Review's Snapshot is exactly attributed to the
  agent session the action was handed to; or
* explicitly (:func:`link_outcome_change`), when the prompt was copied and a
  human names the new Change.

Automatic rollback runs only when an organization automation rule enables
``post_merge.rollback`` (P12-AUTO) and a CD rollback adapter exists; this build
ships no adapter, so the decision is shown and nothing rolls back on its own.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from lemoncrow.pro.capabilities.review.outcomes.deployment_outcomes import DeploymentLedger
from lemoncrow.pro.capabilities.review.outcomes.deployments import ATTENTION_STATES, sentence
from lemoncrow.pro.capabilities.review.outcomes.incidents import review_links
from lemoncrow.pro.capabilities.review.outcomes.observation_outcomes import production_records

OUTCOME_SUBJECT_PREFIX: Final[str] = "outcome:"
LINKED_ACTIVITY: Final[str] = "outcome.linked"
RELATIONS: Final[dict[str, tuple[str, str]]] = {
    "revert": ("reverted_by", "reverts"),
    "fix_forward": ("fixed_forward_by", "fixes_forward"),
}
"""Action kind -> (relation on the merged Change, relation on the new Change)."""
# lc-debt: automatic linking looks at the 200 most recently updated Reviews of the store; upgrade path: index
# exact attributions by (host, session id) once stores hold more concurrent Changes than this.
LINK_SCAN_LIMIT: Final[int] = 200
ROLLBACK_UNAVAILABLE: Final[str] = "no CD rollback adapter is connected, so nothing rolls back automatically"


@dataclass(frozen=True, slots=True)
class ProductionOutcome:
    """One production problem of a merged Change, as an action subject."""

    id: str
    kind: str
    """``deployment_failed``, ``deployment_rolled_back``, ``contradicted`` or ``incident``."""
    title: str
    detail: str
    revision_id: str
    environment: str = ""
    observed_at: str = ""
    advisory: bool = False

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "revision_id": self.revision_id,
            "environment": self.environment,
            "observed_at": self.observed_at,
            "advisory": self.advisory,
        }


def production_outcomes(store: Any, session: Any) -> tuple[ProductionOutcome, ...]:
    """Every current production problem of one merged Review (deployments, contradictions, incidents)."""

    out: list[ProductionOutcome] = []
    records = DeploymentLedger(store).records(session.id)
    for key in sorted(records):
        record = records[key][1]
        outcome = record.outcome()
        if outcome is None or not outcome.production or outcome.state not in ATTENTION_STATES:
            continue
        out.append(
            ProductionOutcome(
                id=f"{OUTCOME_SUBJECT_PREFIX}deployment:{outcome.key}",
                kind="deployment_failed" if outcome.state == "failed" else "deployment_rolled_back",
                title=sentence(outcome),
                detail=f"environment: {record.environment}\nstate: {outcome.state}\nmerged commit: {record.merged.sha}",
                revision_id=record.revision_id,
                environment=record.environment,
                observed_at=outcome.since,
            )
        )
    for observed in sorted(production_records(store, session.id), key=lambda item: item.scope):
        for target in observed.outcomes:
            if target.state != "contradicted":
                continue
            out.append(
                ProductionOutcome(
                    id=f"{OUTCOME_SUBJECT_PREFIX}contradicted:{observed.deployment_id}:{target.key}",
                    kind="contradicted",
                    title=f"Production contradicted: {target.title or target.subject}",
                    detail=f"environment: {observed.environment}\nprediction: {target.subject}\n"
                    f"observed: {target.reason}\nmerged commit: {observed.merged_commit}",
                    revision_id=observed.revision_id,
                    environment=observed.environment,
                    observed_at=observed.contradicted_at.get(target.key, ""),
                )
            )
    for link in review_links(store, session.id):
        out.append(
            ProductionOutcome(
                id=f"{OUTCOME_SUBJECT_PREFIX}incident:{link.incident.key}",
                kind="incident",
                title=link.summary,
                detail=f"environment: {link.environment}\nreasons: {', '.join(link.reasons)}\n"
                "correlation only: this incident may be unrelated to the Change",
                revision_id=link.revision_id,
                environment=link.environment,
                observed_at=link.incident.started_at,
                advisory=True,
            )
        )
    return tuple(out)


# --------------------------------------------------------------------------- #
# Linking the new Change both ways
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class OutcomeLink:
    action_id: str
    kind: str
    review_id: str
    target_review_id: str
    certainty: str

    def to_wire(self) -> dict[str, str]:
        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "review_id": self.review_id,
            "target_review_id": self.target_review_id,
            "relation": RELATIONS[self.kind][0],
            "certainty": self.certainty,
        }


def _record_activity(
    store: Any, review_id: str, revision_id: str, *, summary: str, detail: dict[str, Any], actor: str, actor_type: str
) -> None:
    from lemoncrow.pro.capabilities.review.session_models import ReviewActivityEvent

    store.record_activity(
        ReviewActivityEvent(
            id="",
            review_id=review_id,
            revision_id=revision_id,
            kind=LINKED_ACTIVITY,  # type: ignore[arg-type]
            actor_id=actor,
            actor_type=actor_type,  # type: ignore[arg-type]
            subject_type="change",
            subject_id=f"change:{detail.get('target_review_id') or detail.get('source_review_id') or ''}",
            summary=summary[:500],
            detail_json=json.dumps(detail, sort_keys=True),
        )
    )


def link_outcome_change(
    store: Any,
    action: Any,
    target_review_id: str,
    *,
    certainty: str,
    actor_id: str,
    actor_type: str = "human",
) -> OutcomeLink:
    """Link the new Change a Revert/Fix forward produced, both ways; finish the action.

    Idempotent: relationships are upserts and the action transition accepts
    only an in-flight action (a recorded one is returned as it is).
    Raises ``ValueError`` for a non-cross-change action or an unknown target;
    a Review of another repository (a local store holds several) is unknown.
    """

    from lemoncrow.pro.capabilities.review.review_actions import (
        IN_FLIGHT_STATUSES,
        ActionConflict,
        ActionEvent,
        ReviewActionLedger,
        now_iso,
    )
    from lemoncrow.pro.capabilities.review.session_models import ReviewRelationship

    if action.kind not in RELATIONS:
        raise ValueError("only a Revert or Fix forward action links a new Change")
    target = store.get_session(target_review_id)
    source = store.get_session(action.review_id)
    if target is None or source is None or target.id == source.id or target.repo_root != source.repo_root:
        raise ValueError("the new Change must be another Review in this repository")
    forward, backward = RELATIONS[action.kind]
    metadata = json.dumps({"action_id": action.id, "certainty": certainty}, sort_keys=True)
    store.upsert_relationship(
        ReviewRelationship(
            id="",
            review_id=action.review_id,
            kind="change",
            relation=forward,
            target_review_id=target.id,
            title=str(target.title or ""),
            status="linked",
            source="review_action",
            metadata_json=metadata,
        )
    )
    store.upsert_relationship(
        ReviewRelationship(
            id="",
            review_id=target.id,
            kind="change",
            relation=backward,
            target_review_id=action.review_id,
            title=str(source.title or ""),
            status="linked",
            source="review_action",
            metadata_json=metadata,
        )
    )
    target_revision = store.latest_revision(target.id)
    label = "Revert" if action.kind == "revert" else "Fix forward"
    if action.status in IN_FLIGHT_STATUSES:
        try:
            ReviewActionLedger(store).transition(
                action.source_revision_id,
                action.id,
                status="snapshot_recorded",
                allowed_from=IN_FLIGHT_STATUSES,
                event=ActionEvent(
                    "snapshot_recorded",
                    now_iso(),
                    "human" if actor_type == "human" else "system",
                    actor_id if actor_type == "human" else "",
                    detail=f"new Change r/{target.id} linked ({certainty})",
                    revision_id="" if target_revision is None else target_revision.id,
                ),
                resulting_revision_id="" if target_revision is None else target_revision.id,
                result_certainty=certainty,
                message=f"{label}: new Change r/{target.id}",
            )
        except (ActionConflict, LookupError):
            pass
        _record_activity(
            store,
            action.review_id,
            action.source_revision_id,
            summary=f"{label} opened as a new Change r/{target.id}.",
            detail={"action_id": action.id, "kind": action.kind, "target_review_id": target.id, "certainty": certainty},
            actor=actor_id,
            actor_type=actor_type,
        )
        if target_revision is not None:
            _record_activity(
                store,
                target.id,
                target_revision.id,
                summary=f"This Change is the {label.lower()} of r/{action.review_id}.",
                detail={
                    "action_id": action.id,
                    "kind": action.kind,
                    "source_review_id": action.review_id,
                    "certainty": certainty,
                },
                actor=actor_id,
                actor_type=actor_type,
            )
    return OutcomeLink(action.id, action.kind, action.review_id, target.id, certainty)


def reconcile_outcome_actions(store: Any, session: Any, actions: Sequence[Any]) -> tuple[OutcomeLink, ...]:
    """Link in-flight Revert/Fix forward actions to a later Change exactly attributed to their agent session."""

    from lemoncrow.pro.capabilities.review.review_actions import exactly_attributed

    pending = [
        item
        for item in actions
        if item.kind in RELATIONS and item.in_flight and item.route.host and item.route.session_id
    ]
    if not pending:
        return ()
    attributed = exactly_attributed(store)
    recent = [
        item
        for item in store.list_sessions(status="", limit=LINK_SCAN_LIMIT)
        if item.id != session.id and item.repo_root == session.repo_root
    ]
    links: list[OutcomeLink] = []
    for action in pending:
        for candidate in recent:
            if str(candidate.created_at or "") < str(action.created_at or ""):
                continue
            revisions = store.list_revisions(candidate.id)
            if any(attributed(item.id, action.route.host, action.route.session_id) for item in revisions):
                links.append(
                    link_outcome_change(
                        store, action, candidate.id, certainty="exact", actor_id="lemoncrow", actor_type="system"
                    )
                )
                break
    return tuple(links)


def change_links(store: Any, review_id: str) -> list[dict[str, Any]]:
    """Revert / fix-forward relationships of one Change, either direction."""

    wanted = {relation for pair in RELATIONS.values() for relation in pair}
    return [
        {
            "relation": item.relation,
            "target_review_id": item.target_review_id,
            "title": item.title,
            "created_at": item.created_at,
        }
        for item in store.list_relationships(review_id, kind="change")
        if item.relation in wanted
    ]


# --------------------------------------------------------------------------- #
# Automatic rollback (P12-AUTO post_merge.rollback)
# --------------------------------------------------------------------------- #


class RollbackAdapter(Protocol):
    """A CD system that can roll one environment back to its previous release."""

    name: str

    def rollback(self, *, environment: str, merged_commit: str, reason: str) -> str: ...


@dataclass(frozen=True)
class RollbackView:
    decision: Any
    adapter: str = ""
    reason: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "decision": self.decision.to_wire(),
            "adapter": self.adapter,
            "automatic": self.decision.mode == "rule" and bool(self.adapter),
            "reason": self.reason,
        }


def rollback_decision(
    rules: Sequence[Any],
    *,
    review_id: str,
    repository: str,
    problems: Sequence[ProductionOutcome],
    permission_check: Callable[[Any, str], bool],
    adapter: RollbackAdapter | None = None,
) -> RollbackView:
    """Whether a rule enables automatic rollback for this Change now, and whether anything could run it.

    Only a non-advisory problem (failed/rolled-back production deployment or
    a contradicted prediction) satisfies the trigger guard; an incident link
    never does. lc-debt: no CD rollback adapter ships in this build, so a firing
    rule is shown with :data:`ROLLBACK_UNAVAILABLE`; upgrade path: register a
    CD adapter (Argo Rollouts, Spinnaker) behind :class:`RollbackAdapter`.
    """

    from lemoncrow.pro.capabilities.review.automation_rules import AutomationFacts, decide_automation

    concrete = [item for item in problems if not item.advisory]
    facts = AutomationFacts(
        review_id=review_id,
        repository=repository,
        values={"outcome.production_problem": bool(concrete), "change.repository": repository},
        sources={"outcome.production_problem": "deployment outcomes and production observation"},
    )
    decision = decide_automation("post_merge.rollback", rules=rules, facts=facts, permission_check=permission_check)
    if decision.mode != "rule":
        return RollbackView(decision, reason=decision.reason)
    if adapter is None:
        return RollbackView(decision, reason=ROLLBACK_UNAVAILABLE)
    return RollbackView(decision, adapter=adapter.name, reason=decision.reason)


__all__ = [
    "LINKED_ACTIVITY",
    "OUTCOME_SUBJECT_PREFIX",
    "RELATIONS",
    "ROLLBACK_UNAVAILABLE",
    "OutcomeLink",
    "ProductionOutcome",
    "RollbackAdapter",
    "RollbackView",
    "change_links",
    "link_outcome_change",
    "production_outcomes",
    "reconcile_outcome_actions",
    "rollback_decision",
]

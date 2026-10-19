"""P15-OBS items 6, 7, 8: Revert / Fix forward, attention, escaped-defect feeds, and the production route.

Revert and Fix forward are Phase 12 controlled actions on a merged Review's
production problems; their result is a new Change linked both ways. Nothing
appears without a deployment or observability connector.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.automation_rules import (
    ActingIdentity,
    local_permission_check,
    parse_automation_rules,
)
from lemoncrow.pro.capabilities.review.outcomes.deployment_outcomes import apply_observations
from lemoncrow.pro.capabilities.review.outcomes.deployments import (
    DeploymentObservation,
    DeploymentSettings,
    MergedCommit,
    shifted,
)
from lemoncrow.pro.capabilities.review.outcomes.local_deployments import LOCAL_SETTINGS_FILE
from lemoncrow.pro.capabilities.review.outcomes.local_observability import LOCAL_OBSERVABILITY_FILE
from lemoncrow.pro.capabilities.review.outcomes.observability import parse_observability_settings
from lemoncrow.pro.capabilities.review.outcomes.observation_outcomes import (
    due_plans,
    record_aggregates,
    start_observations,
)
from lemoncrow.pro.capabilities.review.outcomes.outcome_actions import (
    ROLLBACK_UNAVAILABLE,
    production_outcomes,
    reconcile_outcome_actions,
    rollback_decision,
)
from lemoncrow.pro.capabilities.review.outcomes.outcome_feeds import (
    escaped_defects,
    escapes_by_depth_and_check,
    store_escaped_defects,
)
from lemoncrow.pro.capabilities.review.review_actions import ReviewActionLedger, resolve_results, snapshot_refs
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.side_effects import DERIVED_KIND, PRODUCER_ID, STAMP, StaticReport
from lemoncrow.pro.capabilities.review.store import DerivedResult, new_revision_id, new_session_id
from lemoncrow.pro.capabilities.review.track_record import change_outcome
from tests.gateway.test_review_actions import Review, _review

MERGED_AT = "2026-10-01T10:00:00.000000Z"
DEPLOYED = DeploymentSettings(connector="github_deployments", observation_window_hours=72, link_horizon_days=14)
OBSERVABILITY = {
    "connectors": [{"kind": "prometheus", "base_url": "https://prom.example.com", "credential": {"env": "PROM_TOKEN"}}],
    "services": ["checkout"],
    "query_budget": 50,
}


def _at(hours: float) -> str:
    return shifted(MERGED_AT, hours=hours)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    root = tmp_path / "lemoncrow-root"
    root.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    return root


def _configure(root: Path) -> None:
    (root / LOCAL_SETTINGS_FILE).write_text(
        json.dumps({"version": 1, "deployments": {"connector": "github_deployments"}}), encoding="utf-8"
    )
    (root / LOCAL_OBSERVABILITY_FILE).write_text(
        json.dumps({"version": 1, "observability": OBSERVABILITY}), encoding="utf-8"
    )


def _contradicted(review: Review) -> None:
    """The merged Snapshot removed a host; production still calls it."""

    store = review.store
    session = store.get_session(review.review_id)
    assert session is not None
    revision = review.latest()
    from lemoncrow.pro.capabilities.review.behavior import BehaviorDelta

    removed = BehaviorDelta(
        kind="side_effect",
        producer=PRODUCER_ID,
        subject="outbound_host:old.example.com",
        title="Outbound host removed: old.example.com",
        summary="",
        change="removed",
    )
    store.put_derived_results(
        [
            DerivedResult(
                review_id=session.id,
                revision_id=revision.id,
                kind=DERIVED_KIND,
                scope_key="current",
                stamp=STAMP,
                payload=StaticReport(status="ran", deltas=(removed,)).to_wire(),
            )
        ]
    )
    merged = MergedCommit(provider="github", sha="a" * 40, merged_at=MERGED_AT, ref="main")
    observation = DeploymentObservation(
        provider="github",
        deployment_id="1",
        environment="production",
        commit="a" * 40,
        state="success",
        created_at=_at(1),
        status_at=_at(1),
        version=f"{_at(1)}|{1:020d}",
    )
    apply_observations(
        store, session, merged, [observation], {observation.key: "exact"}, DEPLOYED, actor_id="p", now=_at(1.1)
    )
    settings = parse_observability_settings(OBSERVABILITY, source="t", locus="local")
    assert start_observations(store, settings, now=_at(1.2)) == (session.id,)
    [plan] = due_plans(store, now=_at(13))
    answers = [
        {"query_id": query["id"], "status": "ok", "value": 9}
        for query in plan["queries"]
        if query["signal"] == "outbound_calls"
    ]
    assert record_aggregates(store, session.id, plan["scope"], answers, now=_at(13.1)).contradicted


def _other_change(review: Review, *, repo_root: str = "", title: str = "Revert", **revision: Any) -> ReviewSession:
    store = review.store
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="pull_request",
            repo_root=repo_root or str(review.repo_root),
            range_mode="commit_range",
            title=title,
        )
    )
    store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=0,
            range_mode="commit_range",
            tree_fingerprint="tree-r",
            packet_schema_version=1,
            head_sha="e" * 40,
            **revision,
        ),
        [],
    )
    current = store.get_session(session.id)
    assert current is not None
    return current


def test_nothing_appears_without_a_connector(tmp_path: Path) -> None:
    review = _review(tmp_path)
    kinds = {item["kind"] for item in review.actions()["catalog"]}
    assert "revert" not in kinds and "fix_forward" not in kinds
    assert review.get("production").json() == {"configured": False}
    refused = review.post("actions/preview", {"kind": "revert", "subject_ids": []})
    assert refused.status_code == 409


def test_revert_is_a_controlled_action_whose_new_change_links_both_ways(tmp_path: Path, _isolated: Path) -> None:
    _configure(_isolated)
    review = _review(tmp_path)
    catalog = {item["kind"]: item for item in review.actions()["catalog"]}
    assert catalog["revert"]["available"] is False and "no problem" in catalog["revert"]["reason"]
    _contradicted(review)
    catalog = {item["kind"]: item for item in review.actions()["catalog"]}
    assert catalog["revert"]["available"] and catalog["fix_forward"]["available"]
    [subject] = catalog["revert"]["subject_ids"]
    assert subject.startswith("outcome:contradicted:1:beh:side_effect:")
    body = {"kind": "revert", "subject_ids": [subject], "route": "prompt_fallback"}
    preview = review.preview(body)
    assert preview["ready"] and preview["included"][0]["kind"] == "production_outcome"
    handoff = preview["handoff"]
    assert "reverts exactly this Change's merged commit" in handoff
    assert "origin=behavior_observation trust=untrusted" in handoff and "old.example.com" in handoff
    # Explicit human action: a copy route needs the confirmation, like any Phase 12 action.
    assert review.start(body, preview).status_code == 409
    action = review.start(body, preview, copied=True).json()["action"]
    assert action["status"] == "awaiting_snapshot" and action["kind"] == "revert"
    # A later Snapshot of the merged Review is never the revert's result.
    actions = ReviewActionLedger(review.store).list([item.id for item in review.store.list_revisions(review.review_id)])
    refs = snapshot_refs(review.store.list_revisions(review.review_id))
    assert resolve_results(actions, refs, lambda *_: True) == {}
    revert = _other_change(review)
    linked = review.post("production/links", {"action_id": action["id"], "target_review_id": revert.id}, expected=False)
    assert linked.status_code == 200, linked.text
    forward = review.store.list_relationships(review.review_id, kind="change")
    backward = review.store.list_relationships(revert.id, kind="change")
    assert [(item.relation, item.target_review_id) for item in forward] == [("reverted_by", revert.id)]
    assert [(item.relation, item.target_review_id) for item in backward] == [("reverts", review.review_id)]
    [stored] = ReviewActionLedger(review.store).list(
        [item.id for item in review.store.list_revisions(review.review_id)]
    )
    assert stored.status == "snapshot_recorded" and stored.result_certainty == "unattributed"
    assert review.store.list_activity_events(revert.id, kinds=("outcome.linked",))
    production = review.get("production").json()
    assert production["configured"] and production["links"][0]["relation"] == "reverted_by"
    assert production["in_readiness"] is False
    # Replaying the same link is idempotent; another target is refused once the action finished.
    # (A Review of another repository in the same store is refused as unknown: see the test below.)
    again = review.post("production/links", {"action_id": action["id"], "target_review_id": revert.id}, expected=False)
    assert again.status_code == 200
    other = _other_change(review)
    assert (
        review.post(
            "production/links", {"action_id": action["id"], "target_review_id": other.id}, expected=False
        ).status_code
        == 409
    )
    session = review.store.get_session(review.review_id)
    assert session is not None
    outcome = change_outcome(review.store, session)
    assert outcome.escaped and outcome.reverted and outcome.deployed


def test_fix_forward_goes_to_the_exact_authoring_agent_and_links_automatically(tmp_path: Path, _isolated: Path) -> None:
    _configure(_isolated)
    review = _review(tmp_path)
    review.attribute_revision(review.latest().id, "claude", "s-author")
    _contradicted(review)
    preview = review.preview({"kind": "fix_forward"})
    assert preview["route"]["tier"] == "exact_session" and preview["route"]["session_id"] == "s-author"
    assert "fixes those problems" in preview["handoff"]
    # The authoring session opens the new Change; its Snapshot is exactly attributed to that session.
    action = _stored_action(review, preview)
    fix = _other_change(
        review, provenance_host="claude", provenance_session_id="s-author", provenance_certainty="exact"
    )
    # The same agent session also opened a later Change in another repository of this store: never a candidate.
    elsewhere = _other_change(
        review,
        repo_root=str(review.repo_root) + "-other",
        provenance_host="claude",
        provenance_session_id="s-author",
        provenance_certainty="exact",
    )
    session = review.store.get_session(review.review_id)
    assert session is not None
    [link] = reconcile_outcome_actions(review.store, session, [action])
    assert link.target_review_id == fix.id and link.certainty == "exact"
    relations = {item.relation for item in review.store.list_relationships(fix.id, kind="change")}
    assert relations == {"fixes_forward"}
    assert not review.store.list_relationships(elsewhere.id, kind="change")


def test_a_review_of_another_repository_is_never_linked_and_never_named(tmp_path: Path, _isolated: Path) -> None:
    _configure(_isolated)
    review = _review(tmp_path)
    _contradicted(review)
    catalog = {item["kind"]: item for item in review.actions()["catalog"]}
    body = {"kind": "revert", "subject_ids": catalog["revert"]["subject_ids"], "route": "prompt_fallback"}
    action = review.start(body, review.preview(body), copied=True).json()["action"]
    foreign = _other_change(review, repo_root=str(review.repo_root) + "-other", title="Private payroll rewrite")
    refused = review.post(
        "production/links", {"action_id": action["id"], "target_review_id": foreign.id}, expected=False
    )
    assert refused.status_code == 409
    assert refused.json()["detail"] == "the new Change must be another Review in this repository"
    assert "payroll" not in refused.text
    assert not review.store.list_relationships(review.review_id, kind="change")
    assert not review.store.list_relationships(foreign.id, kind="change")
    [stored] = ReviewActionLedger(review.store).list(
        [item.id for item in review.store.list_revisions(review.review_id)]
    )
    assert stored.in_flight  # still waiting for its own Change
    unknown = review.post(
        "production/links", {"action_id": action["id"], "target_review_id": "no-such-review"}, expected=False
    )
    assert (unknown.status_code, unknown.json()) == (refused.status_code, refused.json())


def _stored_action(review: Review, preview: dict[str, Any]) -> Any:
    from lemoncrow.pro.capabilities.review.review_actions import (
        ActionEvent,
        ActionRoute,
        ActionSubject,
        ReviewAction,
        now_iso,
    )

    at = now_iso()
    route = preview["route"]
    action = ReviewAction(
        id=preview["operation_id"],
        review_id=review.review_id,
        kind="fix_forward",
        source_revision_id=review.latest().id,
        route=ActionRoute(route["tier"], route["mode"], True, route["host"], route["session_id"], route["label"]),
        status="awaiting_snapshot",
        created_by="reviewer",
        created_at=at,
        updated_at=at,
        subjects=tuple(ActionSubject.from_wire(item) for item in preview["included"]),
        events=(ActionEvent("created", at, "human", "reviewer"),),
    )
    created, _ok = ReviewActionLedger(review.store).create(action)
    return created


def test_escaped_defects_feed_by_depth_and_check(tmp_path: Path, _isolated: Path) -> None:
    _configure(_isolated)
    review = _review(tmp_path)
    _contradicted(review)
    session = review.store.get_session(review.review_id)
    assert session is not None
    [defect] = escaped_defects(review.store, session)
    assert defect.kind == "contradicted" and defect.subject == "old.example.com"
    assert defect.target_kind == "side_effect" and defect.category == "outbound_host"
    assert store_escaped_defects(review.store) == (defect,)
    summary = escapes_by_depth_and_check([defect])
    assert summary["total"] == 1 and summary["by_kind"] == {"contradicted": 1}
    assert sum(summary["by_depth"].values()) == 1


def test_automatic_rollback_needs_an_enabling_rule_and_never_counts_an_incident() -> None:
    from lemoncrow.pro.capabilities.review.outcomes.outcome_actions import ProductionOutcome

    problem = ProductionOutcome(id="outcome:x", kind="contradicted", title="t", detail="d", revision_id="r")
    incident = replace(problem, id="outcome:i", kind="incident", advisory=True)
    none = rollback_decision(
        (), review_id="r", repository="repo", problems=[problem], permission_check=local_permission_check
    )
    assert none.decision.mode == "needs_human" and none.to_wire()["automatic"] is False
    rules, errors = parse_automation_rules(
        [
            {
                "id": "rollback",
                "trigger": "production_problem",
                "action": "post_merge.rollback",
                "enabled": True,
                "enabled_by": "ops-admin@acme",
                "acts_as": {"subject": "owner", "role": "owner"},
            }
        ],
        layer="local_operator",
        source="t",
    )
    assert errors == ()
    enabled = rollback_decision(
        rules, review_id="r", repository="repo", problems=[problem], permission_check=local_permission_check
    )
    assert enabled.decision.mode == "rule" and enabled.reason == ROLLBACK_UNAVAILABLE
    assert enabled.to_wire()["automatic"] is False
    advisory = rollback_decision(
        rules, review_id="r", repository="repo", problems=[incident], permission_check=local_permission_check
    )
    assert advisory.decision.mode == "needs_human"

    def nobody(identity: ActingIdentity, permission: str) -> bool:
        return False

    refused = rollback_decision(rules, review_id="r", repository="repo", problems=[problem], permission_check=nobody)
    assert refused.decision.mode == "refused"


def test_production_outcomes_are_empty_for_a_review_without_problems(tmp_path: Path) -> None:
    review = _review(tmp_path)
    session = review.store.get_session(review.review_id)
    assert session is not None
    assert production_outcomes(review.store, session) == ()

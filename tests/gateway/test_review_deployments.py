"""P15-DEPLOY: deployment linkage, outcome evidence, attention triggers, retention (core model + store).

Pure model first (settings, connector seam, the commit-matching rule, outcome
derivation, status line, bounds at and past each limit), then the store-bound
ledger: immutable ``external_observation`` evidence, idempotent replay,
attention triggers and the observation-window closer.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.freshness import freshness_class
from lemoncrow.pro.capabilities.review.outcomes import deployments as model
from lemoncrow.pro.capabilities.review.outcomes.deployment_outcomes import (
    DEPLOYMENT_EVIDENCE_KIND,
    DeploymentLedger,
    apply_observations,
    close_observation_windows,
    deployment_attention_triggers,
    deployment_projection,
    outcome_attention_triggers,
)
from lemoncrow.pro.capabilities.review.outcomes.deployments import (
    MAX_ENTRIES_PER_ENVIRONMENT,
    MAX_ENVIRONMENTS_PER_REVIEW,
    MAX_TRANSITIONS_PER_ENTRY,
    DeploymentObservation,
    DeploymentSettings,
    MergedCommit,
    connector_status,
    derive_outcome,
    parse_deployment_settings,
    relation_for,
    resolve_relations,
    status_line,
    update_entries,
)
from lemoncrow.pro.capabilities.review.outcomes.github_deployments import compare_ancestry, deployment_summary
from lemoncrow.pro.capabilities.review.outcomes.local_deployments import (
    LOCAL_SETTINGS_FILE,
    load_local_deployment_settings,
    watch_local_deployments,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_revision_id, new_session_id

MERGED_SHA = "a" * 40
CHILD_SHA = "b" * 40
OLD_SHA = "c" * 40
OTHER_SHA = "d" * 40
MERGED_AT = "2026-10-01T10:00:00.000000Z"
CONFIGURED = DeploymentSettings(connector="github_deployments", observation_window_hours=72, link_horizon_days=14)
MERGED = MergedCommit(provider="github", sha=MERGED_SHA, merged_at=MERGED_AT, ref="main")


def _at(hours: float) -> str:
    return model.shifted(MERGED_AT, hours=hours)


def _obs(
    deployment_id: str,
    *,
    commit: str = MERGED_SHA,
    state: str = "success",
    environment: str = "production",
    created: float = 1.0,
    status: float | None = None,
    sequence: int = 1,
    traffic: int | None = None,
) -> DeploymentObservation:
    status_at = _at(created if status is None else status)
    return DeploymentObservation(
        provider="github",
        deployment_id=deployment_id,
        environment=environment,
        commit=commit,
        state=state,
        created_at=_at(created),
        status_at=status_at,
        version=f"{status_at}|{sequence:020d}",
        traffic_percent=traffic,
    )


def _entries(*observations: DeploymentObservation, relations: dict[str, Any] | None = None, now: float = 2.0) -> Any:
    rel = relations if relations is not None else {o.key: relation_for(MERGED, o, {}) for o in observations}
    entries, _dropped = update_entries((), observations, rel, CONFIGURED, now=_at(now))
    return entries


# --------------------------------------------------------------------------- #
# Settings and the connector seam
# --------------------------------------------------------------------------- #


def test_settings_bounds_are_enforced_at_and_past_each_limit() -> None:
    assert parse_deployment_settings(None, source="t").configured is False
    ok = parse_deployment_settings(
        {
            "connector": "github_deployments",
            "observation_window_hours": model.MAX_OBSERVATION_WINDOW_HOURS,
            "link_horizon_days": model.MAX_LINK_HORIZON_DAYS,
            "production_environments": [f"prod-{i}" for i in range(model.MAX_PRODUCTION_ENVIRONMENTS)],
        },
        source="t",
    )
    assert ok.configured and ok.observation_window_hours == 2160 and ok.link_horizon_days == 90
    assert parse_deployment_settings({"observation_window_hours": 1, "link_horizon_days": 1}, source="t")
    for bad in (
        {"observation_window_hours": 0},
        {"observation_window_hours": model.MAX_OBSERVATION_WINDOW_HOURS + 1},
        {"observation_window_hours": True},
        {"link_horizon_days": 0},
        {"link_horizon_days": model.MAX_LINK_HORIZON_DAYS + 1},
        {"production_environments": [f"p{i}" for i in range(model.MAX_PRODUCTION_ENVIRONMENTS + 1)]},
        {"production_environments": [""]},
        {"connector": "jenkins"},
        {"surprise": 1},
    ):
        with pytest.raises(ValueError):
            parse_deployment_settings(bad, source="t")


def test_connector_seam_reports_honest_not_configured_for_unimplemented_connectors() -> None:
    assert connector_status(CONFIGURED).state == "configured"
    assert connector_status(CONFIGURED).locus == "provider_webhook"
    assert connector_status(DeploymentSettings()).state == "not_configured"
    for kind, locus in (
        ("gitlab_deployments", "provider_webhook"),
        ("argocd", "customer_worker"),
        ("flux", "customer_worker"),
    ):
        settings = parse_deployment_settings({"connector": kind}, source="t")
        status = connector_status(settings)
        assert settings.configured is False
        assert (status.state, status.locus) == ("not_configured", locus)
        assert "not available yet" in status.reason
        stub = model.UNAVAILABLE_CONNECTORS[kind]
        assert stub.observations("repo", since=MERGED_AT, limit=10) == ()
        assert stub.relation("repo", MERGED_SHA, CHILD_SHA) is None
        assert stub.status().state == "not_configured"


def test_local_settings_file_missing_invalid_and_valid(tmp_path: Path) -> None:
    assert load_local_deployment_settings(tmp_path).configured is False
    path = tmp_path / LOCAL_SETTINGS_FILE
    path.write_text(json.dumps({"version": 1, "deployments": {"observation_window_hours": 0}}), encoding="utf-8")
    invalid = load_local_deployment_settings(tmp_path)
    assert invalid.configured is False and "observation_window_hours" in invalid.error
    assert connector_status(invalid).reason == invalid.error
    path.write_text(json.dumps({"version": 1, "deployments": {"connector": "github_deployments"}}), encoding="utf-8")
    assert load_local_deployment_settings(tmp_path).configured is True


# --------------------------------------------------------------------------- #
# The commit-matching rule
# --------------------------------------------------------------------------- #


def test_relation_rule_exact_descendant_not_contained_and_conservative_unknown() -> None:
    assert relation_for(MERGED, _obs("1"), {}) == "exact"
    # Created before the merge existed: cannot contain it, whatever its commit.
    assert relation_for(MERGED, _obs("2", commit=OLD_SHA, created=-1.0), {OLD_SHA: "contains"}) == "not_contained"
    assert relation_for(MERGED, _obs("3", commit=CHILD_SHA), {CHILD_SHA: "contains"}) == "descendant"
    assert relation_for(MERGED, _obs("4", commit=OTHER_SHA), {OTHER_SHA: "not_contained"}) == "not_contained"
    assert relation_for(MERGED, _obs("5", commit=CHILD_SHA), {}) == "unknown"
    assert compare_ancestry({"status": "ahead"}) == "contains"
    assert compare_ancestry({"status": "identical"}) == "contains"
    assert compare_ancestry({"status": "behind"}) == "not_contained"
    assert compare_ancestry({"status": "diverged"}) == "not_contained"
    assert compare_ancestry({"status": ""}) is None and compare_ancestry([]) is None


def test_ancestry_lookups_are_deduplicated_and_bounded_at_and_past_the_budget() -> None:
    calls: list[str] = []

    def lookup(commit: str) -> Any:
        calls.append(commit)
        if commit == "e" * 40:
            raise RuntimeError("provider down")
        return "contains"

    commits = ["1" * 40, "2" * 40, "1" * 40, "e" * 40]
    observations = [_obs(str(i), commit=commit) for i, commit in enumerate(commits)]
    budget = model.LookupBudget(remaining=3)
    relations, learned, deferred = resolve_relations(MERGED, observations, {}, lookup, budget=budget)
    assert calls == ["1" * 40, "2" * 40, "e" * 40]  # at the budget: each distinct commit once
    assert deferred == frozenset() and budget.remaining == 0 and budget.deferred == 0
    assert learned == {"1" * 40: "contains", "2" * 40: "contains"}
    assert relations["github:3"] == "unknown"  # a lookup that ran but failed proves nothing
    calls.clear()
    budget = model.LookupBudget(remaining=2)
    relations, learned, deferred = resolve_relations(MERGED, observations, {}, lookup, budget=budget)
    # Past the budget: not looked at, so deferred (left out of the pass), never recorded as unknown.
    assert calls == ["1" * 40, "2" * 40] and deferred == frozenset({"github:3"}) and budget.deferred == 1
    # One budget is shared across Reviews of one event: a second Review gets nothing more.
    _relations, _learned, deferred = resolve_relations(MERGED, observations[:1], {}, lookup, budget=budget)
    assert deferred == frozenset({"github:0"}) and len(calls) == 2
    _relations, _learned, deferred = resolve_relations(MERGED, observations, {}, None)
    assert deferred == frozenset()  # no comparison possible at all: unknown, not deferred


def test_github_deployment_summary_keeps_only_the_summary() -> None:
    deployment = {
        "id": 77,
        "sha": CHILD_SHA,
        "ref": "main",
        "task": "deploy",
        "environment": "production",
        "created_at": "2026-10-01T11:00:00Z",
        "production_environment": True,
        "payload": {"canary_percent": 10, "secret_note": "never stored"},
        "description": "ignored",
    }
    status = {
        "id": 900,
        "state": "success",
        "created_at": "2026-10-01T11:05:00Z",
        "environment_url": "https://prod.example",
        "log_url": "https://logs.example/raw",
        "description": "ignored",
    }
    summary = deployment_summary(deployment, status)
    assert summary is not None
    assert (summary.deployment_id, summary.commit, summary.state, summary.traffic_percent) == (
        "77",
        CHILD_SHA,
        "success",
        10,
    )
    assert summary.production and summary.url == "https://prod.example"
    assert "never stored" not in repr(summary) and "logs.example" not in repr(summary)
    created = deployment_summary(deployment, None)
    assert created is not None and created.state == "queued" and created.status_at == ""
    assert deployment_summary({**deployment, "sha": "not-a-sha"}, status) is None
    assert deployment_summary(deployment, {**status, "environment_url": "http://plain"}).url == ""  # type: ignore[union-attr]
    assert deployment_summary({**deployment, "payload": {"canary_percent": 101}}, status).traffic_percent is None  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# Entries and the environment outcome
# --------------------------------------------------------------------------- #


def test_outcome_states_follow_the_latest_containing_deployment() -> None:
    deployed = derive_outcome("production", _entries(_obs("1")), MERGED)
    assert deployed is not None and deployed.state == "deployed" and deployed.relation == "exact"
    failed = derive_outcome("production", _entries(_obs("1", state="failure")), MERGED)
    assert failed is not None and failed.state == "failed"
    errored = derive_outcome("production", _entries(_obs("1", state="error")), MERGED)
    assert errored is not None and errored.state == "failed"
    inactive = derive_outcome("production", _entries(_obs("1", state="inactive")), MERGED)
    assert inactive is not None and inactive.state == "superseded"
    deploying = derive_outcome("production", _entries(_obs("1", state="in_progress")), MERGED)
    assert deploying is not None and deploying.state == "deploying"
    # A redeploy in progress keeps the earlier successful containing deployment live.
    redeploy = _entries(_obs("1"), _obs("2", state="queued", created=1.5), now=1.6)
    held = derive_outcome("production", redeploy, MERGED)
    assert held is not None and held.state == "deployed" and held.deciding_id == "1"


def test_rollback_needs_a_later_proven_non_containing_success_after_a_containing_success() -> None:
    first, back = _obs("1"), _obs("2", commit=OLD_SHA, created=2.0)
    relations: dict[str, Any] = {first.key: "exact", back.key: "not_contained"}
    rolled = derive_outcome("production", _entries(first, back, relations=relations, now=3.0), MERGED)
    assert rolled is not None and rolled.state == "rolled_back" and rolled.deciding_id == "2"
    # Unproven ancestry of the later success: never claimed deployed, never claimed rolled back.
    unproven = derive_outcome(
        "production", _entries(first, back, relations={**relations, back.key: "unknown"}, now=3.0), MERGED
    )
    assert unproven is not None and unproven.state == "unknown"
    # A failed containing deployment followed by an older-code success is not a rollback of a live change.
    failed_first = _obs("1", state="failure")
    after_failure = derive_outcome(
        "production",
        _entries(failed_first, back, relations={failed_first.key: "exact", back.key: "not_contained"}, now=3.0),
        MERGED,
    )
    assert after_failure is not None and after_failure.state == "failed"
    # A Review's environment appears only once a deployment provably contains the change.
    assert derive_outcome("production", _entries(_obs("9", commit=CHILD_SHA)), MERGED) is None
    assert _entries(_obs("9", commit=CHILD_SHA)) == ()
    assert derive_outcome("production", (), MERGED) is None


def test_status_line_reads_like_the_brief() -> None:
    staging = derive_outcome("staging", _entries(_obs("1", environment="staging")), MERGED)
    canary = derive_outcome("production", _entries(_obs("2", traffic=10)), MERGED)
    assert staging is not None and canary is not None
    assert status_line([canary, staging]) == "Deployed to staging · production canary 10%"
    failed = derive_outcome("production", _entries(_obs("3", state="failure")), MERGED)
    assert failed is not None
    assert status_line([staging, failed]) == "Deployed to staging · production deployment failed"
    assert status_line([]) == ""
    live, later = _obs("4"), _obs("5", commit=CHILD_SHA, created=2.0)
    unknown = derive_outcome(
        "production", _entries(live, later, relations={live.key: "exact", later.key: "unknown"}, now=3.0), MERGED
    )
    assert unknown is not None and status_line([unknown]) == "Production not proven"


def test_entries_are_version_gated_against_out_of_order_and_replayed_observations() -> None:
    success = _obs("1", status=1.5, sequence=2)
    late_older = _obs("1", state="in_progress", status=1.2, sequence=1)
    entries = _entries(success)
    relations = {success.key: "exact"}
    again, _ = update_entries(entries, [late_older, success], relations, CONFIGURED, now=_at(2.0))
    assert again == entries  # an older status and a replay change nothing
    assert again[0].state == "success"
    # Closed (summarized) entries are frozen; newer statuses no longer apply.
    frozen = (replace(entries[0], closed=True),)
    newer = _obs("1", state="inactive", status=9.0, sequence=3)
    kept, _ = update_entries(frozen, [newer], relations, CONFIGURED, now=_at(10.0))
    assert kept == frozen
    # A deployment first observed after its own window closed records nothing.
    stale = _obs("7", created=1.0)
    nothing, _ = update_entries((), [stale], {stale.key: "exact"}, CONFIGURED, now=_at(1.0 + 72))
    assert nothing == ()
    inside, _ = update_entries((), [stale], {stale.key: "exact"}, CONFIGURED, now=_at(1.0 + 71.9))
    assert len(inside) == 1
    # Non-containing deployments matter only after the first containing one (rollback detection).
    before = _obs("0", commit=OLD_SHA, created=0.5)
    containing = _obs("1", created=1.0)
    after = _obs("2", commit=OLD_SHA, created=2.0)
    rel: dict[str, Any] = {before.key: "not_contained", containing.key: "exact", after.key: "not_contained"}
    relevant, _ = update_entries((), [before, containing, after], rel, CONFIGURED, now=_at(3.0))
    assert [entry.deployment_id for entry in relevant] == ["1", "2"]


def test_entry_and_transition_ceilings_hold_at_and_past_the_bound() -> None:
    at_bound = [_obs(str(i), created=1.0 + i / 1000) for i in range(MAX_ENTRIES_PER_ENVIRONMENT)]
    entries, dropped = update_entries((), at_bound, {o.key: "exact" for o in at_bound}, CONFIGURED, now=_at(2.0))
    assert len(entries) == MAX_ENTRIES_PER_ENVIRONMENT and dropped == 0
    past = [*at_bound, _obs("x", created=1.9)]
    entries, dropped = update_entries((), past, {o.key: "exact" for o in past}, CONFIGURED, now=_at(2.0))
    assert len(entries) == MAX_ENTRIES_PER_ENVIRONMENT and dropped == 1
    assert entries[0].deployment_id == "1"  # the oldest went first, and it was counted

    states = ["queued", "in_progress"] * MAX_TRANSITIONS_PER_ENTRY
    held = _entries(_obs("1", state="queued", status=1.0, sequence=1))
    for index, state in enumerate(states[1:MAX_TRANSITIONS_PER_ENTRY], start=2):
        step = _obs("1", state=state, status=1.0 + index / 1000, sequence=index)
        held, _ = update_entries(held, [step], {step.key: "exact"}, CONFIGURED, now=_at(2.0))
    assert len(held[0].transitions) == MAX_TRANSITIONS_PER_ENTRY and held[0].transitions_dropped == 0
    step = _obs("1", state="success", status=1.5, sequence=999)
    held, _ = update_entries(held, [step], {step.key: "exact"}, CONFIGURED, now=_at(2.0))
    assert len(held[0].transitions) == MAX_TRANSITIONS_PER_ENTRY and held[0].transitions_dropped == 1


def test_link_horizon_is_inclusive_at_the_bound_and_excludes_past_it() -> None:
    horizon_hours = CONFIGURED.link_horizon_days * 24
    assert model.in_horizon(MERGED, _obs("1", created=horizon_hours), CONFIGURED)
    assert not model.in_horizon(MERGED, _obs("2", created=horizon_hours + 0.001), CONFIGURED)


# --------------------------------------------------------------------------- #
# Store-bound: evidence, idempotence, attention triggers, retention closer
# --------------------------------------------------------------------------- #


def _review(store: ReviewStore, *, status: str = "open", repo_root: str = "/repo/deploy") -> ReviewSession:
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="pull_request",
            repo_root=repo_root,
            range_mode="commit_range",
            source_ref="github:acme/widgets#7",
        )
    )
    store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=0,
            range_mode="commit_range",
            tree_fingerprint="tree-1",
            packet_schema_version=1,
            head_sha="f" * 40,
        ),
        [],
    )
    current = store.get_session(session.id)
    assert current is not None
    if status != "open":
        current = replace(current, status=status)  # type: ignore[arg-type]
    return current


def _deployments(store: ReviewStore, review_id: str) -> list[Any]:
    return [row for row in store.list_evidence(review_id) if row.kind == DEPLOYMENT_EVIDENCE_KIND]


def test_observations_become_immutable_external_observation_evidence_once(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "store")
    session = _review(store)
    first = _obs("1", state="in_progress", status=1.0, sequence=1)
    done = _obs("1", state="success", status=1.2, sequence=2)
    applied = apply_observations(
        store, session, MERGED, [first], {first.key: "exact"}, CONFIGURED, actor_id="provider_github", now=_at(1.1)
    )
    assert applied.touched and [c.current for c in applied.state_changes] == ["deploying"]
    applied = apply_observations(
        store, session, MERGED, [done], {done.key: "exact"}, CONFIGURED, actor_id="provider_github", now=_at(1.3)
    )
    assert [c.current for c in applied.state_changes] == ["deployed"]
    rows = _deployments(store, session.id)
    assert len(rows) == 2 and {freshness_class(row) for row in rows} == {"external_observation"}
    assert all(
        row.source == "external" and row.attestation_class == "" and row.verification_status == "" for row in rows
    )
    assert all(json.loads(row.detail)["raw"] is True for row in rows)
    activity = store.list_activity_events(session.id, kinds=("deployment.observed",))
    assert [event.summary for event in activity] == [
        f"Deploying to production ({MERGED_SHA[:12]} is the merged commit).",
        f"Deployed to production ({MERGED_SHA[:12]} is the merged commit).",
    ]
    # Replay and out-of-order delivery: no row write, no evidence, no activity.
    replay = apply_observations(
        store,
        session,
        MERGED,
        [first, done],
        {first.key: "exact"},
        CONFIGURED,
        actor_id="provider_github",
        now=_at(1.4),
    )
    assert not replay.touched and replay.evidence_ids == ()
    assert len(_deployments(store, session.id)) == 2
    assert len(store.list_activity_events(session.id, kinds=("deployment.observed",))) == 2
    # Not configured: nothing is stored.
    other = _review(store)
    assert not apply_observations(
        store, other, MERGED, [done], {done.key: "exact"}, DeploymentSettings(), actor_id="x"
    ).touched
    assert DeploymentLedger(store).records(other.id) == {}


def test_environment_ceiling_per_review_holds_at_and_past_the_bound(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "store")
    session = _review(store)
    observations = [_obs(str(i), environment=f"env-{i:02d}") for i in range(MAX_ENVIRONMENTS_PER_REVIEW)]
    result = apply_observations(
        store,
        session,
        MERGED,
        observations,
        {o.key: "exact" for o in observations},
        CONFIGURED,
        actor_id="p",
        now=_at(2),
    )
    assert len(result.changed) == MAX_ENVIRONMENTS_PER_REVIEW and result.environments_dropped == 0
    extra = _obs("x", environment="env-extra")
    result = apply_observations(
        store, session, MERGED, [extra], {extra.key: "exact"}, CONFIGURED, actor_id="p", now=_at(2)
    )
    assert result.changed == () and result.environments_dropped == 1


def test_attention_triggers_cover_failed_and_rolled_back_production_while_the_window_is_open(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "store")
    session = _review(store)
    staging_failure = _obs("1", environment="staging", state="failure")
    apply_observations(
        store,
        session,
        MERGED,
        [staging_failure],
        {staging_failure.key: "exact"},
        CONFIGURED,
        actor_id="p",
        now=_at(1.1),
    )
    assert deployment_attention_triggers(store, session, _at(1.2)) == ()  # not production
    live = _obs("2")
    back = _obs("3", commit=OLD_SHA, created=5.0)
    apply_observations(
        store,
        session,
        MERGED,
        [live, back],
        {live.key: "exact", back.key: "not_contained"},
        CONFIGURED,
        actor_id="p",
        now=_at(5.1),
    )
    [trigger] = outcome_attention_triggers(store, session, now=_at(5.2))
    assert trigger.kind == "deployment_rolled_back" and trigger.subject_id == "deployment:production"
    assert trigger.fact == f"Rolled back from production ({OLD_SHA[:12]} does not contain this change)."
    assert trigger.open_until == _at(5.0 + 72)
    assert outcome_attention_triggers(store, session, now=_at(5.0 + 72)) == ()  # window closed: trigger cleared
    assert outcome_attention_triggers(store, replace(session, status="archived"), now=_at(5.2)) == ()


def test_the_closer_keeps_the_summary_expires_raw_signal_and_is_idempotent(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "store")
    session = _review(store)
    running = _obs("1", state="in_progress", status=1.0, sequence=1)
    done = _obs("1", state="success", status=1.5, sequence=2)
    for item, at in ((running, 1.1), (done, 1.6)):
        apply_observations(store, session, MERGED, [item], {item.key: "exact"}, CONFIGURED, actor_id="p", now=_at(at))
    assert len(_deployments(store, session.id)) == 2

    early = close_observation_windows(store, now=_at(1.0 + 71.99))
    assert early.closed == () and len(_deployments(store, session.id)) == 2  # window still open

    report = close_observation_windows(store, now=_at(1.0 + 72))
    [closed] = report.closed
    assert closed.deployment_ids == ("1",) and closed.raw_rows_deleted == 2 and closed.summaries_written == 1
    [summary] = _deployments(store, session.id)
    detail = json.loads(summary.detail)
    assert detail["summary"] is True and detail["state"] == "success" and detail["transitions"] == 2
    assert "raw" not in detail and freshness_class(summary) == "external_observation"
    [(_stamp, record)] = DeploymentLedger(store).records(session.id).values()
    assert record.entries[0].closed and record.entries[0].transitions == ()
    outcome = record.outcome()
    assert outcome is not None and outcome.state == "deployed"  # the summarized outcome stays
    assert [e.kind for e in store.list_activity_events(session.id, kinds=("deployment.summarized",))] == [
        "deployment.summarized"
    ]
    projection = deployment_projection(store, session, CONFIGURED)
    assert projection["status_line"] == "Deployed to production"

    again = close_observation_windows(store, now=_at(500))
    assert again.closed == () and again.scanned == 0  # closed rows are no longer scanned
    late = _obs("1", state="inactive", status=80.0, sequence=3)
    assert not apply_observations(
        store, session, MERGED, [late], {late.key: "exact"}, CONFIGURED, actor_id="p", now=_at(80.5)
    ).touched


def test_deployment_evidence_never_becomes_a_check_or_artifact(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.verification import build_verification_projection

    store = ReviewStore(tmp_path / "store")
    session = _review(store)
    item = _obs("1", state="failure", status=1.0)
    apply_observations(store, session, MERGED, [item], {item.key: "exact"}, CONFIGURED, actor_id="p", now=_at(1.1))
    # Even a deployment row that carried a verification status never becomes a (failed) check.
    rows = [replace(row, verification_status="FAIL") for row in _deployments(store, session.id)]
    revisions = store.list_revisions(session.id)
    projection = build_verification_projection(packet=None, evidence=rows, revision=revisions[-1], revisions=revisions)
    assert rows and projection["checks"] == [] and projection["summary"]["fail"] == 0
    assert not any(row.id in json.dumps(projection, default=str) for row in rows)


def test_the_local_pass_still_closes_windows_after_the_connector_is_removed(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "store")
    session = _review(store)
    item = _obs("1", state="failure", status=1.0)
    apply_observations(store, session, MERGED, [item], {item.key: "exact"}, CONFIGURED, actor_id="p", now=_at(1.1))
    calls: list[Sequence[str]] = []

    def runner(args: Sequence[str], stdin: str | None) -> str:
        calls.append(args)
        raise AssertionError("no gh call without a connector")

    assert watch_local_deployments(store, settings=DeploymentSettings(), runner=runner, now=_at(50)) == ()
    assert len(DeploymentLedger(store).open_rows()) == 1  # window still open
    assert watch_local_deployments(store, settings=DeploymentSettings(), runner=runner, now=_at(73)) == ()
    assert DeploymentLedger(store).open_rows() == () and calls == []
    assert [row.source_ref for row in _deployments(store, session.id)] == ["deployment-summary:github:1"]


def test_projection_and_route_show_nothing_without_a_connector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "lemoncrow-root"
    root.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    store = ReviewStore(tmp_path / "store")
    session = _review(store, repo_root=str(tmp_path.resolve()))
    done = _obs("1")
    apply_observations(store, session, MERGED, [done], {done.key: "exact"}, CONFIGURED, actor_id="p", now=_at(1.1))
    assert deployment_projection(store, session, DeploymentSettings()) == {
        "configured": False,
        "connector": {
            "connector": "",
            "state": "not_configured",
            "locus": "",
            "reason": "No deployment connector is configured.",
        },
    }
    app = FastAPI()
    register_review_api(app, store, auth_dependency=make_token_dependency("tok"), repo_root=tmp_path, port=8765)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"Authorization": "Bearer tok"}
    body = client.get(f"/api/reviews/{session.id}/deployments", headers=headers).json()
    assert body["configured"] is False and "environments" not in body
    (root / LOCAL_SETTINGS_FILE).write_text(
        json.dumps({"version": 1, "deployments": {"connector": "github_deployments"}}), encoding="utf-8"
    )
    body = client.get(f"/api/reviews/{session.id}/deployments", headers=headers).json()
    assert body["configured"] is True and body["status_line"] == "Deployed to production"
    assert body["in_readiness"] is False and body["freshness_class"] == "external_observation"
    assert client.get("/api/reviews/missing/deployments", headers=headers).status_code == 404
    assert client.get(f"/api/reviews/{session.id}/deployments").status_code in {401, 403}


def test_deployment_evidence_cannot_be_entered_by_hand(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "store")
    session = _review(store, repo_root=str(tmp_path.resolve()))
    app = FastAPI()
    register_review_api(app, store, auth_dependency=make_token_dependency("tok"), repo_root=tmp_path, port=8765)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    revision = store.latest_revision(session.id)
    assert revision is not None
    refused = client.post(
        f"/api/reviews/{session.id}/evidence/upload",
        params={"kind": "deployment", "title": "prod", "expected_revision_id": revision.id},
        files={"file": ("x.txt", b"x", "text/plain")},
        headers={"Authorization": "Bearer tok"},
    )
    assert refused.status_code == 422 and "deployment" in refused.text

"""Auto-approval eligibility (Phase 10 items 8-9, P10-LAND): deterministic, auditable, fail closed.

Every gate is evaluated and explained; Gate F preconditions L1-L4, the
hard-deny defaults, Gate H qualification and change-set completeness each
block on their own; a standing approval is withdrawn only through the one
revalidation path and never created by it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import auto_approval as aa
from lemoncrow.pro.capabilities.review.auto_approval import (
    GATE_IDS,
    MAX_PATHS_NAMED,
    AutoApprovalFacts,
    FindingsFact,
    ProviderSyncFact,
    RequestProgressFact,
    TrustedBaseFact,
    evaluate_auto_approval,
    hard_deny_classes,
)
from lemoncrow.pro.capabilities.review.landing_policy import AutoApprovePolicy, LandingPolicy
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_revision_id, new_session_id

HEAD = "a" * 40
NOW = "2026-10-05T12:00:00Z"


def _row(
    condition_id: str, state: str = "pass", *, owner: str = "lemoncrow", reason: str = "", **extra: Any
) -> dict[str, Any]:
    return {
        "id": condition_id,
        "label": condition_id.replace("-", " ").capitalize(),
        "state": state,
        "owner": owner,
        "reason": reason or ("satisfied" if state == "pass" else "awaiting"),
        "category": "test",
        **extra,
    }


def _readiness(*rows: dict[str, Any], authority: str = "final", generation: str = "policy:one") -> dict[str, Any]:
    return {
        "conditions_hash": "conditions:" + "-".join(f"{row['id']}={row['state']}" for row in rows),
        "policy_generation": {"digest": generation, "authority": authority, "layers": []},
        "valid_until": "",
        "conditions": list(rows) if rows else [_row("verification")],
    }


def _policy(**auto: Any) -> LandingPolicy:
    return LandingPolicy(auto_approve=AutoApprovePolicy(**{"enabled": True, "max_criticality": "normal", **auto}))


def _facts(**overrides: Any) -> AutoApprovalFacts:
    base = AutoApprovalFacts(
        review_id="r1",
        snapshot_id="s1",
        tree_fingerprint="tree-1",
        head_revision=HEAD,
        policy=_policy(),
        readiness=_readiness(_row("verification"), _row("latest-snapshot-reviewed", "pending")),
        changed_paths=("docs/guide.md", "tests/test_widgets.py"),
        criticality="low",
        labels=(),
        target_branch="main",
        findings=FindingsFact(),
        human_needs_changes=0,
        provider_blockers=(),
        carried_blockers=(),
        unqualified_evidence=(),
        provider_sync=ProviderSyncFact("synced", HEAD),
        requests=RequestProgressFact(),
        trusted_base=TrustedBaseFact("current"),
    )
    return replace(base, **overrides)


def _decide(**overrides: Any) -> aa.AutoApprovalDecision:
    return evaluate_auto_approval(_facts(**overrides), evaluated_at=NOW)


def _failed(decision: aa.AutoApprovalDecision) -> dict[str, aa.Gate]:
    return {gate.id: gate for gate in decision.failed}


def test_auto_approval_gate_evaluation_contract() -> None:
    """case: test_an_eligible_change_passes_every_gate_and_each_gate_is_explained"""
    decision = _decide()
    assert decision.eligible is True
    assert [gate.id for gate in decision.gates] == list(GATE_IDS)
    assert all(gate.detail for gate in decision.gates)
    wire = decision.to_wire()
    assert wire["eligible"] is True and wire["blocking"] == []
    assert wire["policy_generation"] == "policy:one"
    assert wire["conditions_hash"] == decision.conditions_hash != ""
    assert wire["decision_digest"] == decision.digest
    "case: test_one_failed_gate_never_short_circuits_the_rest"
    decision = _decide(policy=_policy(enabled=False), provider_sync=None, criticality="critical")
    assert [gate.id for gate in decision.gates] == list(GATE_IDS)
    assert set(_failed(decision)) == {"policy", "provider_sync", "criticality"}
    assert decision.to_wire()["blocking"] == ["Auto-approval policy", "Provider in sync", "Criticality"]
    "case: test_the_digest_is_the_decision_basis_never_the_instant"
    first = _decide()
    again = evaluate_auto_approval(_facts(), evaluated_at="2026-10-06T00:00:00Z")
    assert first.digest == again.digest
    assert _decide(readiness=_readiness(_row("verification"), generation="policy:two")).digest != first.digest
    assert _decide(head_revision="b" * 40, provider_sync=ProviderSyncFact("synced", "b" * 40)).digest != first.digest
    assert _decide(criticality="high").digest != first.digest


def test_auto_approval_snapshot_authority_contract() -> None:
    """case: test_l1_the_provider_must_be_synced_at_the_exact_snapshot_head"""
    for sync, reason, transient in [
        (None, "unknown", True),
        (ProviderSyncFact("provider_delayed", HEAD), "provider_delayed", True),
        (ProviderSyncFact("sync_failed", HEAD), "sync_failed", True),
        (ProviderSyncFact("provider_newer", "b" * 40), "provider_newer", False),
        (ProviderSyncFact("not_linked", ""), "not_linked", False),
        (ProviderSyncFact("synced", "b" * 40), "head_mismatch", False),
    ]:
        gate = _failed(_decide(provider_sync=sync))["provider_sync"]
        assert (gate.reason, gate.transient) == (reason, transient)
    "case: test_l1_an_empty_snapshot_head_never_matches"
    gate = _failed(_decide(head_revision="", provider_sync=ProviderSyncFact("synced", "")))["provider_sync"]
    assert gate.reason == "head_mismatch"
    "case: test_l4_the_trusted_base_must_be_current_at_decision_time"
    for base, passed, transient in [
        (TrustedBaseFact("current"), True, False),
        (TrustedBaseFact("not_rechecked"), True, False),
        (TrustedBaseFact("stale", "policy changed on main"), False, False),
        (TrustedBaseFact("unknown"), False, True),
        (None, False, True),
    ]:
        gate = next(gate for gate in _decide(trusted_base=base).gates if gate.id == "trusted_base")
        assert (gate.passed, gate.transient) == (passed, transient)
    "case: test_policy_authority_must_be_final"
    provisional = _failed(_decide(readiness=_readiness(_row("verification"), authority="provisional")))
    invalid = _failed(_decide(readiness=_readiness(_row("verification"), authority="invalid")))
    assert provisional["policy_authority"].transient is True
    assert invalid["policy_authority"].transient is False


def test_auto_approval_path_scope_contract() -> None:
    """case: test_every_hard_deny_class_blocks_auto_approval"""
    for path, klass in [
        ("src/auth/session.py", "auth"),
        ("app/oauth2_callback.ts", "auth"),
        ("config/.env.production", "secrets_crypto"),
        ("deploy/keys/id_rsa", "secrets_crypto"),
        ("certs/server.key", "secrets_crypto"),
        ("app/billing/invoice.py", "billing"),
        ("db/migrations/0001_init.py", "migrations_persistence"),
        ("schema/drop_users.sql", "migrations_persistence"),
        (".github/workflows/ci.yml", "infra_ci_release"),
        ("charts/api/values.yaml", "infra_ci_release"),
        (".gitlab-ci.yml", "infra_ci_release"),
        ("api/openapi.yaml", "public_contracts"),
        ("proto/widgets.proto", "public_contracts"),
        (".lemoncrow/review.yaml", "review_policy"),
        ("CODEOWNERS", "review_policy"),
        ("SECURITY.md", "review_policy"),
    ]:
        assert klass in hard_deny_classes([path])
        gate = _failed(_decide(changed_paths=("docs/guide.md", path)))["hard_deny"]
        assert klass in gate.reason.split(",") and path in gate.detail and (gate.transient is False)
    "case: test_policy_named_sensitive_paths_are_hard_denied_too"
    policy = _policy(sensitive_paths=("src/ledger/**",))
    gate = _failed(_decide(policy=policy, changed_paths=("src/ledger/core.py",)))["hard_deny"]
    assert gate.reason == "configured_sensitive"
    "case: test_no_known_changed_path_is_never_proof_of_insensitivity"
    assert _failed(_decide(changed_paths=()))["hard_deny"].reason == "unknown"
    "case: test_named_paths_are_bounded_but_the_count_is_exact"
    at_bound = tuple(f"src/auth/m{index}.py" for index in range(MAX_PATHS_NAMED))
    past = (*at_bound, "src/auth/extra.py")
    assert "more" not in _failed(_decide(changed_paths=at_bound))["hard_deny"].detail
    detail = _failed(_decide(changed_paths=past))["hard_deny"].detail
    assert "and 1 more" in detail and "extra.py" not in detail
    "case: test_scope_includes_and_excludes"
    for overrides, reason, transient in [
        ({"policy": _policy(include_paths=("docs/**",))}, "outside_include", False),
        ({"policy": _policy(exclude_paths=("tests/**",))}, "excluded_path", False),
        ({"policy": _policy(exclude_labels=("Security",)), "labels": ("security",)}, "excluded_label", False),
        ({"policy": _policy(exclude_labels=("security",)), "labels": None}, "labels_unknown", True),
        (
            {"policy": _policy(exclude_branches=("release/**",)), "target_branch": "release/2.0"},
            "excluded_branch",
            False,
        ),
        ({"policy": _policy(exclude_branches=("release/**",)), "target_branch": None}, "branch_unknown", True),
    ]:
        gate = _failed(_decide(**overrides))["scope"]
        assert (gate.reason, gate.transient) == (reason, transient)
    "case: test_an_unsafe_path_is_never_inside_an_include_scope"
    gate = _failed(_decide(policy=_policy(include_paths=("docs/**",)), changed_paths=("docs/../src/x.py",)))
    assert gate["scope"].reason == "outside_include"
    "case: test_criticality_above_the_ceiling_and_critical_are_never_approved"
    for criticality, ceiling, reason in [
        ("critical", "high", "critical"),
        ("high", "normal", "high"),
        (None, "high", "unknown"),
        ("bogus", "high", "unknown"),
    ]:
        gate = _failed(_decide(criticality=criticality, policy=_policy(max_criticality=ceiling)))["criticality"]
        assert gate.reason == reason


def test_auto_approval_readiness_contract() -> None:
    """case: test_readiness_needs_every_lemoncrow_condition_but_stands_in_for_review_progress_only"""
    scm = _row("scm-requirement:ci", "pending", owner="scm")
    assert _decide(readiness=_readiness(_row("latest-snapshot-reviewed", "pending"), scm)).eligible is True
    blocked = _failed(_decide(readiness=_readiness(_row("human-change-requests", "fail"))))["readiness"]
    assert blocked.reason == "not_ready" and blocked.transient is False
    waived = _failed(_decide(readiness=_readiness(_row("verification", "waived"))))["readiness"]
    assert waived.reason == "waived"
    transient = _failed(_decide(readiness=_readiness(_row("verification", "unknown", reason="evaluation_error"))))
    assert transient["readiness"].transient is True
    unknown = _failed(_decide(readiness={"conditions": [], "policy_generation": {"authority": "final"}}))
    assert unknown["readiness"].reason == "unknown"
    "case: test_human_blockers_are_never_approved_over"
    assert _failed(_decide(human_needs_changes=2))["human_blockers"].reason == "human_blocker"
    provider = _failed(_decide(provider_blockers=("alice requested changes on GitHub",)))["human_blockers"]
    assert "alice requested changes" in provider.detail
    assert _failed(_decide(human_needs_changes=None))["human_blockers"].transient is True
    "case: test_the_carried_verification_weakening_and_agent_instruction_blocker_is_consumed"
    carried = (
        {"code": "verification_weakened", "title": "Test assertion removed"},
        {"code": "agent_instruction_changed", "title": "AGENTS.md changed"},
    )
    gate = _failed(_decide(carried_blockers=carried))["verification_weakening"]
    assert gate.reason == "agent_instruction_changed,verification_weakened"
    assert "Test assertion removed" in gate.detail and gate.transient is False
    assert _failed(_decide(carried_blockers=None))["verification_weakening"].transient is True
    "case: test_unresolved_p0_p1_and_unrated_findings_always_block"
    assert "P0/P1" in _failed(_decide(findings=FindingsFact(p0_p1=1)))["findings"].detail
    assert "without a severity" in _failed(_decide(findings=FindingsFact(unrated=1)))["findings"].detail
    other = FindingsFact(other_unresolved=2, stale_unresolved=1)
    assert "findings" in _failed(_decide(findings=other))
    assert _decide(findings=other, policy=_policy(require_clean_review=False)).eligible is True
    assert _failed(_decide(findings=None))["findings"].transient is True
    "case: test_gate_h_unqualified_lemoncrow_evidence_never_feeds_auto_approval"
    gate = _failed(_decide(unqualified_evidence=("pytest (LemonCrow)",)))["evidence_qualification"]
    assert gate.reason == "unqualified" and "pytest (LemonCrow)" in gate.detail
    enforce = _row(
        "behavior-changes-judged", evidence_qualification={"gate": "H", "mode": "enforce", "advisory_targets": 2}
    )
    report = _row(
        "org-policy:check:unit",
        evidence_qualification={
            "gate": "H",
            "mode": "report",
            "reason": "unqualified",
            "would_be": {"state": "pending"},
        },
    )
    for row in (enforce, report):
        failed = _failed(_decide(readiness=_readiness(row)))
        assert set(failed) == {"evidence_qualification"}, row
    assert _failed(_decide(unqualified_evidence=None))["evidence_qualification"].transient is True
    "case: test_a_change_set_read_without_a_principal_is_incomplete_and_never_approves"
    incomplete = _row("change-set:members", "unknown", reason="evaluation_error", set_state="incomplete")
    gate = _failed(_decide(readiness=_readiness(incomplete)))["change_set"]
    assert gate.reason == "incomplete" and gate.transient is True
    blocked = _row("change-set:members", "pending", reason="set_member_blocked", set_state="blocked")
    assert _failed(_decide(readiness=_readiness(blocked)))["change_set"].reason == "not_ready"
    ready = [_row("change-set:members", set_state="ready"), _row("change-set:contracts", set_state="ready")]
    assert _decide(readiness=_readiness(*ready)).eligible is True
    partial = _row("change-set:members", "pass", set_state="incomplete")
    assert _failed(_decide(readiness=_readiness(partial)))["change_set"].reason == "incomplete"
    "case: test_a_failing_merge_result_blocks_and_a_pending_one_is_the_scms"
    failing = _row("merge-result", "fail", reason="merge_failure", detail="unit fails once merged")
    assert _failed(_decide(readiness=_readiness(failing)))["merge_result"].detail == "unit fails once merged"
    pending = _row("merge-result", "pending", owner="scm", reason="awaiting_merge_result")
    assert _decide(readiness=_readiness(pending)).eligible is True


def test_a_gate_that_raises_fails_closed_as_a_transient_unknown(monkeypatch: pytest.MonkeyPatch) -> None:

    def broken(facts: AutoApprovalFacts) -> aa.Gate:
        raise RuntimeError("rule store unavailable")

    monkeypatch.setattr(aa, "_GATES", (broken, *aa._GATES[1:]))
    decision = _decide()
    gate = _failed(decision)["policy"]
    assert decision.eligible is False
    assert gate.transient is True and gate.reason == "evaluation_error"
    assert "rule store unavailable" in gate.detail


@pytest.mark.parametrize(
    ("requests", "reason"),
    [
        (RequestProgressFact(stale_completed=("alice",)), "request_reopened"),
        (RequestProgressFact(open_requests=("bob",)), "review_requested"),
        (None, "unknown"),
    ],
)
def test_l3_request_progress_is_evaluated_directly_on_the_latest_snapshot(
    requests: RequestProgressFact | None, reason: str
) -> None:
    assert _failed(_decide(requests=requests))["requests"].reason == reason


@pytest.mark.parametrize("path", ["docs/authoring.md", "src/review_actions.py", "tests/test_widgets.py"])
def test_a_token_inside_a_word_is_not_a_hard_deny_match(path: str) -> None:
    assert hard_deny_classes([path]) == {}


def _store(tmp_path: Path) -> tuple[ReviewStore, ReviewSession]:
    store = ReviewStore(tmp_path)
    store.init()
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="local_change",
            repo_root="/repo/alpha",
            range_mode="working_tree",
            title="docs",
            source_ref="",
        )
    )
    store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=0,
            range_mode="working_tree",
            tree_fingerprint="tree-1",
            packet_schema_version=1,
        ),
        [],
    )
    return (store, session)


def _approved(store: ReviewStore, session: ReviewSession, decision: aa.AutoApprovalDecision) -> Any:
    item = aa.record_auto_approval(
        store,
        decision=decision,
        revision_number=1,
        policy_layers=[{"layer": "built_in"}],
        valid_until="",
        provider="github",
        external_id="acme/widgets#7",
    )
    return aa.settle_delivery(store, item, {"state": "sent", "remote_ref": "review-1"})


class _Provider:

    def __init__(self, *, withdraw_state: str = "done") -> None:
        self.withdrawn: list[str] = []
        self.delivered: list[str] = []
        self.withdraw_state = withdraw_state

    def withdraw(self, metadata: Any) -> dict[str, str]:
        self.withdrawn.append(str(metadata.get("decision_digest")))
        return {"state": self.withdraw_state, "detail": "dismissed"}

    def deliver(self, metadata: Any) -> dict[str, str]:
        self.delivered.append(str(metadata.get("decision_digest")))
        return {"state": "sent", "remote_ref": "review-2"}


def _revalidate(
    store: ReviewStore,
    session: ReviewSession,
    provider: _Provider,
    decision: aa.AutoApprovalDecision | Exception,
    *,
    tree: str = "tree-1",
    audit: list[tuple[str, dict[str, Any]]] | None = None,
) -> str:
    events = audit if audit is not None else []

    def _evaluate() -> aa.AutoApprovalDecision:
        if isinstance(decision, Exception):
            raise decision
        return decision

    return aa.revalidate_auto_approval(
        store,
        review_id=session.id,
        latest_tree_fingerprint=tree,
        origin="sweep",
        evaluate=_evaluate,
        valid_until=lambda: "",
        withdraw=provider.withdraw,
        deliver=provider.deliver,
        audit=lambda item, kind, summary, detail: events.append((kind, dict(detail))),
    )


def test_the_decision_is_stored_once_per_basis(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    snapshot = store.list_revisions(session.id)[0].id
    decision = replace(_decide(), review_id=session.id, snapshot_id=snapshot)
    assert aa.store_decision(store, decision) is True
    assert aa.store_decision(store, replace(decision, evaluated_at="2026-10-06T00:00:00Z")) is False
    stored = aa.stored_decision(store, snapshot)
    assert stored is not None and stored["decision_digest"] == decision.digest
    changed = replace(_decide(criticality="critical"), review_id=session.id, snapshot_id=snapshot)
    assert aa.store_decision(store, changed) is True
    assert aa.stored_decision(store, snapshot)["eligible"] is False
    assert aa.stored_decision(store, "missing") is None


def test_only_an_eligible_decision_is_recorded_and_its_basis_is_named(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    with pytest.raises(ValueError, match="eligible"):
        aa.record_auto_approval(
            store,
            decision=replace(_decide(policy=_policy(enabled=False)), review_id=session.id),
            revision_number=1,
            policy_layers=[],
            valid_until="",
            provider="github",
            external_id="x",
        )
    decision = replace(_decide(), review_id=session.id)
    item = _approved(store, session, decision)
    metadata = aa.approval_metadata(item)
    assert item.status == "approved" and metadata["delivery"] == "sent"
    assert metadata["policy_generation"] == "policy:one" and metadata["conditions_hash"] == decision.conditions_hash
    assert metadata["decision_digest"] == decision.digest and metadata["head_revision"] == HEAD
    assert aa.decision_for_record(item)["decision_digest"] == decision.digest


def test_revalidation_never_creates_an_approval(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    provider = _Provider()
    assert _revalidate(store, session, provider, replace(_decide(), review_id=session.id)) == "none"
    assert aa.auto_approval_relationship(store, session.id) is None and provider.delivered == []


def test_l2_a_policy_change_that_still_qualifies_is_revalidated_and_audited(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    provider = _Provider()
    _approved(store, session, replace(_decide(), review_id=session.id))
    assert _revalidate(store, session, provider, replace(_decide(), review_id=session.id)) == "unchanged"
    audit: list[tuple[str, dict[str, Any]]] = []
    same_conditions = _readiness(
        _row("verification"), _row("latest-snapshot-reviewed", "pending"), generation="policy:two"
    )
    newer = replace(_decide(readiness=same_conditions), review_id=session.id)
    assert _revalidate(store, session, provider, newer, audit=audit) == "revalidated"
    assert audit == [
        ("landing.revalidated", {"origin": "sweep", "previous_generation": "policy:one", "generation": "policy:two"})
    ]
    metadata = aa.approval_metadata(aa.auto_approval_relationship(store, session.id))
    assert metadata["policy_generation"] == "policy:two" and metadata["policy_revalidated_from"] == "policy:one"
    assert provider.withdrawn == []


def test_l2_a_definite_regression_withdraws_exactly_once(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    provider = _Provider()
    _approved(store, session, replace(_decide(), review_id=session.id))
    audit: list[tuple[str, dict[str, Any]]] = []
    regressed = replace(
        _decide(readiness=_readiness(_row("human-change-requests", "fail"), generation="policy:two")),
        review_id=session.id,
    )
    assert _revalidate(store, session, provider, regressed, audit=audit) == "withdrawn"
    assert _revalidate(store, session, provider, regressed, audit=audit) == "none"
    assert len(provider.withdrawn) == 1
    [(kind, detail)] = audit
    assert kind == "landing.auto_approval_withdrawn"
    assert detail["blocked_by"] == [{"id": "readiness", "label": "Review readiness", "reason": "not_ready"}]
    assert detail["previous_generation"] == "policy:one" and detail["generation"] == "policy:two"
    item = aa.auto_approval_relationship(store, session.id)
    assert item.status == "withdrawn"
    assert "Review policy changed" in aa.approval_metadata(item)["detail"]


def test_l2_a_transient_failure_defers_and_never_withdraws(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    provider = _Provider()
    _approved(store, session, replace(_decide(), review_id=session.id))
    audit: list[tuple[str, dict[str, Any]]] = []
    unknown = replace(_decide(provider_sync=None), review_id=session.id)
    for _ in range(3):
        assert _revalidate(store, session, provider, unknown, audit=audit) == "deferred"
    assert _revalidate(store, session, provider, RuntimeError("store down"), audit=audit) == "deferred"
    assert provider.withdrawn == []
    assert [kind for kind, _detail in audit] == ["landing.revalidation_deferred", "landing.revalidation_deferred"]
    assert aa.auto_approval_relationship(store, session.id).status == "approved"


def test_a_new_snapshot_withdraws_the_standing_approval(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    provider = _Provider()
    _approved(store, session, replace(_decide(), review_id=session.id))
    assert _revalidate(store, session, provider, replace(_decide(), review_id=session.id), tree="tree-2") == "stale"
    assert len(provider.withdrawn) == 1
    assert (
        aa.approval_wire(aa.auto_approval_relationship(store, session.id), latest_tree_fingerprint="tree-2")["state"]
        == "withdrawn"
    )


def test_a_failed_provider_withdrawal_stays_owed_and_is_retried(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    provider = _Provider(withdraw_state="failed")
    _approved(store, session, replace(_decide(), review_id=session.id))
    regressed = replace(_decide(human_needs_changes=1), review_id=session.id)
    assert _revalidate(store, session, provider, regressed) == "withdrawn"
    item = aa.auto_approval_relationship(store, session.id)
    metadata = aa.approval_metadata(item)
    assert item.status == "approved" and metadata["provider_cancel"] == "pending"
    assert "FAILED" in metadata["detail"]
    assert _revalidate(store, session, provider, regressed) == "deferred"
    provider.withdraw_state = "done"
    assert _revalidate(store, session, provider, regressed) == "withdrawn"
    assert aa.auto_approval_relationship(store, session.id).status == "withdrawn"
    assert len(provider.withdrawn) == 3


def test_an_owed_delivery_is_retried_only_on_the_same_eligible_basis(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    provider = _Provider()
    decision = replace(_decide(), review_id=session.id)
    item = aa.record_auto_approval(
        store,
        decision=decision,
        revision_number=1,
        policy_layers=[],
        valid_until="",
        provider="github",
        external_id="x",
    )
    owed = aa.settle_delivery(store, item, {"state": "retryable", "detail": "502"})
    assert owed is not None and owed.status == "approving" and (aa.approval_metadata(owed)["delivery"] == "pending")
    assert _revalidate(store, session, provider, decision) == "unchanged"
    assert provider.delivered == [decision.digest]
    assert aa.auto_approval_relationship(store, session.id).status == "approved"
    fresh, session_two = _store(tmp_path / "second")
    second = replace(decision, review_id=session_two.id)
    pending = aa.record_auto_approval(
        fresh, decision=second, revision_number=1, policy_layers=[], valid_until="", provider="github", external_id="x"
    )
    assert pending.status == "approving"
    other = _Provider()
    assert (
        _revalidate(fresh, session_two, other, replace(_decide(human_needs_changes=1), review_id=session_two.id))
        == "withdrawn"
    )
    assert other.withdrawn == [] and other.delivered == []


def test_the_wire_marks_an_approval_of_an_older_snapshot_stale(tmp_path: Path) -> None:
    store, session = _store(tmp_path)
    item = _approved(store, session, replace(_decide(), review_id=session.id))
    assert aa.approval_wire(item, latest_tree_fingerprint="tree-1")["state"] == "approved"
    assert aa.approval_wire(item, latest_tree_fingerprint="tree-2")["state"] == "stale"
    assert aa.approval_wire(None, latest_tree_fingerprint="tree-1") == {"state": "none"}
    assert json.loads(item.metadata_json)["decided_by"] == aa.AUTO_APPROVAL_ACTOR

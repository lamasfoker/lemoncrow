"""Landing policy, merge-result revalidation, break-glass and blocking reasons (P10-LAND, pure parts)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import break_glass as bg
from lemoncrow.pro.capabilities.review.blocking_reasons import (
    STATUS_CONTEXT,
    STATUS_DESCRIPTION_MAX,
    action_for,
    blocking_reasons,
    merge_result_status,
    scm_status,
    summary_text,
)
from lemoncrow.pro.capabilities.review.landing_policy import (
    FOLLOW_UP_HOURS_MAX,
    MAX_PATTERNS,
    MAX_RESPONDERS,
    merge_landing_policies,
    parse_landing_section,
    parse_trusted_landing,
)
from lemoncrow.pro.capabilities.review.merge_result import (
    MAX_CHECKS,
    BehaviorOutcome,
    MergeResultFacts,
    SideResult,
    classify,
    merge_result_condition,
    side_results,
)


def _operator(document: Any, source: str = "organization") -> Any:
    return parse_landing_section(document, layer="operator", source=source)


def test_landing_policy_contract() -> None:
    """case: test_only_operator_policy_enables_auto_approval_and_names_responders"""
    trusted = parse_trusted_landing(
        "landing:\n  auto_approve:\n    enabled: true\n  break_glass:\n    responders: [mallory]\n", source="base"
    )
    policy = merge_landing_policies([trusted])
    assert policy.auto_approve_enabled is False and policy.break_glass_enabled is False
    assert len(policy.notes) == 2
    enabled = merge_landing_policies(
        [_operator({"auto_approve": {"enabled": True}, "break_glass": {"responders": ["alice"]}}), trusted]
    )
    assert enabled.auto_approve_enabled is True and enabled.break_glass.responders == ("alice",)
    "case: test_the_trusted_base_can_only_tighten"
    operator = _operator(
        {
            "auto_approve": {
                "enabled": True,
                "max_criticality": "high",
                "require_clean_review": False,
                "include": {"paths": ["docs/**", "tests/**"]},
            },
            "break_glass": {"responders": ["alice"], "follow_up_hours": 48},
        }
    )
    trusted = parse_trusted_landing(
        "landing:\n  auto_approve:\n    max_criticality: low\n    require_clean_review: false\n    include: {paths: ['docs/**', 'src/**']}\n    exclude: {labels: [security]}\n  merge_result: {required: true}\n  break_glass: {follow_up_hours: 12}\n",
        source="base",
    )
    policy = merge_landing_policies([operator, trusted])
    auto = policy.auto_approve
    assert auto.max_criticality == "low" and auto.require_clean_review is False
    assert auto.include_paths == ("docs/**",)
    assert auto.exclude_labels == ("security",)
    assert policy.merge_result_required is True and policy.break_glass.follow_up_hours == 12
    disabled = merge_landing_policies(
        [operator, parse_trusted_landing("landing: {auto_approve: {enabled: false}}", source="b")]
    )
    assert disabled.auto_approve_enabled is False
    "case: test_an_invalid_section_fails_closed"
    for document in [
        "landing",
        {"auto_approve": {"enabled": "yes"}},
        {"auto_approve": {"max_criticality": "critical"}},
        {"auto_approve": {"surprise": True}},
        {"auto_approve": {"exclude": {"paths": ["../**"]}}},
        {"break_glass": {"follow_up_hours": 0}},
        {"break_glass": {"follow_up_hours": FOLLOW_UP_HOURS_MAX + 1}},
        {"break_glass": {"follow_up_hours": True}},
    ]:
        policy = merge_landing_policies([_operator({"auto_approve": {"enabled": True}}), _operator(document, "repo")])
        assert policy.errors and policy.auto_approve_enabled is False and (policy.break_glass_enabled is False)
    "case: test_list_bounds_are_errors_never_truncation"
    at = _operator({"auto_approve": {"exclude": {"labels": [f"l{index}" for index in range(MAX_PATTERNS)]}}})
    past = _operator({"auto_approve": {"exclude": {"labels": [f"l{index}" for index in range(MAX_PATTERNS + 1)]}}})
    assert not at.errors and len(at.exclude_labels) == MAX_PATTERNS
    assert past.errors and "at most 64" in past.errors[0]
    responders_at = _operator({"break_glass": {"responders": [f"u{index}" for index in range(MAX_RESPONDERS)]}})
    responders_past = _operator({"break_glass": {"responders": [f"u{index}" for index in range(MAX_RESPONDERS + 1)]}})
    assert not responders_at.errors and responders_past.errors
    assert not _operator({"break_glass": {"follow_up_hours": FOLLOW_UP_HOURS_MAX}}).errors
    "case: test_unparsable_trusted_config_naming_landing_is_an_error"
    assert parse_trusted_landing("landing: [", source="base").errors
    assert not parse_trusted_landing("review: [", source="base").errors


def test_landing_merge_result_contract() -> None:
    """case: test_two_changes_that_pass_alone_but_break_once_merged_are_blocked_at_landing"""
    facts = _merge(merge=_side(("unit", "failure"), ("lint", "failure")))
    condition = merge_result_condition(facts)
    assert condition is not None
    assert (condition.state, condition.owner, condition.reason) == ("fail", "lemoncrow", "merge_failure")
    assert condition.extra["new_failures"] == ["unit"] and condition.extra["inherited_failures"] == ["lint"]
    assert "unit" in condition.detail and "judgments are unchanged" in condition.detail
    "case: test_inherited_and_head_failures_are_not_new_failures"
    result = classify(_merge(merge=_side(("unit", "success"), ("lint", "failure"))))
    assert [row.identity for row in result.inherited] == ["lint"] and (not result.new_failures)
    head_failing = classify(
        _merge(head=_side(("unit", "failure")), base=_side(("unit", "success")), merge=_side(("unit", "failure")))
    )
    assert [row.identity for row in head_failing.head_failures] == ["unit"] and (not head_failing.new_failures)
    passing = merge_result_condition(_merge())
    assert passing is not None and passing.state == "pass" and ("inherited" in passing.detail)
    "case: test_a_merge_failure_never_seen_on_either_side_fails_closed"
    condition = merge_result_condition(_merge(head=(), base=(), merge=_side(("e2e", "failure"))))
    assert condition is not None and condition.state == "fail"
    "case: test_a_semantic_conflict_blocks_landing"
    conflict = BehaviorOutcome(key="GET /widgets", head="h1", base="b1", merge="m1")
    same = BehaviorOutcome(key="GET /health", head="h1", base="b1", merge="h1")
    condition = merge_result_condition(_merge(behavior=(conflict, same)))
    assert condition is not None and condition.state == "fail" and (condition.reason == "merge_conflict")
    assert condition.extra["semantic_conflicts"] == ["GET /widgets"]
    "case: test_no_merge_result_is_scm_owned_pending_only_when_required"
    assert merge_result_condition(MergeResultFacts()) is None
    pending = merge_result_condition(MergeResultFacts(required=True))
    assert pending is not None and (pending.state, pending.owner) == ("pending", "scm")
    running = merge_result_condition(_merge(merge=_side(("unit", "in_progress"))))
    assert running is not None and (running.state, running.owner) == ("pending", "scm")
    error = merge_result_condition(MergeResultFacts(error="GitHub unavailable"))
    assert error is not None and (error.state, error.reason) == ("unknown", "evaluation_error")
    "case: test_the_comparison_bound_is_unknown_past_it_never_a_pass"
    at = tuple(SideResult(identity=f"c{index:04d}", name=f"c{index}", status="PASS") for index in range(MAX_CHECKS))
    past = (*at, SideResult(identity="zzzz", name="late", status="FAIL"))
    within = merge_result_condition(_merge(merge=at, head=at, base=at))
    assert within is not None and within.state == "pass"
    over = merge_result_condition(_merge(merge=past, head=at, base=at))
    assert over is not None and (over.state, over.reason) == ("unknown", "over_bound")


_TYPED = (("lemoncrow/contracts", "lemoncrow_verification"), ("15368:unit", "provider_ci"))


def _typed_merge(*rows: SideResult) -> MergeResultFacts:
    return _merge(required_checks=_TYPED, merge=(*_side(("lint", "failure")), *rows))


def test_typed_required_checks_refuse_provider_ci_only_evidence() -> None:
    """RC11: a provider CI row under the identity of a required LemonCrow check never satisfies it."""

    ci_unit = SideResult(identity="15368:unit", name="unit", status="PASS")
    ci_contracts = SideResult(identity="lemoncrow/contracts", name="contracts", status="PASS")
    provider_only = merge_result_condition(_typed_merge(ci_unit, ci_contracts))
    assert provider_only is not None
    assert (provider_only.state, provider_only.reason) == ("pending", "awaiting_required_checks")
    assert provider_only.extra["required_unsatisfied"] == ["lemoncrow/contracts (lemoncrow_verification)"]
    # Missing entirely: pending, never a pass (without the requirement the same facts pass).
    missing = merge_result_condition(_typed_merge(ci_unit))
    assert missing is not None and missing.state == "pending"
    assert merge_result_condition(_merge(merge=(*_side(("lint", "failure")), ci_unit))).state == "pass"  # type: ignore[union-attr]
    # The typed producer passes it.
    verified = replace(ci_contracts, source="lemoncrow_verification")
    passing = merge_result_condition(_typed_merge(ci_unit, verified))
    assert passing is not None and passing.state == "pass"
    # A required check failing on the merge result fails, even when the base fails it too.
    failing = merge_result_condition(
        _merge(
            required_checks=(("lint", "provider_ci"),),
            merge=_side(("unit", "success"), ("lint", "failure")),
        )
    )
    assert failing is not None and (failing.state, failing.reason) == ("fail", "merge_failure")
    assert failing.extra["required_failed"] == ["lint (provider_ci)"]
    # Required checks make the merge result required before the provider builds it.
    awaiting = merge_result_condition(MergeResultFacts(required_checks=_TYPED))
    assert awaiting is not None and awaiting.state == "pending"


def test_typed_required_checks_parse_strictly_and_any_layer_adds_them() -> None:
    operator = _operator(
        {"merge_result": {"required_checks": [{"check": "15368:unit", "class": "provider_ci"}]}},
    )
    trusted = parse_trusted_landing(
        "landing:\n  merge_result:\n    required_checks:\n" "      - {check: lint, class: provider_ci}\n",
        source="base",
    )
    policy = merge_landing_policies([operator, trusted])
    assert policy.merge_result_required is True
    assert [(item.check, item.check_class) for item in policy.merge_result_checks] == [
        ("15368:unit", "provider_ci"),
        ("lint", "provider_ci"),
    ]
    assert policy.to_wire()["merge_result"]["required_checks"][1] == {"check": "lint", "class": "provider_ci"}
    assert "required_checks" not in merge_landing_policies([]).to_wire()["merge_result"]
    for bad in (
        [{"check": "unit"}],
        [{"check": "unit", "class": "human_waiver"}],
        [{"check": "", "class": "provider_ci"}],
        [{"check": "unit", "class": "provider_ci", "extra": 1}],
        {"check": "unit", "class": "provider_ci"},
        [{"check": f"c{index}", "class": "provider_ci"} for index in range(MAX_PATTERNS + 1)],
    ):
        layer = _operator({"merge_result": {"required_checks": bad}})
        assert layer.errors, bad


@pytest.mark.parametrize("check_class", ["lemoncrow_verification", "trusted_security_feed"])
def test_a_required_check_of_a_class_nothing_produces_is_a_policy_error(check_class: str) -> None:
    """Every merge-result row is provider CI today: such a check could never pass, so it is refused."""

    for layer in (
        _operator({"merge_result": {"required_checks": [{"check": "lemoncrow/contracts", "class": check_class}]}}),
        parse_trusted_landing(
            f"landing:\n  merge_result:\n    required_checks:\n      - {{check: lemoncrow/contracts, class: {check_class}}}\n",
            source="base",
        ),
    ):
        assert any(f"class {check_class} is not supported yet" in error for error in layer.errors), layer.errors
        policy = merge_landing_policies([layer])
        assert policy.errors and policy.merge_result_checks == ()


def test_a_check_named_under_two_classes_is_a_policy_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import landing_policy

    monkeypatch.setattr(landing_policy, "PRODUCED_MERGE_CHECK_CLASSES", landing_policy.MERGE_CHECK_CLASSES)
    layer = _operator(
        {
            "merge_result": {
                "required_checks": [
                    {"check": "unit", "class": "provider_ci"},
                    {"check": "unit", "class": "lemoncrow_verification"},
                ]
            }
        }
    )
    assert any("under both provider_ci and lemoncrow_verification" in error for error in layer.errors), layer.errors


def test_landing_scm_status_contract() -> None:
    """case: test_blocking_reasons_use_product_words_lemoncrow_first"""
    rows = [
        _row("scm-requirement:ci", owner="scm"),
        _row("latest-snapshot-reviewed"),
        _row("verification", "fail", category="verification"),
        _row("merge-result", "fail"),
        _row("human-comments", "pass"),
    ]
    reasons = blocking_reasons(rows)
    assert [row["action"] for row in reasons] == [
        "Finish review",
        "Fix failing checks",
        "Merged result fails checks",
        "Provider requirement",
    ]
    assert len(blocking_reasons(rows, limit=2)) == 2
    assert action_for(_row("stack:abc")) == "Land dependencies first"
    assert action_for(_row("org-policy:reviewer:alice")) == "Approval needed"
    assert summary_text(reasons, shown=2) == "Finish review · Fix failing checks · 2 more"
    "case: test_the_scm_status_reflects_lemoncrow_conditions_only"
    ready = scm_status(_readiness(_row("scm-requirement:ci", owner="scm")))
    assert ready == {"state": "success", "description": "Merge readiness: ready", "context": STATUS_CONTEXT}
    waiting = scm_status(_readiness(_row("latest-snapshot-reviewed")))
    assert (waiting["state"], waiting["description"]) == ("pending", "Waiting: Finish review")
    blocked = scm_status(_readiness(_row("human-change-requests", "fail"), _row("latest-snapshot-reviewed")))
    assert blocked["state"] == "failure"
    assert blocked["description"] == "Blocked: Resolve requested changes · Finish review"
    assert scm_status(_readiness(_row("latest-snapshot-reviewed")), reviewed=True)["state"] == "success"
    approved = scm_status(_readiness(_row("latest-snapshot-reviewed")), auto_approval={"state": "approved"})
    assert approved["description"] == "Approved by LemonCrow policy"
    owed = scm_status(_readiness(_row("latest-snapshot-reviewed")), auto_approval={"state": "approving"})
    assert owed["state"] == "pending"
    "case: test_the_scm_status_description_is_bounded_with_an_ellipsis"
    many = [_row(f"extension:{index}", label="x" * 60) for index in range(10)]
    for row in many:
        row["label"] = f"Custom gate {row['id']} " + "y" * 50
    description = scm_status(_readiness(*many))["description"]
    assert len(description) == STATUS_DESCRIPTION_MAX and description.endswith("…")
    short = scm_status(_readiness(_row("latest-snapshot-reviewed")))["description"]
    assert len(short) < STATUS_DESCRIPTION_MAX and (not short.endswith("…"))
    "case: test_break_glass_is_named_in_the_status_and_only_its_bypassed_blockers_are_skipped"
    record = {"actor_id": "alice", "bypassed": ["latest-snapshot-reviewed"], "follow_up_due_at": "2026-10-08T12:00:00Z"}
    status = scm_status(_readiness(_row("latest-snapshot-reviewed")), break_glass=record)
    assert status["state"] == "success"
    assert status["description"] == "Break-glass by alice; follow-up review due 2026-10-08"
    other = scm_status(_readiness(_row("latest-snapshot-reviewed"), _row("human-comments", "fail")), break_glass=record)
    assert other["state"] == "failure" and "Resolve open comments" in other["description"]
    "case: test_the_merge_result_status_targets_the_merge_commit"
    assert merge_result_status(_readiness(_row("merge-result", target_sha=""))) is None
    failing = merge_result_status(_readiness(_row("merge-result", "fail", target_sha="m" * 40, detail="unit")))
    assert failing is not None and failing["state"] == "failure"
    passing = merge_result_status(_readiness(_row("merge-result", "pass", target_sha="m" * 40)))
    assert passing is not None and passing["state"] == "success"


def test_break_glass_authorization_contract() -> None:
    """case: test_break_glass_is_never_available_to_agents_services_or_unnamed_people"""
    for principal, message in [
        (None, "only for an authorized person"),
        (SimpleNamespace(kind="agent", principal_id="alice", reason="not a human sign-in"), "never use it"),
        (SimpleNamespace(kind="service", principal_id="alice", reason=""), "never use it"),
        (SimpleNamespace(kind="unknown", principal_id="alice", reason=""), "never use it"),
        (SimpleNamespace(kind="human", principal_id="", reason=""), "never use it"),
        (SimpleNamespace(kind="human", principal_id="mallory", reason=""), "not an authorized break-glass responder"),
    ]:
        with pytest.raises(bg.BreakGlassRefused, match=message) as refused:
            bg.authorize(principal, responders=("alice",), may_manage=True, enabled=True)
        assert refused.value.status == 403
    "case: test_a_named_responder_still_needs_policy_and_manage_permission"
    alice = SimpleNamespace(kind="human", principal_id="alice", reason="")
    assert bg.authorize(alice, responders=("alice",), may_manage=True, enabled=True) == "alice"
    with pytest.raises(bg.BreakGlassRefused, match="names no break-glass responders"):
        bg.authorize(alice, responders=("alice",), may_manage=True, enabled=False)
    with pytest.raises(bg.BreakGlassRefused, match="management permission"):
        bg.authorize(alice, responders=("alice",), may_manage=False, enabled=True)
    "case: test_a_break_glass_request_must_be_complete_and_bounded"
    for overrides in [
        {"reason": "too short"},
        {"reason": "x" * (bg.MAX_REASON_CHARS + 1)},
        {"incident_url": "javascript:alert(1)"},
        {"incident_url": "https://" + "a" * bg.MAX_INCIDENT_URL_CHARS},
        {"bypass": []},
        {"bypass": [f"c{index}" for index in range(bg.MAX_BYPASSED + 1)]},
        {"bypass": [3]},
        {"expected_conditions_hash": ""},
    ]:
        with pytest.raises(bg.BreakGlassRefused) as refused:
            bg.parse_request(_body(**overrides))
        assert refused.value.status == 400
    "case: test_bounds_at_the_limit_are_accepted"
    parsed = bg.parse_request(
        _body(
            reason="r" * bg.MAX_REASON_CHARS,
            bypass=[f"c{index}" for index in range(bg.MAX_BYPASSED)],
            incident_url="https://e.com/" + "a" * (bg.MAX_INCIDENT_URL_CHARS - len("https://e.com/")),
        )
    )
    assert len(parsed.bypass) == bg.MAX_BYPASSED and len(parsed.reason) == bg.MAX_REASON_CHARS
    assert len(bg.parse_request(_body(reason="r" * bg.MIN_REASON_CHARS)).reason) == bg.MIN_REASON_CHARS
    "case: test_break_glass_names_every_lemoncrow_blocker_and_never_an_scm_requirement"
    conditions = [
        _row("latest-snapshot-reviewed"),
        _row("human-comments", "fail"),
        _row("scm-requirement:ci", owner="scm"),
        _row("verification", "pass"),
        _row("policy-generation", "fail"),
    ]
    named = bg.bypassed_conditions(conditions, ["latest-snapshot-reviewed", "human-comments"])
    assert [row["id"] for row in named] == ["latest-snapshot-reviewed", "human-comments"]
    for bypass, message in (
        (["latest-snapshot-reviewed"], "name every LemonCrow blocker"),
        (["latest-snapshot-reviewed", "human-comments", "scm-requirement:ci"], "bypassed in the SCM itself"),
        (["latest-snapshot-reviewed", "human-comments", "verification"], "not a current LemonCrow blocker"),
        (["latest-snapshot-reviewed", "human-comments", "policy-generation"], "not a current LemonCrow blocker"),
        (["nope"], "not a current readiness condition"),
    ):
        with pytest.raises(bg.BreakGlassRefused, match=message) as refused:
            bg.bypassed_conditions(conditions, bypass)
        assert refused.value.status == 409
    assert [row["id"] for row in bg.effective_blockers(conditions, ["latest-snapshot-reviewed"])] == ["human-comments"]


def test_break_glass_audit_and_followup_contract() -> None:
    """case: test_the_follow_up_is_completed_only_by_another_persons_decision_after_break_glass"""
    created = datetime(2026, 10, 5, 12, tzinfo=UTC)
    record = {
        "id": "bg_1",
        "revision_id": "rev_1",
        "actor_id": "alice",
        "created_at": "2026-10-05T12:00:00Z",
        "follow_up": {"due_at": "2026-10-08T12:00:00Z", "hours": 72},
    }

    def outcome(reviewer: str, kind: str, at: datetime, revision: str = "rev_1") -> Any:
        return SimpleNamespace(
            revision_id=revision, outcome=kind, reviewer_id=reviewer, created_at=at.isoformat().replace("+00:00", "Z")
        )

    later = created + timedelta(hours=1)
    not_counted = [
        outcome("alice", "lgtm", later),
        outcome("bob", "comment", later),
        outcome("bob", "lgtm", created - timedelta(hours=1)),
        outcome("bob", "lgtm", later, revision="rev_2"),
    ]
    assert bg.follow_up_state(record, not_counted, now=later)["state"] == "open"
    assert bg.follow_up_state(record, not_counted, now=created + timedelta(hours=72))["state"] == "overdue"
    done = bg.follow_up_state(record, [*not_counted, outcome("bob", "changes_requested", later)], now=later)
    assert done["state"] == "completed" and done["completed_by"] == "bob"
    "case: test_the_signed_record_detail_names_actor_reason_incident_and_bypassed"
    record = {
        "id": "bg_1",
        "actor_id": "alice",
        "reason": "outage",
        "incident_url": "https://status.example.com/42",
        "bypassed": [{"id": "latest-snapshot-reviewed"}, {"id": "human-comments"}],
        "conditions_hash": "conditions:1",
        "policy_generation": "policy:1",
        "follow_up": {"due_at": "2026-10-08T12:00:00Z"},
    }
    assert bg.decision_detail(record) == {
        "basis": "break_glass",
        "break_glass_id": "bg_1",
        "actor": "alice",
        "reason": "outage",
        "incident": "https://status.example.com/42",
        "bypassed": "latest-snapshot-reviewed,human-comments",
        "conditions_hash": "conditions:1",
        "policy_generation": "policy:1",
        "follow_up_due_at": "2026-10-08T12:00:00Z",
    }
    "case: test_the_inbox_names_the_exact_blocking_actions_from_the_content_free_record"
    from lemoncrow.pro.capabilities.review.workflow_state import WorkflowFacts, derive_workflow_state

    waiting = _inbox_fact(
        _record(
            {"id": "required-check:ci", "owner": "scm", "category": "scm", "state": "pending", "label": "secret"},
            {"id": "latest-snapshot-reviewed", "owner": "reviewer", "category": "human_review", "state": "pending"},
        )
    )
    assert waiting.state == "not_ready"
    assert waiting.reason == "Waiting: Finish review · Provider requirement"
    assert [row["action"] for row in waiting.blocking] == ["Finish review", "Provider requirement"]
    assert "secret" not in str(waiting.to_wire())
    failing = _inbox_fact(_record({"id": "merge-result", "owner": "lemoncrow", "category": "scm", "state": "fail"}))
    assert failing.reason == "Blocked: Merged result fails checks"
    decision = derive_workflow_state(WorkflowFacts(review_complete=True, readiness=failing))
    assert decision.explanation == "Review complete; Blocked: Merged result fails checks"
    "case: test_inbox_blockers_past_the_record_bound_are_counted_never_dropped"
    from lemoncrow.pro.capabilities.review.workflow_state import READINESS_BLOCKER_WIRE_LIMIT

    def rows(count: int) -> list[dict[str, str]]:
        return [
            {"id": f"premerge:{index}", "owner": "lemoncrow", "category": "verification", "state": "pending"}
            for index in range(count)
        ]

    at = _inbox_fact(_record(*rows(READINESS_BLOCKER_WIRE_LIMIT)))
    assert at.reason == "Waiting: Waiting for checks" and len(at.blocking) == READINESS_BLOCKER_WIRE_LIMIT
    past = _inbox_fact(_record(*rows(READINESS_BLOCKER_WIRE_LIMIT + 3)))
    assert past.blocker_count == READINESS_BLOCKER_WIRE_LIMIT + 3
    assert past.reason == "Waiting: Waiting for checks · 3 more"
    assert len(past.blocking) == READINESS_BLOCKER_WIRE_LIMIT
    bare = dict(_record(), blocker_count=1, blockers=[])
    assert _inbox_fact(bare).reason == "1 readiness condition(s) not satisfied"


def _side(*rows: tuple[str, str]) -> tuple[SideResult, ...]:
    return side_results([{"identity": name, "name": name, "status": status} for name, status in rows])


def _merge(**overrides: Any) -> MergeResultFacts:
    values: dict[str, Any] = {
        "target_kind": "merge_group",
        "target_sha": "m" * 40,
        "base_sha": "b" * 40,
        "head_sha": "h" * 40,
        "head": _side(("unit", "success"), ("lint", "success")),
        "base": _side(("unit", "success"), ("lint", "failure")),
        "merge": _side(("unit", "success"), ("lint", "failure")),
    }
    values.update(overrides)
    return MergeResultFacts(**values)


def _row(condition_id: str, state: str = "pending", owner: str = "lemoncrow", **extra: Any) -> dict[str, Any]:
    return {"id": condition_id, "label": condition_id, "state": state, "owner": owner, "category": "", **extra}


def _readiness(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"conditions": list(rows)}


def _body(**overrides: Any) -> dict[str, Any]:
    return {
        "reason": "Payment outage: hotfix the retry loop",
        "incident_url": "https://status.example.com/incidents/42",
        "bypass": ["latest-snapshot-reviewed"],
        "expected_revision_id": "rev_1",
        "expected_conditions_hash": "conditions:1",
        **overrides,
    }


def _record(*rows: dict[str, str]) -> dict[str, Any]:
    from lemoncrow.pro.capabilities.review.workflow_state import readiness_summary_payload

    return readiness_summary_payload(
        {"ready": False, "conditions": list(rows), "evaluated_at": "2026-10-05T11:00:00Z"},
        review_id="rev-1",
        revision_id="rrv-1",
    )


def _inbox_fact(payload: dict[str, Any]) -> Any:
    from lemoncrow.pro.capabilities.review.workflow_state import readiness_fact

    return readiness_fact(payload, revision_id="rrv-1", watermark="", now=datetime(2026, 10, 5, 12, tzinfo=UTC))


def test_an_open_break_glass_follow_up_keeps_a_merged_review_in_monitoring(tmp_path: Any) -> None:
    from lemoncrow.pro.capabilities.review.inbox_inputs import SnapshotRef, load_stored_inbox_facts
    from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession
    from lemoncrow.pro.capabilities.review.store import ReviewStore, new_revision_id, new_session_id
    from lemoncrow.pro.capabilities.review.workflow_state import WorkflowFacts, derive_workflow_state

    store = ReviewStore(tmp_path)
    store.init()
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="local_change",
            repo_root="/repo/alpha",
            range_mode="working_tree",
            title="hotfix",
            source_ref="",
        )
    )
    revision = store.add_revision(
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
    refs = [SnapshotRef(review_id=session.id, revision_id=revision.id)]
    now = datetime.now(UTC)

    def running() -> int:
        return load_stored_inbox_facts(store, refs, viewer="alice", now=now).post_merge_running.get(session.id, 0)

    assert running() == 0
    bg.record_break_glass(
        store,
        review_id=session.id,
        revision=revision,
        actor_id="alice",
        request=bg.parse_request(_body()),
        bypassed=[{"id": "latest-snapshot-reviewed", "owner": "reviewer", "state": "pending"}],
        readiness={"conditions_hash": "c:1"},
        follow_up_hours=72,
        now=(now - timedelta(minutes=1)).isoformat(),
    )
    assert running() == 1
    merged = derive_workflow_state(WorkflowFacts(merged=True, post_merge_running=running()))
    assert merged.state == "monitoring"
    store.record_outcome(session.id, revision.id, "alice", outcome="lgtm")
    assert running() == 1
    store.record_outcome(session.id, revision.id, "bob", outcome="lgtm")
    assert running() == 0

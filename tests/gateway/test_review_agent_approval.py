"""Human approval before risky agent actions (Phase 12 item 14, P12-AGENT).

Each acceptance property is asserted with its refusal beside it: a configured
class blocks until a human decides; a denial never runs and is recorded; an
approval runs once and shows as pre-approved; a differing observed action is
not covered; an unreachable service fails closed; agents and service principals
cannot approve; unconfigured classes pass through; approvals expire.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review import action_approval
from lemoncrow.pro.capabilities.review.action_approval import (
    DEFAULT_WAIT_SECONDS,
    MAX_ACTION_CHARS,
    MAX_DECISION_TTL,
    MAX_PATTERN_TOKENS,
    MAX_PATTERN_WILDCARDS,
    MAX_PENDING_PER_SESSION,
    MAX_WAIT_SECONDS,
    MIN_DECISION_TTL,
    MIN_WAIT_SECONDS,
    PENDING_TTL_SECONDS,
    ApprovalConfigError,
    ApprovalPolicy,
    action_allowed_by,
    action_digest,
    annotate_observed,
    clamp_wait,
    classify_action,
    pattern_covers,
    policy_from_settings,
    read_policy,
    unclassifiable_parts,
    validate_pattern,
)
from lemoncrow.pro.capabilities.review.action_approval_api import ApprovalPrincipal
from lemoncrow.pro.capabilities.review.agent_ledger import MAX_SHELL_DEPTH
from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.approval_hook import (
    HookOutcome,
    ServiceUnavailable,
    main_from_text,
    resolve_review,
    run_gate,
    tool_action,
)
from lemoncrow.pro.capabilities.review.development import push_events
from lemoncrow.pro.capabilities.review.development_ingest import map_ledger_side_effect
from tests.gateway.test_review_development import BASE, PORT, TOKEN, Change, _at, _command, _edit

HEADERS = {"Authorization": f"Bearer {TOKEN}"}
T0 = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    monkeypatch.setenv("LEMONCROW_LIVE_REVIEW_PREWARM", "0")
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "lemoncrow-root"))


class Clock:

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def at(self, minutes: float) -> None:
        self.now = T0 + timedelta(minutes=minutes)

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock(T0)
    monkeypatch.setattr(action_approval, "utc_now", fake)
    return fake


@dataclass
class Caller:
    kind: str = "agent"
    principal: str = "dev-token"

    def __call__(self) -> ApprovalPrincipal:
        return ApprovalPrincipal(self.kind, self.principal)

    def human(self, principal: str = "alice") -> None:
        self.kind, self.principal = ("human", principal)

    def agent(self, principal: str = "dev-token") -> None:
        self.kind, self.principal = ("agent", principal)


def _configure(**classes: bool) -> None:
    root = Path(os.environ["LEMONCROW_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin_settings.json").write_text(
        json.dumps({f"review.approval.{name}": value for name, value in classes.items()}), encoding="utf-8"
    )


@dataclass
class Gate:
    change: Change
    client: TestClient
    caller: Caller
    review_id: str = ""
    agent: dict[str, str] = field(default_factory=lambda: {"host": "claude", "session_id": "author"})

    def url(self, tail: str = "") -> str:
        return f"/api/reviews/{self.review_id}/agent-approvals{tail}"

    def request(self, action: str, *, kind: str = "command", classes: list[str] | None = None, **extra: Any) -> Any:
        self.caller.agent()
        body = {**self.agent, "kind": kind, "action": action, "cwd": str(self.change.repo), **extra}
        if classes is not None:
            body["classes"] = classes
        return self.client.post(self.url(), headers=HEADERS, json=body)

    def claim(
        self, approval_id: str, action: str, *, kind: str = "command", cwd: str | None = None, **agent: str
    ) -> dict[str, Any]:
        self.caller.agent()
        body = {**self.agent, **agent, "kind": kind, "action": action, "cwd": cwd or str(self.change.repo)}
        response = self.client.post(self.url(f"/{approval_id}/claim"), headers=HEADERS, json=body)
        assert response.status_code == 200, response.text
        payload: dict[str, Any] = response.json()
        return payload

    def decide(self, approval: Mapping[str, Any], decision: str = "approve", **body: Any) -> Any:
        payload = {"decision": decision, "expected_action_digest": approval["action_digest"], **body}
        return self.client.post(self.url(f"/{approval['id']}/decision"), headers=HEADERS, json=payload)

    def listing(self) -> dict[str, Any]:
        response = self.client.get(self.url(), headers=HEADERS)
        assert response.status_code == 200, response.text
        payload: dict[str, Any] = response.json()
        return payload


def _gate(tmp_path: Path, *, authority: bool = True, separate: bool = True) -> Gate:
    change = Change(tmp_path)
    revision = change.snapshot()
    change.store.publish_snapshot(change.session.id, revision.id)
    caller = Caller()
    app = FastAPI()
    register_review_api(
        app,
        change.store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=change.repo,
        port=PORT,
        source_repo_root=change.repo,
        source_store_root=change.store_root,
        approval_authority=caller if authority else None,
        approval_separate_requester=separate,
    )
    return Gate(change, TestClient(app, base_url=BASE), caller, review_id=change.session.id)


def test_agent_action_classification_contract() -> None:
    """case: test_actions_are_classified_into_every_gated_class_they_touch"""
    for kind, action, expected in [
        ("command", "git push origin HEAD", {"git_push"}),
        ("command", "git push --force origin main", {"git_push"}),
        ("command", "npm install left-pad", {"dependency_install"}),
        ("command", "npm update", {"dependency_install"}),
        ("command", "poetry update requests", {"dependency_install"}),
        ("command", "uv lock --upgrade", {"dependency_install"}),
        ("command", "alembic upgrade head", {"schema_migration"}),
        ("command", "psql -c 'drop table users'", {"schema_migration"}),
        ("command", "curl https://api.example.com/v1", {"network_cloud"}),
        ("command", "aws s3 ls", {"network_cloud"}),
        ("command", "cat ~/.aws/credentials", {"secret_access"}),
        ("command", "cp tool /usr/local/bin/tool", {"outside_write"}),
        ("write", "/etc/hosts", {"outside_write"}),
        ("command", "git push origin HEAD && npm install left-pad", {"git_push", "dependency_install"}),
        ("command", "lc settings set review.approval.git_push false", {"supervision_change"}),
        ("command", "ls -la && uv run pytest -q", set()),
        ("command", "npm install", {"dependency_install"}),
        ("write", "/work/repo/src/app.py", set()),
    ]:
        assert classify_action(kind, action, repo_root="/work/repo") == frozenset(expected)
    "case: test_wrapped_commands_are_classified_by_what_they_run"
    for action, expected in [
        ('bash -c "pip install requests"', {"dependency_install"}),
        ('sh -lc "git push --force"', {"git_push"}),
        ("zsh -ec 'npm i -g foo'", {"dependency_install"}),
        ('dash -c "curl https://api.example.com"', {"network_cloud"}),
        ('bash -o pipefail -c "git push origin main"', {"git_push"}),
        ('bash -c "ls && git push"', {"git_push"}),
        ('bash -c "ls" > /etc/motd', {"outside_write"}),
        ('eval "pip install x"', {"dependency_install"}),
        ('builtin eval "git push"', {"git_push"}),
        ("xargs pip install < reqs.txt", {"dependency_install"}),
        ("xargs -I {} pip install {}", {"dependency_install"}),
        ("xargs git push", {"git_push"}),
        ("timeout 10s pip install x", {"dependency_install"}),
        ("timeout -s KILL 5 git push", {"git_push"}),
        ("nice -n 5 git push", {"git_push"}),
        ("nohup git push &", {"git_push"}),
        ("command git push", {"git_push"}),
        ("exec git push", {"git_push"}),
        ("stdbuf -oL git push", {"git_push"}),
        ("stdbuf -o L git push", {"git_push"}),
        ("doas -u deploy git push", {"git_push"}),
        ("sudo -u root git push", {"git_push"}),
        ('env -S "git push origin"', {"git_push"}),
        ("echo $(git push)", {"git_push"}),
        ('echo "$(git push)"', {"git_push"}),
        ("echo `git push`", {"git_push"}),
        ("(git push)", {"git_push"}),
        ("{ git push; }", {"git_push"}),
        ("if true; then git push; fi", {"git_push"}),
        ('bash -c "ls -la"', set()),
        ('echo "git push"', set()),
        ("echo 'a; git push'", set()),
        ("echo '$(git push)'", set()),
        ("xargs -n1 echo", set()),
        ("git status 2>&1 | head", set()),
    ]:
        assert classify_action("command", action, repo_root="/work/repo") == frozenset(expected)
        assert unclassifiable_parts("command", action) == ()
    "case: test_a_command_computed_at_run_time_is_every_class"
    for action, reason in [
        ("$(echo pip) install x", "is computed only when it runs"),
        ("`echo git` push", "is computed only when it runs"),
        ("$CMD install x", "is computed only when it runs"),
        ('"$PIP" install x', "is computed only when it runs"),
        ('sudo "$TOOL" push', "is computed only when it runs"),
        ('eval "$CMD"', "is computed only when it runs"),
        ('echo "git push" | bash', "piped into a shell"),
        ("curl https://get.example.com | sh", "piped into a shell"),
    ]:
        "Fail closed: its effect cannot be read, so each configured class gates it."
        assert classify_action("command", action) >= frozenset(action_approval.RISK_CLASSES)
        parts = unclassifiable_parts("command", action)
        assert parts and reason in parts[0]
        assert ApprovalPolicy(frozenset({"secret_access"})).required(classify_action("command", action)) == {
            "secret_access"
        }
        assert ApprovalPolicy().required(classify_action("command", action)) == frozenset()
    "case: test_nested_command_text_is_read_to_the_bound_and_fails_closed_past_it"

    def nested(levels: int) -> str:
        command = "git push"
        for _ in range(levels):
            command = "bash -c " + shlex.quote(command)
        return command

    assert classify_action("command", nested(MAX_SHELL_DEPTH)) == {"git_push"}
    assert unclassifiable_parts("command", nested(MAX_SHELL_DEPTH)) == ()
    past = nested(MAX_SHELL_DEPTH + 1)
    assert classify_action("command", past) == frozenset(action_approval.RISK_CLASSES)
    assert "more than 4 levels deep" in unclassifiable_parts("command", past)[0]
    assert classify_action("command", 'eval "eval \\"eval \\\\\\"eval git push\\\\\\"\\""') == {"git_push"}
    assert classify_action("command", "echo $(echo $(echo $(echo $(git push))))") == {"git_push"}
    assert classify_action("command", "echo $(echo $(echo $(echo $(echo $(git push)))))") == frozenset(
        action_approval.RISK_CLASSES
    )
    "case: test_the_hook_wait_is_bounded"
    for raw, expected in [
        (MIN_WAIT_SECONDS - 1, MIN_WAIT_SECONDS),
        (MIN_WAIT_SECONDS, MIN_WAIT_SECONDS),
        (MAX_WAIT_SECONDS, MAX_WAIT_SECONDS),
        (MAX_WAIT_SECONDS + 1, MAX_WAIT_SECONDS),
        ("junk", DEFAULT_WAIT_SECONDS),
    ]:
        assert clamp_wait(raw) == expected
    "case: test_normalization_is_minimal_so_different_text_is_a_different_action"
    assert action_digest("command", "git push origin main\r\n") == action_digest("command", "  git push origin main")
    assert action_digest("command", "git push origin main") != action_digest("command", "git  push origin main")
    assert action_digest("command", "git push origin main") != action_digest("command", "git push 'origin' main")
    assert action_digest("write", "/etc/hosts") != action_digest("command", "/etc/hosts")


def test_agent_approval_pattern_contract() -> None:
    """case: test_patterns_cover_one_plain_token_per_wildcard_and_nothing_else"""
    action = "git push origin feature-1"
    pattern = validate_pattern("command", "git push origin *", action)
    assert pattern_covers("command", pattern, "git push origin feature-2")
    for other in (
        "git push origin --force",
        "git push origin a b",
        "git push origin x; rm -rf /",
        "git push origin $(curl evil)",
        "git push origin 'x'",
        "git push  origin x",
        "git push upstream x",
    ):
        assert not pattern_covers("command", pattern, other), other
    with pytest.raises(ValueError, match="cover the action"):
        validate_pattern("command", "git push upstream *", action)
    with pytest.raises(ValueError, match="plain words"):
        validate_pattern("command", "* push origin main", "git push origin main")
    with pytest.raises(ValueError, match="plain words"):
        validate_pattern("command", "git push origin * && *", "git push origin main && ls")
    at_bound = " ".join(["cmd"] + ["*"] * MAX_PATTERN_WILDCARDS)
    assert validate_pattern("command", at_bound, " ".join(["cmd"] + ["x"] * MAX_PATTERN_WILDCARDS)) == at_bound
    past = " ".join(["cmd"] + ["*"] * (MAX_PATTERN_WILDCARDS + 1))
    with pytest.raises(ValueError, match="wildcards"):
        validate_pattern("command", past, " ".join(["cmd"] + ["x"] * (MAX_PATTERN_WILDCARDS + 1)))
    words = ["w"] * MAX_PATTERN_TOKENS
    assert validate_pattern("command", " ".join(words), " ".join(words))
    with pytest.raises(ValueError, match="at most"):
        validate_pattern("command", " ".join([*words, "w"]), " ".join([*words, "w"]))
    "case: test_write_patterns_cover_exactly_one_component"
    pattern = validate_pattern("write", "/srv/out/*", "/srv/out/a.txt")
    assert pattern_covers("write", pattern, "/srv/out/b.txt")
    for other in ("/srv/out/sub/a.txt", "/srv/out/../etc/passwd", "/srv/out/./a", "/srv/outx/a", "/srv/out/"):
        assert not pattern_covers("write", pattern, other), other
    with pytest.raises(ValueError):
        validate_pattern("write", "/srv/*/a.txt", "/srv/out/a.txt")
    with pytest.raises(ValueError):
        validate_pattern("write", "/srv/../out/*", "/srv/../out/a")
    "case: test_a_once_approval_covers_exactly_one_matching_observed_action"
    rows = [
        _observed("git push origin main", 2),
        _observed("git push origin main", 3),
        _observed("git push --force origin main", 4),
        _observed("git push origin main", 20),
        _observed("git push origin main", 2.5, session_id="other"),
        _observed("git push origin main", 2.6, source="api"),
    ]
    out = annotate_observed(rows, [_record()], ApprovalPolicy())
    statuses = [(row["approval"] or {}).get("status") for row in out]
    assert statuses == ["pre_approved", "unapproved", "unapproved", "unapproved", None, "unapproved"]
    assert out[0]["approval"]["decided_by"] == "alice"
    "case: test_a_differing_action_neither_borrows_nor_consumes_a_once_approval"
    rows = [_observed("git push --force origin main", 1.8), _observed("git push origin main", 2)]
    statuses = [row["approval"]["status"] for row in annotate_observed(rows, [_record()], ApprovalPolicy())]
    assert statuses == ["unapproved", "pre_approved"]
    "case: test_an_unclaimed_once_approval_covers_nothing"
    out = annotate_observed([_observed("git push origin main", 2)], [_record(claimed_at="")], ApprovalPolicy())
    assert out[0]["approval"] == {"status": "unapproved"}
    "case: test_a_pattern_approval_covers_only_complete_matching_text"
    record = _record(scope="pattern", pattern="git push origin *", claimed_at="")
    truncated = _observed("git push origin feature", 3)
    truncated["action_digest"] = action_digest("command", "git push origin feature-with-a-much-longer-name")
    rows = [_observed("git push origin feature", 2), truncated, _observed("git push origin --force", 4)]
    statuses = [row["approval"]["status"] for row in annotate_observed(rows, [record], ApprovalPolicy())]
    assert statuses == ["pre_approved", "unapproved", "unapproved"]
    "case: test_unconfigured_and_unrequested_classes_carry_no_approval_label"
    rows = [_observed("curl https://example.com", 2, category="network")]
    assert annotate_observed(rows, [_record()], ApprovalPolicy())[0]["approval"] is None
    assert annotate_observed(rows, [], ApprovalPolicy(frozenset({"network_cloud"})))[0]["approval"] == {
        "status": "unapproved"
    }
    assert annotate_observed(rows, [], None)[0]["approval"] == {"status": "unapproved"}


def test_agent_approval_ledger_contract() -> None:
    """case: test_ledger_side_effects_carry_the_exact_untruncated_action_digest"""
    command = "git push origin " + "x" * 400
    raw = {"kind": "command_result", "at": _at(1), "summary": command, "payload": {"command": command}}
    draft = map_ledger_side_effect(raw, host="claude", session_id="s", repo_root="/work/repo", order=0)
    assert draft is not None and draft.task_id == action_digest("command", command)
    assert len(draft.detail) == 300
    write = {"kind": "file_edit", "at": _at(1), "payload": {"path": "/etc/hosts"}}
    edited = map_ledger_side_effect(write, host="claude", session_id="s", repo_root="/work/repo", order=0)
    assert edited is not None and edited.task_id == action_digest("write", "/etc/hosts")
    "case: test_hook_passes_unconfigured_classes_and_ordinary_actions_untouched"
    service = FakeService(down=True)
    for payload in (_payload("curl https://example.com"), _payload("ls -la"), _payload("x", tool="Read")):
        outcome, _ = _run(payload, service)
        assert outcome.decision == "pass" and outcome.render() == ""
    outcome, _ = _run(_payload(), service, policy=ApprovalPolicy())
    assert outcome.render() == ""
    assert service.requests == []


def test_agent_approval_hook_failure_contract() -> None:
    """case: test_hook_blocks_until_a_human_approves_and_never_force_allows"""
    service = FakeService(
        [
            {"granted": False, "reason": "pending", "approval": {}},
            {"granted": True, "reason": "", "approval": {"decided_by": "alice"}},
        ]
    )
    outcome, ticks = _run(_payload(), service)
    assert outcome.decision == "pass" and service.claims == 2 and (ticks.now > 0)
    rendered = json.loads(outcome.render())
    assert "permissionDecision" not in json.dumps(rendered)
    assert "approved by alice" in rendered["systemMessage"]
    ((review_id, body),) = service.requests
    assert review_id == "rv1" and body["classes"] == ["git_push"] and (body["action"] == "git push origin main")
    "case: test_hook_denies_a_denied_action_with_the_reviewers_reason"
    service = FakeService(
        [{"granted": False, "reason": "denied", "approval": {"decided_by": "alice", "note": "not on Friday"}}]
    )
    outcome, _ = _run(_payload(), service)
    assert _decision(outcome) == "deny" and "Denied by alice" in outcome.reason and ("not on Friday" in outcome.reason)
    "case: test_hook_times_out_closed"
    outcome, ticks = _run(_payload(), FakeService(), policy=ApprovalPolicy(frozenset({"git_push"}), 10))
    assert _decision(outcome) == "deny" and "No decision within 10s" in outcome.reason
    assert ticks.now >= 10
    "case: test_hook_fails_closed_when_the_service_is_unreachable"
    outcome, _ = _run(_payload(), FakeService(down=True))
    assert _decision(outcome) == "deny"
    assert "could not be reached" in outcome.reason and "fail closed" in outcome.reason
    assert "Git push" in outcome.reason
    discovery, _ = _run(_payload(), FakeService(down=True), env={})
    assert _decision(discovery) == "deny" and "could not be reached" in discovery.reason
    assert "fail closed" in discovery.reason
    "case: test_hook_names_a_command_it_cannot_read_and_gates_it_as_every_configured_class"
    service = FakeService(down=True)
    outcome, _ = _run(_payload("$(echo pip) install left-pad"), service)
    assert _decision(outcome) == "deny" and "Git push" in outcome.reason
    assert "`$(echo pip) install left-pad` is computed only when it runs" in outcome.reason
    wrapped, _ = _run(_payload('bash -lc "git push --force"'), FakeService())
    assert _decision(wrapped) == "deny" and "No decision within" in wrapped.reason
    assert "cannot be read" not in wrapped.reason
    untouched, _ = _run(_payload("$(echo pip) install left-pad"), service, policy=ApprovalPolicy())
    assert untouched.render() == ""
    "case: test_hook_fails_closed_on_unreadable_settings_for_risky_actions_only"

    def broken() -> ApprovalPolicy:
        raise ApprovalConfigError("plugin_settings.json is not valid JSON")

    outcome, _ = _run(_payload(), FakeService(), policy=broken)
    assert _decision(outcome) == "deny" and "could not be read" in outcome.reason
    harmless, _ = _run(_payload("ls"), FakeService(), policy=broken)
    assert harmless.render() == ""


def test_agent_approval_review_resolution_contract() -> None:
    """case: test_hook_resolves_the_review_conservatively"""
    service = FakeService()

    def git(cwd: str, *args: str) -> str:
        return "/work/repo"

    with pytest.raises(LookupError, match="LEMONCROW_REVIEW_ID"):
        resolve_review(service, env={}, cwd="/work/repo", local=False, git=git)
    with pytest.raises(LookupError, match="no open LemonCrow Review"):
        resolve_review(service, env={}, cwd="/work/repo", local=True, git=git)
    service.rows = [
        {"id": "r1", "status": "open", "worktree_root": "/work/repo"},
        {"id": "r2", "status": "finished", "worktree_root": "/work/repo"},
        {"id": "r3", "status": "open", "worktree_root": "/elsewhere"},
    ]
    assert resolve_review(service, env={}, cwd="/work/repo", local=True, git=git) == "r1"
    service.rows.append({"id": "r4", "status": "open", "worktree_root": "/work/repo"})
    with pytest.raises(LookupError, match="2 open Reviews"):
        resolve_review(service, env={}, cwd="/work/repo", local=True, git=git)
    assert resolve_review(service, env={"LEMONCROW_REVIEW_ID": "r9"}, cwd="/", local=False, git=git) == "r9"
    outcome, _ = _run(_payload(), service, env={}, local=False)
    assert _decision(outcome) == "deny" and "No Review to ask" in outcome.reason
    "case: test_hook_gates_writes_and_joins_relative_paths_without_normalizing"
    assert tool_action(_payload("/etc/hosts", tool="Write")) == ("write", "/etc/hosts")
    assert tool_action(_payload("../../etc/hosts", tool="Edit")) == ("write", "/work/repo/../../etc/hosts")
    service = FakeService()
    outcome, _ = _run(
        _payload("/etc/hosts", tool="Write"), service, policy=ApprovalPolicy(frozenset({"outside_write"}), 5)
    )
    assert _decision(outcome) == "deny" and service.requests[0][1]["kind"] == "write"


def test_writes_to_the_approval_settings_or_hook_are_a_supervision_change(tmp_path: Path) -> None:
    settings = str(Path.home() / ".lemoncrow" / "plugin_settings.json")
    assert "supervision_change" in classify_action("write", settings, repo_root="/work/repo")
    assert "supervision_change" in classify_action("write", "/home/u/.claude/settings.json", repo_root="/w")
    assert "supervision_change" in classify_action("command", "echo {} > ~/.lemoncrow/plugin_settings.json")
    assert ApprovalPolicy().required({"supervision_change"}) == frozenset()
    assert ApprovalPolicy(frozenset({"git_push"})).required({"supervision_change"}) == {"supervision_change"}


def test_classes_default_off_and_corrupt_settings_fail_closed(tmp_path: Path) -> None:
    assert read_policy(tmp_path / "missing").classes == frozenset()
    assert policy_from_settings({}).classes == frozenset()
    assert policy_from_settings({"review.approval.git_push": True, "review.approval.network_cloud": "on"}).classes == {
        "git_push",
        "network_cloud",
    }
    assert policy_from_settings({"lemoncrow": {}, "review.approval.git_push": "no"}).classes == frozenset()
    root = tmp_path / "root"
    root.mkdir()
    (root / "plugin_settings.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ApprovalConfigError):
        read_policy(root)
    (root / "plugin_settings.json").write_text(json.dumps({"lemoncrow": {"review.approval.git_push": True}}))
    assert read_policy(root).classes == {"git_push"}


def _record(**fields: Any) -> dict[str, Any]:
    base = {
        "id": "apr_1",
        "status": "approved",
        "classes": ["git_push"],
        "kind": "command",
        "action": "git push origin main",
        "action_digest": action_digest("command", "git push origin main"),
        "host": "claude",
        "session_id": "author",
        "decided_by": "alice",
        "decided_at": _at(1),
        "valid_until": _at(16),
        "claimed_at": _at(1.5),
        "scope": "once",
        "pattern": "",
    }
    return {**base, **fields}


def _observed(action: str, minutes: float, **fields: Any) -> dict[str, Any]:
    return {
        "category": "git_push",
        "detail": action,
        "host": "claude",
        "session_id": "author",
        "at": _at(minutes),
        "action_digest": action_digest("command", action),
        "source": "run_ledger",
        **fields,
    }


def test_an_agent_cannot_report_its_own_approval_event(tmp_path: Path) -> None:
    change = Change(tmp_path)
    with pytest.raises(ValueError, match="human decision"):
        push_events(
            change.development,
            change.session.id,
            [{"kind": "approval", "host": "claude", "session_id": "a", "summary": "Approved git push"}],
        )


def test_a_configured_class_waits_for_a_human_and_an_approval_runs_once(tmp_path: Path, clock: Clock) -> None:
    _configure(git_push=True)
    gate = _gate(tmp_path)
    filed = gate.request("git push origin main", classes=["git_push"])
    assert filed.status_code == 200, filed.text
    approval = filed.json()["approval"]
    assert approval["status"] == "pending" and approval["classes"] == ["git_push"]
    assert approval["requested_by"] == "dev-token"
    assert gate.request("git push origin main", classes=["git_push"]).json()["approval"]["id"] == approval["id"]
    assert gate.claim(approval["id"], "git push origin main")["granted"] is False
    gate.caller.human("alice")
    wrong = gate.client.post(
        gate.url(f"/{approval['id']}/decision"),
        headers=HEADERS,
        json={"decision": "approve", "expected_action_digest": action_digest("command", "git push --force")},
    )
    assert wrong.status_code == 409 and "different action" in wrong.text
    decided = gate.decide(approval)
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["approval"]["status"] == "approved" and body["approval"]["decided_by"] == "alice"
    assert body["approval"]["scope"] == "once" and body["development_recorded"] is True
    differing = gate.claim(approval["id"], "git push --force origin main")
    assert differing["granted"] is False and "differs" in differing["reason"]
    gate.caller.agent()
    stolen = gate.client.post(
        gate.url(f"/{approval['id']}/claim"),
        headers=HEADERS,
        json={"host": "claude", "session_id": "other", "kind": "command", "action": "git push origin main"},
    )
    assert stolen.status_code == 403
    first = gate.claim(approval["id"], "git push origin main")
    assert first["granted"] is True and first["approval"]["used"] is True
    again = gate.claim(approval["id"], "git push origin main")
    assert again["granted"] is False and "already used" in again["reason"]
    assert gate.request("git push origin main", classes=["git_push"]).json()["approval"]["status"] == "pending"


def test_a_denied_action_never_runs_and_the_denial_is_recorded(tmp_path: Path, clock: Clock) -> None:
    _configure(dependency_install=True)
    gate = _gate(tmp_path)
    approval = gate.request("npm install left-pad", classes=["dependency_install"]).json()["approval"]
    gate.caller.human("alice")
    denied = gate.decide(approval, "deny", note="use the vendored copy")
    assert denied.status_code == 200, denied.text
    assert denied.json()["approval"]["status"] == "denied"
    claim = gate.claim(approval["id"], "npm install left-pad")
    assert claim["granted"] is False and claim["reason"] == "denied"
    assert claim["approval"]["note"] == "use the vendored copy"
    gate.caller.human("alice")
    assert gate.decide(approval).status_code == 409
    events = gate.client.get(f"/api/reviews/{gate.review_id}/development/events?scope=all", headers=HEADERS).json()
    rows = [row for row in events["events"] if row["kind"] == "approval"]
    assert len(rows) == 1
    assert rows[0]["status"] == "denied" and "npm install left-pad" in rows[0]["summary"]
    assert "Denied by alice" in rows[0]["detail"] and "Action (command): npm install left-pad" in rows[0]["detail"]
    development = gate.client.get(f"/api/reviews/{gate.review_id}/development", headers=HEADERS).json()
    assert [(row["status"], row["action"]) for row in development["agent_approvals"]] == [
        ("denied", "npm install left-pad")
    ]
    behavior = gate.client.get(f"/api/reviews/{gate.review_id}/behavior", headers=HEADERS).json()["behavior"]
    assert behavior is not None
    assert [row["status"] for row in behavior["agent_approvals"]] == ["denied"]


def test_only_a_human_principal_can_decide(tmp_path: Path, clock: Clock) -> None:
    _configure(git_push=True)
    gate = _gate(tmp_path)
    approval = gate.request("git push origin main", classes=["git_push"]).json()["approval"]
    for kind, principal in (("agent", "dev-token"), ("service", "svc"), ("unknown", ""), ("human", "")):
        gate.caller.kind, gate.caller.principal = (kind, principal)
        refused = gate.decide(approval)
        assert refused.status_code == 403, (kind, refused.text)
        assert "only a signed-in human" in refused.text
    gate.caller.human("dev-token")
    self_approval = gate.decide(approval)
    assert self_approval.status_code == 403 and "cannot decide it" in self_approval.text
    assert gate.listing()["approvals"][0]["status"] == "pending"


def test_a_server_without_an_approval_authority_refuses_every_decision(tmp_path: Path, clock: Clock) -> None:
    _configure(git_push=True)
    gate = _gate(tmp_path, authority=False)
    approval = gate.request("git push origin main", classes=["git_push"]).json()["approval"]
    assert approval["requested_by"] == "agent:claude:author"
    refused = gate.decide(approval)
    assert refused.status_code == 403


def test_approvals_and_requests_expire(tmp_path: Path, clock: Clock) -> None:
    _configure(git_push=True)
    gate = _gate(tmp_path)
    approval = gate.request("git push origin main", classes=["git_push"]).json()["approval"]
    gate.caller.human("alice")
    for ttl in (MIN_DECISION_TTL - 1, MAX_DECISION_TTL + 1, True, "60"):
        assert gate.decide(approval, ttl_seconds=ttl).status_code == 422, ttl
    decided = gate.decide(approval, ttl_seconds=MIN_DECISION_TTL)
    assert decided.status_code == 200, decided.text
    clock.advance(MIN_DECISION_TTL - 1)
    assert gate.listing()["approvals"][0]["status"] == "approved"
    clock.advance(1)
    expired = gate.claim(approval["id"], "git push origin main")
    assert expired["granted"] is False and expired["reason"] == "expired"
    assert gate.listing()["approvals"][0]["status"] == "expired"
    pending = gate.request("git push origin feature", classes=["git_push"]).json()["approval"]
    clock.advance(PENDING_TTL_SECONDS - 1)
    assert gate.claim(pending["id"], "git push origin feature")["reason"] == "pending"
    clock.advance(1)
    assert gate.claim(pending["id"], "git push origin feature")["reason"] == "expired"
    gate.caller.human("alice")
    late = gate.decide(pending)
    assert late.status_code == 409 and "expired" in late.text
    longest = gate.request("git push origin x", classes=["git_push"]).json()["approval"]
    gate.caller.human("alice")
    assert gate.decide(longest, ttl_seconds=MAX_DECISION_TTL).status_code == 200


def test_a_pattern_approval_is_reused_only_for_covered_actions(tmp_path: Path, clock: Clock) -> None:
    _configure(git_push=True)
    gate = _gate(tmp_path)
    approval = gate.request("git push origin feature-1", classes=["git_push"]).json()["approval"]
    gate.caller.human("alice")
    bad = gate.decide(approval, scope="pattern", pattern="git push upstream *")
    assert bad.status_code == 422 and "cover the action" in bad.text
    assert gate.decide(approval, scope="pattern", pattern="git push origin *").status_code == 200
    reused = gate.request("git push origin feature-2", classes=["git_push"]).json()
    assert reused["reused"] is True and reused["approval"]["id"] == approval["id"]
    assert gate.claim(approval["id"], "git push origin feature-2")["granted"] is True
    assert gate.claim(approval["id"], "git push origin feature-3")["granted"] is True
    forced = gate.request("git push origin --force", classes=["git_push"]).json()
    assert forced["reused"] is False and forced["approval"]["status"] == "pending"
    assert gate.claim(approval["id"], "git push origin --force")["granted"] is False


def test_an_approval_covers_only_the_working_directory_it_was_granted_for(tmp_path: Path, clock: Clock) -> None:
    """The same text in another directory is another action (relative paths, another repo's remote)."""
    _configure(git_push=True)
    gate = _gate(tmp_path)
    elsewhere = str(tmp_path / "other-checkout")
    once = gate.request("git push origin main", classes=["git_push"]).json()["approval"]
    gate.caller.human("alice")
    assert gate.decide(once).status_code == 200
    moved = gate.claim(once["id"], "git push origin main", cwd=elsewhere)
    assert moved["granted"] is False and "differs" in moved["reason"]
    filed = gate.request("git push origin main", classes=["git_push"], cwd=elsewhere).json()
    assert filed["reused"] is False and filed["approval"]["id"] != once["id"]
    assert filed["approval"]["status"] == "pending"
    assert gate.claim(once["id"], "git push origin main")["granted"] is True
    pattern = gate.request("git push origin feature-1", classes=["git_push"]).json()["approval"]
    gate.caller.human("alice")
    assert gate.decide(pattern, scope="pattern", pattern="git push origin *").status_code == 200
    away = gate.request("git push origin feature-2", classes=["git_push"], cwd=elsewhere).json()
    assert away["reused"] is False and away["approval"]["status"] == "pending"
    assert gate.claim(pattern["id"], "git push origin feature-2", cwd=elsewhere)["granted"] is False
    assert gate.claim(pattern["id"], "git push origin feature-2")["granted"] is True


@pytest.mark.parametrize("scope", ["once", "pattern"])
def test_scope_matching_compares_the_working_directory_exactly(scope: str) -> None:
    record = _record(scope=scope, pattern="git push origin *" if scope == "pattern" else "", cwd="/work/repo")
    assert action_allowed_by(record, kind="command", action="git push origin main", cwd="/work/repo") is True
    for cwd in ("/work/other", "/work/repo/", "/work/repo/sub", ""):
        assert action_allowed_by(record, kind="command", action="git push origin main", cwd=cwd) is False, cwd


def test_the_server_classifies_and_unconfigured_actions_need_no_request(tmp_path: Path, clock: Clock) -> None:
    _configure(git_push=True)
    gate = _gate(tmp_path)
    plain = gate.request("curl https://example.com")
    assert plain.status_code == 422 and "not in a risk class" in plain.text
    added = gate.request("curl https://example.com && git push origin main", classes=[])
    assert added.status_code == 200 and added.json()["approval"]["classes"] == ["git_push"]
    unknown = gate.request("git push origin main", classes=["everything"])
    assert unknown.status_code == 422
    extra = gate.request("git push origin main", classes=["git_push"], tool="Bash")
    assert extra.status_code == 422 and "unsupported approval field" in extra.text


def test_request_bounds_refuse_visibly(tmp_path: Path, clock: Clock) -> None:
    _configure(git_push=True)
    gate = _gate(tmp_path)
    at_bound = "git push origin " + "b" * (MAX_ACTION_CHARS - len("git push origin "))
    assert len(at_bound) == MAX_ACTION_CHARS
    assert gate.request(at_bound, classes=["git_push"]).status_code == 200
    past = gate.request(at_bound + "c", classes=["git_push"])
    assert past.status_code == 422 and "cannot be approved exactly" in past.text
    for index in range(MAX_PENDING_PER_SESSION - 1):
        assert gate.request(f"git push origin b{index}", classes=["git_push"]).status_code == 200
    crowded = gate.request("git push origin one-more", classes=["git_push"])
    assert crowded.status_code == 429 and f"{MAX_PENDING_PER_SESSION} approval requests" in crowded.text
    gate.agent = {"host": "claude", "session_id": "another"}
    assert gate.request("git push origin one-more", classes=["git_push"]).status_code == 200


def test_observed_side_effects_show_pre_approved_only_for_the_exact_action(tmp_path: Path, clock: Clock) -> None:
    _configure(git_push=True)
    gate = _gate(tmp_path)
    clock.at(0.5)
    approval = gate.request("git push origin main", classes=["git_push"]).json()["approval"]
    gate.caller.human("alice")
    assert gate.decide(approval).status_code == 200
    assert gate.claim(approval["id"], "git push origin main")["granted"] is True
    gate.change.ledger(
        "claude",
        "author",
        [
            _edit(str(gate.change.repo / "src/auth.py"), 1),
            _command("git push origin main", 2),
            _command("git push --force origin main", 3),
            _command("git push origin main", 4),
        ],
    )
    gate.change.write("src/auth.py", "def auth():\n    return 3\n")
    gate.change.sync()
    revision = gate.change.snapshot()
    gate.change.store.publish_snapshot(gate.review_id, revision.id)
    expected = [
        ("git push origin main", "pre_approved"),
        ("git push --force origin main", "unapproved"),
        ("git push origin main", "unapproved"),
    ]
    activity = gate.client.get(f"/api/reviews/{gate.review_id}/development", headers=HEADERS).json()
    rows = sorted(activity["snapshot"]["outside_workspace"], key=lambda row: row["at"])
    assert [(row["detail"], (row["approval"] or {}).get("status")) for row in rows] == expected
    assert rows[0]["approval"]["decided_by"] == "alice" and rows[0]["approval"]["approval_id"] == approval["id"]
    assert [row["status"] for row in activity["agent_approvals"]] == ["approved"]
    behavior = gate.client.get(f"/api/reviews/{gate.review_id}/behavior", headers=HEADERS).json()["behavior"]
    actions = sorted(behavior["agent_actions"], key=lambda row: row["at"])
    assert [(row["detail"], (row["approval"] or {}).get("status")) for row in actions] == expected
    assert [row["status"] for row in behavior["agent_approvals"]] == ["approved"]
    events = gate.client.get(f"/api/reviews/{gate.review_id}/development/events?scope=all", headers=HEADERS).json()
    approvals = [row for row in events["events"] if row["kind"] == "approval"]
    assert [(row["status"], row["session_id"]) for row in approvals] == [("approved", "author")]


class FakeService:
    """An approval service: answers from a script, or raises like an unreachable one."""

    def __init__(self, answers: list[dict[str, Any]] | None = None, *, down: bool = False) -> None:
        self.answers = list(answers or [])
        self.down = down
        self.requests: list[tuple[str, Mapping[str, Any]]] = []
        self.claims = 0
        self.rows: list[Mapping[str, Any]] = []

    def reviews(self) -> list[Mapping[str, Any]]:
        if self.down:
            raise ServiceUnavailable("the Review service at http://127.0.0.1:9 could not be reached")
        return self.rows

    def request(self, review_id: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.down:
            raise ServiceUnavailable("the Review service at http://127.0.0.1:9 could not be reached")
        self.requests.append((review_id, body))
        return {"approval": {"id": "apr_x", "status": "pending"}}

    def claim(self, review_id: str, approval_id: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        self.claims += 1
        if self.answers:
            return self.answers.pop(0)
        return {"granted": False, "reason": "pending", "approval": {}}


class Ticks:

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _payload(command: str = "git push origin main", tool: str = "Bash", **extra: Any) -> dict[str, Any]:
    tool_input = {"command": command} if tool == "Bash" else {"file_path": command}
    return {"tool_name": tool, "tool_input": tool_input, "session_id": "author", "cwd": "/work/repo", **extra}


_GIT_PUSH_ONLY = ApprovalPolicy(frozenset({"git_push"}), 10)


def _run(
    payload: Mapping[str, Any],
    service: FakeService,
    *,
    policy: ApprovalPolicy | Callable[[], ApprovalPolicy] = _GIT_PUSH_ONLY,
    env: Mapping[str, str] | None = None,
    local: bool = True,
) -> tuple[HookOutcome, Ticks]:
    ticks = Ticks()
    loader = policy if callable(policy) else lambda: policy
    outcome = run_gate(
        payload,
        policy=loader,
        client=lambda: (service, local),
        env=env if env is not None else {"LEMONCROW_REVIEW_ID": "rv1"},
        clock=ticks,
        sleep=ticks.sleep,
        git=lambda cwd, *args: "/work/repo" if args[:1] == ("rev-parse",) else "",
    )
    return (outcome, ticks)


def _decision(outcome: HookOutcome) -> str:
    rendered = outcome.render()
    if not rendered:
        return "pass"
    data = json.loads(rendered)
    return str(data.get("hookSpecificOutput", {}).get("permissionDecision") or ("notice" if data else "pass"))


def test_hook_entry_never_raises_and_fails_closed_when_the_gate_breaks(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(git_push=True)
    from lemoncrow.pro.capabilities.review import approval_hook

    def boom(*_args: Any, **_kwargs: Any) -> HookOutcome:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(approval_hook, "run_gate", boom)
    denied = json.loads(main_from_text(json.dumps(_payload())))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert main_from_text(json.dumps({"tool_name": "Read", "tool_input": {}})) == ""
    assert main_from_text("not json") == ""


def test_hook_end_to_end_against_the_local_review_api(tmp_path: Path, clock: Clock) -> None:
    """The real hook flow over the real routes: request, human approval, claim."""
    _configure(git_push=True)
    gate = _gate(tmp_path)

    class Adapter:

        def reviews(self) -> list[Mapping[str, Any]]:
            return []

        def request(self, review_id: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
            gate.caller.agent()
            response = gate.client.post(f"/api/reviews/{review_id}/agent-approvals", headers=HEADERS, json=body)
            assert response.status_code == 200, response.text
            payload: Mapping[str, Any] = response.json()
            return payload

        def claim(self, review_id: str, approval_id: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
            gate.caller.agent()
            response = gate.client.post(
                f"/api/reviews/{review_id}/agent-approvals/{approval_id}/claim", headers=HEADERS, json=body
            )
            payload: Mapping[str, Any] = response.json()
            if payload["reason"] == "pending":
                gate.caller.human("alice")
                assert gate.decide(payload["approval"]).status_code == 200
            return payload

    ticks = Ticks()
    outcome = run_gate(
        {**_payload(), "cwd": str(gate.change.repo)},
        policy=lambda: read_policy(Path(os.environ["LEMONCROW_ROOT"])),
        client=lambda: (Adapter(), True),
        env={"LEMONCROW_REVIEW_ID": gate.review_id},
        clock=ticks,
        sleep=ticks.sleep,
    )
    assert outcome.decision == "pass" and "approved by alice" in outcome.message
    (row,) = gate.listing()["approvals"]
    assert row["status"] == "approved" and row["used"] is True

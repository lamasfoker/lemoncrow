"""Multi-repository change sets (Phase 9R item 42, ``change_sets``).

Linked Changes across repositories are reviewed as one set: each repository
keeps its own Review and Snapshot, set readiness is the conjunction of every
member's own readiness plus the set-level checks (contract compatibility
between heads, deploy order), and a member the viewer cannot read makes the
set ``incomplete`` without being described.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import threading
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import change_sets as cs
from tests.gateway.test_review_artifact_analyses_api import _ok, _record
from tests.gateway.test_review_check_runs_api import HEADERS, _review
from tests.gateway.test_review_hermetic import needs_bwrap

OPENAPI_OLD = "openapi: 3.0.0\npaths:\n  /orders:\n    get:\n      responses: {'200': {description: ok}}\n"
OPENAPI_NEW = OPENAPI_OLD + "  /invoices:\n    get:\n      responses: {'200': {description: ok}}\n"
OPENAPI_WITHOUT_ORDERS = "openapi: 3.0.0\npaths:\n  /invoices:\n    get:\n      responses: {'200': {description: ok}}\n"
MIGRATION = "ALTER TABLE orders ADD COLUMN status TEXT;\n"
API_SERVES_INVOICES_CALLS_HOOKS = 'HOOKS = "/hooks"\n@app.get("/invoices")\ndef invoices():\n    pass\n'
WEB_SERVES_HOOKS_CALLS_INVOICES = 'URL = "/invoices"\n@app.post("/hooks")\ndef hooks():\n    pass\n'
CLIENT_OLD = 'ORDERS = "/orders"\n'
CLIENT_NEW = 'ORDERS = "/orders"\nINVOICES = "/api/invoices"\nSTATUS = "SELECT status FROM orders WHERE id = ?"\n'


# --------------------------------------------------------------------------- #
# in-memory members (the pure core)
# --------------------------------------------------------------------------- #


def _member(
    repository: str,
    *,
    base: Mapping[str, str] | None = None,
    head: Mapping[str, str] | None = None,
    ready: bool | None = True,
    links: Sequence[cs.MemberRef] = (),
    review_id: str = "",
) -> cs.MemberSnapshot:
    before, after = dict(base or {}), dict(head if head is not None else base or {})
    changed = [
        (path, "", "added" if path not in before else "deleted" if path not in after else "modified")
        for path in sorted(set(before) | set(after))
        if before.get(path) != after.get(path)
    ]
    review = review_id or f"rev-{repository}"
    linked = list(links)

    def link(ref: cs.MemberRef) -> None:
        if ref not in linked:  # an upsert, like the store row
            linked.append(ref)

    def unlink(ref: cs.MemberRef) -> None:
        if ref in linked:
            linked.remove(ref)

    def own_readiness() -> dict[str, Any]:
        if ready is None:
            raise RuntimeError("store unavailable")
        return {"ready": ready, "blocker_count": 0 if ready else 2}

    return cs.MemberSnapshot(
        repository=repository,
        identity=f"test:{repository}",
        review_id=review,
        title=f"{repository} change",
        status="open",
        # Unique per call: surfaces are memoized per immutable Snapshot id.
        revision_id=uuid.uuid4().hex,
        snapshot_number=1,
        changed_files=lambda: changed,
        read_side=lambda path, side: (before if side == "old" else after).get(path),
        head_paths=lambda: sorted(after),
        own_readiness=own_readiness,
        links=lambda: list(linked),
        link=link,
        unlink=unlink,
    )


def _world(*members: cs.MemberSnapshot, hidden: frozenset[str] = frozenset()) -> cs.Discovery:
    """Fully link *members*; the first is the start. ``hidden`` repositories resolve as unreadable."""

    by_ref = {member.ref: member for member in members}
    for member in members:
        for other in members:
            if other is not member:
                member.link(other.ref)
    return cs.discover(members[0], lambda ref: None if ref.repository in hidden else by_ref.get(ref))


def test_set_readiness_is_the_conjunction_of_every_member() -> None:
    api, web, worker = _member("api"), _member("web"), _member("worker")
    assert cs.assess(_world(api, web, worker), include_start=True).state == "ready"

    assessment = cs.assess(_world(_member("api"), _member("web"), _member("worker", ready=False)), include_start=True)
    assert assessment.state == "blocked"
    # The ready member's own readiness carries the set blocker naming the blocked one.
    discovery = _world(_member("api"), _member("worker", ready=False))
    rows = {row.id: row for row in cs.conditions(cs.assess(discovery, include_start=False))}
    members = rows["change-set:members"]
    assert (members.state, members.reason) == ("pending", "set_member_blocked")
    assert "in worker" in members.detail and not members.satisfied

    # A member whose readiness cannot be read is never counted as ready.
    unread = cs.assess(_world(_member("api"), _member("web", ready=None)), include_start=False)
    assert unread.state == "blocked"
    row = cs.conditions(unread)[0]
    assert (row.state, row.reason) == ("unknown", "evaluation_error")


@pytest.mark.parametrize(
    "readiness",
    [{"blocker_count": 0}, {"ready": None, "blocker_count": 0}, {"ready": "true", "blocker_count": 0}],
    ids=["missing", "none", "truthy-string"],
)
def test_only_an_explicit_ready_true_counts_a_member_as_ready(readiness: dict[str, Any]) -> None:
    web = dataclasses.replace(_member("web"), own_readiness=lambda: readiness)
    assessment = cs.assess(_world(_member("api"), web), include_start=False)
    assert [(item.member.repository, item.ready) for item in assessment.states] == [("web", False)]
    assert assessment.state == "blocked"
    row = cs.conditions(assessment)[0]
    assert (row.id, row.state, row.reason) == ("change-set:members", "pending", "set_member_blocked")
    assert cs.to_wire(assessment)["ready_members"] == 0


def test_an_unreadable_member_makes_the_set_incomplete_without_describing_it() -> None:
    secret = _member("payments-secret", review_id="rev-hidden-77")
    discovery = _world(_member("api"), _member("web"), secret, hidden=frozenset({"payments-secret"}))
    assert (discovery.unreadable, discovery.complete) == (1, False)
    assessment = cs.assess(discovery, include_start=True)
    assert assessment.state == "incomplete"
    wire = cs.to_wire(assessment)
    text = json.dumps(wire)
    assert "payments-secret" not in text and "rev-hidden-77" not in text
    assert cs.alias(secret.ref) not in text
    assert wire["members"][-1] == {"alias": "", "readable": False}
    assert (wire["unreadable"], wire["deploy_order"]["state"]) == (1, "unknown")
    rows = cs.conditions(cs.assess(discovery, include_start=False))
    assert [(row.id, row.state, row.reason) for row in rows] == [("change-set:members", "unknown", "evaluation_error")]
    assert "payments-secret" not in rows[0].detail and "1 member" in rows[0].detail


def test_the_member_bound_holds_at_the_limit_and_is_incomplete_past_it() -> None:
    at_limit = [_member(f"repo{index}") for index in range(cs.MAX_MEMBERS)]
    discovery = _world(*at_limit)
    assert (len(discovery.members), discovery.truncated, discovery.complete) == (cs.MAX_MEMBERS, False, True)

    past = [_member(f"repo{index}") for index in range(cs.MAX_MEMBERS + 1)]
    discovery = _world(*past)
    assert (len(discovery.members), discovery.truncated) == (cs.MAX_MEMBERS, True)
    assert cs.assess(discovery, include_start=True).state == "incomplete"
    rows = cs.conditions(cs.assess(discovery, include_start=False))
    assert rows[0].state == "unknown" and f"more than {cs.MAX_MEMBERS}" in rows[0].detail


def test_a_dangling_row_at_the_member_bound_neither_truncates_nor_takes_a_slot() -> None:
    at_limit = [_member(f"repo{index}") for index in range(cs.MAX_MEMBERS)]
    by_ref = {member.ref: member for member in at_limit}
    for member in at_limit:
        for other in at_limit:
            if other is not member:
                member.link(other.ref)
    # A row naming a Change that does not link back (a half-written link): at the bound and past it.
    stray = _member("stray")
    by_ref[stray.ref] = stray
    for count in (1, 2):
        for member in at_limit[:count]:
            member.link(stray.ref)
        discovery = cs.discover(at_limit[0], by_ref.get)
        assert (len(discovery.members), discovery.truncated, discovery.complete) == (cs.MAX_MEMBERS, False, True)
        assert stray.ref not in {member.ref for member in discovery.members}
    # A ninth member that does link back still truncates.
    stray.link(at_limit[0].ref)
    discovery = cs.discover(at_limit[0], by_ref.get)
    assert (len(discovery.members), discovery.truncated) == (cs.MAX_MEMBERS, True)


def test_deploy_order_puts_providers_before_consumers_and_migrations_before_code() -> None:
    api = _member(
        "api",
        base={"openapi.yaml": OPENAPI_OLD, "db/migrations/0001.sql": "CREATE TABLE orders (id INTEGER);\n"},
        head={
            "openapi.yaml": OPENAPI_NEW,
            "db/migrations/0001.sql": "CREATE TABLE orders (id INTEGER);\n",
            "db/migrations/0002.sql": MIGRATION,
        },
    )
    web = _member("web", base={"web/client.py": CLIENT_OLD}, head={"web/client.py": CLIENT_NEW})
    # The consumer is the start: the order is the same from either side.
    for start, other in ((web, api), (api, web)):
        assessment = cs.assess(_world(start, other), include_start=True)
        assert assessment.contracts.state == "compatible"
        assert assessment.order is not None and not assessment.order.cycle
        steps = [(ref.repository, phase) for ref, phase in assessment.order.steps]
        assert steps == [("api", "migrations"), ("api", "code"), ("web", "code")]
        assert assessment.state == "ready"
    wire = cs.to_wire(cs.assess(_world(web, api), include_start=True))
    web_step = wire["deploy_order"]["steps"][-1]
    reasons = " | ".join(row["reason"] for row in web_step["after"])
    assert "calls /invoices" in reasons and "uses table orders" in reasons


def test_deploy_order_ties_are_deterministic_and_a_cycle_needs_a_decision() -> None:
    a, b, c = cs.MemberRef("a", "1"), cs.MemberRef("b", "2"), cs.MemberRef("c", "3")
    nodes = [(c, "code"), (a, "code"), (b, "code"), (b, "migrations")]
    order = cs.deploy_order(nodes, [cs.OrderEdge((b, "migrations"), (b, "code"), "migrations before code")])
    assert list(order.steps) == [(b, "migrations"), (a, "code"), (b, "code"), (c, "code")]
    cyclic = [
        cs.OrderEdge((a, "code"), (b, "code"), "b calls a"),
        cs.OrderEdge((b, "code"), (a, "code"), "a calls b"),
    ]
    order = cs.deploy_order([(a, "code"), (b, "code"), (c, "code")], cyclic)
    assert list(order.steps) == [(c, "code")] and list(order.cycle) == [(a, "code"), (b, "code")]

    # Each head serves a route the other's new code calls: no order exists.
    api = _member("api", base={"api/app.py": ""}, head={"api/app.py": API_SERVES_INVOICES_CALLS_HOOKS})
    web = _member("web", base={"web/app.py": ""}, head={"web/app.py": WEB_SERVES_HOOKS_CALLS_INVOICES})
    discovery = _world(api, web)
    assessment = cs.assess(discovery, include_start=True)
    assert assessment.state == "needs_decision"
    rows = {row.id: row for row in cs.conditions(cs.assess(discovery, include_start=False))}
    order_row = rows["change-set:deploy-order"]
    assert (order_row.state, order_row.reason) == ("pending", "needs_decision")
    assert cs.to_wire(assessment)["deploy_order"]["state"] == "needs_decision"


def test_a_contract_one_head_removes_and_another_head_uses_is_incompatible() -> None:
    for provider_base, provider_head, consumer_code, contract in [
        ({"openapi.yaml": OPENAPI_OLD}, {"openapi.yaml": OPENAPI_WITHOUT_ORDERS}, CLIENT_OLD, "route /orders"),
        (
            {},
            {"db/migrations/0003.sql": "DROP TABLE legacy_audit;\n"},
            'Q = "SELECT id FROM legacy_audit WHERE id = ?"\n',
            "table legacy_audit",
        ),
        (
            {},
            {"db/migrations/0003.sql": "ALTER TABLE users DROP COLUMN legacy;\n"},
            'Q = "SELECT legacy FROM users WHERE id = ?"\n',
            "column users.legacy",
        ),
        (
            {"proto/orders.proto": "message Order { string id = 1; }\nmessage LegacyOrder { string id = 1; }\n"},
            {"proto/orders.proto": "message Order { string id = 1; }\n"},
            "def read(raw):\n    return LegacyOrder.decode(raw)\n",
            "message LegacyOrder",
        ),
    ]:
        provider = _member("api", base=provider_base, head=provider_head)
        consumer = _member(
            "web",
            base={"web/client.py": consumer_code, "web/other.py": ""},
            head={"web/client.py": consumer_code, "web/other.py": "X = 1\n"},
        )
        discovery = _world(consumer, provider)
        assessment = cs.assess(discovery, include_start=True)
        assert assessment.contracts.state == "incompatible"
        issues = [
            (row.provider.repository, row.consumer.repository, row.contract) for row in assessment.contracts.issues
        ]
        assert issues == [("api", "web", contract)]
        assert assessment.contracts.issues[0].paths == ("web/client.py",)
        assert assessment.state == "blocked"
        rows = {row.id: row for row in cs.conditions(cs.assess(discovery, include_start=False))}
        assert (rows["change-set:contracts"].state, rows["change-set:contracts"].reason) == (
            "fail",
            "contract_incompatible",
        )


def test_a_consumer_that_stops_using_a_contract_deploys_before_its_removal() -> None:
    provider = _member("api", base={"openapi.yaml": OPENAPI_OLD}, head={"openapi.yaml": OPENAPI_WITHOUT_ORDERS})
    consumer = _member("web", base={"web/client.py": CLIENT_OLD}, head={"web/client.py": "X = 1\n"})
    assessment = cs.assess(_world(provider, consumer), include_start=True)
    assert assessment.contracts.state == "compatible" and not assessment.contracts.issues
    assert assessment.order is not None
    # Alphabetically api would go first; the expand/contract edge puts the consumer first.
    steps = [(ref.repository, phase) for ref, phase in assessment.order.steps]
    assert steps == [("web", "code"), ("api", "code")]
    edge = assessment.order.after[(provider.ref, "code")][0]
    assert "stops using route /orders" in edge.reason

    # A dropped column: the consumer stops reading it, then the migration runs, then the provider's code.
    schema = {"db/migrations/0001.sql": "CREATE TABLE users (id INTEGER, legacy TEXT);\n"}
    provider = _member(
        "api",
        base=schema,
        head={**schema, "db/migrations/0002.sql": "ALTER TABLE users DROP COLUMN legacy;\n"},
    )
    consumer = _member(
        "web", base={"web/repo.py": 'SQL = "SELECT legacy FROM users"\n'}, head={"web/repo.py": "X = 1\n"}
    )
    assessment = cs.assess(_world(provider, consumer), include_start=True)
    assert assessment.contracts.state == "compatible" and assessment.order is not None
    steps = [(ref.repository, phase) for ref, phase in assessment.order.steps]
    assert steps == [("web", "code"), ("api", "migrations"), ("api", "code")]


def test_the_head_scan_bound_is_unknown_past_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cs, "MAX_SCAN_FILES", 3)
    provider = {"base": {"openapi.yaml": OPENAPI_OLD}, "head": {"openapi.yaml": OPENAPI_WITHOUT_ORDERS}}
    at_limit = {f"web/m{index}.py": "X = 1\n" for index in range(3)}
    assessment = cs.assess(_world(_member("api", **provider), _member("web", base=at_limit)), include_start=True)
    assert assessment.contracts.state == "compatible"
    past = {f"web/m{index}.py": "X = 1\n" for index in range(4)}
    assessment = cs.assess(_world(_member("api", **provider), _member("web", base=past)), include_start=True)
    assert assessment.contracts.state == "unknown" and assessment.state == "blocked"
    assert any("stops at 3 of 4" in note for note in assessment.contracts.notes)


def test_the_changed_file_bound_is_unknown_past_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cs, "MAX_CHANGED_FILES", 2)
    at_limit = cs.check_contracts([_member("api", base={}, head={"a.py": "A = 1\n", "b.py": "B = 1\n"})])
    assert at_limit.state == "not_applicable"
    past = cs.check_contracts([_member("api", base={}, head={"a.py": "", "b.py": "", "c.py": "C = 1\n"})])
    assert past.state == "unknown" and "only the first 2 of 3" in past.notes[0]


def test_the_file_size_bound_is_unknown_past_the_limit_for_changed_and_head_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    at_limit_text = 'A = "/orders"\n'
    monkeypatch.setattr(cs, "MAX_SCAN_BYTES", len(at_limit_text))
    # A changed file exactly at the bound is read; one byte past it is not, and the check is unknown.
    at_limit = cs.check_contracts([_member("api", base={}, head={"a.py": at_limit_text})])
    assert at_limit.state == "not_applicable"
    past = cs.check_contracts([_member("api", base={}, head={"a.py": at_limit_text + "#"})])
    assert past.state == "unknown" and "a.py is over" in past.notes[0]

    # The consumer's unchanged head file: at the bound it is scanned (and still uses the removed route).
    provider = {"base": {"openapi.yaml": OPENAPI_OLD}, "head": {"openapi.yaml": OPENAPI_WITHOUT_ORDERS}}
    bound = max(len(OPENAPI_OLD), len(OPENAPI_WITHOUT_ORDERS))
    monkeypatch.setattr(cs, "MAX_SCAN_BYTES", bound)
    exact = {"web/c.py": at_limit_text + " " * (bound - len(at_limit_text))}
    scanned = cs.assess(_world(_member("api", **provider), _member("web", base=exact)), include_start=True)
    assert scanned.contracts.state == "incompatible"
    big = {"web/c.py": exact["web/c.py"] + " "}
    skipped = cs.assess(_world(_member("api", **provider), _member("web", base=big)), include_start=True)
    assert skipped.contracts.state == "unknown" and skipped.state == "blocked"
    assert any("web/c.py is over" in note and "not scanned" in note for note in skipped.contracts.notes)


def test_an_unreadable_head_tree_is_unknown_never_compatible() -> None:
    provider = {"base": {"openapi.yaml": OPENAPI_OLD}, "head": {"openapi.yaml": OPENAPI_WITHOUT_ORDERS}}
    web = dataclasses.replace(_member("web", base={"web/c.py": CLIENT_OLD}), head_paths=lambda: None)
    assessment = cs.assess(_world(_member("api", **provider), web), include_start=True)
    assert assessment.contracts.state == "unknown"
    assert any("head tree is not readable" in note for note in assessment.contracts.notes)


def test_routes_match_under_a_mount_prefix_and_parameters() -> None:
    assert cs.route_matches(cs.route_segments("/orders/{id}"), cs.route_segments("/api/v1/orders/${orderId}"))
    assert cs.route_matches(cs.route_segments("/orders/:id"), cs.route_segments("/orders/", open_end=True))
    assert not cs.route_matches(cs.route_segments("/orders"), cs.route_segments("/invoices"))
    assert not cs.route_matches(cs.route_segments("/{id}"), cs.route_segments("/anything"))


def test_a_link_needs_another_repository_manage_permission_and_a_readable_target() -> None:
    api, web = _member("api"), _member("web")
    secret = _member("secret")
    members = {member.ref: member for member in (api, web, secret)}

    class Directory:
        repository = "web"

        def __init__(self, manage: frozenset[str]) -> None:
            self.manage = manage

        def member(self, ref: cs.MemberRef) -> cs.MemberSnapshot | None:
            return None if ref.repository == "secret" else members.get(ref)

        def may_link(self, repository: str) -> bool:
            return repository in self.manage

        def write_lock(self) -> contextlib.AbstractContextManager[None]:
            return _TENANT_LOCK

    allowed = Directory(frozenset({"api", "web"}))
    with pytest.raises(cs.LinkRefused) as same:
        cs.link(allowed, web, "web", web.review_id)
    assert same.value.status == 422
    with pytest.raises(cs.LinkRefused) as unknown:
        cs.link(allowed, web, "api", "rev-nope")
    with pytest.raises(cs.LinkRefused) as hidden:
        cs.link(allowed, web, "secret", secret.review_id)
    # Unreadable and unknown are the same answer: existence is never confirmed.
    assert (
        (unknown.value.status, unknown.value.detail)
        == (hidden.value.status, hidden.value.detail)
        == (
            404,
            "related Change not found",
        )
    )
    with pytest.raises(cs.LinkRefused) as forbidden:
        cs.link(Directory(frozenset({"web"})), web, "api", api.review_id)
    assert forbidden.value.status == 403
    assert web.links() == [] and api.links() == []
    cs.link(allowed, web, "api", f"r/{api.review_id}")
    assert web.links() == [api.ref] and api.links() == [web.ref]


class _OpenDirectory:
    """Manage everywhere; members in ``hidden`` repositories resolve as unreadable."""

    repository = "web"

    def __init__(self, *members: cs.MemberSnapshot, hidden: frozenset[str] = frozenset()) -> None:
        self.members = {member.ref: member for member in members}
        self.hidden = hidden

    def member(self, ref: cs.MemberRef) -> cs.MemberSnapshot | None:
        return None if ref.repository in self.hidden else self.members.get(ref)

    def may_link(self, repository: str) -> bool:
        return True

    def write_lock(self) -> contextlib.AbstractContextManager[None]:
        return _TENANT_LOCK


#: One lock for every in-memory directory: the tests' single tenant.
_TENANT_LOCK = threading.Lock()


def test_a_set_with_members_the_caller_cannot_read_is_never_extended(monkeypatch: pytest.MonkeyPatch) -> None:
    ops = _member("ops")
    # Their set holds a member the caller cannot read.
    web, api = _member("web"), _member("api", links=[ops.ref])
    with pytest.raises(cs.LinkRefused) as theirs:
        cs.link(_OpenDirectory(web, api, ops, hidden=frozenset({"ops"})), web, "api", api.review_id)
    assert (theirs.value.status, theirs.value.detail) == (
        409,
        "the change set has members you cannot read, so it cannot be extended",
    )
    assert web.links() == [] and api.links() == [ops.ref]
    # Our own set holds one.
    web, api = _member("web", links=[ops.ref]), _member("api")
    with pytest.raises(cs.LinkRefused) as ours:
        cs.link(_OpenDirectory(web, api, ops, hidden=frozenset({"ops"})), web, "api", api.review_id)
    assert ours.value.status == 409
    assert web.links() == [ops.ref] and api.links() == []
    # Their set was cut at the member bound: what lies past it is unknown, so it is not extended either.
    monkeypatch.setattr(cs, "MAX_MEMBERS", 3)
    api_ref = cs.MemberRef("api", "rev-api")
    ops, worker, jobs = (_member(name, links=[api_ref]) for name in ("ops", "worker", "jobs"))
    web, api = _member("web"), _member("api", links=[ops.ref, worker.ref, jobs.ref])
    with pytest.raises(cs.LinkRefused) as truncated:
        cs.link(_OpenDirectory(web, api, ops, worker, jobs), web, "api", api.review_id)
    assert truncated.value.status == 409
    assert web.links() == [] and api.links() == [ops.ref, worker.ref, jobs.ref]


def test_joining_two_sets_never_holds_two_changes_of_one_repository() -> None:
    first, second = _member("api", review_id="rev-api-1"), _member("api", review_id="rev-api-2")
    web, worker = _member("web", links=[first.ref]), _member("worker", links=[second.ref])
    first.link(web.ref)
    second.link(worker.ref)
    with pytest.raises(cs.LinkRefused) as refused:
        cs.link(_OpenDirectory(first, second, web, worker), web, "worker", worker.review_id)
    assert (refused.value.status, refused.value.detail) == (422, "a change set holds one Change per repository")
    assert (web.links(), worker.links()) == ([first.ref], [second.ref])
    assert (first.links(), second.links()) == ([web.ref], [worker.ref])
    # The same api Change on both sides is one member, not two: that join is allowed.
    jobs = _member("jobs", links=[first.ref])
    first.link(jobs.ref)
    cs.link(_OpenDirectory(first, web, jobs), web, "jobs", jobs.review_id)
    assert web.links() == [first.ref, jobs.ref] and jobs.links() == [first.ref, web.ref]


class _ManageDirectory(_OpenDirectory):
    """Readable everywhere it is not hidden; manage only in ``manage``."""

    def __init__(self, *members: cs.MemberSnapshot, manage: frozenset[str], hidden: frozenset[str] = frozenset()):
        super().__init__(*members, hidden=hidden)
        self.manage = manage

    def may_link(self, repository: str) -> bool:
        return repository in self.manage


def test_a_link_is_a_pair_of_rows_and_a_dangling_row_joins_nothing() -> None:
    web, api, worker = _member("web"), _member("api"), _member("worker")
    directory = _OpenDirectory(web, api, worker)
    # Only web holds a row naming api (the other side unlinked, or a write stopped half way).
    web.link(api.ref)
    discovery = cs.discover(web, directory.member)
    assert ([member.repository for member in discovery.members], discovery.complete) == (["web"], True)
    assert discovery.linked is False
    # The dangling row neither hides api from a member that does link it, nor takes a slot.
    web.link(worker.ref)
    worker.link(web.ref)
    worker.link(api.ref)
    api.link(worker.ref)
    discovery = cs.discover(web, directory.member)
    assert sorted(member.repository for member in discovery.members) == ["api", "web", "worker"]
    assert (discovery.unreadable, discovery.truncated) == (0, False)
    # An unreadable target is still counted: what it links is unknown.
    hidden = cs.discover(web, _OpenDirectory(web, api, worker, hidden=frozenset({"api"})).member)
    assert (hidden.unreadable, hidden.complete) == (1, False)


def test_unlink_needs_manage_in_both_repositories_and_removes_both_rows() -> None:
    web, api = _member("web"), _member("api")
    cs.link(_OpenDirectory(web, api), web, "api", api.review_id)
    for manage in (frozenset({"web"}), frozenset({"api"})):
        with pytest.raises(cs.LinkRefused) as forbidden:
            cs.unlink(_ManageDirectory(web, api, manage=manage), web, "api", api.review_id)
        assert forbidden.value.status == 403
        assert web.links() == [api.ref] and api.links() == [web.ref]
    # Unreadable and unknown are one answer, even for a member this Change links.
    with pytest.raises(cs.LinkRefused) as hidden:
        cs.unlink(_OpenDirectory(web, api, hidden=frozenset({"api"})), web, "api", api.review_id)
    with pytest.raises(cs.LinkRefused) as unknown:
        cs.unlink(_OpenDirectory(web, api), web, "api", "rev-nope")
    assert (
        (hidden.value.status, hidden.value.detail)
        == (unknown.value.status, unknown.value.detail)
        == (
            404,
            "related Change not found",
        )
    )
    stranger = _member("worker")
    with pytest.raises(cs.LinkRefused) as unlinked:
        cs.unlink(_OpenDirectory(web, api, stranger), web, "worker", stranger.review_id)
    assert unlinked.value.status == 404

    cs.unlink(_ManageDirectory(web, api, manage=frozenset({"web", "api"})), web, "api", f"r/{api.review_id}")
    assert web.links() == [] and api.links() == []
    assert cs.discover(web, _OpenDirectory(web, api).member).linked is False
    # A half-unlinked pair (only the other side's row went) is unlinked too.
    cs.link(_OpenDirectory(web, api), web, "api", api.review_id)
    api.unlink(web.ref)
    assert cs.discover(web, _OpenDirectory(web, api).member).linked is False
    cs.unlink(_OpenDirectory(web, api), web, "api", api.review_id)
    assert web.links() == [] and api.links() == []


def test_replace_points_the_link_at_another_change_of_that_repository() -> None:
    web, worker = _member("web"), _member("worker")
    old, new = _member("api", review_id="rev-api-1"), _member("api", review_id="rev-api-2")
    directory = _OpenDirectory(web, worker, old, new)
    cs.link(directory, web, "api", old.review_id)
    cs.link(directory, web, "worker", worker.review_id)

    with pytest.raises(cs.LinkRefused) as forbidden:
        cs.replace(_ManageDirectory(web, worker, old, new, manage=frozenset({"web"})), web, "api", new.review_id)
    assert forbidden.value.status == 403
    with pytest.raises(cs.LinkRefused) as nothing_there:
        cs.replace(directory, worker, "api", new.review_id)
    assert (nothing_there.value.status, "link it instead" in nothing_there.value.detail) == (409, True)
    assert web.links() == [old.ref, worker.ref] and new.links() == []

    cs.replace(directory, web, "api", new.review_id)
    assert web.links() == [worker.ref, new.ref] and new.links() == [web.ref] and old.links() == []
    discovery = cs.discover(web, directory.member)
    assert sorted(member.review_id for member in discovery.members) == ["rev-api-2", "rev-web", "rev-worker"]
    # Replacing with the member already linked is a no-op.
    cs.replace(directory, web, "api", new.review_id)
    assert web.links() == [worker.ref, new.ref]

    # The old Change still linked through another member: the update would hold two api
    # Changes, so it is refused before anything is written.
    worker.link(new.ref)
    new.link(worker.ref)
    with pytest.raises(cs.LinkRefused) as kept:
        cs.replace(directory, web, "api", old.review_id)
    assert (kept.value.status, kept.value.detail) == (422, "a change set holds one Change per repository")
    assert web.links() == [worker.ref, new.ref] and old.links() == []


def _serialize_racer(
    directory: _OpenDirectory, member: cs.MemberSnapshot, rival: Any
) -> tuple[cs.MemberSnapshot, dict[str, Any]]:
    """*member* whose first row write starts *rival* in another thread, mid-request.

    The rival is a whole second request. It must wait for the directory's write
    lock: it is still running when this request's write returns.
    """

    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["result"] = rival()
        except cs.LinkRefused as refused:
            outcome["refused"] = refused

    def racing_link(ref: cs.MemberRef, *, original: Any = member.link) -> None:
        original(ref)
        if "thread" not in outcome:
            thread = threading.Thread(target=run)
            outcome["thread"] = thread
            thread.start()
            thread.join(0.3)
            outcome["blocked_while_writing"] = thread.is_alive()

    racer = dataclasses.replace(member, link=racing_link)
    directory.members[racer.ref] = racer
    return racer, outcome


def test_two_concurrent_links_never_put_two_changes_of_one_repository_in_a_set() -> None:
    """The second request checks only after the first wrote: it is refused and changes nothing."""

    web, first, second = _member("web"), _member("api", review_id="rev-api-1"), _member("api", review_id="rev-api-2")
    directory = _OpenDirectory(web, first, second)
    _racer, outcome = _serialize_racer(directory, first, lambda: cs.link(directory, web, "api", second.review_id))
    cs.link(directory, web, "api", first.review_id)
    outcome["thread"].join(5)
    assert outcome["blocked_while_writing"] is True
    refused = outcome["refused"]
    assert (refused.status, refused.detail) == (422, "a change set holds one Change per repository")
    # The accepted link survives; the refused one left no row.
    discovery = cs.discover(web, directory.member)
    assert sorted(member.review_id for member in discovery.members) == ["rev-api-1", "rev-web"]
    assert web.links() == [first.ref] and second.links() == []


def test_two_concurrent_updates_leave_one_change_of_the_repository_and_both_answers_hold() -> None:
    web = _member("web")
    old, new, other = (_member("api", review_id=f"rev-api-{index}") for index in (1, 2, 3))
    directory = _OpenDirectory(web, old, new, other)
    cs.link(directory, web, "api", old.review_id)
    _racer, outcome = _serialize_racer(directory, new, lambda: cs.replace(directory, web, "api", other.review_id))
    cs.replace(directory, web, "api", new.review_id)
    outcome["thread"].join(5)
    assert outcome["blocked_while_writing"] is True and "refused" not in outcome
    # Both updates were applied, one after the other: the later one names the set's api Change.
    discovery = cs.discover(web, directory.member)
    assert sorted(member.review_id for member in discovery.members) == ["rev-api-3", "rev-web"]
    assert web.links() == [other.ref] and old.links() == [] and new.links() == []
    assert cs.assess(discovery, include_start=True).duplicates == ()


def test_a_refused_update_racing_a_link_changes_nothing() -> None:
    web, jobs = _member("web"), _member("jobs")
    old, new = _member("api", review_id="rev-api-1"), _member("api", review_id="rev-api-2")
    other = _member("api", review_id="rev-api-3", links=[jobs.ref])
    jobs.link(other.ref)
    directory = _OpenDirectory(web, old, new, other, jobs)
    cs.link(directory, web, "api", old.review_id)
    # While the update writes, another request tries to bring a third api Change in through jobs.
    _racer, outcome = _serialize_racer(directory, new, lambda: cs.link(directory, web, "jobs", jobs.review_id))
    cs.replace(directory, web, "api", new.review_id)
    outcome["thread"].join(5)
    assert outcome["blocked_while_writing"] is True
    assert outcome["refused"].status == 422
    assert web.links() == [new.ref] and new.links() == [web.ref] and jobs.links() == [other.ref]
    assert old.links() == []


def test_a_write_that_raises_is_undone_and_one_that_cannot_lock_writes_nothing() -> None:
    web, api = _member("web"), _member("api")
    directory = _OpenDirectory(web, api)

    def failing_link(ref: cs.MemberRef) -> None:
        raise OSError("disk full")

    directory.members[web.ref] = broken = dataclasses.replace(web, link=failing_link)
    with pytest.raises(OSError):
        cs.link(directory, broken, "api", api.review_id)
    # The target's row was written first and is rolled back.
    assert api.links() == [] and web.links() == []

    class Busy(_OpenDirectory):
        def write_lock(self) -> contextlib.AbstractContextManager[None]:
            raise cs.LinkRefused(409, cs.LOCK_BUSY)

    with pytest.raises(cs.LinkRefused) as busy:
        cs.link(Busy(web, api), web, "api", api.review_id)
    assert (busy.value.status, busy.value.detail) == (409, cs.LOCK_BUSY)
    assert api.links() == [] and web.links() == []


def test_an_update_that_stopped_half_way_leaves_a_blocked_set_that_unlink_or_update_repairs() -> None:
    """A process that died between the new link and the old link's removal (no undo ran)."""

    web, worker = _member("web"), _member("worker")
    old, new = _member("api", review_id="rev-api-1"), _member("api", review_id="rev-api-2")
    directory = _OpenDirectory(web, worker, old, new)
    cs.link(directory, web, "api", old.review_id)
    cs.link(directory, web, "worker", worker.review_id)
    new.link(web.ref)
    web.link(new.ref)

    for start in (web, worker):
        assessment = cs.assess(cs.discover(start, directory.member), include_start=True)
        assert assessment.state == "blocked"
        assert assessment.duplicates == (("api", (old.ref, new.ref)),)
        rows = cs.conditions(cs.assess(cs.discover(start, directory.member), include_start=False))
        assert [(row.id, row.state, row.reason) for row in rows] == [
            ("change-set:members", "fail", "set_duplicate_repository")
        ]
        assert "r/rev-api-1" in rows[0].detail and "Unlink" in rows[0].detail
        assert cs.to_wire(assessment)["duplicate_repositories"] == ["api"]
    # Extending the broken set with another Change of that repository stays refused.
    third = _member("api", review_id="rev-api-3")
    directory.members[third.ref] = third
    with pytest.raises(cs.LinkRefused):
        cs.link(directory, worker, "api", third.review_id)

    # Unlinking one repairs it.
    cs.unlink(directory, web, "api", old.review_id)
    repaired = cs.assess(cs.discover(web, directory.member), include_start=True)
    assert (repaired.state, repaired.duplicates) == ("ready", ())
    # So does finishing the update.
    web.link(old.ref)
    old.link(web.ref)
    cs.replace(directory, web, "api", new.review_id)
    assert web.links() == [worker.ref, new.ref] and old.links() == []
    assert cs.assess(cs.discover(web, directory.member), include_start=True).state == "ready"


# --------------------------------------------------------------------------- #
# through the Review API (two repositories, two stores)
# --------------------------------------------------------------------------- #


class _Directory:
    """A test composition: members resolve through their own repository's Reader app."""

    def __init__(self, repository: str, apps: dict[str, Any], hidden: set[str]) -> None:
        self.repository = repository
        self._apps = apps
        self.hidden = hidden

    def member(self, ref: cs.MemberRef) -> cs.MemberSnapshot | None:
        app = self._apps.get(ref.repository)
        if app is None or ref.repository in self.hidden:
            return None
        member = app.state.review_change_set_member(ref.review_id)
        return member if isinstance(member, cs.MemberSnapshot) else None

    def may_link(self, repository: str) -> bool:
        return repository in self._apps and repository not in self.hidden

    def write_lock(self) -> contextlib.AbstractContextManager[None]:
        return _TENANT_LOCK


def _pair(
    tmp_path: Path,
    *,
    api_base: dict[str, str],
    api_head: dict[str, str | None],
    web_base: dict[str, str],
    web_head: dict[str, str | None],
) -> tuple[SimpleNamespace, SimpleNamespace, dict[str, set[str]]]:
    apps: dict[str, Any] = {}
    hidden: dict[str, set[str]] = {"api": set(), "web": set()}
    api = _review(
        tmp_path / "api", base=api_base, head=api_head, change_set_directory=_Directory("api", apps, hidden["api"])
    )
    web = _review(
        tmp_path / "web", base=web_base, head=web_head, change_set_directory=_Directory("web", apps, hidden["web"])
    )
    apps.update({"api": api.client.app, "web": web.client.app})
    return api, web, hidden


def _link(web: SimpleNamespace, api: SimpleNamespace) -> dict[str, Any]:
    response = web.client.post(
        f"/api/reviews/{web.review_id}/change-set", headers=HEADERS, json={"repository": "api", "review": api.review_id}
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def _get(review: SimpleNamespace, suffix: str) -> dict[str, Any]:
    response = review.client.get(f"/api/reviews/{review.review_id}{suffix}", headers=HEADERS)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _set_rows(review: SimpleNamespace) -> dict[str, dict[str, Any]]:
    return {row["id"]: row for row in _get(review, "/readiness")["conditions"] if row["category"] == "change_set"}


API_BASE = {
    "openapi.yaml": OPENAPI_OLD,
    "db/migrations/0001_init.sql": "CREATE TABLE orders (id INTEGER PRIMARY KEY);\n",
    "api/app.py": "X = 1\n",
}
API_HEAD: dict[str, str | None] = {"openapi.yaml": OPENAPI_NEW, "db/migrations/0002_status.sql": MIGRATION}
WEB_BASE = {"web/client.py": CLIENT_OLD}
WEB_HEAD: dict[str, str | None] = {"web/client.py": CLIENT_NEW}


def test_linked_changes_round_trip_as_one_set_with_the_deploy_order(tmp_path: Path) -> None:
    api, web, _hidden = _pair(tmp_path, api_base=API_BASE, api_head=API_HEAD, web_base=WEB_BASE, web_head=WEB_HEAD)
    assert _get(web, "/change-set")["linked"] is False
    assert _set_rows(web) == {}

    linked = _link(web, api)
    # Each repository keeps its own Review: one row in each store, naming the other.
    for review, other, repository in ((web, api, "api"), (api, web, "web")):
        rows = review.store.list_relationships(review.review_id, kind=cs.CHANGE_SET_KIND)
        assert len(rows) == 1
        assert json.loads(rows[0].metadata_json) == {"repository": repository, "review_id": other.review_id}
    for payload in (linked, _get(api, "/change-set")):
        assert payload["linked"] is True and payload["member_count"] == 2
        assert {row["repository"] for row in payload["members"]} == {"api", "web"}
        assert payload["contracts"]["state"] == "compatible"
        steps = [(row["repository"], row["phase"]) for row in payload["deploy_order"]["steps"]]
        assert steps == [("api", "migrations"), ("api", "code"), ("web", "code")]
        # Neither Review is ready on its own yet, so neither is the set.
        assert payload["state"] == "blocked" and payload["ready_members"] == 0

    for review, other in ((web, api), (api, web)):
        rows = _set_rows(review)
        assert rows["change-set:members"]["state"] == "pending"
        assert f"r/{other.review_id}" in rows["change-set:members"]["detail"]
        assert rows["change-set:contracts"]["state"] == "pass"
        assert rows["change-set:deploy-order"]["state"] == "pass"
        assert _get(review, "/readiness")["ready"] is False
        # The header reads the set state from readiness: it is the same state the set view reports.
        assert {row["set_state"] for row in rows.values()} == {_get(review, "/change-set")["state"]} == {"blocked"}
    # The links never leak into the generic relationship lists; the overview counts them.
    relationships = _get(web, "/relationships")
    assert relationships["relations"] == [] and cs.CHANGE_SET_KIND not in json.dumps(relationships)
    assert _get(web, "")["change"]["change_set"] == {"available": True, "linked": True, "links": 1}

    # The Inbox record keeps the set blocker: another member reading this Change's own
    # readiness (without the set) never overwrites it.
    from lemoncrow.pro.capabilities.review.workflow_state import readiness_summaries

    session, revision = api.store.get_session(api.review_id), api.store.latest_revision(api.review_id)
    assert session is not None and revision is not None
    _get(api, "/readiness")
    _get(web, "/change-set")
    record = readiness_summaries(api.store, [revision.id], evaluator=session.reviewer_id)[revision.id]
    assert "change-set:members" in {row["id"] for row in record["blockers"]}


def test_a_contract_removed_by_one_head_and_used_by_another_blocks_every_member(tmp_path: Path) -> None:
    api, web, _hidden = _pair(
        tmp_path,
        api_base=API_BASE,
        api_head={"openapi.yaml": OPENAPI_WITHOUT_ORDERS},
        web_base=WEB_BASE,
        web_head={"web/client.py": CLIENT_OLD + 'EXTRA = "/invoices"\n'},
    )
    payload = _link(web, api)
    assert payload["contracts"]["state"] == "incompatible"
    issue = payload["contracts"]["issues"][0]
    assert (issue["contract"], issue["paths"]) == ("route /orders", ["web/client.py"])
    for review in (api, web):
        row = _set_rows(review)["change-set:contracts"]
        assert (row["state"], row["reason"]) == ("fail", "contract_incompatible")
        assert "route /orders" in row["detail"]


def test_an_unreadable_member_makes_the_set_incomplete_through_the_api(tmp_path: Path) -> None:
    api, web, hidden = _pair(tmp_path, api_base=API_BASE, api_head=API_HEAD, web_base=WEB_BASE, web_head=WEB_HEAD)
    _link(web, api)
    hidden["web"].add("api")
    payload = _get(web, "/change-set")
    assert payload["state"] == "incomplete" and payload["unreadable"] == 1
    readiness = _get(web, "/readiness")
    text = json.dumps(payload) + json.dumps(readiness)
    assert api.review_id not in text and "/api/app.py" not in text and "openapi.yaml" not in text
    row = _set_rows(web)["change-set:members"]
    assert (row["state"], row["reason"], row["set_state"]) == ("unknown", "evaluation_error", "incomplete")
    # Linking to the unreadable repository answers like an unknown Change.
    refused = web.client.post(
        f"/api/reviews/{web.review_id}/change-set", headers=HEADERS, json={"repository": "api", "review": api.review_id}
    )
    unknown = web.client.post(
        f"/api/reviews/{web.review_id}/change-set", headers=HEADERS, json={"repository": "nope", "review": "x"}
    )
    assert (refused.status_code, refused.json()) == (unknown.status_code, unknown.json())
    assert refused.status_code == 404


def test_unlinking_through_the_api_leaves_both_changes_in_no_set(tmp_path: Path) -> None:
    api, web, hidden = _pair(tmp_path, api_base=API_BASE, api_head=API_HEAD, web_base=WEB_BASE, web_head=WEB_HEAD)
    _link(web, api)
    body = {"repository": "api", "review": api.review_id}
    # The other side unreadable: answered like an unknown Change, nothing removed.
    hidden["web"].add("api")
    refused = web.client.post(f"/api/reviews/{web.review_id}/change-set/unlink", headers=HEADERS, json=body)
    assert refused.status_code == 404, refused.text
    assert len(web.store.list_relationships(web.review_id, kind=cs.CHANGE_SET_KIND)) == 1
    hidden["web"].clear()

    response = web.client.post(f"/api/reviews/{web.review_id}/change-set/unlink", headers=HEADERS, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["linked"] is False and response.json()["state"] == "not_linked"
    for review in (web, api):
        assert review.store.list_relationships(review.review_id, kind=cs.CHANGE_SET_KIND) == ()
        assert _set_rows(review) == {}
        assert _get(review, "/change-set")["linked"] is False
    again = web.client.post(f"/api/reviews/{web.review_id}/change-set/unlink", headers=HEADERS, json=body)
    assert again.status_code == 404


def test_the_overview_counts_only_links_both_sides_hold(tmp_path: Path) -> None:
    api, web, hidden = _pair(tmp_path, api_base=API_BASE, api_head=API_HEAD, web_base=WEB_BASE, web_head=WEB_HEAD)
    _link(web, api)
    assert _get(web, "")["change"]["change_set"] == {"available": True, "linked": True, "links": 1}
    # An unreadable member may link back: it counts (the set shows incomplete).
    hidden["web"].add("api")
    assert _get(web, "")["change"]["change_set"]["links"] == 1
    hidden["web"].clear()
    # The other side's row gone (a half-done unlink): web's own row joins nothing and is not counted.
    cs.unlink_row(api.store, api.review_id, cs.MemberRef("web", web.review_id))
    assert len(web.store.list_relationships(web.review_id, kind=cs.CHANGE_SET_KIND)) == 1
    assert _get(web, "")["change"]["change_set"] == {"available": True, "linked": False, "links": 0}
    assert _get(web, "/change-set")["linked"] is False


def test_every_member_whose_set_changed_has_its_armed_landing_revalidated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apps: dict[str, Any] = {}
    api, web, worker = (
        _review(tmp_path / name, base=base, head=head, change_set_directory=_Directory(name, apps, set()))
        for name, base, head in (("api", API_BASE, API_HEAD), ("web", WEB_BASE, WEB_HEAD), ("worker", {}, {"w.py": ""}))
    )
    apps.update({"api": api.client.app, "web": web.client.app, "worker": worker.client.app})
    revalidated: list[str] = []
    resolve = _Directory.member

    def spying(self: _Directory, ref: cs.MemberRef) -> cs.MemberSnapshot | None:
        member = resolve(self, ref)
        if member is None or member.revalidate_landing is None:
            return member
        real = member.revalidate_landing

        def revalidate() -> object:
            revalidated.append(member.repository)
            return real()  # the Reader's own revalidation runs (nothing armed: a no-op)

        return dataclasses.replace(member, revalidate_landing=revalidate)

    monkeypatch.setattr(_Directory, "member", spying)
    _link(web, api)
    assert revalidated == ["api"]
    # The api Change joining worker's set: worker and web (already in the set) are revalidated too.
    revalidated.clear()
    response = api.client.post(
        f"/api/reviews/{api.review_id}/change-set",
        headers=HEADERS,
        json={"repository": "worker", "review": worker.review_id},
    )
    assert response.status_code == 200, response.text
    assert sorted(revalidated) == ["web", "worker"]
    # Leaving: the member unlinked and the members that stay are revalidated as well.
    revalidated.clear()
    response = api.client.post(
        f"/api/reviews/{api.review_id}/change-set/unlink",
        headers=HEADERS,
        json={"repository": "web", "review": web.review_id},
    )
    assert response.status_code == 200, response.text
    assert sorted(revalidated) == ["web", "worker"]


def test_a_single_repository_reader_cannot_link_sets(tmp_path: Path) -> None:
    review = _review(tmp_path, base=WEB_BASE, head=WEB_HEAD)
    assert _get(review, "/change-set")["available"] is False
    response = review.client.post(
        f"/api/reviews/{review.review_id}/change-set", headers=HEADERS, json={"repository": "api", "review": "x"}
    )
    assert response.status_code == 409
    assert _get(review, "")["change"]["change_set"] == {"available": False, "linked": False, "links": 0}


# --------------------------------------------------------------------------- #
# the heads execute together (compat.mixed_version persistence, sandboxed)
# --------------------------------------------------------------------------- #

DEEP = "review:\n  effort: deep\n"
USERS_INIT = "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, legacy TEXT);\n"


@needs_bwrap
def test_another_members_head_sql_runs_against_this_heads_migrated_schema(tmp_path: Path) -> None:
    api, web, hidden = _pair(
        tmp_path,
        api_base={
            ".lemoncrow/review.yaml": DEEP,
            "db/migrations/0001_init.up.sql": USERS_INIT,
            "db/migrations/0001_init.down.sql": "DROP TABLE users;\n",
            "app/db.py": 'Q = "SELECT id, email FROM users WHERE id = ?"\n',
        },
        api_head={
            "db/migrations/0002_drop.up.sql": "ALTER TABLE users DROP COLUMN legacy;\n",
            "db/migrations/0002_drop.down.sql": "SELECT 1;\n",
        },
        web_base={"web/report.py": 'Q = "SELECT email FROM users WHERE id = ?"\n'},
        web_head={"web/report.py": 'Q = "SELECT email, legacy FROM users WHERE id = ?"\n'},
    )
    _link(web, api)
    report = _ok(api, "compat.mixed_version")
    assert report["verdict"] == "FAIL"
    record = _record(api, "compat.mixed_version", "persistence-db-migrations")
    pairings = {row["pairing"]: row for row in record["summary"]["pairings"]}
    together = pairings["set_heads_new_schema"]
    assert (together["state"], together["direction"], together["incompatible"]) == ("incompatible", "forward", 1)
    # This head's own old code never read the dropped column.
    assert pairings["old_code_new_schema"]["state"] == "compatible"
    finding = next(row for row in record["findings"] if row["rule"] == "set_member_statement_fails")
    assert finding["critical"] is True
    # The record on this member never carries the other repository's code, paths or errors.
    text = json.dumps(record)
    assert "web/report.py" not in text and "SELECT email, legacy" not in text and "no such column" not in text
    assert record["summary"]["set_statements"] == 1

    # A member the requester cannot read: the pairing cannot be constructed, never a pass.
    hidden["api"].add("web")
    report = _ok(api, "compat.mixed_version")
    assert report["verdict"] != "PASS"
    record = _record(api, "compat.mixed_version", "persistence-db-migrations")
    together = {row["pairing"]: row for row in record["summary"]["pairings"]}["set_heads_new_schema"]
    assert together["state"] == "not_available" and "not readable" in together["reason"]

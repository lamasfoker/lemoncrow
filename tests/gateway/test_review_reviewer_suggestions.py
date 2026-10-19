"""Reviewer suggestions (Phase 9 item 7): explained, deterministic, trusted-base only, advisory."""

from __future__ import annotations

import random
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.criticality import PolicyOrigin
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.reviewer_history import (
    change_authors,
    clear_history_cache,
    history_paths,
    mine_path_history,
)
from lemoncrow.pro.capabilities.review.reviewer_suggestion_sources import LocalIdentityResolver, compose_suggestions
from lemoncrow.pro.capabilities.review.reviewer_suggestions import (
    CodeOwnersAuthority,
    ReviewerEvidence,
    ReviewerIdentity,
    codeowners_head_changes,
    operator_routing_layers,
    parse_codeowners,
    parse_routing_settings,
    rank_suggestions,
    resolve_codeowners,
    resolve_routing,
    rule_evidence,
    trusted_base_routing,
)
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.trusted_base import TrustedBaseDocument

TOKEN = "t" * 32
PORT = 45991
BASE = f"http://127.0.0.1:{PORT}"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    clear_history_cache()


def _user(name: str) -> ReviewerIdentity:
    return ReviewerIdentity("user", f"handle:@{name}", f"@{name}", handle=name)


def _team(name: str) -> ReviewerIdentity:
    return ReviewerIdentity("team", f"team:@acme/{name}", f"@acme/{name}", handle=f"acme/{name}")


def _normal(_path: str) -> str:
    return "normal"


def _no_exclusion(_identity: ReviewerIdentity) -> None:
    return None


def _granted(_identity: ReviewerIdentity) -> Any:
    return "granted"


def test_reviewer_suggestion_codeowners_contract() -> None:
    """case: test_codeowners_follow_github_pattern_semantics_and_last_match_wins"""
    owners = parse_codeowners(
        "\n".join(
            [
                "# comment",
                "*       @default",
                "*.js    @js-owner  # inline comment",
                "docs/*  @docs",
                "/build/ @acme/build",
                "apps/**/test @tester",
                "src/generated/",
                "vendor/ not-an-owner",
            ]
        ),
        path=".github/CODEOWNERS",
    )

    def who(path: str) -> tuple[str, ...]:
        return owners.owners_for(path)[0]

    assert who("README.md") == ("@default",)
    assert who("web/app.js") == ("@js-owner",)
    assert who("docs/guide.md") == ("@docs",)
    assert who("docs/deep/guide.md") == ("@default",)
    assert who("build/out/x.txt") == ("@acme/build",)
    assert who("nested/build/x.txt") == ("@default",)
    assert who("apps/a/b/test/x.py") == ("@tester",)
    assert who("src/generated/api.py") == ()
    assert owners.errors and "invalid owner" in owners.errors[0]
    "case: test_codeowners_resolution_uses_precedence_and_never_promotes_past_an_unreadable_file"
    present = resolve_codeowners(_reader({"CODEOWNERS": ("present", "* @root\n")}))
    assert present.state == "present" and present.path == "CODEOWNERS"
    assert present.status().state == "available"
    preferred = resolve_codeowners(
        _reader({".github/CODEOWNERS": ("present", "* @gh\n"), "CODEOWNERS": ("present", "* @root\n")})
    )
    assert preferred.path == ".github/CODEOWNERS"
    blocked = resolve_codeowners(
        _reader({".github/CODEOWNERS": ("provisional", ""), "CODEOWNERS": ("present", "* @root\n")})
    )
    assert blocked.state == "provisional" and blocked.codeowners is None
    assert blocked.status().state == "provisional"
    assert resolve_codeowners(_reader({})).state == "absent"
    "case: test_head_codeowners_edit_is_reported_as_a_change_never_used_for_routing"
    files = [{"path": "src/app.py", "status": "modified"}, {"path": ".github/CODEOWNERS", "status": "modified"}]
    base = resolve_codeowners(_reader({".github/CODEOWNERS": ("present", "* @base-owner\n")}))
    result = compose_suggestions(
        revision_id="rev-1",
        changed_files=files,
        criticality=_normal,
        codeowners=base,
        routing=resolve_routing([]),
        resolver=LocalIdentityResolver(),
        exclude=_no_exclusion,
        access=_granted,
    )
    keys = [row.identity.key for row in result.suggestions]
    assert keys == ["handle:@base-owner"]
    assert result.head_changes and result.head_changes[0]["path"] == ".github/CODEOWNERS"
    assert "trusted base" in result.head_changes[0]["note"]
    assert codeowners_head_changes([{"path": "src/x.py", "old_path": "CODEOWNERS", "status": "renamed"}])
    wire = result.to_wire()
    assert wire["advisory"] is True and wire["head_changes"][0]["change"] == "modified"


def test_reviewer_suggestion_candidate_contract() -> None:
    """case: test_suggestions_carry_reasons_and_order_deterministically_for_equal_evidence"""
    first, _excluded, _truncated = rank_suggestions(
        _evidence(), criticality=_crit, exclude=_no_exclusion, access=_granted
    )
    order = [row.identity.key for row in first]
    assert order == ["handle:@lead", "handle:@amy", "handle:@zed", "team:@acme/core", "handle:@bob"]
    for seed in range(5):
        shuffled = _evidence()
        random.Random(seed).shuffle(shuffled)
        again, _e, _t = rank_suggestions(shuffled, criticality=_crit, exclude=_no_exclusion, access=_granted)
        assert [row.to_wire() for row in again] == [row.to_wire() for row in first]
    lead = first[0]
    assert lead.critical_paths == ("src/crit.py",)
    assert lead.reasons[0].signal == "rule" and lead.reasons[0].paths == ("src/crit.py",)
    assert "Reviewer rule r1" in lead.explanation and "src/crit.py" in lead.explanation
    bob = next(row for row in first if row.identity.key == "handle:@bob")
    assert bob.reasons[0].signal == "history" and bob.reasons[0].count == 3
    assert "3 commits" in bob.explanation
    team = next(row for row in first if row.identity.kind == "team")
    assert team.to_wire()["kind"] == "team" and team.to_wire()["status"] == "suggested"
    "case: test_author_authoring_agent_and_no_access_candidates_are_excluded"

    def exclude(identity: ReviewerIdentity) -> Any:
        if identity.key == "handle:@amy":
            return "author"
        if identity.key == "handle:@bob":
            return "authoring_agent"
        return None

    def access(identity: ReviewerIdentity) -> Any:
        return "denied" if identity.key == "handle:@zed" else "unknown"

    kept, excluded, truncated = rank_suggestions(_evidence(), criticality=_crit, exclude=exclude, access=access)
    assert [row.identity.key for row in kept] == ["handle:@lead", "team:@acme/core"]
    assert excluded == {"author": 1, "authoring_agent": 1, "no_access": 1}
    assert truncated == 0
    assert {row.access for row in kept} == {"unknown"}
    limited, _e, truncated = rank_suggestions(
        _evidence(), criticality=_crit, exclude=_no_exclusion, access=_granted, limit=2
    )
    assert len(limited) == 2 and truncated == 3
    "case: test_requested_candidates_are_marked_not_duplicated"
    kept, _e, _t = rank_suggestions(
        _evidence(), criticality=_crit, exclude=_no_exclusion, access=_granted, requested={"handle:@amy"}
    )
    amy = next(row for row in kept if row.identity.key == "handle:@amy")
    assert amy.requested and amy.to_wire()["status"] == "requested"
    assert sum(1 for row in kept if row.identity.key == "handle:@amy") == 1
    "case: test_rules_respect_criticality_floor_and_only_operator_layers_route_automatically"
    org = PolicyOrigin("organization", "operator_review_policy")
    operator = parse_routing_settings(
        {
            "automatic": True,
            "rules": [
                {"id": "auth", "paths": ["src/auth/**"], "users": ["@sec"], "automatic": True},
                {"id": "crit", "paths": ["src/**"], "teams": ["acme/core"], "min_criticality": "high"},
            ],
        },
        origin=org,
    )
    assert not operator.errors
    repository = trusted_base_routing(
        "review:\n  reviewers:\n    - id: repo-self\n      paths: ['src/**']\n      users: ['@self']\n      automatic: true\n",
        source="base .lemoncrow/review.yaml",
        commit="abc",
    )
    assert repository is not None and (not repository.errors)
    routing = resolve_routing([operator, repository])
    assert routing.automatic is True
    by_id = {rule.id: rule for rule in routing.rules}
    assert routing.routes_automatically(by_id["auth"]) is True
    assert routing.routes_automatically(by_id["repo-self"]) is False
    assert routing.routes_automatically(by_id["crit"]) is False
    resolver = LocalIdentityResolver()

    def level(path: str) -> str:
        return "high" if path.startswith("src/auth/") else "low"

    rows = rule_evidence(
        routing,
        ["src/auth/login.py", "src/util.py"],
        criticality=level,
        resolve_user=resolver.user,
        resolve_team=resolver.team,
    )
    by_rule = {(row.rule_id, row.identity.key): row for row in rows}
    assert by_rule["crit", "team:@acme/core"].paths == ("src/auth/login.py",)
    assert by_rule["auth", "handle:@sec"].automatic is True
    assert by_rule["repo-self", "handle:@self"].automatic is False
    repo_off = parse_routing_settings({"automatic": False}, origin=PolicyOrigin("repository", "operator"))
    assert resolve_routing([operator, repo_off]).automatic is False
    assert resolve_routing([]).automatic is False
    "case: test_invalid_rules_are_reported_and_never_guessed"
    layer = parse_routing_settings(
        {"automatic": "yes", "rules": [{"id": "x", "paths": []}, {"paths": ["a"], "users": ["@u"]}], "extra": 1},
        origin=PolicyOrigin("organization", "operator"),
    )
    assert layer.rules == () and layer.automatic is None
    joined = " | ".join(layer.errors)
    assert "unknown field 'extra'" in joined and "routing.automatic must be a boolean" in joined
    assert "needs paths" in joined and "stable identifier" in joined
    projected = operator_routing_layers(
        {"routing": {"layers": [{"origin": {"layer": "organization"}, "settings": {"automatic": True}}]}}
    )
    assert projected[0].automatic is True and projected[0].origin.layer == "organization"


def test_reviewer_suggestion_history_contract() -> None:
    """case: test_ownership_comes_from_judgments_and_comments_in_earlier_reviews_only"""
    from lemoncrow.pro.capabilities.review.reviewer_suggestion_sources import ownership_evidence

    def session(review_id: str, repo: str = "repo") -> Any:
        return SimpleNamespace(id=review_id, repo_root=repo)

    anchor = SimpleNamespace(path="src/a.py")
    store = SimpleNamespace(
        list_sessions=lambda status, limit: [session("current"), session("r1"), session("r2"), session("x", "other")],
        latest_revision=lambda review_id: SimpleNamespace(id=f"{review_id}-rev"),
        list_units=lambda revision_id: [SimpleNamespace(path="src/a.py"), SimpleNamespace(path="src/z.py")],
        latest_outcomes=lambda review_id: [
            SimpleNamespace(outcome="lgtm", reviewer_id="pat"),
            SimpleNamespace(outcome="commented", reviewer_id="noise"),
            SimpleNamespace(outcome="lgtm", reviewer_id="local"),
        ],
        list_annotations=lambda review_id: [
            SimpleNamespace(anchor=anchor, source="human", created_by_actor="human", created_by="cam"),
            SimpleNamespace(anchor=anchor, source="ai_review", created_by_actor="agent", created_by="bot"),
        ],
    )
    evidence, status = ownership_evidence(
        store, "current", ["src/a.py"], LocalIdentityResolver(), same_repository=lambda other: other.repo_root == "repo"
    )
    by_key = {row.identity.key: row for row in evidence}
    assert set(by_key) == {"principal:pat", "principal:cam"}
    assert by_key["principal:pat"].count == 2 and by_key["principal:pat"].paths == ("src/a.py",)
    assert status.state == "available" and status.detail == "2 Review(s) consulted"
    "case: test_codeowners_authority_status_maps_present_to_available"
    assert CodeOwnersAuthority("present").status().state == "available"
    assert CodeOwnersAuthority("invalid").status().state == "invalid"


def _reader(documents: dict[str, tuple[str, str]]) -> Any:

    def read(path: str) -> TrustedBaseDocument:
        state, content = documents.get(path, ("absent", ""))
        return TrustedBaseDocument(path=path, content=content, state=state, source=f"base {path}")

    return read


def _evidence() -> list[ReviewerEvidence]:
    return [
        ReviewerEvidence("codeowners", _user("zed"), ("src/a.py",), "base CODEOWNERS", "CODEOWNERS src/"),
        ReviewerEvidence("codeowners", _user("amy"), ("src/a.py",), "base CODEOWNERS", "CODEOWNERS src/"),
        ReviewerEvidence("codeowners", _team("core"), ("src/a.py",), "base CODEOWNERS", "CODEOWNERS src/"),
        ReviewerEvidence("history", _user("bob"), ("src/a.py", "src/b.py"), "git", "authored 3 commits", count=3),
        ReviewerEvidence("rule", _user("lead"), ("src/crit.py",), "org policy", "reviewer rule r1", rule_id="r1"),
    ]


def _crit(path: str) -> str:
    return "critical" if path == "src/crit.py" else "normal"


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _commit(repo: Any, message: str, offset: int, *, name: str, email: str) -> str:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    signature = pygit2.Signature(name, email, 1700000000 + offset, 0)
    return str(repo.create_commit("HEAD", signature, signature, message, tree, parents))


def _history_repo(tmp_path: Path) -> SimpleNamespace:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    subprocess.run(["git", "-C", str(root), "config", "user.email", "author@example.invalid"], check=True)
    _write(root, "src/auth.py", "v1\n")
    _write(root, "docs/readme.md", "docs\n")
    _commit(repo, "init", 0, name="Alice", email="alice@example.invalid")
    _write(root, "src/auth.py", "v2\n")
    _commit(
        repo,
        "harden auth\n\nReviewed-by: Rita Reviewer <rita@example.invalid>",
        1,
        name="Alice",
        email="alice@example.invalid",
    )
    _write(root, "src/auth.py", "v3\n")
    _commit(repo, "bot bump", 2, name="dependabot[bot]", email="49699333+dependabot[bot]@users.noreply.github.com")
    _write(root, "docs/readme.md", "docs 2\n")
    base = _commit(repo, "docs", 3, name="Dora", email="dora@example.invalid")
    _write(root, "src/auth.py", "v4 change\n")
    _write(root, "src/new_mod.py", "new\n")
    head = _commit(repo, "the change", 4, name="Change Author", email="author@example.invalid")
    return SimpleNamespace(root=root, repo=repo, base=base, head=head)


def test_history_is_mined_at_the_base_with_trailers_bots_excluded_and_cached(tmp_path: Path) -> None:
    fixture = _history_repo(tmp_path)
    files = [{"path": "src/auth.py", "status": "modified"}, {"path": "src/new_mod.py", "status": "added"}]
    paths = history_paths(files)
    assert paths == {"src/auth.py": "src/auth.py", "src/new_mod.py": None}
    result = mine_path_history(fixture.root, fixture.base, paths)
    assert result.state == "available" and result.base_sha == fixture.base
    by_identity = {(row.email, row.role): row for row in result.contributors}
    alice = by_identity["alice@example.invalid", "author"]
    assert alice.commits == 2 and set(alice.paths) == {"src/auth.py", "src/new_mod.py"}
    assert set(by_identity["rita@example.invalid", "reviewer"].paths) == {"src/auth.py", "src/new_mod.py"}
    emails = {row.email for row in result.contributors}
    assert "author@example.invalid" not in emails
    assert not any("[bot]" in email for email in emails)
    assert "dora@example.invalid" not in emails
    assert mine_path_history(fixture.root, fixture.base, paths) is result
    clear_history_cache()
    assert mine_path_history(fixture.root, fixture.base, paths) == result
    bounded = mine_path_history(fixture.root, fixture.base, paths, max_commits=1)
    assert bounded.commits_scanned == 1
    assert mine_path_history(fixture.root, "HEAD", paths).state == "unavailable"
    assert change_authors(fixture.root, fixture.base, fixture.head) == ("author@example.invalid",)


def _local_review(tmp_path: Path, *, intelligence_policy: Any = None) -> SimpleNamespace:
    fixture_root = tmp_path / "repo"
    fixture_root.mkdir(parents=True)
    repo = pygit2.init_repository(str(fixture_root), initial_head="main")
    subprocess.run(["git", "-C", str(fixture_root), "config", "user.email", "author@example.invalid"], check=True)
    _write(fixture_root, "pyproject.toml", "[project]\nname = 'fixture'\n")
    _write(fixture_root, "src/auth.py", "def authorize(user):\n    return False\n")
    _commit(repo, "seed", -1, name="Change Author", email="author@example.invalid")
    _write(fixture_root, "src/auth.py", "def authorize(user):\n    return user.is_admin\n")
    _write(fixture_root, ".github/CODEOWNERS", "src/ @base-owner @acme/core\n")
    _write(
        fixture_root,
        ".lemoncrow/review.yaml",
        "review:\n  focus:\n    - paths: ['src/auth.py']\n      criticality: critical\n  reviewers:\n    - id: auth-rule\n      paths: ['src/auth.py']\n      users: ['@sec-lead']\n",
    )
    _commit(repo, "base", 0, name="Alice", email="alice@example.invalid")
    _write(
        fixture_root,
        "src/auth.py",
        "def authorize(user):\n    if user is None:\n        return True\n    return user.is_admin\n",
    )
    _write(fixture_root, ".github/CODEOWNERS", "src/ @friendly\n")
    _commit(repo, "change", 1, name="Change Author", email="author@example.invalid")
    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(fixture_root, base="HEAD~1", head="HEAD")
    build = build_review_packet_with_blobs(fixture_root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, fixture_root, rng, title=build.packet.title)
    revision = snapshot_revision(store, session, fixture_root, rng, store_root=store_root, build=build)
    store.publish_snapshot(session.id, revision.id)
    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=fixture_root,
        port=PORT,
        intelligence_policy=intelligence_policy,
    )
    client = TestClient(app, base_url=BASE, raise_server_exceptions=False)
    return SimpleNamespace(client=client, store=store, review_id=session.id, revision_id=revision.id)


def test_local_route_suggests_from_the_trusted_base_and_never_requests(tmp_path: Path) -> None:
    review = _local_review(tmp_path)
    before = review.store.list_activity_events(review.review_id)
    response = review.client.get(
        f"/api/reviews/{review.review_id}/reviewer-suggestions",
        params={"revision_id": review.revision_id},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["advisory"] is True and body["revision_id"] == review.revision_id
    keys = [row["key"] for row in body["suggestions"]]
    assert keys[:3] == ["handle:@sec-lead", "handle:@base-owner", "team:@acme/core"]
    assert "email:alice@example.invalid" in keys
    assert "handle:@friendly" not in keys
    assert body["head_changes"] == [
        {"path": ".github/CODEOWNERS", "change": "modified", "note": body["head_changes"][0]["note"]}
    ]
    assert "email:author@example.invalid" not in keys
    assert body["excluded"] == {"author": 1}
    lead = body["suggestions"][0]
    assert lead["reasons"][0]["signal"] == "rule" and lead["reasons"][0]["rule_ids"] == ["auth-rule"]
    assert lead["critical_paths"] == ["src/auth.py"]
    assert lead["access"] == "not_applicable" and lead["status"] == "suggested"
    owner = body["suggestions"][1]
    assert owner["reasons"][0]["signal"] == "codeowners"
    assert ".github/CODEOWNERS src/" in owner["explanation"]
    signals = {row["signal"]: row["state"] for row in body["signals"]}
    assert signals == {"rule": "available", "codeowners": "available", "ownership": "available", "history": "available"}
    assert body["automatic_routing"] is False
    assert review.store.list_activity_events(review.review_id) == before
    again = review.client.get(f"/api/reviews/{review.review_id}/reviewer-suggestions", headers=HEADERS).json()
    assert again == body


def test_local_route_reads_operator_routing_layers(tmp_path: Path) -> None:
    review = _local_review(
        tmp_path,
        intelligence_policy=lambda _session, _revision: {
            "routing": {
                "layers": [
                    {
                        "origin": {"layer": "organization", "source": "org review policy"},
                        "settings": {
                            "automatic": True,
                            "rules": [{"id": "org-auth", "paths": ["src/**"], "teams": ["acme/security"]}],
                        },
                    }
                ]
            }
        },
    )
    body = review.client.get(f"/api/reviews/{review.review_id}/reviewer-suggestions", headers=HEADERS).json()
    assert body["automatic_routing"] is True
    security = next(row for row in body["suggestions"] if row["key"] == "team:@acme/security")
    assert security["reasons"][0]["rule_ids"] == ["org-auth"]
    assert security["automatic_rules"] == []


def test_ownership_states_its_review_bound_at_and_past_the_limit() -> None:
    from lemoncrow.pro.capabilities.review.reviewer_suggestion_sources import ownership_evidence

    def store_of(count: int) -> Any:
        rows = [SimpleNamespace(id=f"r{index}") for index in range(count)]
        return SimpleNamespace(
            list_sessions=lambda status, limit: [SimpleNamespace(id="current"), *rows][:limit],
            latest_revision=lambda review_id: None,
            list_annotations=lambda review_id: [],
        )

    _evidence_at, at_limit = ownership_evidence(
        store_of(3), "current", ["a.py"], LocalIdentityResolver(), limit_reviews=3
    )
    assert (at_limit.state, at_limit.detail) == ("available", "3 Review(s) consulted")
    _evidence_past, past = ownership_evidence(
        store_of(4), "current", ["a.py"], LocalIdentityResolver(), limit_reviews=3
    )
    assert past.state == "provisional"
    assert past.detail == "3 Review(s) consulted; only the 3 most recently updated Reviews were read"


def test_history_states_its_path_and_commit_bounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import reviewer_history
    from lemoncrow.pro.capabilities.review.reviewer_suggestion_sources import history_status

    fixture = _history_repo(tmp_path)
    paths = {"src/auth.py": "src/auth.py", "docs/readme.md": "docs/readme.md"}
    monkeypatch.setattr(reviewer_history, "MAX_HISTORY_PATHSPECS", 2)
    at_limit = mine_path_history(fixture.root, fixture.base, paths)
    assert at_limit.paths_unmined == 0 and history_status(at_limit).state == "available"
    monkeypatch.setattr(reviewer_history, "MAX_HISTORY_PATHSPECS", 1)
    past = mine_path_history(fixture.root, fixture.base, paths)
    # docs/ sorts first, so src/auth.py is the path past the bound: it is counted, never silently dropped.
    assert past.paths_unmined == 1 and all("src/auth.py" not in row.paths for row in past.contributors)
    status = history_status(past)
    assert status.state == "provisional" and "1 changed path(s) past the path bound were not mined" in status.detail

    # src/ has three reachable commits: a bound of 3 may hide older ones, a bound of 4 provably does not.
    src = {"src/auth.py": "src/auth.py"}
    at_commits = mine_path_history(fixture.root, fixture.base, src, max_commits=3)
    assert at_commits.commits_scanned == 3 and at_commits.commits_bounded
    assert "older history was not read" in history_status(at_commits).detail
    past_commits = mine_path_history(fixture.root, fixture.base, src, max_commits=4)
    assert past_commits.commits_scanned == 3 and not past_commits.commits_bounded


def test_the_history_cache_is_keyed_on_every_requested_path_at_and_past_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review import reviewer_history

    fixture = _history_repo(tmp_path)
    monkeypatch.setattr(reviewer_history, "MAX_HISTORY_PATHSPECS", 1)
    # At the bound: one spec, nothing unmined.
    at_limit = mine_path_history(fixture.root, fixture.base, {"docs/readme.md": "docs/readme.md"})
    assert at_limit.paths_unmined == 0
    # Past it: the same mined spec plus one path past the bound is another request, never the cached one.
    past = mine_path_history(
        fixture.root, fixture.base, {"docs/readme.md": "docs/readme.md", "src/auth.py": "src/auth.py"}
    )
    assert past is not at_limit and past.paths_unmined == 1
    # And the same request under a wider bound is mined in full, not served from the narrower result.
    monkeypatch.setattr(reviewer_history, "MAX_HISTORY_PATHSPECS", 2)
    widened = mine_path_history(
        fixture.root, fixture.base, {"docs/readme.md": "docs/readme.md", "src/auth.py": "src/auth.py"}
    )
    assert widened.paths_unmined == 0

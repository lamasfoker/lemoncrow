"""Phase 9 item 24: read-only stack awareness and ownership routing (local git path).

A Review based on another open Review's branch shows its layer and the related
Changes with their state; the diff against the lower layer's head excludes the
lower layer's work; feedback on code the lower layer introduced points at -- and
P12-FIX hands off to -- the lower Change; nothing in the projection or its
routes can create, rebase, retarget or push a branch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review import stack_awareness as sa
from lemoncrow.pro.capabilities.review.anchors import build_anchor
from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.relationships import change_context_payload
from lemoncrow.pro.capabilities.review.session_models import Annotation, ReviewAttribution, ReviewSession
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, read_packet_json, refresh
from lemoncrow.pro.capabilities.review.store import ReviewStore

TOKEN = "stack-awareness-test-token"
PORT = 45991
BASE = f"http://127.0.0.1:{PORT}"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "lemoncrow-root"))
    sa._local_cache.clear()


def _signature(offset: int) -> Any:
    return pygit2.Signature("Fixture Tester", "fixture@example.test", 1700000000 + offset, 0)


def _commit(repo: Any, ref: str, parent: str | None, files: dict[str, str], message: str, offset: int) -> str:
    index = pygit2.Index()
    if parent is not None:
        index.read_tree(repo.get(parent).tree)
    for path, text in files.items():
        index.add(pygit2.IndexEntry(path, repo.create_blob(text.encode("utf-8")), pygit2.enums.FileMode.BLOB))
    tree = index.write_tree(repo)
    sha = str(
        repo.create_commit(
            None, _signature(offset), _signature(offset), message, tree, [] if parent is None else [parent]
        )
    )
    repo.references.create(ref, sha, force=True)
    return sha


def _x(*, lower: bool, upper: bool = False, extra: str = "") -> str:
    lines = [f"L{index:02d} = {index}\n" for index in range(1, 31)]
    if upper:
        lines[11] = "L12 = 'upper'\n"
    if lower:
        lines[10:10] = ["LOWER_1 = 1\n", "LOWER_2 = 2\n", "LOWER_3 = 3\n"]
    return "".join(lines) + extra


class Stack:
    """main <- feature-a (lower Review) <- feature-b (upper Review)."""

    def __init__(self, tmp_path: Path, *, upper_range: str = "feature-a...feature-b") -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "repo"
        self.root.mkdir()
        self.repo = pygit2.init_repository(str(self.root), initial_head="main")
        self.repo.config["user.name"] = "Fixture Tester"
        self.repo.config["user.email"] = "fixture@example.test"
        self.main = _commit(self.repo, "refs/heads/main", None, {"x.py": _x(lower=False), "y.py": "Y = 1\n"}, "base", 0)
        self.repo.checkout("refs/heads/main", strategy=pygit2.enums.CheckoutStrategy.FORCE)
        self.a = _commit(
            self.repo, "refs/heads/feature-a", self.main, {"x.py": _x(lower=True), "y.py": "Y = 2\n"}, "lower", 1
        )
        self.b = _commit(
            self.repo,
            "refs/heads/feature-b",
            self.a,
            {"x.py": _x(lower=True, upper=True), "z.py": "Z = 1\n"},
            "upper",
            2,
        )
        self.store = ReviewStore(tmp_path / "store")
        self.lower = self.open("main...feature-a")
        self.upper = self.open(upper_range)

    def open(self, spec: str) -> ReviewSession:
        session = open_or_create_session(self.store, self.root, resolve_rev_range(self.root, spec))
        self.capture(session)
        return self.store.get_session(session.id) or session

    def capture(self, session: ReviewSession) -> Any:
        current = self.store.get_session(session.id) or session
        return refresh(self.store, current, self.root, store_root=self.tmp / "capture", limit=5000)

    def stack(self, session: ReviewSession) -> sa.StackProjection | None:
        sa._local_cache.clear()
        return sa.stack_for(self.store, self.store.get_session(session.id) or session)

    def refs(self) -> dict[str, str]:
        return {name: str(self.repo.references.get(name).target) for name in self.repo.references}


def _resolver(world: Stack, session: ReviewSession) -> sa.OwnershipResolver:
    sa._local_cache.clear()
    revision = world.store.latest_revision(session.id)
    assert revision is not None
    return sa.ownership_resolver(world.store, world.store.get_session(session.id) or session, revision)


# --------------------------------------------------------------------------- #
# layer, related Changes and readiness                                        #
# --------------------------------------------------------------------------- #


def test_stacked_review_shows_its_layer_and_related_changes_with_readiness(tmp_path: Path) -> None:
    world = Stack(tmp_path)
    refs_before = world.refs()

    upper = world.stack(world.upper)
    assert upper is not None and upper.source == "local_ancestry"
    assert upper.layer == 2 and upper.depth == 2 and upper.base_kind == "lower_change"
    (lower_member,) = upper.lower
    assert lower_member.review_id == world.lower.id and lower_member.relation == "exact"
    assert lower_member.head_sha == world.a
    assert lower_member.readiness.state == "in_review"
    wire = upper.to_wire()
    assert wire["mutation"] == "read_only" and wire["layer"] == 2
    assert wire["base"] == {
        "kind": "lower_change",
        "ref": lower_member.head_ref,
        "sha": world.a,
        "change_ref": f"r/{world.lower.id}",
    }

    lower = world.stack(world.lower)
    assert lower is not None and lower.layer == 1 and lower.depth == 2 and lower.base_kind == "trunk"
    assert [member.review_id for member in lower.upper] == [world.upper.id]

    # Readiness follows the related Change's durable facts on its *current* Snapshot.
    lower_revision = world.store.latest_revision(world.lower.id)
    assert lower_revision is not None
    world.store.record_outcome(world.lower.id, lower_revision.id, "alice", outcome="lgtm")
    refreshed = world.stack(world.upper)
    assert refreshed is not None and refreshed.lower[0].readiness.state == "approved"
    world.store.record_outcome(world.lower.id, lower_revision.id, "bob", outcome="changes_requested")
    refreshed = world.stack(world.upper)
    assert refreshed is not None and refreshed.lower[0].readiness.state == "changes_requested"

    # The projection reads git; it never writes a ref.
    assert world.refs() == refs_before


def test_diff_against_the_lower_head_excludes_lower_layer_work(tmp_path: Path) -> None:
    world = Stack(tmp_path)
    revision = world.store.latest_revision(world.upper.id)
    assert revision is not None
    paths = {unit.path for unit in world.store.list_units(revision.id)}
    assert "y.py" not in paths, "the lower layer's file is not this layer's authored work"
    assert {"x.py", "z.py"} <= paths
    packet = read_packet_json(world.store, revision)
    assert packet is not None
    added = [
        line
        for row in packet["files"]
        if row["path"] == "x.py"
        for hunk in row["hunks"]
        for line in str(hunk.get("patch") or "").splitlines()
        if line.startswith("+")
    ]
    assert added == ["+L12 = 'upper'"], added


def test_included_lower_layer_is_disclosed_and_attributed_file_by_file(tmp_path: Path) -> None:
    world = Stack(tmp_path, upper_range="main...feature-b")
    upper = world.stack(world.upper)
    assert upper is not None and upper.base_kind == "includes_lower_changes"
    (member,) = upper.lower
    assert member.relation == "included" and member.review_id == world.lower.id
    # y.py only the lower layer changed; x.py both changed (stays with this Change); z.py is ours.
    assert member.owned_paths == ("y.py",)
    assert f"r/{world.lower.id}" in upper.note
    # The lower layer is not excluded from a captured diff: the limitation is explicit, with the exact remedy.
    assert "files both layers changed are not split" in upper.note
    assert f"Review against {member.head_ref or member.head_sha[:12]} to exclude that layer." in upper.note
    resolver = _resolver(world, world.upper)
    owner = resolver.resolve(path="y.py", side="new", start_line=1)
    assert owner is not None and owner.review_id == world.lower.id
    assert resolver.resolve(path="x.py", side="new", start_line=12) is None
    assert resolver.resolve(path="z.py", side="new", start_line=1) is None


def test_integrated_lower_layer_is_no_longer_a_layer(tmp_path: Path) -> None:
    world = Stack(tmp_path)
    world.repo.references.create("refs/remotes/origin/main", world.a, force=True)
    upper = world.stack(world.upper)
    assert upper is None or not upper.lower


def test_unrelated_reviews_are_not_a_stack(tmp_path: Path) -> None:
    world = Stack(tmp_path)
    _commit(world.repo, "refs/heads/side", world.main, {"w.py": "W = 1\n"}, "side", 5)
    side = world.open("main...side")
    assert world.stack(side) is None
    upper = world.stack(world.upper)
    assert upper is not None and [item.review_id for item in upper.lower] == [world.lower.id]


def test_depth_bound_at_and_past_the_limit_is_disclosed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sa, "MAX_STACK_DEPTH", 2)
    world = Stack(tmp_path)  # main <- feature-a <- feature-b
    c = _commit(world.repo, "refs/heads/feature-c", world.b, {"c.py": "C = 1\n"}, "third", 6)
    _commit(world.repo, "refs/heads/feature-d", c, {"d.py": "D = 1\n"}, "fourth", 7)
    third = world.open("feature-b...feature-c")
    fourth = world.open("feature-c...feature-d")

    at_bound = world.stack(third)
    assert at_bound is not None and len(at_bound.lower) == 2 and at_bound.complete
    assert [item.review_id for item in at_bound.lower] == [world.upper.id, world.lower.id]

    past_bound = world.stack(fourth)
    assert past_bound is not None and len(past_bound.lower) == 2
    assert past_bound.complete is False and "may be missing" in past_bound.note
    assert past_bound.to_wire()["complete"] is False


def test_candidate_bound_at_and_past_the_limit_is_disclosed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = Stack(tmp_path)
    monkeypatch.setattr(sa, "MAX_LOCAL_CANDIDATES", 1)
    at_bound = world.stack(world.upper)  # exactly one other open Review: nothing is cut
    assert at_bound is not None and at_bound.complete and at_bound.lower[0].review_id == world.lower.id

    _commit(world.repo, "refs/heads/side", world.main, {"w.py": "W = 1\n"}, "side", 5)
    world.open("main...side")
    past_bound = world.stack(world.upper)
    assert past_bound is not None and past_bound.complete is False
    assert "may be missing" in past_bound.note
    wire = change_context_payload(world.store, world.upper, include_stack=True)["stack"]
    assert wire is not None and wire["complete"] is False


# --------------------------------------------------------------------------- #
# ownership routing                                                           #
# --------------------------------------------------------------------------- #


def test_feedback_on_lower_layer_code_routes_to_the_lower_change(tmp_path: Path) -> None:
    world = Stack(tmp_path)
    resolver = _resolver(world, world.upper)
    assert resolver.active
    # New-side line 12 (LOWER_2) is unchanged context in this diff, added by the lower layer.
    owner = resolver.resolve(path="x.py", side="new", start_line=12)
    assert owner is not None and owner.review_id == world.lower.id
    assert owner.to_wire()["change_ref"] == f"r/{world.lower.id}"
    # The line this layer changed, and context neither layer added, stay here.
    assert resolver.resolve(path="x.py", side="new", start_line=15) is None
    assert resolver.resolve(path="x.py", side="new", start_line=17) is None
    assert resolver.resolve(path="z.py", side="new", start_line=1) is None
    # The lower Change's own Review never routes to itself.
    assert not _resolver(world, world.lower).active


def test_a_moved_lower_layer_keeps_ownership_here(tmp_path: Path) -> None:
    world = Stack(tmp_path)
    moved = _commit(
        world.repo, "refs/heads/feature-a", world.a, {"x.py": _x(lower=True), "y.py": "Y = 3\n"}, "lower 2", 4
    )
    world.capture(world.lower)
    upper = world.stack(world.upper)
    assert upper is not None and upper.lower[0].relation == "moved" and upper.lower[0].head_sha == world.a
    assert "moved on" in upper.note
    lower_revision = world.store.latest_revision(world.lower.id)
    assert lower_revision is not None and lower_revision.head_sha == moved
    # The lower layer's current geometry no longer describes this diff's base: no guess.
    assert _resolver(world, world.upper).resolve(path="x.py", side="new", start_line=12) is None


def test_new_to_old_line_mapping_is_exact_or_undecided() -> None:
    row = {
        "path": "f.py",
        "hunks": [
            {
                "old_start": 3,
                "old_lines": 3,
                "new_start": 3,
                "new_lines": 4,
                "patch": " a\n-b\n+B\n+C\n c\n",
            }
        ],
    }
    assert sa.new_to_old_line(row, 1) == 1
    assert sa.new_to_old_line(row, 3) == 3
    assert sa.new_to_old_line(row, 4) is None and sa.new_to_old_line(row, 5) is None
    assert sa.new_to_old_line(row, 6) == 5
    assert sa.new_to_old_line(row, 20) == 19
    assert sa.new_to_old_line(None, 7) == 7
    no_body = {"path": "f.py", "hunks": [{"old_start": 3, "old_lines": 3, "new_start": 3, "new_lines": 4}]}
    assert sa.new_to_old_line(no_body, 4) is None


# --------------------------------------------------------------------------- #
# API round-trip, P12-FIX handoff target, read-only surface                   #
# --------------------------------------------------------------------------- #


def _client(world: Stack) -> TestClient:
    app = FastAPI()
    register_review_api(app, world.store, auth_dependency=make_token_dependency(TOKEN), repo_root=world.root, port=PORT)
    return TestClient(app, base_url=BASE)


def _finding(world: Stack, line: int) -> Annotation:
    revision = world.store.latest_revision(world.upper.id)
    assert revision is not None
    file_unit = next(
        unit for unit in world.store.list_units(revision.id) if unit.path == "x.py" and unit.kind == "file"
    )
    return world.store.add_annotation(
        Annotation(
            id="",
            review_id=world.upper.id,
            revision_id=revision.id,
            anchor=build_anchor(
                path="x.py",
                text=_x(lower=True, upper=True),
                start_line=line,
                end_line=line,
                unit_key=file_unit.unit_key,
            ),
            body=f"Finding at line {line}",
            title=f"Finding at line {line}",
            created_by="LemonCrow AI Review",
            created_by_actor="agent",
            source="ai_review",
            source_id=f"finding-{line}",
            finding_disposition="open",
        )
    )


def _attribute(world: Stack, session: ReviewSession, path: str, host: str, session_id: str) -> None:
    revision = world.store.latest_revision(session.id)
    assert revision is not None
    world.store.add_revision_attributions(
        revision.id,
        (
            ReviewAttribution(
                revision_id=revision.id,
                scope="file",
                scope_key=path,
                host=host,
                session_id=session_id,
                certainty="exact",
                method="recorded_edit",
            ),
        ),
    )


def test_stack_and_ownership_round_trip_through_the_review_api(tmp_path: Path) -> None:
    world = Stack(tmp_path)
    client = _client(world)
    lower_line = _finding(world, 12)
    own_line = _finding(world, 15)

    relationships = client.get(f"/api/reviews/{world.upper.id}/relationships", headers=HEADERS)
    assert relationships.status_code == 200, relationships.text
    stack = relationships.json()["stack"]
    assert stack["layer"] == 2 and stack["mutation"] == "read_only"
    assert stack["lower"][0]["change_ref"] == f"r/{world.lower.id}"
    assert stack["lower"][0]["readiness"]["state"] == "in_review"

    overview = client.get(f"/api/reviews/{world.upper.id}", headers=HEADERS)
    assert overview.status_code == 200, overview.text
    assert overview.json()["change"]["stack"]["layer"] == 2
    lower_overview = client.get(f"/api/reviews/{world.lower.id}", headers=HEADERS).json()
    assert lower_overview["change"]["stack"]["upper"][0]["change_ref"] == f"r/{world.upper.id}"
    # Readiness/inbox context stays cheap: the stack is opt-in.
    assert "stack" not in change_context_payload(world.store, world.upper)

    annotations = client.get(f"/api/reviews/{world.upper.id}/annotations", headers=HEADERS)
    assert annotations.status_code == 200, annotations.text
    by_id = {item["id"]: item for item in annotations.json()["annotations"]}
    assert by_id[lower_line.id]["owning_change"]["change_ref"] == f"r/{world.lower.id}"
    assert "owning_change" not in by_id[own_line.id]


def test_fix_on_lower_layer_code_hands_off_to_the_lower_changes_author(tmp_path: Path) -> None:
    world = Stack(tmp_path)
    client = _client(world)
    _attribute(world, world.lower, "x.py", "claude", "s-lower")
    _attribute(world, world.upper, "x.py", "codex", "s-upper")
    lower_line = _finding(world, 12)
    own_line = _finding(world, 15)
    revision = world.store.latest_revision(world.upper.id)
    assert revision is not None

    def preview(subject: Annotation) -> dict[str, Any]:
        response = client.post(
            f"/api/reviews/{world.upper.id}/actions/preview",
            headers=HEADERS,
            json={"kind": "fix", "subject_ids": [subject.id], "expected_revision_id": revision.id},
        )
        assert response.status_code == 200, response.text
        payload: dict[str, Any] = response.json()
        return payload

    routed = preview(lower_line)
    assert routed["route"]["tier"] == "exact_session"
    assert routed["route"]["target_ref"] == "claude:s-lower"
    assert f"owning Change r/{world.lower.id}" in routed["route"]["label"]
    assert f"Owning Change: r/{world.lower.id}" in routed["handoff"]

    here = preview(own_line)
    assert here["route"]["target_ref"] == "codex:s-upper"
    assert "Owning Change" not in here["handoff"]


def test_no_branch_rebase_or_stack_mutation_route_exists(tmp_path: Path) -> None:
    world = Stack(tmp_path)
    app = FastAPI()
    register_review_api(app, world.store, auth_dependency=make_token_dependency(TOKEN), repo_root=world.root, port=PORT)
    forbidden = ("stack", "rebase", "restack", "retarget", "branch", "push")
    writers = [
        route.path
        for route in app.routes
        if set(getattr(route, "methods", ()) or ()) & {"POST", "PUT", "PATCH", "DELETE"}
        and any(word in route.path.lower() for word in forbidden)
    ]
    assert writers == [], writers
    # The module exposes reads only.
    public = [name for name in sa.__all__ if callable(getattr(sa, name, None))]
    assert not [name for name in public if any(verb in name for verb in ("create", "rebase", "push", "set_"))]

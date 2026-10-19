"""Phase 9 item 34: base/rebase-only movement is labeled, never manufactured as authored change.

A pure rebase keeps every judgment (its authored patch is equivalent on the new
base), a conflict resolution that changed authored content reopens exactly the
changed targets, merge-base movement alone is not authored change, and any case
whose equivalence cannot be decided falls back to "changed".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pygit2
import pytest

from lemoncrow.pro.capabilities.review import rebase_classification as rc
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.revisions import reconcile
from lemoncrow.pro.capabilities.review.session_models import ReviewMark, ReviewRevision, ReviewSession, ReviewUnit
from lemoncrow.pro.capabilities.review.sources.local import (
    compute_frontier,
    mark_unit,
    open_or_create_session,
    read_packet_json,
    refresh,
)
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.targets import derive_review_targets


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


def _signature(offset: int) -> Any:
    return pygit2.Signature("Fixture Tester", "fixture@example.test", 1700000000 + offset, 0)


def _commit_files(repo: Any, ref: str, parent: str | None, files: dict[str, str], message: str, offset: int) -> str:
    """A commit of *parent*'s tree with *files* replaced, written to *ref*."""
    builder_index = pygit2.Index()
    if parent is not None:
        builder_index.read_tree(repo.get(parent).tree)
    for path, text in files.items():
        blob = repo.create_blob(text.encode("utf-8"))
        builder_index.add(pygit2.IndexEntry(path, blob, pygit2.enums.FileMode.BLOB))
    tree = builder_index.write_tree(repo)
    parents = [] if parent is None else [parent]
    sha = str(repo.create_commit(None, _signature(offset), _signature(offset), message, tree, parents))
    repo.references.create(ref, sha, force=True)
    return sha


def _app(beta_value: int, *, tail: str = "    return x\n", alpha: int = 1) -> str:
    filler = "".join(f"    y{index} = {index}\n" for index in range(12))
    return (
        f"def alpha():\n    return {alpha}\n\n\n"
        + "".join(f"CONSTANT_{index} = {index}\n" for index in range(10))
        + f"\n\ndef beta():\n    x = {beta_value}\n"
        + filler
        + tail
    )


def _other(marker: str = "", authored: str = "OTHER = 1") -> str:
    lines = [f"VALUE_{index} = {index}\n" for index in range(20)]
    lines[2] = authored + "\n"
    if marker:
        lines[17] = marker + "\n"
    return "".join(lines)


class World:

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "repo"
        self.root.mkdir()
        self.repo = pygit2.init_repository(str(self.root), initial_head="main")
        self.repo.config["user.name"] = "Fixture Tester"
        self.repo.config["user.email"] = "fixture@example.test"
        self.tmp = tmp_path
        self.store = ReviewStore(tmp_path / "store")
        self.base = _commit_files(
            self.repo,
            "refs/heads/main",
            None,
            {"app.py": _app(1), "other.py": _other(authored="VALUE_2 = 2")},
            "base",
            0,
        )
        self.checkout_main()
        self.feature = _commit_files(
            self.repo, "refs/heads/feature", self.base, {"app.py": _app(2), "other.py": _other()}, "feature", 1
        )
        self.session: ReviewSession | None = None

    def checkout_main(self) -> None:
        self.repo.checkout("refs/heads/main", strategy=pygit2.enums.CheckoutStrategy.FORCE)

    def open(self) -> Any:
        rng = resolve_rev_range(self.root, "main...feature")
        self.session = open_or_create_session(self.store, self.root, rng)
        return self.refresh()

    def refresh(self) -> Any:
        assert self.session is not None
        current = self.store.get_session(self.session.id) or self.session
        return refresh(self.store, current, self.root, store_root=self.tmp / "capture", limit=5000)

    def mark_all(self, result: Any, *, state: str = "reviewed") -> None:
        assert self.session is not None
        for unit in self.store.list_units(result.revision.id):
            mark_unit(self.store, self.session, result.revision, unit, state=state)

    def targets(self, revision: ReviewRevision) -> tuple[Any, ...]:
        assert self.session is not None
        units = self.store.list_units(revision.id)
        frontier = compute_frontier(
            self.session.id, "local", revision, units, self.store.list_marks(self.session.id, reviewer_id="local")
        )
        return derive_review_targets(units, frontier.entries, read_packet_json(self.store, revision))

    def advance_main(self, files: dict[str, str], offset: int) -> str:
        head = str(self.repo.references.get("refs/heads/main").target)
        sha = _commit_files(self.repo, "refs/heads/main", head, files, "upstream", offset)
        self.checkout_main()
        return sha

    def rebase_feature(self, files: dict[str, str], offset: int) -> str:
        main = str(self.repo.references.get("refs/heads/main").target)
        return _commit_files(self.repo, "refs/heads/feature", main, files, "feature (rebased)", offset)


def _beta_units(store: ReviewStore, revision: ReviewRevision) -> list[ReviewUnit]:
    return [unit for unit in store.list_units(revision.id) if unit.path == "app.py" and unit.symbol.endswith("beta")]


def test_pure_rebase_keeps_every_judgment_and_labels_base_only_movement(tmp_path: Path) -> None:
    world = World(tmp_path)
    first = world.open()
    world.mark_all(first)
    before_beta = _beta_units(world.store, first.revision)
    assert before_beta, "the fixture must produce a symbol unit for beta()"
    world.advance_main(
        {"app.py": _app(1, tail="    z = 0\n    return x\n"), "other.py": _other("UPSTREAM = 1", "VALUE_2 = 2")}, 2
    )
    world.rebase_feature({"app.py": _app(2, tail="    z = 0\n    return x\n"), "other.py": _other("UPSTREAM = 1")}, 3)
    second = world.refresh()
    assert second.created and second.revision.base_sha != first.revision.base_sha
    after_beta = _beta_units(world.store, second.revision)
    assert {u.content_fingerprint for u in after_beta} != {u.content_fingerprint for u in before_beta}
    assert second.reconciliation.reopened == ()
    assert {mark.unit_key for mark in second.reconciliation.rebased} >= {u.unit_key for u in after_beta}
    targets = world.targets(second.revision)
    assert targets and all(target.state == "reviewed" for target in targets)
    movement = rc.movement_between(world.store, first.revision, second.revision)
    assert movement is not None
    assert movement.kind == "rebase_only" and movement.deterministic
    assert "app.py" in movement.base_only_paths and "other.py" in movement.base_only_paths
    assert movement.authored_paths == ()
    labeled = rc.label_target_payloads(
        [{"unit_key": t.unit_key} for t in targets], rc.ReviewerMovement(movement, "your_review", first.revision)
    )
    assert any(item.get("movement") == "base_only" for item in labeled)
    assert all(item.get("movement") in {"base_only", "unchanged"} for item in labeled)
    events = world.store.list_mark_events(world.session.id, reviewer_id="local")
    assert any("base-only movement" in (event.reason or "") for event in events)


def test_rebase_with_conflict_resolution_reopens_exactly_the_changed_targets(tmp_path: Path) -> None:
    world = World(tmp_path)
    first = world.open()
    world.mark_all(first)
    other_before = {t.unit_key for t in world.targets(first.revision) if t.path == "other.py"}
    world.advance_main({"app.py": _app(5), "other.py": _other("UPSTREAM = 1", "VALUE_2 = 2")}, 2)
    world.rebase_feature({"app.py": _app(7), "other.py": _other("UPSTREAM = 1")}, 3)
    second = world.refresh()
    targets = world.targets(second.revision)
    reopened = {t.unit_key for t in targets if t.state != "reviewed"}
    assert reopened, "the authored conflict resolution must reopen its target"
    assert all(t.path == "app.py" for t in targets if t.unit_key in reopened)
    assert {t.unit_key for t in targets if t.path == "other.py" and t.state == "reviewed"} == other_before
    movement = rc.movement_between(world.store, first.revision, second.revision)
    assert movement is not None and movement.kind == "rebase_with_authored_changes"
    assert movement.authored_paths == ("app.py",) and movement.base_only_paths == ("other.py",)


def test_merge_base_movement_alone_is_not_authored_change(tmp_path: Path) -> None:
    world = World(tmp_path)
    first = world.open()
    world.mark_all(first)
    world.advance_main({"unrelated.py": "U = 1\n"}, 2)
    again = world.refresh()
    assert again.revision.id == first.revision.id and again.reconciliation.reopened == ()
    main = str(world.repo.references.get("refs/heads/main").target)
    index = world.repo.merge_commits(world.feature, main)
    tree = index.write_tree(world.repo)
    merged = str(
        world.repo.create_commit(None, _signature(3), _signature(3), "merge main", tree, [world.feature, main])
    )
    world.repo.references.create("refs/heads/feature", merged, force=True)
    third = world.refresh()
    assert third.created and third.reconciliation.reopened == ()
    movement = rc.movement_between(world.store, first.revision, third.revision)
    assert movement is not None and movement.base_moved
    assert movement.kind in {"none", "base_only", "rebase_only"} and movement.authored_paths == ()


def _revision(rid: str, *, base: str, head: str, packet_sha: str, mode: str = "commit_range") -> ReviewRevision:
    return ReviewRevision(
        id=rid,
        review_id="r",
        revision_number=1 if rid == "r1" else 2,
        range_mode=mode,
        tree_fingerprint=rid,
        packet_schema_version=1,
        base_sha=base,
        head_sha=head,
        packet_sha256=packet_sha,
    )


def _unit(fingerprint: str, *, key: str = "sym:beta", method: str = "symbol_sha256") -> ReviewUnit:
    return ReviewUnit(
        revision_id="",
        unit_key=key,
        kind="symbol",
        path="app.py",
        content_fingerprint=fingerprint,
        fingerprint_method=method,
        symbol="beta",
    )


def _packet(patch: str, *, added: int = 1, removed: int = 1, header: str = "@@ -1,3 +1,3 @@") -> dict[str, Any]:
    return {
        "files": [
            {
                "path": "app.py",
                "old_path": None,
                "status": "modified",
                "hunks": [{"header": header, "patch": patch, "added": added, "removed": removed}],
            }
        ]
    }


PATCH = " def beta():\n-    x = 1\n+    x = 2\n     y0 = 0\n"


def _classify(before: dict[str, Any], after: dict[str, Any], **revision_kwargs: Any) -> rc.MovementClassification:
    return rc.classify_movement(
        _revision("r1", base="b1", head="h1", packet_sha=repr(before)),
        [_unit("fp-old")],
        before,
        _revision("r2", base="b2", head="h2", packet_sha=repr(after), **revision_kwargs),
        [_unit("fp-new")],
        after,
    )


def test_rebase_patch_identity_contract() -> None:
    """case: test_equivalent_patch_on_a_moved_base_is_base_only"""
    moved_header = _packet(PATCH, header="@@ -40,3 +40,3 @@")
    result = _classify(_packet(PATCH), moved_header)
    assert result.kind == "rebase_only" and result.base_only_unit_keys == {"sym:beta"} and result.deterministic
    "case: test_uncaptured_hunk_body_falls_back_to_changed"
    result = _classify(_packet(PATCH), _packet(""))
    assert result.base_only_unit_keys == frozenset() and result.authored_unit_keys == {"sym:beta"}
    assert not result.deterministic and result.undecided_paths == ("app.py",)
    "case: test_range_mode_change_falls_back_to_changed"
    result = _classify(_packet(PATCH), _packet(PATCH), mode="working_tree")
    assert result.base_only_unit_keys == frozenset() and (not result.deterministic)
    "case: test_context_line_change_is_authored_not_base_only"
    edited_context = PATCH.replace("     y0 = 0", "     y0 = 99")
    result = _classify(_packet(PATCH), _packet(edited_context))
    assert result.base_only_unit_keys == frozenset() and result.deterministic
    assert result.kind == "rebase_with_authored_changes"
    "case: test_unknown_fingerprint_never_carries_as_base_only"
    result = rc.classify_movement(
        _revision("r1", base="b1", head="h1", packet_sha="p1"),
        [_unit("fp", method="unknown")],
        _packet(PATCH),
        _revision("r2", base="b2", head="h2", packet_sha="p2"),
        [_unit("fp2", method="unknown")],
        _packet(PATCH),
    )
    assert result.base_only_unit_keys == frozenset()


def test_rebase_reconcile_contract() -> None:
    """case: test_reconcile_carries_only_marks_current_on_the_baseline"""
    previous = [_unit("fp-old")]
    nxt = [_unit("fp-new")]
    current = ReviewMark(
        review_id="r", unit_key="sym:beta", state="reviewed", reviewed_revision_id="r1", content_fingerprint="fp-old"
    )
    stale = ReviewMark(
        review_id="r",
        unit_key="sym:beta",
        state="reviewed",
        reviewed_revision_id="r0",
        content_fingerprint="fp-ancient",
    )
    carried = reconcile(previous, nxt, [current], base_only=frozenset({"sym:beta"}))
    assert carried.reopened == () and [m.content_fingerprint for m in carried.rebased] == ["fp-new"]
    assert carried.rebased[0].state == "reviewed" and carried.rebased[0].reviewed_revision_id == "r1"
    refused = reconcile(previous, nxt, [stale], base_only=frozenset({"sym:beta"}))
    assert refused.rebased == () and [m.state for m in refused.reopened] == ["changed_since_review"]
    plain = reconcile(previous, nxt, [current])
    assert plain.rebased == () and [m.state for m in plain.reopened] == ["changed_since_review"]


def test_rebase_bounds_contract() -> None:
    """case: test_file_patch_bound_at_and_past_the_limit"""
    row = _packet(PATCH)["files"][0]
    size = len(PATCH.encode("utf-8"))
    assert rc.file_patch_id(row, max_bytes=size) is not None
    assert rc.file_patch_id(row, max_bytes=size - 1) is None
    "case: test_file_count_bound_at_and_past_the_limit"
    packet = {"files": [{**_packet(PATCH)["files"][0], "path": f"f{index}.py"} for index in range(3)]}
    at = rc.compute_patch_ids(packet, max_files=3)
    assert at.complete and all(value is not None for value in at.by_path.values())
    past = rc.compute_patch_ids(packet, max_files=2)
    assert not past.complete and all(value is None for value in past.by_path.values())
    "case: test_total_bytes_bound_classifies_the_remainder_as_changed"
    packet = {"files": [{**_packet(PATCH)["files"][0], "path": f"f{index}.py"} for index in range(3)]}
    size = len(PATCH)
    at = rc.compute_patch_ids(packet, max_total_bytes=3 * size)
    assert at.complete and all(value is not None for value in at.by_path.values())
    past = rc.compute_patch_ids(packet, max_total_bytes=3 * size - 1)
    assert not past.complete and past.by_path["f2.py"] is None and (past.by_path["f0.py"] is not None)
    "case: test_patch_id_ignores_line_numbers_and_whitespace_but_not_content"
    row = _packet(PATCH)["files"][0]
    moved = _packet(PATCH, header="@@ -90,3 +91,3 @@")["files"][0]
    spaced = _packet(PATCH.replace("x = 2", "x  =  2"))["files"][0]
    other = _packet(PATCH.replace("x = 2", "x = 3"))["files"][0]
    assert rc.file_patch_id(row) == rc.file_patch_id(moved) == rc.file_patch_id(spaced)
    assert rc.file_patch_id(row) != rc.file_patch_id(other)
    assert rc.file_patch_id({**row, "is_binary": True}) is None

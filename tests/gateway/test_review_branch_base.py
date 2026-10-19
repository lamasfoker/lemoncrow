"""A branch Review diffs the branch's own work, never upstream work it absorbed.

The Review base is frozen where the Review started, but ``git pull``, merging
main in and rebasing onto main all move upstream commits under ``HEAD``. Those
are someone else's change: they must not become Commit checkpoints or show up
in the Draft. Routing has the same shape of invariant: a Review that a branch
once owned never leaks into another branch through the shared reopen key.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pygit2
import pytest

from lemoncrow.pro.capabilities.review.gitdiff import collect_diff, resolve_new_review_range, resolve_rev_range
from lemoncrow.pro.capabilities.review.session_models import ReviewBinding, ReviewSession
from lemoncrow.pro.capabilities.review.sources import local as local_source
from lemoncrow.pro.capabilities.review.sources.local import (
    open_or_create_session,
    range_for_session,
    refresh,
    sync_commit_checkpoints,
)
from lemoncrow.pro.capabilities.review.store import ReviewStore


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


def _init(root: Path) -> Any:
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.test"
    return repo


def _write(root: Path, rel: str, text: str) -> None:
    (root / rel).write_text(text, encoding="utf-8")


def _commit(repo: Any, message: str, offset: int, paths: tuple[str, ...] = ()) -> str:
    """Commit *paths* (default: everything) onto HEAD without touching the workdir."""

    repo.index.read()
    if paths:
        for path in paths:
            repo.index.add(path)
    else:
        repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    signature = pygit2.Signature("Fixture Tester", "fixture@example.test", 1700000000 + offset, 0)
    return str(repo.create_commit("HEAD", signature, signature, message, tree, parents))


def _files(store: ReviewStore, revision_id: str) -> set[str]:
    return {unit.path for unit in store.list_units(revision_id) if unit.kind == "file"}


def _refresh(store: ReviewStore, session: ReviewSession, root: Path, tmp_path: Path) -> Any:
    current = store.get_session(session.id) or session
    return refresh(store, current, root, store_root=tmp_path / "capture-store", limit=5000)


def _feature_repo(tmp_path: Path) -> tuple[Path, Any, str]:
    root = tmp_path / "repo"
    repo = _init(root)
    _write(root, "base.py", "BASE = 0\n")
    base = _commit(repo, "baseline", 0)
    repo.branches.local.create("feature", repo.get(base))
    repo.checkout("refs/heads/feature")
    return root, repo, base


def test_new_review_includes_existing_commits_and_stays_fixed_after_push(tmp_path: Path) -> None:
    root, repo, base = _feature_repo(tmp_path)
    repo.references.create("refs/remotes/origin/feature", base)
    _write(root, "feature.py", "FEATURE = 1\n")
    committed = _commit(repo, "already committed work", 1)
    _write(root, "pending.py", "PENDING = 1\n")
    rng = resolve_new_review_range(root)
    assert rng.mode == "working_tree"
    assert rng.base_sha == base
    assert {item.path for item in collect_diff(root, rng).files} == {"feature.py", "pending.py"}
    store = ReviewStore(tmp_path / "store")
    session = open_or_create_session(store, root, rng, force_new=True)
    assert session.review_base_policy == "fixed"
    assert ReviewStore(tmp_path / "store").get_session(session.id).review_base_policy == "fixed"
    _refresh(store, session, root, tmp_path)
    repo.references.create("refs/remotes/origin/feature", committed, force=True)
    repo.references.create("refs/remotes/origin/main", committed, force=True)
    result = _refresh(store, session, root, tmp_path)
    assert result.revision.base_sha == base
    assert _files(store, result.revision.id) == {"feature.py", "pending.py"}
    assert committed in {item.commit_sha for item in store.list_commits(session.id)}


def test_new_review_without_remote_branch_uses_default_branch_fork_point(tmp_path: Path) -> None:
    root, repo, base = _feature_repo(tmp_path)
    _write(root, "feature.py", "FEATURE = 1\n")
    _commit(repo, "feature", 1)
    repo.checkout("refs/heads/main")
    _write(root, "upstream.py", "UPSTREAM = 1\n")
    upstream = _commit(repo, "upstream", 2)
    repo.references.create("refs/remotes/origin/main", upstream)
    repo.references.create("refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    repo.checkout("refs/heads/feature", strategy=pygit2.GIT_CHECKOUT_FORCE)
    assert resolve_new_review_range(root).base_sha == base


def test_new_review_explicit_base_and_local_fallback(tmp_path: Path) -> None:
    root, repo, base = _feature_repo(tmp_path)
    _write(root, "feature.py", "FEATURE = 1\n")
    head = _commit(repo, "feature", 1)
    assert resolve_new_review_range(root).base_sha == head
    explicit = resolve_new_review_range(root, base=base, staged=True)
    assert explicit.mode == "staged"
    assert explicit.base_sha == base
    assert {item.path for item in collect_diff(root, explicit).files} == {"feature.py"}
    with pytest.raises(ValueError):
        resolve_new_review_range(root, base="missing-commit")


def test_new_review_unborn_repository_starts_at_empty_tree(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.gitdiff import EMPTY_TREE_SHA

    root = tmp_path / "repo"
    _init(root)
    _write(root, "new.py", "NEW = 1\n")
    rng = resolve_new_review_range(root)
    assert rng.base_sha == EMPTY_TREE_SHA
    assert {item.path for item in collect_diff(root, rng).files} == {"new.py"}


def test_pull_keeps_teammate_commits_out_of_the_review(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    repo = _init(root)
    _write(root, "mine.py", "MINE = 0\n")
    base = _commit(repo, "baseline", 0)
    repo.references.create("refs/remotes/origin/main", base)
    store = ReviewStore(tmp_path / "store")
    _write(root, "mine.py", "MINE = 1\n")
    session = open_or_create_session(store, root, resolve_rev_range(root, working_tree=True))
    _refresh(store, session, root, tmp_path)

    # `git pull` fast-forwards teammates' commits under the dirty edit.
    for index in range(3):
        _write(root, f"teammate_{index}.py", f"MATE = {index}\n")
        pulled = _commit(repo, f"teammate {index}", 10 + index, (f"teammate_{index}.py",))
    repo.references.create("refs/remotes/origin/main", pulled, force=True)

    result = _refresh(store, session, root, tmp_path)

    assert _files(store, result.revision.id) == {"mine.py"}
    assert store.list_commits(session.id) == ()


def test_rebase_onto_updated_main_checkpoints_only_branch_work(tmp_path: Path) -> None:
    root, repo, _base = _feature_repo(tmp_path)
    store = ReviewStore(tmp_path / "store")
    _write(root, "feature.py", "FEATURE = 1\n")
    session = open_or_create_session(store, root, resolve_rev_range(root, working_tree=True))
    _refresh(store, session, root, tmp_path)
    original = _commit(repo, "feature", 1)
    _refresh(store, session, root, tmp_path)

    repo.checkout("refs/heads/main")
    _write(root, "upstream.py", "UPSTREAM = 1\n")
    upstream = _commit(repo, "upstream", 2)
    repo.references.create("refs/remotes/origin/main", upstream)  # fetched
    repo.branches.local.create("feature", repo.get(upstream), force=True)
    repo.checkout("refs/heads/feature", strategy=pygit2.GIT_CHECKOUT_FORCE)
    _write(root, "feature.py", "FEATURE = 1\n")
    rebased = _commit(repo, "feature (rebased)", 3)
    _write(root, "feature.py", "FEATURE = 2\n")

    result = _refresh(store, session, root, tmp_path)

    commits = {item.commit_sha: item for item in store.list_commits(session.id)}
    assert set(commits) == {original, rebased}
    assert commits[original].state == "superseded"
    assert commits[rebased].state == "active"
    assert _files(store, commits[rebased].revision_id) == {"feature.py"}
    assert _files(store, result.revision.id) == {"feature.py"}


def test_merging_main_in_keeps_upstream_out_of_the_review(tmp_path: Path) -> None:
    root, repo, _base = _feature_repo(tmp_path)
    store = ReviewStore(tmp_path / "store")
    _write(root, "feature.py", "FEATURE = 1\n")
    session = open_or_create_session(store, root, resolve_rev_range(root, working_tree=True))
    _refresh(store, session, root, tmp_path)
    _commit(repo, "feature", 1)

    repo.checkout("refs/heads/main")
    _write(root, "upstream.py", "UPSTREAM = 1\n")
    upstream = _commit(repo, "upstream", 2)
    repo.references.create("refs/remotes/origin/main", upstream)  # fetched
    repo.checkout("refs/heads/feature")
    feature_head = repo.head.peel(pygit2.Commit)
    repo.merge(pygit2.Oid(hex=upstream))
    signature = pygit2.Signature("Fixture Tester", "fixture@example.test", 1700000003, 0)
    merge = str(
        repo.create_commit(
            "HEAD",
            signature,
            signature,
            "merge main",
            repo.index.write_tree(),
            [feature_head.id, pygit2.Oid(hex=upstream)],
        )
    )
    repo.state_cleanup()

    result = _refresh(store, session, root, tmp_path)

    commits = {item.commit_sha: item for item in store.list_commits(session.id)}
    assert upstream not in commits
    assert _files(store, commits[merge].revision_id) == {"feature.py"}
    assert _files(store, result.revision.id) == {"feature.py"}


def test_review_released_by_new_is_not_adopted_by_another_branch(tmp_path: Path) -> None:
    root, repo, base = _feature_repo(tmp_path)
    repo.branches.local.create("other", repo.get(base))
    store = ReviewStore(tmp_path / "store")
    _write(root, "base.py", "BASE = 1\n")
    rng = resolve_rev_range(root, working_tree=True)
    released = open_or_create_session(store, root, rng)
    replacement = open_or_create_session(store, root, rng, force_new=True)
    store.update_session(released.id, title="touched after --new")

    repo.checkout("refs/heads/other", strategy=pygit2.GIT_CHECKOUT_FORCE)
    _write(root, "base.py", "BASE = 2\n")
    other = open_or_create_session(store, root, resolve_rev_range(root, working_tree=True))

    assert other.id not in {released.id, replacement.id}


def test_staged_drafts_share_the_branch_base_with_commit_checkpoints(tmp_path: Path) -> None:
    root, repo, base = _feature_repo(tmp_path)
    store = ReviewStore(tmp_path / "store")
    _write(root, "a.py", "A = 1\n")
    repo.index.add("a.py")
    repo.index.write()
    session = open_or_create_session(store, root, resolve_rev_range(root, staged=True))
    _refresh(store, session, root, tmp_path)
    _commit(repo, "a", 1, ("a.py",))
    _write(root, "b.py", "B = 1\n")
    repo.index.add("b.py")
    repo.index.write()

    current = store.get_session(session.id) or session
    assert range_for_session(root, current).base_sha == base
    result = _refresh(store, session, root, tmp_path)
    assert _files(store, result.revision.id) == {"a.py", "b.py"}


def test_commits_on_a_detached_head_stay_in_one_review(tmp_path: Path) -> None:
    root, repo, _base = _feature_repo(tmp_path)
    repo.set_head(repo.head.target)
    store = ReviewStore(tmp_path / "store")
    _write(root, "base.py", "BASE = 1\n")
    first = open_or_create_session(store, root, resolve_rev_range(root, working_tree=True))
    _commit(repo, "detached work", 1)
    _write(root, "base.py", "BASE = 2\n")

    again = open_or_create_session(store, root, resolve_rev_range(root, working_tree=True))

    assert again.id == first.id
    assert first.review_base_sha == ""


def test_paused_rebase_routes_to_the_branch_being_rebased(tmp_path: Path) -> None:
    root, repo, _base = _feature_repo(tmp_path)
    store = ReviewStore(tmp_path / "store")
    _write(root, "base.py", "BASE = 1\n")
    review = open_or_create_session(store, root, resolve_rev_range(root, working_tree=True))
    _commit(repo, "feature", 1)

    repo.set_head(repo.head.target)
    state = Path(repo.path) / "rebase-merge"
    state.mkdir()
    (state / "head-name").write_text("refs/heads/feature\n", encoding="utf-8")
    _write(root, "base.py", "BASE = 2\n")

    during = open_or_create_session(store, root, resolve_rev_range(root, working_tree=True))

    assert during.id == review.id


def test_commit_range_reviews_capture_revisions_not_checkpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, repo, _base = _feature_repo(tmp_path)
    _write(root, "feature.py", "FEATURE = 1\n")
    _commit(repo, "feature", 1)
    notes: list[str] = []
    monkeypatch.setattr(local_source, "_write_review_commit_note", lambda *args: notes.append(args[-1].commit_sha))
    store = ReviewStore(tmp_path / "store")
    rng = resolve_rev_range(root, "HEAD~1..HEAD")
    session = open_or_create_session(store, root, rng)

    result = refresh(store, session, root, store_root=tmp_path / "capture-store", rng=rng, limit=5000)

    assert result.revision.capture_kind == "revision"
    assert store.list_commits(session.id) == ()
    assert notes == []


def test_git_note_is_written_once_per_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, repo, _base = _feature_repo(tmp_path)
    notes: list[str] = []
    monkeypatch.setattr(local_source, "_write_review_commit_note", lambda *args: notes.append(args[-1].commit_sha))
    store = ReviewStore(tmp_path / "store")
    _write(root, "feature.py", "FEATURE = 1\n")
    session = open_or_create_session(store, root, resolve_rev_range(root, working_tree=True))
    _refresh(store, session, root, tmp_path)
    committed = _commit(repo, "feature", 1)

    _refresh(store, session, root, tmp_path)
    _refresh(store, session, root, tmp_path)

    assert notes == [committed]


def test_checkpoint_backfill_is_skipped_past_its_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, repo, _base = _feature_repo(tmp_path)
    store = ReviewStore(tmp_path / "store")
    _write(root, "feature.py", "FEATURE = 0\n")
    session = open_or_create_session(store, root, resolve_rev_range(root, working_tree=True))
    for index in range(3):
        _write(root, "feature.py", f"FEATURE = {index + 1}\n")
        _commit(repo, f"feature {index}", index + 1)
    monkeypatch.setattr(local_source, "_MAX_CHECKPOINT_BACKFILL", 2)

    assert sync_commit_checkpoints(store, session, root, store_root=tmp_path / "capture-store") == ()
    assert store.list_commits(session.id) == ()


def test_racing_creates_converge_on_one_active_review(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "store")
    binding = ReviewBinding(
        id="",
        repository_identity="repo",
        subject_kind="branch",
        subject_key="refs/heads/feature",
        review_id="",
    )

    def template() -> ReviewSession:
        return ReviewSession(
            id="", subject_type="local_change", repo_root="/repo", range_mode="working_tree", review_base_sha="a" * 40
        )

    first, first_created = store.create_bound_session(template(), binding)
    second, second_created = store.create_bound_session(template(), binding)

    assert (first_created, second_created) == (True, False)
    assert second.id == first.id
    rival = store.create_session(template())
    with sqlite3.connect(store.db_path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO review_bindings (id, repository_identity, subject_kind, subject_key, review_id,"
            " worktree_key, active, created_at, updated_at) VALUES ('rbd-x', 'repo', 'branch',"
            " 'refs/heads/feature', ?, '', 1, 'now', 'now')",
            (rival.id,),
        )

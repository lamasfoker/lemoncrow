"""Fix lines: exact Snapshot worktrees and safe apply-back semantics."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review import fix_lines
from lemoncrow.pro.capabilities.review.fix_lines import (
    FixLineError,
    apply_fix_line,
    close_fix_line,
    decide,
    list_fix_lines,
    plan_fix_apply,
    recreate_fix_worktree,
    start_fix_line,
)
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.sources.local import (
    open_or_create_session,
    range_for_session,
    record_revision,
)
from lemoncrow.pro.capabilities.review.store import ReviewStore

_APP = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
_BASE = b"one\ntwo\nthree\nfour\nfive\nsix\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "app.py").write_text(_APP, encoding="utf-8")
    (root / "old.txt").write_text("old\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


@pytest.fixture
def store(tmp_path: Path) -> ReviewStore:
    return ReviewStore(tmp_path / "store")


def _capture(
    store: ReviewStore,
    repo: Path,
    **range_args: object,
) -> tuple[ReviewSession, ReviewRevision]:
    rng = resolve_rev_range(repo, **range_args) if range_args else resolve_rev_range(repo, working_tree=True)
    session = open_or_create_session(store, repo, rng, title="Checkout")
    recording = record_revision(store, session, repo, rng, store_root=store.root)
    refreshed = store.get_session(session.id)
    assert refreshed is not None
    return refreshed, recording.revision


def _tree(root: Path) -> dict[str, bytes]:
    rows: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_file() and not path.is_symlink() and rel.parts[0] not in {".git", ".lemoncrow"}:
            rows[rel.as_posix()] = path.read_bytes()
    return rows


def _start(
    store: ReviewStore,
    repo: Path,
    parent: ReviewSession,
    snap: ReviewRevision,
    number: int = 3,
):
    return start_fix_line(
        store,
        parent,
        snap,
        repo_root=repo,
        repository_identity="local:test",
        review_short="k7q2",
        snapshot_number=number,
    )


def _branch_exists(repo: Path, branch: str) -> bool:
    return (
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            ],
            check=False,
        ).returncode
        == 0
    )


def _fixed(
    store: ReviewStore,
    repo: Path,
) -> tuple[ReviewSession, fix_lines.FixLine]:
    (repo / "app.py").write_text(_APP.replace("return 1", "return 10"), encoding="utf-8")
    parent, snap = _capture(store, repo)
    line, _ = _start(store, repo, parent, snap)
    path = Path(line.worktree_path)
    (path / "app.py").write_text(
        _APP.replace("return 1", "return 10").replace("return 2", "return 20"),
        encoding="utf-8",
    )
    (path / "added.txt").write_text("added\n", encoding="utf-8")
    child = store.get_session(line.child_review_id)
    assert child is not None
    record_revision(
        store,
        child,
        path,
        range_for_session(path, child),
        store_root=store.root,
    )
    return parent, line


def test_fix_worktree_holds_uncommitted_snapshot_byte_for_byte(repo: Path, store: ReviewStore) -> None:
    (repo / "app.py").write_text(_APP.replace("return 1", "return 10"), encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    (repo / "old.txt").unlink()
    parent, snap = _capture(store, repo)
    expected = _tree(repo)

    line, created = _start(store, repo, parent, snap)

    path = Path(line.worktree_path)
    assert created
    assert _tree(path) == expected
    assert _git(path, "status", "--porcelain") == ""
    assert (
        _git(
            repo,
            "config",
            "--get",
            f"branch.{line.branch}.lemoncrowReview",
        ).strip()
        == line.child_review_id
    )
    assert fix_lines.fix_child_review_ids(store) == {line.child_review_id}


@pytest.mark.parametrize("kind", ["symlink", "executable"])
def test_direct_capture_fails_closed_on_added_symlink_or_executable(
    repo: Path,
    store: ReviewStore,
    kind: str,
) -> None:
    target = repo / "special"
    if kind == "symlink":
        target.symlink_to("app.py")
    else:
        target.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        target.chmod(0o755)
    parent, snap = _capture(store, repo)

    with pytest.raises(FixLineError, match="symlink or file mode"):
        _start(store, repo, parent, snap)


@pytest.mark.parametrize("new_mode", [0o100755, 0o120000])
def test_exact_draft_reconstruction_fails_closed_on_mode_or_symlink_change(
    repo: Path,
    store: ReviewStore,
    monkeypatch: pytest.MonkeyPatch,
    new_mode: int,
) -> None:
    (repo / "app.py").write_text(_APP.replace("return 1", "return 10"), encoding="utf-8")
    parent, snap = _capture(store, repo)

    def source_tree(_review_id: str, _revision_id: str, *, side: str = "new"):
        mode = new_mode if side == "new" else 0o100644
        return {
            "app.py": {"content_digest": "deadbeef", "mode": mode, "size": 1},
            "old.txt": {"content_digest": "feedface", "mode": 0o100644, "size": 1},
        }

    monkeypatch.setattr(store, "read_source_tree_artifact", source_tree)

    with pytest.raises(FixLineError, match="symlink or file mode"):
        _start(store, repo, parent, snap)


def test_existing_fix_branch_is_never_overwritten(repo: Path, store: ReviewStore) -> None:
    (repo / "app.py").write_text(_APP.replace("return 1", "return 10"), encoding="utf-8")
    _git(repo, "branch", "lc-fix/k7q2-s3")
    before = _git(repo, "rev-parse", "lc-fix/k7q2-s3").strip()
    parent, snap = _capture(store, repo)

    line, _ = _start(store, repo, parent, snap)

    assert line.branch == "lc-fix/k7q2-s3-2"
    assert _git(repo, "rev-parse", "lc-fix/k7q2-s3").strip() == before


def test_preflight_table() -> None:
    for base, ours, theirs, outcome in [
        (_BASE, _BASE, _BASE.replace(b"two", b"TWO"), "write"),
        (_BASE, _BASE.replace(b"six", b"SIX"), _BASE.replace(b"one", b"ONE"), "write"),
        (_BASE, _BASE.replace(b"two", b"deux"), _BASE.replace(b"two", b"TWO"), "conflict"),
        (None, None, b"new\n", "write"),
        (None, b"new\n", b"new\n", "unchanged"),
        (None, b"mine\n", b"new\n", "conflict"),
        (_BASE, _BASE, None, "delete"),
        (_BASE, None, None, "unchanged"),
        (_BASE, _BASE + b"seven\n", None, "conflict"),
        (_BASE, None, _BASE + b"seven\n", "conflict"),
        (b"\x00bin", b"\x00bin", b"\x00BIN", "write"),
        (b"\x00bin", b"\x00mine", b"\x00BIN", "conflict"),
    ]:
        assert decide("f.txt", base, ours, theirs).outcome == outcome


def test_unrelated_main_edits_in_same_file_survive() -> None:
    ours = _BASE.replace(b"six", b"six # main")
    theirs = _BASE.replace(b"one", b"one # fix")

    result = decide("f.txt", _BASE, ours, theirs)

    assert result.outcome == "write"
    assert result.content == _BASE.replace(b"six", b"six # main").replace(b"one", b"one # fix")


def test_file_without_trailing_newline_merges_exactly() -> None:
    result = decide(
        "f.txt",
        b"a\nb\nc\nd\ne",
        b"a\nb\nc\nd\nE",
        b"A\nb\nc\nd\ne",
    )
    assert result.outcome == "write"
    assert result.content == b"A\nb\nc\nd\nE"


def test_stale_fix_worktree_is_refused(repo: Path, store: ReviewStore) -> None:
    parent, line = _fixed(store, repo)
    (Path(line.worktree_path) / "late.txt").write_text("not captured\n", encoding="utf-8")

    with pytest.raises(FixLineError, match="Capture the fix first"):
        plan_fix_apply(store, parent, line, repo_root=repo)


def test_clean_apply_is_uncommitted_and_closes_line(repo: Path, store: ReviewStore) -> None:
    parent, line = _fixed(store, repo)
    (repo / "notes.md").write_text("unrelated work\n", encoding="utf-8")

    result = apply_fix_line(store, parent, line, repo_root=repo)

    assert result.applied
    assert result.line.status == "applied"
    assert "return 20" in (repo / "app.py").read_text(encoding="utf-8")
    assert (repo / "added.txt").read_text(encoding="utf-8") == "added\n"
    assert (repo / "notes.md").read_text(encoding="utf-8") == "unrelated work\n"
    assert not Path(line.worktree_path).exists()
    assert not _branch_exists(repo, line.branch)
    assert _git(repo, "log", "-1", "--format=%s").strip() == "base"


def test_recreate_restores_latest_captured_fix_snapshot(repo: Path, store: ReviewStore) -> None:
    _parent, line = _fixed(store, repo)
    expected = _tree(Path(line.worktree_path))
    _git(repo, "worktree", "remove", "--force", line.worktree_path)

    recreated = recreate_fix_worktree(store, line, repo_root=repo)

    assert not recreated.worktree_missing
    assert _tree(Path(recreated.worktree_path)) == expected


def test_discard_requires_confirmation_for_uncaptured_or_captured_work(repo: Path, store: ReviewStore) -> None:
    _parent, line = _fixed(store, repo)

    with pytest.raises(fix_lines.FixLineDirty):
        close_fix_line(store, line, repo_root=repo, outcome="abandoned")

    closed = close_fix_line(
        store,
        line,
        repo_root=repo,
        outcome="abandoned",
        force=True,
    )
    assert closed.status == "abandoned"
    assert not Path(line.worktree_path).exists()


def test_state_failure_restores_main_files(repo: Path, store: ReviewStore, monkeypatch: pytest.MonkeyPatch) -> None:
    parent, line = _fixed(store, repo)
    before = _tree(repo)
    real_update = store.update_relationship_if_unchanged

    def fail_state(*args, **kwargs):
        return None

    monkeypatch.setattr(store, "update_relationship_if_unchanged", fail_state)

    with pytest.raises(FixLineError, match="changed meanwhile"):
        apply_fix_line(store, parent, line, repo_root=repo)

    assert _tree(repo) == before
    assert not (repo / ".lemoncrow" / "fixes" / f"{line.name}.apply.json").exists()
    monkeypatch.setattr(store, "update_relationship_if_unchanged", real_update)


def test_apply_revalidates_main_before_writing(repo: Path, store: ReviewStore, monkeypatch: pytest.MonkeyPatch) -> None:
    parent, line = _fixed(store, repo)
    real_plan = fix_lines.plan_fix_apply

    def moving_plan(*args, **kwargs):
        plan = real_plan(*args, **kwargs)
        (repo / "app.py").write_text("changed after plan\n", encoding="utf-8")
        return plan

    monkeypatch.setattr(fix_lines, "plan_fix_apply", moving_plan)

    with pytest.raises(FixLineError, match="changed while the fix was being prepared"):
        apply_fix_line(store, parent, line, repo_root=repo)

    assert (repo / "app.py").read_text(encoding="utf-8") == "changed after plan\n"
    [again] = list_fix_lines(store, parent.id)
    assert again.status == "fixing"

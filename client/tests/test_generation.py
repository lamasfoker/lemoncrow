from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from lemoncrow_client.errors import ClientError, ErrorCode
from lemoncrow_client.generation import plan_repository_generation
from lemoncrow_client.manifest import manifest_root, walk_worktree

CAP = 1024**2


def _git_bytes(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_bytes,
        capture_output=True,
        check=True,
    )
    return completed.stdout


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> str:
    return _git_bytes(repo, *args, input_bytes=input_bytes).decode("utf-8", errors="replace").strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "LemonCrow Tests")
    (repo / "src").mkdir()
    (repo / "src" / "keep.py").write_text("def keep():\n    return 1\n", encoding="utf-8")
    (repo / "src" / "change.py").write_text("def change():\n    return 'base'\n", encoding="utf-8")
    (repo / "src" / "delete.py").write_text("def delete_me():\n    return 3\n", encoding="utf-8")
    (repo / ".lemoncrow").mkdir()
    (repo / ".lemoncrow" / "tracked.txt").write_text("internal\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def _walk(repo: Path):  # type: ignore[no-untyped-def]
    return walk_worktree(repo, size_cap=CAP, max_files=2_000_000)


def test_the_committed_review_config_is_part_of_the_generation(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".lemoncrow" / "review.yaml").write_text("version: 1\nsurfaces: []\n", encoding="utf-8")
    _git(repo, "add", "-f", ".lemoncrow/review.yaml")
    _git(repo, "commit", "-qm", "review config")

    plan = plan_repository_generation(repo, _walk(repo).entries(), content_size_cap=CAP)
    base = {entry.path for entry in plan.base_entries}

    assert ".lemoncrow/review.yaml" in base
    assert ".lemoncrow/tracked.txt" not in base
    # The walked worktree agrees with the committed tree, so nothing is overlaid.
    assert ".lemoncrow/review.yaml" not in {entry.path for entry in plan.overlay_entries}


def test_generation_is_head_and_worktree_is_exact_overlay(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    base_change = _git_bytes(repo, "show", "HEAD:src/change.py")
    base_delete = _git_bytes(repo, "show", "HEAD:src/delete.py")

    (repo / "src" / "change.py").write_text("def change():\n    return 'dirty'\n", encoding="utf-8")
    (repo / "src" / "delete.py").unlink()
    (repo / "src" / "new.py").write_text("def new_file():\n    return 4\n", encoding="utf-8")

    walk = _walk(repo)
    plan = plan_repository_generation(repo, walk.entries(), content_size_cap=CAP)
    base = {entry.path: entry for entry in plan.base_entries}
    overlay = {entry.path: entry for entry in plan.overlay_entries}

    assert plan.base_revision == head
    assert set(base) == {"src/change.py", "src/delete.py", "src/keep.py"}
    assert ".lemoncrow/tracked.txt" not in base
    assert set(overlay) == {"src/change.py", "src/new.py"}
    assert plan.removed_paths == ("src/delete.py",)
    assert plan.base_payloads[base["src/change.py"].content_digest][0] == base_change
    assert plan.base_payloads[base["src/delete.py"].content_digest][0] == base_delete

    effective = dict(base)
    for removed in plan.removed_paths:
        effective.pop(removed, None)
    effective.update(overlay)
    assert effective == {entry.path: entry for entry in walk.entries()}


def test_dirty_worktrees_on_same_commit_share_generation_root(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True)

    (repo / "src" / "change.py").write_text("def change():\n    return 'left'\n", encoding="utf-8")
    (clone / "src" / "keep.py").write_text("def keep():\n    return 99\n", encoding="utf-8")

    left = plan_repository_generation(repo, _walk(repo).entries(), content_size_cap=CAP)
    right = plan_repository_generation(clone, _walk(clone).entries(), content_size_cap=CAP)

    assert left.base_revision == right.base_revision
    assert manifest_root(left.base_entries) == manifest_root(right.base_entries)
    assert left.overlay_paths != right.overlay_paths


def test_clean_worktree_has_empty_overlay(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plan = plan_repository_generation(repo, _walk(repo).entries(), content_size_cap=CAP)
    assert plan.overlay_entries == ()
    assert plan.removed_paths == ()
    assert plan.base_payloads == {}
    assert {entry.path for entry in plan.base_entries} == {"src/change.py", "src/delete.py", "src/keep.py"}


def test_unborn_repository_is_empty_generation_plus_worktree_overlay(tmp_path: Path) -> None:
    repo = tmp_path / "unborn"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "first.py").write_text("def first():\n    return 1\n", encoding="utf-8")
    walk = _walk(repo)
    plan = plan_repository_generation(repo, walk.entries(), content_size_cap=CAP)
    assert plan.base_revision == "0" * 40
    assert plan.base_entries == ()
    assert plan.overlay_entries == walk.entries()
    assert plan.removed_paths == ()


def test_generation_refuses_head_change_during_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow_client import generation as module

    repo = _repo(tmp_path)
    walk = _walk(repo)
    real_head = _git(repo, "rev-parse", "HEAD")
    calls = 0

    def moving_head(_repo: Path) -> str:
        nonlocal calls
        calls += 1
        return real_head if calls == 1 else "f" * 40

    monkeypatch.setattr(module, "_head", moving_head)
    with pytest.raises(ClientError) as raised:
        plan_repository_generation(repo, walk.entries(), content_size_cap=CAP)
    assert raised.value.code is ErrorCode.VIEW_REVISION_STALE

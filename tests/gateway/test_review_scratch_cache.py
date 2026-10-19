"""Review scratch caches: one closer, fast exact materialization, shared Snapshot sources."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lemoncrow.pro.capabilities.review import snapshot, web_preview
from lemoncrow.pro.capabilities.review.scratch_cache import (
    TRASH_PREFIX,
    prune_cache_entries,
    retire_tree,
    wait_for_retired,
)


def _age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def _live(root: Path) -> set[str]:
    return {path.name for path in root.iterdir() if not path.name.startswith(TRASH_PREFIX)}


# --------------------------------------------------------------------------- #
# The closer                                                                  #
# --------------------------------------------------------------------------- #


def test_retire_tree_is_immediate_and_deletes_in_the_background(tmp_path: Path) -> None:
    tree = tmp_path / "entry"
    (tree / "deep" / "deeper").mkdir(parents=True)
    (tree / "deep" / "deeper" / "file.txt").write_text("x")
    single = tmp_path / "file.txt"
    single.write_text("x")

    retire_tree(tree)
    retire_tree(single)
    retire_tree(tmp_path / "missing")

    assert not tree.exists() and not single.exists()
    wait_for_retired()
    assert list(tmp_path.iterdir()) == []


def test_ttl_retires_idle_entries_even_within_the_keep_count(tmp_path: Path) -> None:
    for name, age in (("fresh", 10), ("recent", 1800), ("stale", 4 * 3600)):
        (tmp_path / name).mkdir()
        _age(tmp_path / name, age)

    prune_cache_entries(tmp_path, None, keep_count=10, idle_seconds=3600, ttl_seconds=3 * 3600)

    assert _live(tmp_path) == {"fresh", "recent"}


def test_the_count_cap_spares_entries_still_in_their_grace(tmp_path: Path) -> None:
    for index in range(5):
        (tmp_path / f"idle-{index}").mkdir()
        _age(tmp_path / f"idle-{index}", 7200 + index)
    (tmp_path / "busy").mkdir()
    _age(tmp_path / "busy", 60)
    keep = tmp_path / "idle-4"  # oldest, but the one in use right now

    prune_cache_entries(tmp_path, keep, keep_count=3, idle_seconds=3600)

    # keep + the two most recently used rows; `busy` sorts first and stays.
    assert _live(tmp_path) == {"idle-4", "busy", "idle-0"}


def test_prune_sweeps_trash_a_crashed_process_left_behind(tmp_path: Path) -> None:
    leftover = tmp_path / f"{TRASH_PREFIX}deadbeef"
    (leftover / "tree").mkdir(parents=True)
    (tmp_path / "entry").mkdir()

    prune_cache_entries(tmp_path, None, keep_count=10, idle_seconds=3600)
    wait_for_retired()

    assert sorted(path.name for path in tmp_path.iterdir()) == ["entry"]


# --------------------------------------------------------------------------- #
# Fast exact materialization                                                  #
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _fixture_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main", ".")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    files = {
        "README.md": b"top\n",
        "frontend/src/page.tsx": b"export default 1\n",
        "frontend/package.json": b'{"name":"web"}\n',
        "backend/deep/nested/mod.py": b"print('x')\n",
        "assets/blob.bin": bytes(range(256)) * 64,
        "name with spaces/ünicode.txt": b"u\n",
    }
    for rel, payload in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    script = repo / "bin" / "run.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    os.symlink("../README.md", repo / "frontend" / "readme-link")
    # A checkout would smudge these (CRLF, $Id$); materialization reads raw objects.
    (repo / ".gitattributes").write_text("*.py text eol=crlf\n*.tsx ident\n")
    _git(repo, "add", "-A")
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},vendor/sub")
    _git(repo, "commit", "--quiet", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _tree_state(root: Path) -> dict[str, tuple[bytes, bool, bool]]:
    out: dict[str, tuple[bytes, bool, bool]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        info = path.lstat()
        out[path.relative_to(root).as_posix()] = (
            path.read_bytes(),
            bool(info.st_mode & stat.S_IXUSR),
            stat.S_ISLNK(info.st_mode),
        )
    return out


def test_git_stream_matches_the_pygit2_walk_byte_for_byte(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for case_index, scope in enumerate(
        [".", "frontend", "backend/deep", "frontend/package.json", "vendor/sub", "nope"]
    ):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        with monkeypatch.context() as case_monkeypatch:
            repo, sha = _fixture_repo(case_tmp_path)
            fast = case_tmp_path / "fast"
            snapshot.materialize_git_revision(repo, sha, fast, scope=scope)
            case_monkeypatch.setattr(snapshot, "_stream_git_tree", lambda *_args: False)
            slow = case_tmp_path / "slow"
            snapshot.materialize_git_revision(repo, sha, slow, scope=scope)
            assert _tree_state(fast) == _tree_state(slow)
            if scope == ".":
                state = _tree_state(fast)
                assert state["bin/run.sh"][1] is True
                assert state["frontend/readme-link"] == (b"../README.md", False, False)
                assert state["backend/deep/nested/mod.py"][0] == b"print('x')\n"
                assert not (fast / "vendor" / "sub").exists()


def test_git_stream_ignores_inherited_git_redirection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, sha = _fixture_repo(tmp_path)
    empty = tmp_path / "empty"
    (empty / "objects").mkdir(parents=True)
    monkeypatch.setenv("GIT_DIR", str(empty))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(empty / "objects"))
    results: list[bool] = []
    original = snapshot._stream_git_tree

    def observed(*args):  # type: ignore[no-untyped-def]
        results.append(original(*args))
        return results[-1]

    monkeypatch.setattr(snapshot, "_stream_git_tree", observed)
    target = tmp_path / "out"

    snapshot.materialize_git_revision(repo, sha, target)

    assert results == [True]  # read from the opened repository, not the inherited object directory
    assert (target / "README.md").read_bytes() == b"top\n"


def test_an_incomplete_stream_falls_back_to_the_pygit2_walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, sha = _fixture_repo(tmp_path)
    monkeypatch.setattr(snapshot.shutil, "which", lambda _name: None)  # no git CLI
    target = tmp_path / "out"

    snapshot.materialize_git_revision(repo, sha, target)

    assert (target / "frontend/src/page.tsx").read_bytes() == b"export default 1\n"


# --------------------------------------------------------------------------- #
# Shared Snapshot sources                                                     #
# --------------------------------------------------------------------------- #


def _revision(revision_id: str, *, base: str = "base-sha", fingerprint: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        id=revision_id,
        review_id="review-1",
        base_sha=base,
        head_sha="",
        range_mode="working_tree",
        source_fingerprint=fingerprint or f"fp-{revision_id}",
        tree_fingerprint="",
    )


def test_snapshots_sharing_a_side_share_one_materialized_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_materialize(_store_root, _repo_root, revision, *, side, target, scope=".", store=None):  # type: ignore[no-untyped-def]
        del scope, store
        calls.append((revision.id, side))
        target.mkdir(parents=True)
        (target / "side.txt").write_text(f"{revision.id}:{side}")
        return snapshot.revision_side_identity(revision, side)

    monkeypatch.setattr(snapshot, "materialize_review_side", fake_materialize)
    cache = tmp_path / "snapshot-sources"
    first, second = _revision("r1"), _revision("r2")

    old_first = snapshot.cached_review_side(cache, tmp_path, tmp_path, first, side="old")  # type: ignore[arg-type]
    old_second = snapshot.cached_review_side(cache, tmp_path, tmp_path, second, side="old")  # type: ignore[arg-type]
    new_first = snapshot.cached_review_side(cache, tmp_path, tmp_path, first, side="new")  # type: ignore[arg-type]
    again = snapshot.cached_review_side(cache, tmp_path, tmp_path, first, side="new")  # type: ignore[arg-type]

    assert old_first == old_second  # same base: one tree for every Snapshot
    assert new_first == again and new_first != old_first
    assert calls == [("r1", "old"), ("r1", "new")]  # materialized once per content, never per request


def test_a_failed_materialization_leaves_nothing_to_reuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_store_root, _repo_root, _revision, *, side, target, scope=".", store=None):  # type: ignore[no-untyped-def]
        del side, scope, store
        (target / "partial").mkdir(parents=True)
        raise snapshot.ReviewSnapshotUnavailable("gone")

    monkeypatch.setattr(snapshot, "materialize_review_side", broken)
    cache = tmp_path / "snapshot-sources"

    with pytest.raises(snapshot.ReviewSnapshotUnavailable):
        snapshot.cached_review_side(cache, tmp_path, tmp_path, _revision("r1"), side="new")  # type: ignore[arg-type]
    wait_for_retired()

    (entry,) = _live(cache)
    assert not (cache / entry / "tree").exists()
    assert not (cache / entry / "identity").exists()


def test_legacy_per_snapshot_sources_are_retired_by_shape_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snapshot, "_LEGACY_SWEPT_AT", {})
    root = tmp_path / "_source"
    for layout in (
        "01a0review/01a0snap/old/workspace",
        "01a0review/01a0snap/new/workspace",
        "01a0review/01a0older/workspace/src",
        "r-review/rr-snap/new",
    ):
        (root / layout).mkdir(parents=True)
    # Other tenants of a shared scratch root keep their own shapes.
    (root / "runtime-runs" / "review" / "snap" / "hosted-runner").mkdir(parents=True)
    (root / "interactive-previews" / "ips-old-1").mkdir(parents=True)
    (root / "snapshot-sources" / "key" / "tree").mkdir(parents=True)
    (root / "fresh-review" / "snap" / "old").mkdir(parents=True)
    for name in ("01a0review", "r-review", "runtime-runs", "interactive-previews", "snapshot-sources"):
        _age(root / name, 7200)

    snapshot.retire_legacy_snapshot_sources([root])

    assert _live(root) == {"runtime-runs", "interactive-previews", "snapshot-sources", "fresh-review"}


# --------------------------------------------------------------------------- #
# Web preview builds keep only their output                                   #
# --------------------------------------------------------------------------- #


def _install_fake_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cache_root: Path, *, succeed: bool
) -> tuple[Path, Path]:
    dependency_root = tmp_path / "checkout"
    package = '{"scripts":{"build":"next build"},"dependencies":{"next":"15"}}\n'
    (dependency_root / "frontend" / "node_modules" / "next").mkdir(parents=True)
    (dependency_root / "frontend" / "package.json").write_text(package)

    def fake_materialize(_store_root, _repo_root, _revision, *, side, target, scope, store=None):  # type: ignore[no-untyped-def]
        del side, scope, store
        (target / "frontend" / "src").mkdir(parents=True)
        (target / "frontend" / "package.json").write_text(package)
        (target / "frontend" / "src" / "page.tsx").write_text("export default 1")
        return "head"

    def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        project = cache_root / "new" / "workspace" / "frontend"
        (project / ".next" / "cache").mkdir(parents=True)
        (project / ".next" / "cache" / "big.pack").write_bytes(b"\0" * 4096)
        if succeed:
            (project / "out").mkdir()
            (project / "out" / "index.html").write_text("<html></html>")
        return SimpleNamespace(returncode=0 if succeed else 1, stdout="" if succeed else "boom")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(web_preview, "_materialize_review_side", fake_materialize)
    monkeypatch.setattr(web_preview, "_package_manager", lambda _root: "npm")
    monkeypatch.setattr(web_preview, "_bubblewrap_prefix", lambda *_a, **_k: ["bwrap", "--chdir", "/x", "--", "sh"])
    monkeypatch.setattr(web_preview, "run_limited", fake_run)
    return dependency_root, dependency_root / "frontend"


def _build(tmp_path: Path, cache_root: Path, dependency_root: Path) -> Path:
    revision = SimpleNamespace(
        id="revision", review_id="review", range_mode="commit_range", base_sha="base", head_sha="head"
    )
    return web_preview._ensure_built_unlocked(
        cache_root,
        tmp_path / "frozen",
        revision,  # type: ignore[arg-type]
        store_root=tmp_path / "store",
        side="new",
        framework="next",
        project_root="frontend",
        dependency_root=dependency_root,
    )


def test_a_finished_build_keeps_only_its_static_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_root = tmp_path / "builds" / "key"
    dependency_root, _project = _install_fake_build(monkeypatch, tmp_path, cache_root, succeed=True)

    output = _build(tmp_path, cache_root, dependency_root)
    wait_for_retired()

    workspace = cache_root / "new" / "workspace"
    assert output == workspace / "frontend" / "out"
    assert (output / "index.html").read_text() == "<html></html>"
    assert [path.name for path in workspace.iterdir()] == ["frontend"]
    assert [path.name for path in (workspace / "frontend").iterdir()] == ["out"]  # no sources, .next, node_modules
    assert not [path for path in (tmp_path / "builds").iterdir() if path.name.startswith(TRASH_PREFIX)]
    # The slim entry is still a cache hit.
    monkeypatch.setattr(web_preview, "run_limited", lambda *_a, **_k: pytest.fail("rebuilt a cached output"))
    assert _build(tmp_path, cache_root, dependency_root) == output


def test_a_failed_build_drops_its_workspace_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_root = tmp_path / "builds" / "key"
    dependency_root, _project = _install_fake_build(monkeypatch, tmp_path, cache_root, succeed=False)

    with pytest.raises(web_preview.WebPreviewUnavailable, match="preview build failed"):
        _build(tmp_path, cache_root, dependency_root)
    wait_for_retired()

    assert not (cache_root / "new" / "workspace").exists()
    assert not (cache_root / "new" / "build.json").exists()


def _crafted_tree_repo(tmp_path: Path, hostile_name: str) -> tuple[Path, str]:
    """A commit whose root tree has a subtree literally named *hostile_name* (SL-2)."""

    repo = tmp_path / "hostile"
    repo.mkdir()

    def git(*args: str, stdin: bytes | None = None) -> str:
        return (
            subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True, check=True)
            .stdout.decode()
            .strip()
        )

    git("init", "-q")
    blob = git("hash-object", "-w", "--stdin", stdin=b"pwned\n")
    inner = git("mktree", stdin=f"100644 blob {blob}\tescaped.txt\n".encode())
    good = git("hash-object", "-w", "--stdin", stdin=b"ok\n")
    root = git(
        "mktree", "--missing", stdin=f"040000 tree {inner}\t{hostile_name}\n100644 blob {good}\tok.txt\n".encode()
    )
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
    }
    sha = (
        subprocess.run(
            ["git", "-C", str(repo), "commit-tree", root, "-m", "hostile"], capture_output=True, check=True, env=env
        )
        .stdout.decode()
        .strip()
    )
    return repo, sha


@pytest.mark.parametrize("hostile_name", ["..", ".git", ".GIT"])
@pytest.mark.parametrize("streamed", [True, False])
def test_materialization_refuses_tree_entries_that_escape_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hostile_name: str, streamed: bool
) -> None:
    repo, sha = _crafted_tree_repo(tmp_path, hostile_name)
    if not streamed:
        monkeypatch.setattr(snapshot, "_stream_git_tree", lambda *_args: False)
    target = tmp_path / "out" / "target"

    with pytest.raises(snapshot.ReviewSnapshotUnavailable, match="unsafe path"):
        snapshot.materialize_git_revision(repo, sha, target)

    assert not (tmp_path / "out" / "escaped.txt").exists()
    assert not (target / hostile_name / "escaped.txt").exists()


def test_materialization_never_writes_through_a_planted_symlink(tmp_path: Path) -> None:
    repo, sha = _fixture_repo(tmp_path)
    target = tmp_path / "out"
    outside = tmp_path / "outside"
    outside.mkdir()
    target.mkdir()
    (target / "frontend").symlink_to(outside, target_is_directory=True)

    with pytest.raises(snapshot.ReviewSnapshotUnavailable, match="unsafe path"):
        snapshot.materialize_git_revision(repo, sha, target)

    assert list(outside.iterdir()) == []

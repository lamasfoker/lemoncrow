"""8F special Git changes: semantic deltas over the packet, never raw/empty diffs."""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pygit2
import pytest

from lemoncrow.pro.capabilities.review.gitdiff import collect_diff, resolve_rev_range
from lemoncrow.pro.capabilities.review.models import ChangedFile, ReviewPacket, review_packet_from_dict
from lemoncrow.pro.capabilities.review.special_changes import (
    special_git_change,
    submodule_repository_url,
    symlink_boundary,
)

OLD = "a" * 40
NEW = "b" * 40


def _hunk(body: str) -> dict[str, Any]:
    return {"header": "@@ -1 +1 @@", "patch": body}


def test_rename_only_names_both_paths() -> None:
    change = special_git_change(
        {"path": "src/new.py", "old_path": "src/old.py", "status": "renamed", "similarity": 100, "hunks": []}
    )
    assert change == {
        "kind": "rename",
        "old_path": "src/old.py",
        "new_path": "src/new.py",
        "copied": False,
        "similarity": 100,
        "mode": "",
    }


@pytest.mark.parametrize(
    ("old_mode", "new_mode", "executable"),
    [(0o100644, 0o100755, "added"), (0o100755, 0o100644, "removed")],
)
def test_mode_only_reports_the_executable_bit(old_mode: int, new_mode: int, executable: str) -> None:
    change = special_git_change(
        {"path": "bin/run", "status": "modified", "hunks": [], "old_mode": old_mode, "new_mode": new_mode}
    )
    assert change is not None
    assert change["kind"] == "mode"
    assert change["executable"] == executable
    assert (change["old_mode"], change["new_mode"]) == (f"{old_mode:06o}", f"{new_mode:06o}")


def test_unrecorded_modes_never_claim_an_executable_change() -> None:
    change = special_git_change({"path": "bin/run", "status": "modified", "hunks": []})
    assert change is not None
    assert change["kind"] == "metadata"
    assert change["modes_recorded"] is False
    assert "executable" not in change


def test_ordinary_text_diff_and_added_file_are_not_special() -> None:
    assert special_git_change({"path": "a.py", "status": "modified", "hunks": [_hunk("-a\n+b\n")]}) is None
    assert special_git_change({"path": "a.py", "status": "added", "hunks": []}) is None
    assert special_git_change({"path": "a.bin", "status": "modified", "is_binary": True}) is None


def test_in_repository_symlink_reports_old_and_new_target() -> None:
    change = special_git_change(
        {
            "path": "docs/current",
            "status": "modified",
            "old_mode": 0o120000,
            "new_mode": 0o120000,
            "hunks": [_hunk("-v1\n\\ No newline at end of file\n+v2\n\\ No newline at end of file\n")],
        }
    )
    assert change is not None
    assert change["kind"] == "symlink"
    assert (change["old_target"], change["new_target"]) == ("v1", "v2")
    assert change["resolved_target"] == "docs/v2"
    assert change["escapes_repository"] is False
    assert change["warnings"] == []


def test_escaping_symlink_carries_a_boundary_warning() -> None:
    for target in ["../../etc/passwd", "/etc/passwd", "~/.ssh/id_rsa", "C:\\Windows\\win.ini"]:
        change = special_git_change(
            {
                "path": "config/link",
                "status": "added",
                "old_mode": 0,
                "new_mode": 40960,
                "hunks": [_hunk(f"+{target}\n")],
            }
        )
        assert change is not None
        assert change["kind"] == "symlink"
        assert change["old_type"] == "absent"
        assert change["new_target"] == target
        assert change["escapes_repository"] is True
        assert any("leaves the repository" in warning for warning in change["warnings"])


def test_symlink_into_trust_relevant_path_is_warned() -> None:
    boundary = symlink_boundary("scripts/ci", "../.github/workflows/release.yml")
    assert boundary == {
        "resolved": ".github/workflows/release.yml",
        "escapes_repository": False,
        "trust_area": "repository automation and policy",
    }
    change = special_git_change(
        {
            "path": "scripts/ci",
            "status": "modified",
            "old_mode": 0o120000,
            "new_mode": 0o120000,
            "hunks": [_hunk("-ci.yml\n+../.github/workflows/release.yml\n")],
        }
    )
    assert change is not None
    assert change["warnings"] == [
        "Symlink target points into a trust-relevant path (repository automation and policy)."
    ]


def test_submodule_reports_commit_pair_and_known_host_links_only() -> None:
    gitmodules = '[submodule "vendor/lib"]\n\tpath = vendor/lib\n\turl = git@github.com:acme/lib.git\n'
    url = submodule_repository_url(gitmodules, "vendor/lib")
    assert url == "git@github.com:acme/lib.git"
    change = special_git_change(
        {"path": "vendor/lib", "status": "modified", "submodule_pointer": [OLD, NEW], "hunks": []},
        submodule_url=url,
    )
    assert change == {
        "kind": "submodule",
        "old_path": "",
        "new_path": "vendor/lib",
        "change": "updated",
        "old_commit": OLD,
        "new_commit": NEW,
        "repository_url": url,
        "commit_url": f"https://github.com/acme/lib/commit/{NEW}",
        "compare_url": f"https://github.com/acme/lib/compare/{OLD}...{NEW}",
    }
    relative = special_git_change(
        {"path": "vendor/lib", "status": "added", "submodule_pointer": ["", NEW]},
        submodule_url="../lib.git",
    )
    assert relative is not None
    assert relative["change"] == "added"
    assert (relative["commit_url"], relative["compare_url"]) == ("", "")


def test_packet_round_trips_file_modes_and_rejects_impossible_ones() -> None:
    packet = ReviewPacket(
        schema_version=2,
        generated_at="2026-10-03T00:00:00+00:00",
        repo_root="/repo",
        range_mode="commit_range",
        base_rev="HEAD~1",
        head_rev="HEAD",
        base_sha=OLD,
        head_sha=NEW,
        files=(ChangedFile(path="bin/run", old_path=None, status="modified", old_mode=0o100644, new_mode=0o100755),),
    )
    decoded = review_packet_from_dict(packet.to_dict())
    assert (decoded.files[0].old_mode, decoded.files[0].new_mode) == (0o100644, 0o100755)
    legacy = packet.to_dict()
    del legacy["files"][0]["old_mode"], legacy["files"][0]["new_mode"]
    assert review_packet_from_dict(legacy).files[0].new_mode == 0
    raw = packet.to_dict()
    raw["files"][0]["new_mode"] = -1
    with pytest.raises(ValueError, match="Git file mode"):
        review_packet_from_dict(raw)


def _signature() -> pygit2.Signature:
    return pygit2.Signature("Fixture Tester", "fixture@example.invalid", 1700000000, 0)


def _commit(repo: Any, message: str) -> None:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    repo.create_commit("HEAD", _signature(), _signature(), message, tree, parents)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes and symlinks")
def test_collect_diff_records_modes_for_chmod_rename_and_symlink(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    repo = pygit2.init_repository(str(root), initial_head="main")
    (root / "run.sh").write_text("echo hi\n", encoding="utf-8")
    (root / "old_name.py").write_text("value = 1\n" * 20, encoding="utf-8")
    (root / "target_v1").write_text("one\n", encoding="utf-8")
    os.symlink("target_v1", root / "current")
    _commit(repo, "base")

    (root / "run.sh").chmod(0o755)
    (root / "old_name.py").rename(root / "new_name.py")
    (root / "current").unlink()
    os.symlink("../outside", root / "current")
    repo.index.remove("old_name.py")
    _commit(repo, "head")

    rows = {item.path: item for item in collect_diff(root, resolve_rev_range(root), with_patch_text=True).files}

    chmod = rows["run.sh"]
    assert (chmod.old_mode, chmod.new_mode) == (0o100644, 0o100755)
    mode_change = special_git_change(asdict(chmod))
    assert mode_change is not None and mode_change["executable"] == "added"

    link = rows["current"]
    assert link.new_mode == 0o120000
    link_change = special_git_change(asdict(link))
    assert link_change is not None
    assert (link_change["old_target"], link_change["new_target"]) == ("target_v1", "../outside")
    assert link_change["escapes_repository"] is True

    renamed = rows["new_name.py"]
    assert (renamed.status, renamed.old_path, renamed.hunks) == ("renamed", "old_name.py", ())
    rename_change = special_git_change(asdict(renamed))
    assert rename_change is not None
    assert (rename_change["kind"], rename_change["old_path"], rename_change["new_path"]) == (
        "rename",
        "old_name.py",
        "new_name.py",
    )

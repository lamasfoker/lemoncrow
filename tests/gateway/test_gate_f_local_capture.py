"""Gate F property 1 on the local path: `lc review --pr` captures one exact base/head pair.

`resolve_pull_request` takes the pair from one `gh pr view` answer, fetches,
and refuses when a commit of that pair did not arrive (the pull request was
force-pushed in between): it never falls back to whatever the fetch brought.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.sources.github import PullRequestRef, resolve_pull_request

PR = PullRequestRef(owner="acme", repo="widgets", number=7)


def _run(cwd: Path, *args: str) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _world(tmp_path: Path) -> tuple[Path, Path, str]:
    origin = tmp_path / "origin.git"
    _run(tmp_path, "git", "init", "--bare", "-q", "-b", "main", str(origin))
    author = tmp_path / "author"
    _run(tmp_path, "git", "clone", "-q", str(origin), str(author))
    for key, value in (("user.name", "A"), ("user.email", "a@example.invalid")):
        _run(author, "git", "config", key, value)
    (author / "a.txt").write_text("one\n")
    _run(author, "git", "add", ".")
    _run(author, "git", "commit", "-qm", "base")
    _run(author, "git", "push", "-q", "origin", "main")
    base = _run(author, "git", "rev-parse", "HEAD")
    clone = tmp_path / "clone"
    _run(tmp_path, "git", "clone", "-q", str(origin), str(clone))
    _run(clone, "git", "remote", "set-url", "origin", "git@github.com:acme/widgets.git")
    _run(clone, "git", "config", f"url.{origin}.insteadOf", "git@github.com:acme/widgets.git")
    _run(author, "git", "checkout", "-qb", "feature")
    return author, clone, base


def _gh(head: str, base: str) -> Any:
    def gh(args: Any, stdin: str | None) -> str:
        assert list(args[:3]) == ["pr", "view", "7"]
        return json.dumps(
            {
                "url": PR.url,
                "title": "Widgets",
                "author": {"login": "alice"},
                "state": "OPEN",
                "headRefOid": head,
                "baseRefName": "main",
                "baseRefOid": base,
            }
        )

    return gh


def test_a_head_force_pushed_away_during_the_fetch_is_refused_not_replaced(tmp_path: Path) -> None:
    author, clone, base = _world(tmp_path)
    (author / "a.txt").write_text("two\n")
    _run(author, "git", "commit", "-qam", "first push")
    reported = _run(author, "git", "rev-parse", "HEAD")  # what `gh pr view` answered
    # The author amends and force-pushes before this clone fetches: the reported
    # head is no longer reachable from the pull request ref.
    (author / "a.txt").write_text("three\n")
    _run(author, "git", "commit", "-q", "--amend", "-am", "amended")
    _run(author, "git", "push", "-q", "-f", "origin", "HEAD:refs/pull/7/head")

    with pytest.raises(ValueError, match="moved while it was being fetched"):
        resolve_pull_request(clone, "#7", runner=_gh(reported, base))


def test_a_head_that_advanced_but_is_still_reachable_keeps_the_reported_pair(tmp_path: Path) -> None:
    author, clone, base = _world(tmp_path)
    (author / "a.txt").write_text("two\n")
    _run(author, "git", "commit", "-qam", "first push")
    reported = _run(author, "git", "rev-parse", "HEAD")
    (author / "b.txt").write_text("later\n")
    _run(author, "git", "add", ".")
    _run(author, "git", "commit", "-qm", "second push")
    _run(author, "git", "push", "-q", "origin", "HEAD:refs/pull/7/head")

    pull = resolve_pull_request(clone, "#7", runner=_gh(reported, base))
    # The capture names exactly the pair GitHub reported, never the newer fetched tip.
    assert (pull.base_sha, pull.head_sha) == (base, reported)

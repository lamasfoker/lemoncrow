"""Differential check: fix apply decisions must never be less safe than git merge."""

from __future__ import annotations

import random
import subprocess
from pathlib import Path

from lemoncrow.pro.capabilities.review.fix_lines import decide

_WORDS = ["alpha", "beta", "gamma", "delta", "eps", "zeta", "eta", "theta"]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _edit(rng: random.Random, lines: list[str]) -> list[str]:
    out = list(lines)
    for _ in range(rng.randint(1, 3)):
        op = rng.choice(["replace", "insert", "delete"])
        at = rng.randrange(len(out) + (1 if op == "insert" else 0)) if out else 0
        if op == "replace" and out:
            out[at] = rng.choice(_WORDS) + str(rng.randint(0, 9))
        elif op == "insert":
            out.insert(at, rng.choice(_WORDS) + str(rng.randint(0, 9)))
        elif op == "delete" and len(out) > 1:
            del out[at]
    return out


def _oracle(
    repo: Path,
    base: bytes,
    ours: bytes,
    theirs: bytes,
) -> tuple[bytes, bool]:
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "f").write_bytes(base)
    _git(repo, "add", "f")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-q", "-b", "fix")
    (repo / "f").write_bytes(theirs)
    _git(repo, "commit", "-qam", "fix", "--allow-empty")
    _git(repo, "checkout", "-q", "main")
    (repo / "f").write_bytes(ours)
    _git(repo, "commit", "-qam", "main", "--allow-empty")
    clean = _git(repo, "merge", "--no-edit", "-q", "fix").returncode == 0
    return (repo / "f").read_bytes(), clean


def test_planner_never_fails_open_against_git_merge(tmp_path: Path) -> None:
    rng = random.Random(20260929)
    fail_open: list[int] = []
    for case in range(40):
        base_lines = [rng.choice(_WORDS) for _ in range(rng.randint(6, 14))]
        base = ("\n".join(base_lines) + "\n").encode()
        ours = ("\n".join(_edit(rng, base_lines)) + "\n").encode()
        theirs = ("\n".join(_edit(rng, base_lines)) + "\n").encode()
        expected, clean = _oracle(tmp_path / f"case{case}", base, ours, theirs)
        result = decide("f", base, ours, theirs)
        landed = ours if result.outcome == "unchanged" else result.content
        if result.outcome != "conflict" and (not clean or landed != expected):
            fail_open.append(case)

    assert fail_open == [], f"planner wrote what git would not: cases {fail_open}"

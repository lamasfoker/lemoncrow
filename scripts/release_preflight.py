#!/usr/bin/env python3
"""Fail before a LemonCrow release can mutate Git state or publish anything."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    workdir = REPO_ROOT if cwd is None else cwd
    result = subprocess.run(["git", *args], cwd=workdir, text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _TAG_RE.fullmatch("v" + value if not value.startswith("v") else value)
    if match is None:
        raise ValueError(f"version must be X.Y.Z, got {value!r}")
    return tuple(int(part) for part in match.groups())


def validate_tag(tag: str, current_version: str, *, force: bool = False) -> str:
    if _TAG_RE.fullmatch(tag) is None:
        raise ValueError(f"tag must be vX.Y.Z, got {tag!r}")
    version = tag[1:]
    if not force and _version_tuple(version) <= _version_tuple(current_version):
        raise ValueError(f"{tag} must be newer than current version {current_version}")
    return version


def _root_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _require_clean_tree() -> None:
    status = _git("status", "--porcelain=v1", "--untracked-files=all")
    if status:
        lines = "\n".join(status.splitlines()[:20])
        raise RuntimeError(f"working tree/submodules are not clean:\n{lines}")


def _require_main_synced() -> None:
    branch = _git("branch", "--show-current")
    if branch != "main":
        raise RuntimeError(f"release must run from main, currently {branch or 'detached HEAD'}")
    _git("fetch", "--quiet", "origin", "main")
    counts = _git("rev-list", "--left-right", "--count", "origin/main...HEAD").split()
    if len(counts) != 2:
        raise RuntimeError("could not compare HEAD with origin/main")
    behind, ahead = (int(value) for value in counts)
    if behind or ahead:
        raise RuntimeError(
            f"main must be fully pushed and synchronized before versioning (behind={behind}, ahead={ahead})"
        )


def _require_submodules_reproducible() -> None:
    config = _git("config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$", check=False)
    if not config:
        return
    for line in config.splitlines():
        key, path_value = line.split(maxsplit=1)
        name = key.removeprefix("submodule.").removesuffix(".path")
        path = REPO_ROOT / path_value
        if not (path / ".git").exists() and not (path / ".git").is_file():
            raise RuntimeError(f"submodule {path_value} is not initialized")

        _git("fetch", "--quiet", "origin", cwd=path)
        configured_branch = _git(
            "config",
            "--file",
            ".gitmodules",
            "--get",
            f"submodule.{name}.branch",
            check=False,
        )
        remote_head = _git(
            "symbolic-ref",
            "--quiet",
            "--short",
            "refs/remotes/origin/HEAD",
            cwd=path,
            check=False,
        )
        configured_ref = f"origin/{configured_branch}" if configured_branch and configured_branch != "." else ""
        candidates = [configured_ref, remote_head, "origin/main", "origin/master"]
        remote_ref = next(
            (
                candidate
                for candidate in candidates
                if candidate and _git("rev-parse", "--verify", candidate, cwd=path, check=False)
            ),
            None,
        )
        if remote_ref is None:
            raise RuntimeError(f"submodule {path_value}: cannot resolve the origin default branch")

        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", "HEAD", remote_ref],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            head = _git("rev-parse", "--short=12", "HEAD", cwd=path)
            raise RuntimeError(
                f"submodule {path_value}@{head} is not reachable from {remote_ref}; push it before releasing"
            )


def _require_unreleased_notes() -> None:
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    marker = "## [Unreleased]"
    if text.count(marker) != 1:
        raise RuntimeError("CHANGELOG.md must contain exactly one [Unreleased] section")
    body = text.split(marker, 1)[1].split("\n## [", 1)[0].strip()
    if not body:
        raise RuntimeError("CHANGELOG.md [Unreleased] section is empty")


def run_preflight(tag: str, *, force: bool = False, check_remotes: bool = True) -> str:
    version = validate_tag(tag, _root_version(), force=force)
    _require_clean_tree()
    _require_main_synced()
    if check_remotes:
        _require_submodules_reproducible()
    _require_unreleased_notes()
    _require_tag_absent(tag, force=force)
    return version


def _require_tag_absent(tag: str, *, force: bool) -> None:
    if not force and _git("rev-parse", "--verify", f"refs/tags/{tag}", check=False):
        raise RuntimeError(f"tag {tag} already exists locally")


def _require_release_commit(tag: str, version: str) -> None:
    """HEAD is the commit ``make release-prepare`` made: the version, on top of (or already in) origin/main."""

    branch = _git("branch", "--show-current")
    if branch != "main":
        raise RuntimeError(f"release must run from main, currently {branch or 'detached HEAD'}")
    subject = _git("log", "-1", "--format=%s")
    if subject != f"chore(release): {tag}":
        raise RuntimeError(
            f"HEAD is not the {tag} release commit (run make release-prepare tag={tag}); its subject is {subject!r}"
        )
    if _root_version() != version:
        raise RuntimeError(f"HEAD carries version {_root_version()}, not {version}")
    if f"## [{version}]" not in (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8"):
        raise RuntimeError(f"CHANGELOG.md has no [{version}] section")
    _git("fetch", "--quiet", "origin", "main")
    upstream = _git("rev-parse", "origin/main")
    # Directly on origin/main (prepared, not pushed), or origin/main itself (a release resumed after its push).
    if upstream not in {_git("rev-parse", "HEAD^"), _git("rev-parse", "HEAD")}:
        raise RuntimeError("the release commit must sit directly on origin/main: prepare it again on the current main")


def run_prepared(tag: str, *, force: bool = False, check_remotes: bool = True) -> str:
    """Checks for the prepared release commit, which ``make release-qualify`` qualifies and ``make release`` tags."""

    if _TAG_RE.fullmatch(tag) is None:
        raise ValueError(f"tag must be vX.Y.Z, got {tag!r}")
    version = tag[1:]
    _require_clean_tree()
    _require_release_commit(tag, version)
    if check_remotes:
        _require_submodules_reproducible()
    _require_tag_absent(tag, force=force)
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-submodule-remotes", action="store_true")
    parser.add_argument(
        "--prepared",
        action="store_true",
        help="check the prepared release commit (make release-qualify, make release) instead of main before it",
    )
    args = parser.parse_args(argv)
    check = run_prepared if args.prepared else run_preflight
    try:
        version = check(
            args.tag,
            force=args.force,
            check_remotes=not args.skip_submodule_remotes,
        )
    except (OSError, RuntimeError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"release preflight failed: {exc}", file=sys.stderr)
        return 1
    print(f"✓ Release preflight passed for v{version}" + (" (prepared release commit)" if args.prepared else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

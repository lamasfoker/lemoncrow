"""Bounded, deterministic git-history mining for reviewer suggestions (Phase 9 item 7).

History is read at the Snapshot's **immutable base commit** -- never the head --
so the Change's own commits cannot nominate reviewers for themselves, and the
same base always yields the same answer. One ``git log`` per (base, paths) is
run with ``--literal-pathspecs`` and a hard commit bound, and the result is
cached per base commit; nothing scans full history per request.

"Meaningful" here means: non-merge commits by non-bot authors, plus explicit
``Reviewed-by:`` trailers. Authors of the Change itself are reported separately
(:func:`change_authors`) so callers can exclude them.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

MAX_HISTORY_COMMITS: Final[int] = 200
MAX_HISTORY_PATHSPECS: Final[int] = 200
HISTORY_TIMEOUT_S: Final[float] = 10.0
_CACHE_SIZE: Final[int] = 64
_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_ANGLE_EMAIL = re.compile(r"<([^<>\s]+@[^<>\s]+)>")
_RECORD, _FIELD, _MULTI = "\x1e", "\x1f", "\x1c"
_BOT_EMAILS: Final[tuple[str, ...]] = ("noreply@anthropic.com", "noreply@openai.com")

HistoryRole = Literal["author", "reviewer"]


@dataclass(frozen=True, slots=True)
class HistoryContributor:
    email: str
    name: str
    role: HistoryRole
    paths: tuple[str, ...]
    commits: int


@dataclass(frozen=True, slots=True)
class HistoryResult:
    state: Literal["available", "unavailable"]
    base_sha: str = ""
    contributors: tuple[HistoryContributor, ...] = ()
    commits_scanned: int = 0
    detail: str = ""
    #: Changed paths past :data:`MAX_HISTORY_PATHSPECS` whose history was not read.
    paths_unmined: int = 0
    #: The commit bound was reached, so older history was not read.
    commits_bounded: bool = False


def is_bot_identity(name: str, email: str) -> bool:
    """Agents and automation never count as meaningful human reviewers/authors."""

    lowered = f"{name} {email}".lower()
    return "[bot]" in lowered or email.strip().lower() in _BOT_EMAILS


def _pathspecs(paths: Mapping[str, str | None]) -> tuple[dict[str, tuple[str, ...]], int]:
    """``(pathspec -> changed paths, changed paths left unmined)``.

    The pathspec is the base-side path for an existing file and the parent
    directory for a new one. Past :data:`MAX_HISTORY_PATHSPECS` distinct specs
    the remaining changed paths are counted, never silently dropped.
    """

    out: dict[str, tuple[str, ...]] = {}
    unmined = 0
    for path, base_path in sorted(paths.items()):
        spec = base_path or (path.rsplit("/", 1)[0] if "/" in path else "")
        if not spec:
            continue
        if spec not in out and len(out) >= MAX_HISTORY_PATHSPECS:
            # lc-debt: only the first 200 distinct pathspecs (sorted) are mined and the rest are reported as unmined; batch git log calls if larger Changes need full coverage.
            unmined += 1
            continue
        out[spec] = (*out.get(spec, ()), path)
    return out, unmined


def _changed_paths_for(file_name: str, specs: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    """Every changed path a history file stands for: its own spec and any parent-directory spec."""

    found: list[str] = list(specs.get(file_name, ()))
    parent = file_name
    while "/" in parent:
        parent = parent.rsplit("/", 1)[0]
        found.extend(specs.get(parent, ()))
    return tuple(dict.fromkeys(found))


_CACHE: OrderedDict[tuple[str, str, str, int, int], HistoryResult] = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _git(root: Path, *args: str, timeout_s: float) -> str | None:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "--literal-pathspecs", *args],
            capture_output=True,
            timeout=timeout_s,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", errors="replace")


def mine_path_history(
    repo_root: Path,
    base_sha: str,
    paths: Mapping[str, str | None],
    *,
    max_commits: int = MAX_HISTORY_COMMITS,
    timeout_s: float = HISTORY_TIMEOUT_S,
) -> HistoryResult:
    """Recent authors/reviewers of *paths* in the history reachable from *base_sha*.

    *paths* maps each changed path to its base-side path (``None`` for a file the
    base does not have, whose parent directory's history is used instead).
    """

    if not _SHA.match(base_sha):
        return HistoryResult("unavailable", base_sha, detail="no immutable base commit")
    specs, unmined = _pathspecs(paths)
    if not specs:
        return HistoryResult("available", base_sha, detail="no base-side paths to mine")
    # Keyed on every requested path, mined or not: two requests that share their
    # mined specs but differ past the bound must not share an unmined count.
    material = "\x00".join(f"{path}\x01{base_path or ''}" for path, base_path in sorted(paths.items()))
    digest = hashlib.sha256(material.encode()).hexdigest()
    key = (str(repo_root.expanduser().resolve()), base_sha, digest, max_commits, MAX_HISTORY_PATHSPECS)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
            return cached
    output = _git(
        repo_root,
        "log",
        "--no-merges",
        f"--max-count={max(1, max_commits)}",
        f"--format={_RECORD}%H{_FIELD}%an{_FIELD}%ae{_FIELD}%(trailers:key=Reviewed-by,valueonly,separator=%x1c)",
        "--name-only",
        base_sha,
        "--",
        *specs,
        timeout_s=timeout_s,
    )
    if output is None:
        return HistoryResult("unavailable", base_sha, detail="git history is not readable here")
    by_identity: dict[tuple[str, HistoryRole], tuple[str, list[str], set[str]]] = {}
    scanned = 0
    for record in output.split(_RECORD):
        if not record.strip():
            continue
        header, _, body = record.partition("\n")
        fields = header.split(_FIELD)
        if len(fields) < 4:
            continue
        sha, name, email, trailers = fields[0], fields[1].strip(), fields[2].strip().lower(), fields[3]
        scanned += 1
        touched = list(
            dict.fromkeys(
                changed
                for line in body.splitlines()
                if line.strip()
                for changed in _changed_paths_for(line.strip(), specs)
            )
        )
        if not touched:
            continue
        people: list[tuple[str, str, HistoryRole]] = [(name, email, "author")]
        for value in trailers.split(_MULTI):
            match = _ANGLE_EMAIL.search(value)
            if match:
                people.append((value.split("<", 1)[0].strip(), match.group(1).lower(), "reviewer"))
        for person, address, role in people:
            if not address or is_bot_identity(person, address):
                continue
            held_name, held_paths, commits = by_identity.setdefault((address, role), (person, [], set()))
            commits.add(sha)
            held_paths.extend(path for path in touched if path not in held_paths)
            by_identity[(address, role)] = (held_name or person, held_paths, commits)
    contributors = tuple(
        HistoryContributor(email=address, name=name, role=role, paths=tuple(held), commits=len(commits))
        for (address, role), (name, held, commits) in sorted(by_identity.items())
    )
    result = HistoryResult(
        "available",
        base_sha,
        contributors,
        scanned,
        paths_unmined=unmined,
        commits_bounded=scanned >= max(1, max_commits),
    )
    with _CACHE_LOCK:
        # lc-debt: per-process LRU of 64 (base, paths) results; a shared cache keyed by base commit if hosted ever mines history.
        _CACHE[key] = result
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return result


def change_authors(
    repo_root: Path,
    base_sha: str,
    head_sha: str,
    *,
    working_tree: bool = False,
    max_commits: int = MAX_HISTORY_COMMITS,
    timeout_s: float = HISTORY_TIMEOUT_S,
) -> tuple[str, ...]:
    """Lower-cased emails of whoever authored this Change (its commits, or the local committer)."""

    emails: list[str] = []
    if _SHA.match(base_sha) and _SHA.match(head_sha) and base_sha != head_sha:
        output = _git(
            repo_root,
            "log",
            f"--max-count={max(1, max_commits)}",
            "--format=%ae",
            f"{base_sha}..{head_sha}",
            timeout_s=timeout_s,
        )
        emails.extend(line.strip().lower() for line in (output or "").splitlines() if line.strip())
    if working_tree or not emails:
        configured = _git(repo_root, "config", "user.email", timeout_s=timeout_s)
        if configured and configured.strip():
            emails.append(configured.strip().lower())
    return tuple(dict.fromkeys(emails))


def clear_history_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def history_paths(files: Sequence[Mapping[str, object]]) -> dict[str, str | None]:
    """Packet file rows -> ``changed path -> base-side path`` (``None`` for added files)."""

    out: dict[str, str | None] = {}
    for item in files:
        path = str(item.get("path") or "")
        if not path:
            continue
        status = str(item.get("status") or "modified")
        old_path = str(item.get("old_path") or "")
        out[path] = None if status in {"added", "untracked"} else (old_path or path)
    return out


__all__ = [
    "MAX_HISTORY_COMMITS",
    "MAX_HISTORY_PATHSPECS",
    "HistoryContributor",
    "HistoryResult",
    "change_authors",
    "clear_history_cache",
    "history_paths",
    "is_bot_identity",
    "mine_path_history",
]

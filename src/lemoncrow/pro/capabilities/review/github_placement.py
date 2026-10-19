"""The one canonical GitHub review-comment placement rule.

Shared by the CLI publisher and the hosted GitHub provider so the two can never
disagree about which side of the diff a comment belongs to. Pure and offline.

An annotation anchored on the old side (``old``) is a ``LEFT`` comment, one on
the new side (``new``) is ``RIGHT``. Anything else -- a file-level or
``document`` anchor, an unknown side -- is never guessed: it has no placement.
A comment is inline only when its whole line range sits inside one of the
hunks GitHub shows for that side; otherwise the caller must fold it into the
review body (a safe top-level comment) instead of dropping it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Protocol

SIDE_BY_ANCHOR: dict[str, str] = {"new": "RIGHT", "old": "LEFT"}

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def github_side(anchor_side: str) -> str:
    """``LEFT``/``RIGHT`` for an anchor side, or ``""`` when it has no diff side."""
    return SIDE_BY_ANCHOR.get(anchor_side, "")


def hunk_ranges(patch: str) -> dict[str, tuple[tuple[int, int], ...]]:
    """Inclusive line spans GitHub shows per side (``LEFT``/``RIGHT``), one per hunk."""

    left: list[tuple[int, int]] = []
    right: list[tuple[int, int]] = []
    for line in patch.splitlines():
        match = _HUNK.match(line)
        if match is None:
            continue
        old_start, old_len = int(match[1]), int(match[2]) if match[2] is not None else 1
        new_start, new_len = int(match[3]), int(match[4]) if match[4] is not None else 1
        if old_len > 0:
            left.append((old_start, old_start + old_len - 1))
        if new_len > 0:
            right.append((new_start, new_start + new_len - 1))
    return {"LEFT": tuple(left), "RIGHT": tuple(right)}


def unplaceable_reason(*, patch: str | None, anchor_side: str, start_line: int, end_line: int) -> str:
    """Why this range cannot be an inline comment on *patch*; ``""`` when it can."""

    side = github_side(anchor_side)
    start = start_line
    end = max(end_line, start)
    if start <= 0:
        return "file-level comment"
    if not side:
        return "not anchored to a diff line"
    if patch is None:
        return "GitHub shows no diff for this file"
    if not any(low <= start and end <= high for low, high in hunk_ranges(patch)[side]):
        return "outside the lines GitHub shows in the diff"
    return ""


class _HasPaths(Protocol):
    @property
    def path(self) -> str: ...

    @property
    def previous_path(self) -> str: ...


def index_files[F: _HasPaths](files: Iterable[F]) -> dict[str, F]:
    """Look changed files up by current path, falling back to the pre-rename path.

    A renamed file's old-side anchor carries the old path; GitHub still wants
    the file's current path on the comment, so callers publish ``file.path``.
    """

    rows = tuple(files)
    indexed: dict[str, F] = {}
    for item in rows:
        indexed[item.path] = item
    for item in rows:
        if item.previous_path:
            indexed.setdefault(item.previous_path, item)
    return indexed

"""Reviewer-authored source proposals bound to immutable Review revisions."""

from __future__ import annotations

import difflib
import hashlib


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def selected_line_text(text: str, start_line: int, end_line: int) -> str:
    """Return the exact selected source bytes represented as decoded text."""

    lines = text.splitlines(keepends=True)
    if start_line < 1 or end_line < start_line or end_line > len(lines):
        raise ValueError("proposal range is outside the reviewed file")
    return "".join(lines[start_line - 1 : end_line])


def replace_line_range(text: str, start_line: int, end_line: int, replacement_text: str) -> str:
    """Replace one 1-based inclusive line range, preserving its terminal newline convention."""

    lines = text.splitlines(keepends=True)
    if start_line < 1 or end_line < start_line or end_line > len(lines):
        raise ValueError("proposal range is outside the current file")
    original = "".join(lines[start_line - 1 : end_line])
    replacement = replacement_text
    if replacement and not replacement.endswith(("\n", "\r")):
        if original.endswith("\r\n"):
            replacement += "\r\n"
        elif original.endswith("\n"):
            replacement += "\n"
        elif original.endswith("\r"):
            replacement += "\r"
    return "".join((*lines[: start_line - 1], replacement, *lines[end_line:]))


def unified_proposal_patch(
    path: str,
    base_text: str,
    start_line: int,
    end_line: int,
    replacement_text: str,
) -> tuple[str, str]:
    """Return ``(proposed_text, unified_diff)`` for one exact line replacement."""

    proposed = replace_line_range(base_text, start_line, end_line, replacement_text)
    diff_lines = list(
        difflib.unified_diff(
            base_text.splitlines(keepends=True),
            proposed.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )
    # Lead with a `diff --git` header so @pierre/diffs (the frontend's patch
    # parser) resolves the real path instead of keeping the a/ b/ prefix in
    # the parsed filename, which breaks path-keyed annotations and language
    # detection. Every other patch in the app already carries this header.
    patch = f"diff --git a/{path} b/{path}\n" + "".join(diff_lines) if diff_lines else ""
    return proposed, patch

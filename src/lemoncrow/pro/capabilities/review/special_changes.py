"""Semantic presentation of special Git changes over one packet file entry.

The packet already records what a reviewer needs to judge a rename-only,
mode-only, symlink or submodule change: ``status``/``old_path``, the per-side
Git modes, the symlink target (git stores it as the link's blob, so it is the
hunk text) and the gitlink commit pair. Rendering those as an empty diff, a
``no hunks`` refusal or git's own ``Subproject commit`` prose hides the fact the
reviewer has to judge. This module only *reads* that entry; it never consults
the working tree and never guesses a value the packet did not record.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping, Sequence
from typing import Any

SYMLINK_MODE = 0o120000
GITLINK_MODE = 0o160000
_FILE_TYPE_MASK = 0o170000
_REGULAR_FILE = 0o100000
_EXECUTABLE_BITS = 0o111

_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_GITHUB_REMOTE_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)

# Paths whose content steers trust decisions: repository internals, automation
# and policy, ownership, agent instructions and credentials. A symlink into one
# of them can make a reviewed change read or replace something the reviewer
# never saw as changed.
_TRUST_PREFIXES: tuple[tuple[str, str], ...] = (
    (".git/", "Git repository internals"),
    (".github/", "repository automation and policy"),
    (".gitlab/", "repository automation and policy"),
    (".circleci/", "repository automation and policy"),
    (".husky/", "Git hooks"),
    (".lemoncrow/", "Review policy"),
    (".claude/", "agent configuration"),
    (".cursor/", "agent configuration"),
    (".ssh/", "credentials"),
)
_TRUST_NAMES: dict[str, str] = {
    ".git": "Git repository internals",
    ".gitattributes": "Git attributes",
    ".gitmodules": "submodule configuration",
    ".gitlab-ci.yml": "repository automation and policy",
    "codeowners": "code ownership",
    "agents.md": "agent instructions",
    "claude.md": "agent instructions",
    ".npmrc": "credentials",
    ".pypirc": "credentials",
    ".netrc": "credentials",
    "id_rsa": "credentials",
    "id_ed25519": "credentials",
}
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def _mode(entry: Mapping[str, Any], key: str) -> int:
    value = entry.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _mode_text(mode: int) -> str:
    return f"{mode:06o}" if mode else ""


def _is_regular(mode: int) -> bool:
    return bool(mode) and mode & _FILE_TYPE_MASK == _REGULAR_FILE


def _hunk_sides(hunks: Sequence[Any]) -> tuple[str, str] | None:
    """``(old_text, new_text)`` from recorded hunk bodies, or None when a body is missing."""

    old: list[str] = []
    new: list[str] = []
    for hunk in hunks:
        if not isinstance(hunk, Mapping):
            return None
        body = str(hunk.get("patch") or "")
        if not body:
            return None
        lines = body.split("\n")
        for index, line in enumerate(lines):
            if index + 1 < len(lines) and lines[index + 1].startswith("\\") and line[-1:] in {"<", ">", "="}:
                # The recorder writes libgit2's end-of-file-newline line as its
                # origin char plus its content, so that origin (`<`, `>` or `=`)
                # lands on the end of the line before the `\ No newline` marker.
                # A symlink blob never ends in a newline, so it always has one.
                line = line[:-1]
            if line.startswith("-"):
                old.append(line[1:])
            elif line.startswith("+"):
                new.append(line[1:])
            elif line.startswith(" "):
                old.append(line[1:])
                new.append(line[1:])
    return "\n".join(old).rstrip("\r"), "\n".join(new).rstrip("\r")


def _trust_area(resolved: str) -> str:
    lower = resolved.lower()
    name = lower.rsplit("/", 1)[-1]
    for prefix, label in _TRUST_PREFIXES:
        if lower.startswith(prefix) or f"/{prefix}" in f"/{lower}":
            return label
    if name in _TRUST_NAMES:
        return _TRUST_NAMES[name]
    if name == ".env" or name.startswith(".env.") or name.endswith(_SECRET_SUFFIXES):
        return "credentials"
    return ""


def symlink_boundary(link_path: str, target: str) -> dict[str, Any]:
    """Where a symlink at *link_path* pointing at *target* lands, relative to the repository.

    Pure path arithmetic over recorded strings: nothing is resolved on disk, so
    a chain of links is judged one hop at a time. An absolute or home-relative
    target, or one that climbs above the repository root, leaves the repository.
    """

    if not target:
        return {"resolved": "", "escapes_repository": False, "trust_area": ""}
    normalized = target.replace("\\", "/")
    if normalized.startswith(("/", "~")) or _WINDOWS_ABSOLUTE_RE.match(target):
        return {"resolved": target, "escapes_repository": True, "trust_area": _trust_area(normalized.lstrip("/~"))}
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(link_path), normalized))
    if resolved == ".." or resolved.startswith("../"):
        return {"resolved": resolved, "escapes_repository": True, "trust_area": _trust_area(resolved)}
    return {"resolved": resolved, "escapes_repository": False, "trust_area": _trust_area(resolved)}


def _symlink_change(entry: Mapping[str, Any], path: str, old_mode: int, new_mode: int) -> dict[str, Any]:
    status = str(entry.get("status") or "modified")
    hunks = entry.get("hunks") or ()
    sides = _hunk_sides(hunks) if isinstance(hunks, Sequence) and hunks else None
    recorded = sides is not None
    old_target, new_target = sides if sides is not None else ("", "")
    old_is_link = old_mode == SYMLINK_MODE
    new_is_link = new_mode == SYMLINK_MODE
    if not old_is_link:
        old_target = ""
    if not new_is_link:
        new_target = ""
    unchanged = not hunks and status in {"renamed", "copied"} and old_is_link and new_is_link
    link_path = path if new_is_link else str(entry.get("old_path") or path)
    judged = new_target if new_is_link else old_target
    boundary = symlink_boundary(link_path, judged)
    warnings: list[str] = []
    if boundary["escapes_repository"]:
        warnings.append(f"Symlink target leaves the repository: {boundary['resolved']}")
    if boundary["trust_area"]:
        warnings.append(f"Symlink target points into a trust-relevant path ({boundary['trust_area']}).")
    return {
        "kind": "symlink",
        "old_path": str(entry.get("old_path") or ""),
        "new_path": path,
        "old_type": "symlink" if old_is_link else ("file" if old_mode else "absent"),
        "new_type": "symlink" if new_is_link else ("file" if new_mode else "absent"),
        "old_target": old_target,
        "new_target": new_target,
        "target_recorded": recorded or unchanged,
        "target_unchanged": unchanged,
        "resolved_target": boundary["resolved"],
        "escapes_repository": boundary["escapes_repository"],
        "trust_area": boundary["trust_area"],
        "warnings": warnings,
    }


def submodule_repository_url(gitmodules: str, path: str) -> str:
    """The ``url`` that ``.gitmodules`` records for the submodule at *path*, or ""."""

    sections: list[dict[str, str]] = []
    for raw in gitmodules.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            sections.append({})
            continue
        if "=" not in line or not sections:
            continue
        key, value = line.split("=", 1)
        sections[-1][key.strip().lower()] = value.strip()
    for section in sections:
        if section.get("path", "").strip("/") == path.strip("/"):
            return section.get("url", "")
    return ""


def _submodule_change(entry: Mapping[str, Any], path: str, repository_url: str) -> dict[str, Any]:
    pointer = entry.get("submodule_pointer")
    old_commit = str(pointer[0] or "") if isinstance(pointer, (list, tuple)) and len(pointer) == 2 else ""
    new_commit = str(pointer[1] or "") if isinstance(pointer, (list, tuple)) and len(pointer) == 2 else ""
    change = "added" if not old_commit else "removed" if not new_commit else "updated"
    commit_url = ""
    compare_url = ""
    match = _GITHUB_REMOTE_RE.match(repository_url)
    if match is not None:
        # Only a host whose URL shape is known gets links; any other remote is
        # shown as recorded text and never turned into a guessed web address.
        base = f"https://github.com/{match.group('owner')}/{match.group('repo')}"
        if _COMMIT_RE.match(new_commit):
            commit_url = f"{base}/commit/{new_commit}"
        if _COMMIT_RE.match(old_commit) and _COMMIT_RE.match(new_commit):
            compare_url = f"{base}/compare/{old_commit}...{new_commit}"
    return {
        "kind": "submodule",
        "old_path": str(entry.get("old_path") or ""),
        "new_path": path,
        "change": change,
        "old_commit": old_commit,
        "new_commit": new_commit,
        "repository_url": repository_url,
        "commit_url": commit_url,
        "compare_url": compare_url,
    }


def special_git_change(entry: Mapping[str, Any], *, submodule_url: str = "") -> dict[str, Any] | None:
    """The semantic delta of a special Git change, or None for an ordinary text diff.

    ``kind`` is one of ``submodule``, ``symlink``, ``mode``, ``rename`` or
    ``metadata``. ``metadata`` is the honest answer for a content-free change
    whose per-file modes this packet did not record: it names what is known
    and never claims an executable bit moved.
    """

    path = str(entry.get("path") or "")
    status = str(entry.get("status") or "modified")
    if not path or entry.get("diff_omitted_reason"):
        return None
    old_mode = _mode(entry, "old_mode")
    new_mode = _mode(entry, "new_mode")
    if entry.get("submodule_pointer") is not None or GITLINK_MODE in {old_mode, new_mode}:
        return _submodule_change(entry, path, submodule_url)
    if SYMLINK_MODE in {old_mode, new_mode}:
        return _symlink_change(entry, path, old_mode, new_mode)
    hunks = entry.get("hunks") or ()
    if hunks or bool(entry.get("is_binary")) or status in {"added", "deleted"}:
        return None
    old_path = str(entry.get("old_path") or "")
    if _is_regular(old_mode) and _is_regular(new_mode) and old_mode != new_mode:
        old_exec = bool(old_mode & _EXECUTABLE_BITS)
        new_exec = bool(new_mode & _EXECUTABLE_BITS)
        return {
            "kind": "mode",
            "old_path": old_path if old_path != path else "",
            "new_path": path,
            "old_mode": _mode_text(old_mode),
            "new_mode": _mode_text(new_mode),
            "executable": (
                "added" if new_exec and not old_exec else "removed" if old_exec and not new_exec else "unchanged"
            ),
        }
    if status in {"renamed", "copied"} and old_path and old_path != path:
        return {
            "kind": "rename",
            "old_path": old_path,
            "new_path": path,
            "copied": status == "copied",
            "similarity": int(entry.get("similarity") or 0),
            "mode": _mode_text(new_mode),
        }
    return {
        "kind": "metadata",
        "old_path": old_path if old_path != path else "",
        "new_path": path,
        "old_mode": _mode_text(old_mode),
        "new_mode": _mode_text(new_mode),
        "modes_recorded": bool(old_mode and new_mode),
    }

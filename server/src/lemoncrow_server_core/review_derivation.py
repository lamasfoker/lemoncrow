"""Derive canonical Review packets from two exact source Views.

A Review client should not need the full Review engine just to publish a diff.
It already synchronizes exact immutable source Views; this module reconstructs
those bytes inside the server, builds the ordinary public Review packet there,
and rewrites only the descriptive range metadata back to the client's original
Git identity. The hosted server and the local engine both capture
``derive_packet`` requests through it.

The temporary Git repository is server-owned disposable state. No client-supplied
revision string is ever passed to Git: only synthetic commit ids created inside
that repository are used for diffing. This keeps revision parsing and shell
interpretation outside the trust boundary while reusing the one Review engine.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final, cast

from lemoncrow.pro.capabilities.review.gitdiff import RevRange
from lemoncrow.pro.capabilities.review.models import ChangedFile, FileStatus, RangeMode
from lemoncrow.pro.capabilities.review.ordering import rank_files
from lemoncrow.pro.capabilities.review.packet import (
    PacketBuild,
    attach_changed_symbols,
    build_review_packet_with_blobs,
)

from .errors import AgentAction, ErrorCode, ServerError
from .index.contracts import ContentStore, ManifestEntry, validate_path

__all__ = ["derive_review_packet_from_views", "gitlinks_from_wire", "review_range_from_wire"]

_ALLOWED_RANGE_MODES: Final[frozenset[str]] = frozenset({"commit_range", "working_tree", "staged"})
_MAX_RANGE_LABEL: Final[int] = 256
_MAX_TITLE: Final[int] = 1024
_GIT_ENV_KEYS: Final[dict[str, str]] = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "LemonCrow",
    "GIT_AUTHOR_EMAIL": "review@localhost",
    "GIT_COMMITTER_NAME": "LemonCrow",
    "GIT_COMMITTER_EMAIL": "review@localhost",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
}
#: The only server environment the scratch repository's git inherits: enough to
#: find and run git. An allowlist, not a denylist of ``GIT_*``: a server started
#: from a hook, an IDE task or ``git rebase --exec`` carries ``GIT_DIR``,
#: ``GIT_INDEX_FILE``, ``GIT_OBJECT_DIRECTORY``, ``GIT_CONFIG_PARAMETERS`` and
#: friends for its whole life, plus non-``GIT_`` inputs git also reads
#: (``XDG_CONFIG_HOME``, ``EMAIL``); any of them would point derivation at the
#: user's real repository or change what it writes.
_GIT_INHERITED_ENV: Final[tuple[str, ...]] = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "SYSTEMROOT")


def _wire_string(raw: Mapping[str, object], name: str, *, max_len: int, default: str = "") -> str:
    value = raw.get(name, default)
    if not isinstance(value, str) or len(value) > max_len:
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            f"review range {name} must be a string of at most {max_len} characters",
            details={"field": f"range.{name}"},
            action=AgentAction.FIX_REQUEST,
        )
    return value


def review_range_from_wire(raw: Mapping[str, object]) -> RevRange:
    """Validate the bounded descriptive Git range a thin client sends."""

    mode_raw = raw.get("mode")
    if not isinstance(mode_raw, str) or mode_raw not in _ALLOWED_RANGE_MODES:
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            "review range mode is invalid",
            details={"field": "range.mode"},
            action=AgentAction.FIX_REQUEST,
        )
    dirty = raw.get("dirty", mode_raw != "commit_range")
    if not isinstance(dirty, bool):
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            "review range dirty must be a boolean",
            details={"field": "range.dirty"},
            action=AgentAction.FIX_REQUEST,
        )
    return RevRange(
        mode=cast(RangeMode, mode_raw),
        base_rev=_wire_string(raw, "base_rev", max_len=_MAX_RANGE_LABEL),
        head_rev=_wire_string(raw, "head_rev", max_len=_MAX_RANGE_LABEL),
        base_sha=_wire_string(raw, "base_sha", max_len=_MAX_RANGE_LABEL),
        head_sha=_wire_string(raw, "head_sha", max_len=_MAX_RANGE_LABEL),
        merge_base_sha=_wire_string(raw, "merge_base_sha", max_len=_MAX_RANGE_LABEL),
        dirty=dirty,
        title=_wire_string(raw, "title", max_len=_MAX_TITLE),
    )


def gitlinks_from_wire(raw: Mapping[str, object]) -> tuple[dict[str, str], dict[str, str]]:
    """Validate the tiny gitlink metadata Views intentionally do not contain."""

    def side(name: str) -> dict[str, str]:
        value = raw.get(name, {})
        if not isinstance(value, Mapping):
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                f"review gitlinks {name} must be an object",
                details={"field": f"gitlinks.{name}"},
                action=AgentAction.FIX_REQUEST,
            )
        result: dict[str, str] = {}
        for path_raw, oid_raw in value.items():
            if not isinstance(path_raw, str) or not isinstance(oid_raw, str):
                raise ServerError(
                    ErrorCode.PAYLOAD_INVALID,
                    "review gitlinks must map repository paths to Git object ids",
                    details={"field": f"gitlinks.{name}"},
                    action=AgentAction.FIX_REQUEST,
                )
            path = validate_path(path_raw)
            oid = oid_raw.lower()
            if len(oid) not in {40, 64} or any(char not in "0123456789abcdef" for char in oid):
                raise ServerError(
                    ErrorCode.PAYLOAD_INVALID,
                    "review gitlink object id must be a full hexadecimal Git object id",
                    details={"field": f"gitlinks.{name}", "path": path},
                    action=AgentAction.FIX_REQUEST,
                )
            result[path] = oid
        return result

    return side("base"), side("head")


def _attach_gitlinks(build: PacketBuild, base: Mapping[str, str], head: Mapping[str, str]) -> PacketBuild:
    pointers: list[ChangedFile] = []
    for path in sorted(set(base) | set(head)):
        old_oid = base.get(path, "")
        new_oid = head.get(path, "")
        if old_oid == new_oid:
            continue
        status: FileStatus = "added" if not old_oid else "deleted" if not new_oid else "modified"
        pointers.append(
            ChangedFile(
                path=path,
                old_path=path if status == "deleted" else None,
                status=status,
                category="config",
                submodule_pointer=(old_oid, new_oid),
            )
        )
    if not pointers:
        return build
    packet = build.packet
    files = tuple(sorted((*packet.files, *pointers), key=lambda item: item.path))
    try:
        order = rank_files(
            files,
            packet.symbols,
            packet.impact,
            inspected=frozenset(packet.provenance.files_inspected),
        )
    except Exception:
        order = packet.order
    stats = dict(packet.stats)
    stats["files"] = len(files)
    return replace(build, packet=replace(packet, files=files, order=order, stats=stats))


def _git(repo: Path, *arguments: str) -> str:
    env = {name: os.environ[name] for name in _GIT_INHERITED_ENV if name in os.environ}
    env.update(_GIT_ENV_KEYS)
    env.update({"GIT_CONFIG_GLOBAL": os.devnull, "HOME": str(repo.parent)})
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        env=env,
        capture_output=True,
        timeout=30.0,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "git failed"
        raise ServerError(
            ErrorCode.INTERNAL,
            "server could not derive the Review diff from the committed source Views",
            details={"operation": arguments[0] if arguments else "git", "detail": detail[:256]},
        )
    return completed.stdout.strip()


def _clear_worktree(repo: Path) -> None:
    for child in repo.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)


def _write_membership(
    repo: Path,
    *,
    org_id: str,
    membership: Mapping[str, ManifestEntry],
    content: ContentStore,
) -> None:
    root = repo.resolve()
    for path, entry in sorted(membership.items()):
        # Manifest rows have already crossed the server path validator, but run
        # it again at the filesystem boundary: this helper must be safe if used
        # by a future call site that did not originate in ViewStore.
        relative = validate_path(path)
        target = (repo / relative).resolve()
        if target == root or root not in target.parents:
            raise ServerError(
                ErrorCode.PATH_NOT_ADDRESSABLE,
                "review source path escapes the synthetic repository",
                details={"path": relative},
                action=AgentAction.FIX_REQUEST,
            )
        payload = content.get(org_id, entry.content_digest)
        if payload is None:
            raise ServerError(
                ErrorCode.BLOB_MISSING,
                "a committed Review View references source bytes that are no longer available",
                details={"path": relative},
                action=AgentAction.USE_SYNC_PROTOCOL,
            )
        if hashlib.sha256(payload).hexdigest() != entry.content_digest:
            raise ServerError(
                ErrorCode.BLOB_DIGEST_MISMATCH,
                "stored Review source bytes do not match their committed digest",
                details={"path": relative},
                action=AgentAction.USE_SYNC_PROTOCOL,
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        target.chmod(0o755 if entry.mode & stat.S_IXUSR else 0o644)


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", message, "--no-gpg-sign")
    return _git(repo, "rev-parse", "HEAD")


def derive_review_packet_from_views(
    *,
    org_id: str,
    repo_id: str,
    content: ContentStore,
    base_membership: Mapping[str, ManifestEntry],
    head_membership: Mapping[str, ManifestEntry],
    range_spec: RevRange,
    scratch_root: Path,
    base_gitlinks: Mapping[str, str] | None = None,
    head_gitlinks: Mapping[str, str] | None = None,
    with_impact: bool = True,
    limit: int = 40,
    repo_root: str | None = None,
) -> PacketBuild:
    """Build the ordinary Review packet from exact old/new View membership.

    The client-provided range is descriptive only. The actual diff always uses
    synthetic commit ids created from bytes the caller already proved possession
    of through the View protocol. ``repo_root`` labels the packet's repository
    (it names no filesystem path); the hosted label ``hosted:{org_id}:{repo_id}``
    is the default.
    """

    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="derive-", dir=scratch_root) as temporary:
        temporary_root = Path(temporary)
        repo = temporary_root / "repo"
        repo.mkdir(mode=0o700)
        _git(repo, "init", "-q")
        _write_membership(repo, org_id=org_id, membership=base_membership, content=content)
        base_commit = _commit(repo, "base")

        _clear_worktree(repo)
        _write_membership(repo, org_id=org_id, membership=head_membership, content=content)

        if range_spec.mode == "staged":
            _git(repo, "add", "-A")
            synthetic = RevRange(
                mode="staged",
                base_rev="HEAD",
                head_rev="INDEX",
                base_sha=base_commit,
                head_sha="",
                dirty=True,
                title=range_spec.title,
            )
        elif range_spec.mode == "working_tree":
            synthetic = RevRange(
                mode="working_tree",
                base_rev="HEAD",
                head_rev="WORKDIR",
                base_sha=base_commit,
                head_sha="",
                dirty=True,
                title=range_spec.title,
            )
        else:
            head_commit = _commit(repo, "head")
            synthetic = RevRange(
                mode="commit_range",
                base_rev=base_commit,
                head_rev=head_commit,
                base_sha=base_commit,
                head_sha=head_commit,
                dirty=False,
                title=range_spec.title,
            )

        build = build_review_packet_with_blobs(
            repo,
            synthetic,
            store_root=temporary_root / "state",
            with_impact=with_impact,
            # Local agent/session provenance is not present in the server's
            # copy of the Views. A later thin metadata envelope may attach it,
            # but the server must not fabricate it from its own process state.
            with_provenance=False,
            limit=limit,
            with_patch_text=True,
            unbounded_patch_text=True,
        )
        if not with_impact:
            # Skipping the impact pass skips callers and sites, never the targets
            # themselves: a Snapshot names its changed symbols either way.
            build = attach_changed_symbols(repo, build, limit=limit)
        build = _attach_gitlinks(build, base_gitlinks or {}, head_gitlinks or {})
        packet = replace(
            build.packet,
            repo_root=repo_root if repo_root is not None else f"hosted:{org_id}:{repo_id}",
            range_mode=range_spec.mode,
            base_rev=range_spec.base_rev,
            head_rev=range_spec.head_rev,
            base_sha=range_spec.base_sha,
            head_sha=range_spec.head_sha,
            merge_base_sha=range_spec.merge_base_sha,
            dirty=range_spec.dirty,
            title=range_spec.title or build.packet.title,
        )
        return replace(build, packet=packet)

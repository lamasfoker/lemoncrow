"""Git-owned immutable base and a bounded working-tree overlay.

Committed bytes come from Git objects only when their digest is not cached or
the server requests them. Ordinary synchronization never walks clean files.
The existing walk cache owns digest persistence and its retention limits.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import LOCAL_FS_PROOF_PATH, REVIEW_CONFIG_PATH
from .errors import AgentAction, ClientError, ErrorCode
from .generation import _git as _generation_git
from .manifest import MANIFEST_FORMAT, ManifestEntry, profile_for_path, sha256_hex
from .walkcache import WalkCache

_READ_LIMIT = 64 * 1024 * 1024
_BATCH_BYTES = 8 * 1024 * 1024
_INTERNAL = frozenset({".git", ".claude", ".lc-worktrees", ".lemoncrow"})


def included(path: str) -> bool:
    return path != LOCAL_FS_PROOF_PATH and (path.split("/", 1)[0] not in _INTERNAL or path == REVIEW_CONFIG_PATH)


def _git(repo: Path, *args: str, required: bool = True) -> bytes:
    try:
        return _generation_git(repo, *args, required=required)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClientError(ErrorCode.PAYLOAD_INVALID, "Git could not prepare repository state", retryable=True) from exc


def head(repo: Path) -> str:
    return _git(repo, "rev-parse", "--verify", "HEAD^{commit}", required=False).decode("ascii").strip() or "0" * 40


@dataclass(frozen=True)
class GitBase:
    revision: str
    entries: Mapping[str, ManifestEntry]
    # digest -> (Git object id, size, parser profile), never working-tree paths.
    sources: Mapping[str, tuple[str, int, str]]
    hashed: int = 0


def object_batches(repo: Path, objects: Mapping[str, int]) -> Iterator[dict[str, bytes]]:
    """Read Git blobs in byte/count bounded batches, preserving exact bytes."""
    pending: list[tuple[str, int]] = []
    size = 0
    for oid, length in objects.items():
        if pending and (size + length > _BATCH_BYTES or len(pending) >= 256):
            yield _objects(repo, pending)
            pending, size = [], 0
        pending.append((oid, length))
        size += length
    if pending:
        yield _objects(repo, pending)


def _objects(repo: Path, objects: Sequence[tuple[str, int]]) -> dict[str, bytes]:
    try:
        result = subprocess.run(
            ["git", "cat-file", "--batch"],
            cwd=repo,
            input=b"".join(oid.encode("ascii") + b"\n" for oid, _size in objects),
            capture_output=True,
            check=False,
            timeout=60,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClientError(ErrorCode.PAYLOAD_INVALID, "Git could not supply committed bytes", retryable=True) from exc
    if result.returncode:
        raise ClientError(ErrorCode.PAYLOAD_INVALID, "Git could not supply the committed base")
    raw = result.stdout
    offset = 0
    blobs: dict[str, bytes] = {}
    for oid, length in objects:
        end = raw.find(b"\n", offset)
        expected = f"{oid} blob {length}".encode("ascii")
        if end < 0 or raw[offset:end] != expected:
            raise ClientError(ErrorCode.PAYLOAD_INVALID, "Git returned an invalid blob header")
        offset = end + 1
        data = raw[offset : offset + length]
        offset += length
        if len(data) != length or raw[offset : offset + 1] != b"\n":
            raise ClientError(ErrorCode.PAYLOAD_INVALID, "Git returned a truncated blob")
        offset += 1
        blobs[oid] = data
    if offset != len(raw):
        raise ClientError(ErrorCode.PAYLOAD_INVALID, "Git returned unexpected blob bytes")
    return blobs


def committed_base(repo: Path, cache: WalkCache, *, max_files: int) -> GitBase:
    revision = head(repo)
    rows: list[tuple[str, str, int, int]] = []
    if revision != "0" * 40:
        for record in _git(repo, "ls-tree", "-rlz", "--full-tree", revision).split(b"\0"):
            if not record:
                continue
            metadata, path_raw = record.split(b"\t", 1)
            mode_raw, kind, oid_raw, size_raw = metadata.split()
            path = os.fsdecode(path_raw)
            if kind != b"blob" or int(mode_raw, 8) not in {0o100644, 0o100755} or not included(path):
                continue
            rows.append((path, oid_raw.decode("ascii"), int(mode_raw, 8), int(size_raw)))
    if len(rows) > max_files:
        raise ClientError(ErrorCode.REQUEST_TOO_LARGE, "committed base exceeds the file limit")
    missing = {oid: size for _path, oid, _mode, size in rows if oid not in cache.by_blob and size <= _READ_LIMIT}
    for batch in object_batches(repo, missing):
        for oid, data in batch.items():
            cache.fresh_blob[oid] = sha256_hex(data)
    digests = {**cache.by_blob, **cache.fresh_blob}
    entries: dict[str, ManifestEntry] = {}
    sources: dict[str, tuple[str, int, str]] = {}
    for path, oid, mode, size in rows:
        digest = (
            digests[oid] if size <= _READ_LIMIT else sha256_hex(f"{MANIFEST_FORMAT}:oversize:{path}:{size}".encode())
        )
        profile = profile_for_path(path)
        entries[path] = ManifestEntry(path, digest, profile, mode, size)
        if size <= _READ_LIMIT:
            sources[digest] = (oid, size, profile)
    if head(repo) != revision:
        raise ClientError(ErrorCode.VIEW_REVISION_STALE, "HEAD changed while preparing the base", retryable=True)
    return GitBase(revision, entries, sources, len(missing))


def dirty_paths(repo: Path, *, max_files: int) -> frozenset[str]:
    """Git enumerates individual staged, unstaged and unignored untracked paths."""
    raw = _git(
        repo,
        "-c",
        "status.renames=false",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=all",
    )
    paths: set[str] = set()
    for record in raw.split(b"\0"):
        if not record:
            continue
        status = record[:2]
        path = os.fsdecode(record[3:])
        if not included(path):
            continue
        if b"U" in status or status in {b"AA", b"DD"}:
            raise ClientError(
                ErrorCode.PAYLOAD_INVALID,
                "resolve Git conflicts before synchronizing",
                details={"path": path},
                action=AgentAction.FIX_REQUEST,
            )
        paths.add(path)
        if len(paths) > max_files:
            raise ClientError(ErrorCode.REQUEST_TOO_LARGE, "working-tree overlay exceeds the file limit")
    return frozenset(paths)


def overlay_entries(repo: Path, paths: Sequence[str]) -> dict[str, ManifestEntry]:
    """Read only selected regular files; refuse concurrent edits or unreadable bytes."""
    try:
        return _overlay_entries(repo, paths)
    except OSError as exc:
        raise ClientError(
            ErrorCode.VIEW_REVISION_STALE, "overlay changed or could not be read", retryable=True
        ) from exc


def _overlay_entries(repo: Path, paths: Sequence[str]) -> dict[str, ManifestEntry]:
    entries: dict[str, ManifestEntry] = {}
    for path in paths:
        if not included(path):
            continue
        target = repo / path
        # A changed directory replaced by a symlink must never disclose outside files.
        if any(parent.is_symlink() for parent in target.parents if parent != repo and repo in parent.parents):
            continue
        try:
            before = target.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(before.st_mode):
            continue
        if before.st_size > _READ_LIMIT:
            digest = sha256_hex(f"{MANIFEST_FORMAT}:oversize:{path}:{before.st_size}".encode())
        else:
            hasher = hashlib.sha256()
            with target.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    hasher.update(block)
            digest = hasher.hexdigest()
        after = target.stat(follow_symlinks=False)
        if any(
            getattr(before, field) != getattr(after, field)
            for field in ("st_size", "st_mtime_ns", "st_ctime_ns", "st_ino", "st_dev", "st_mode")
        ):
            raise ClientError(
                ErrorCode.VIEW_REVISION_STALE,
                "file changed while synchronizing",
                details={"path": path},
                retryable=True,
            )
        entries[path] = ManifestEntry(
            path,
            digest,
            profile_for_path(path),
            0o100755 if before.st_mode & stat.S_IXUSR else 0o100644,
            before.st_size,
        )
    return entries

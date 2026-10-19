"""Standalone Review's Git-backed content-addressed snapshot builder and cursor."""

from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows has no POSIX advisory lock. Fail closed instead of publishing unsafely.
    fcntl = None  # type: ignore[assignment]
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType

from .errors import ClientError, ErrorCode
from .git_sync import _git, _objects, dirty_paths, object_batches
from .manifest import ManifestEntry, profile_for_path, sha256_hex

_MAX_FILES = 200_000
_MAX_BLOB = 64 * 1024 * 1024
_PRIVATE = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    rb"(?i:aws_secret_access_key|openai_api_key|anthropic_api_key|github_token|stripe_secret_key)\s*[:=]\s*['\"]?[A-Za-z0-9/+_=\-]{16,}|"
    rb"(?:AKIA|ASIA)[0-9A-Z]{16}|"
    rb"gh[pousr]_[A-Za-z0-9_]{20,}|"
    rb"github_pat_[A-Za-z0-9_]{20,}|"
    rb"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"
)
_DIGEST = re.compile("[0-9a-f]{64}\\Z")
_OID = re.compile("[0-9a-f]{40,64}\\Z")
# Untracked bytes have not been explicitly approved by Git. Only source-like
# filenames are eligible by default; unusual/binary paths need an exact
# operator-provided per-process allowlist (never broad globs or directories).
_UNTRACKED_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".go",
        ".rs",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".mjs",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".java",
        ".kt",
        ".swift",
        ".rb",
        ".php",
        ".sh",
        ".sql",
        ".proto",
        ".graphql",
        ".md",
        ".mdx",
        ".txt",
        ".json",
        ".jsonl",
        ".yaml",
        ".yml",
        ".toml",
        ".xml",
        ".html",
        ".css",
        ".scss",
        ".svg",
        ".ipynb",
        ".gitignore",
        ".dockerignore",
    }
)
_UNTRACKED_NAMES = frozenset(
    {
        "Dockerfile",
        "Makefile",
        "LICENSE",
        "README",
        "CMakeLists.txt",
        ".gitignore",
        ".dockerignore",
        ".editorconfig",
        ".env.example",
        ".env.sample",
        ".env.template",
    }
)


def _untracked_allowed(path: str) -> bool:
    if not _safe(path):
        return False
    name = path.rsplit("/", 1)[-1]
    if name in _UNTRACKED_NAMES or Path(name).suffix.lower() in _UNTRACKED_EXTENSIONS:
        return True
    # An operator may approve a *specific* odd source path (for example a
    # generated test .bin); this never weakens the sensitive-path/secret gate.
    explicitly = os.environ.get("LEMONCROW_REVIEW_UNTRACKED_ALLOW", "").split(",")
    return path in {value.strip() for value in explicitly if value.strip() and _safe(value.strip())}


def _safe(path: str) -> bool:
    parts = path.lower().split("/")
    name = parts[-1]
    return (
        bool(path)
        and not path.startswith("/")
        and ".." not in parts
        and "\x00" not in path
        and not any(
            part in {".git", ".claude", ".lc-worktrees", ".lemoncrow", ".ssh", ".aws", ".gnupg", ".kube", ".terraform"}
            for part in parts
        )
        and name
        not in {
            ".env",
            ".npmrc",
            ".pypirc",
            ".netrc",
            ".git-credentials",
            "id_rsa",
            "id_ed25519",
            "credentials.json",
            "secrets.json",
            "secrets.yaml",
            "secrets.yml",
            "terraform.tfstate",
        }
        and (not name.startswith(".env.") or name in {".env.example", ".env.sample", ".env.template"})
        and not name.endswith((".pem", ".key", ".p12", ".pfx", ".kdbx", ".tfstate", ".tfstate.backup"))
    )


def _protect(data: bytes, path: str) -> None:
    if _PRIVATE.search(data):
        raise ClientError(ErrorCode.PAYLOAD_INVALID, f"Review refuses private key content in: {path}")


def _stale() -> ClientError:
    return ClientError(ErrorCode.VIEW_REVISION_STALE, "source changed during Review delta capture", retryable=True)


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_dev, info.st_mode)


def _read_regular(repo: Path, path: str) -> tuple[bytes, os.stat_result]:
    """Read through no-follow directory FDs, not a previously checked pathname.

    Opening a Path after lstat permits a concurrent symlink replacement to
    redirect the read outside the repository. O_NONBLOCK also rejects a raced
    FIFO without hanging the foreground Review.
    """
    parts = path.split("/")
    if not _safe(path) or any(part in {"", ".", ".."} for part in parts):
        raise ClientError(ErrorCode.PAYLOAD_INVALID, f"Review refuses unsafe source path: {path}")
    if not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_CLOEXEC", "O_DIRECTORY")):
        raise ClientError(
            ErrorCode.CAPABILITY_UNSUPPORTED, "secure Review delta requires POSIX no-follow file descriptors"
        )
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(repo, flags | os.O_DIRECTORY))
        for part in parts[:-1]:
            descriptors.append(os.open(part, flags | os.O_DIRECTORY, dir_fd=descriptors[-1]))
        fd = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=descriptors[-1])
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise _stale()
        if before.st_size > _MAX_BLOB:
            raise ClientError(ErrorCode.REQUEST_TOO_LARGE, f"Review file exceeds 64 MiB: {path}")
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(_MAX_BLOB + 1)
        after = os.fstat(fd)
        if len(data) != before.st_size or _identity(before) != _identity(after):
            raise _stale()
        current = (repo / path).lstat()
        if _identity(current) != _identity(before):
            raise _stale()
        return data, before
    except (OSError, ValueError) as exc:
        raise _stale() from exc
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


class PublisherLock:
    """One foreground Review publisher per canonical worktree, across processes.

    Hold the *same inode* for the entire capture loop, including HTTPS publish.
    Never unlink the lock on release: unlinking permits another process to
    create a new inode while an earlier process still owns the original lock.
    No Runtime daemon, server process or agent session is required.
    """

    def __init__(self, repo: Path, state_dir: Path) -> None:
        self.repo = repo.resolve(strict=True)
        self.state_dir = state_dir
        identity = hashlib.sha256(os.fsencode(str(self.repo))).hexdigest()[:32]
        self.path = state_dir / "review-publishers" / f"{identity}.lock"
        self._fd: int | None = None

    def acquire(self) -> None:
        if fcntl is None:
            raise ClientError(
                ErrorCode.CAPABILITY_UNSUPPORTED, "Review capture requires a qualified POSIX publisher lock"
            )
        if self._fd is not None:
            raise RuntimeError("Review publisher lock is already held")
        if self.state_dir.is_symlink():
            raise ClientError(ErrorCode.PAYLOAD_INVALID, "Review state directory cannot be a symlink")
        folder = self.path.parent
        if folder.is_symlink():
            raise ClientError(ErrorCode.PAYLOAD_INVALID, "Review publisher directory cannot be a symlink")
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = folder.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            raise ClientError(
                ErrorCode.PAYLOAD_INVALID, "Review publisher state needs owner-only directory permissions"
            )
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(self.path, flags, 0o600)
        try:
            record = os.fstat(fd)
            if not stat.S_ISREG(record.st_mode) or record.st_mode & 0o077:
                raise ClientError(ErrorCode.PAYLOAD_INVALID, "Review publisher lock file needs owner-only permissions")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ClientError(
                    ErrorCode.VIEW_REVISION_STALE,
                    "another Review publisher already owns this worktree; stop it before starting a second",
                ) from exc
            self._fd = fd
        finally:
            if self._fd != fd:
                os.close(fd)

    def close(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is not None:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def __enter__(self) -> PublisherLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


@dataclass
class DeltaCursor:
    """Owner-only cache of immutable Git OID→SHA256; never a substitute for server ACK."""

    repo: Path
    state_dir: Path
    known: dict[str, tuple[str, int]] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        key = hashlib.sha256(str(self.repo.resolve()).encode("utf-8", "surrogatepass")).hexdigest()[:32]
        return self.state_dir / "delta-cursors" / f"{key}.json"

    @classmethod
    def load(cls, repo: Path, state_dir: Path) -> DeltaCursor:
        cursor = cls(repo.resolve(), state_dir)
        try:
            info = cursor.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 20_000_000:
                return cursor
            raw = json.loads(cursor.path.read_text(encoding="utf-8"))
            if raw.get("format") != "review-delta/1" or raw.get("repo") != str(cursor.repo):
                return cursor
            records = raw["objects"]
            if not isinstance(records, dict) or len(records) > _MAX_FILES:
                return cursor
            for oid, pair in records.items():
                if (
                    isinstance(oid, str)
                    and _OID.fullmatch(oid)
                    and isinstance(pair, list)
                    and len(pair) == 2
                    and isinstance(pair[0], str)
                    and _DIGEST.fullmatch(pair[0])
                    and type(pair[1]) is int
                    and 0 <= pair[1] <= _MAX_BLOB
                ):
                    cursor.known[oid] = (pair[0], pair[1])
        except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeError):
            pass
        return cursor

    def save(self) -> None:
        directory = self.path.parent
        if directory.is_symlink() or self.path.is_symlink():
            raise OSError("Review cursor must not be a symlink")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        raw = {
            "format": "review-delta/1",
            "repo": str(self.repo),
            "objects": {oid: list(pair) for oid, pair in list(self.known.items())[-_MAX_FILES:]},
        }
        fd, name = tempfile.mkstemp(dir=directory, prefix=".cursor-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(raw, stream, separators=(",", ":"), sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


@dataclass
class DeltaSnapshot:
    entries: dict[str, ManifestEntry]
    objects: dict[str, tuple[str, int, str]]
    changed: dict[str, str]
    repo: Path

    def payload(self, digest: str) -> tuple[bytes, str]:
        if digest in self.changed:
            path = self.changed[digest]
            data, _info = _read_regular(self.repo, path)
        elif digest in self.objects:
            oid, size, path = self.objects[digest]
            data = _objects(self.repo, [(oid, size)])[oid]
        else:
            raise ClientError(ErrorCode.MANIFEST_INCOMPLETE, "server requested absent Review source")
        if len(data) > _MAX_BLOB or sha256_hex(data) != digest:
            raise _stale()
        _protect(data, path)
        return data, profile_for_path(path)


def _rows(repo: Path, source: str, cursor: DeltaCursor) -> list[tuple[str, str, int, int]]:
    if source == ":index:":
        records = []
        for record in _git(repo, "ls-files", "--stage", "-z").split(b"\0"):
            if not record:
                continue
            meta, raw_path = record.split(b"\t", 1)
            mode, oid, stage = meta.decode("ascii").split()
            path = os.fsdecode(raw_path)
            if path.split("/", 1)[0] in {".git", ".claude", ".lc-worktrees", ".lemoncrow"}:
                continue
            if stage != "0":
                raise ClientError(ErrorCode.PAYLOAD_INVALID, "resolve Git conflicts before Review")
            if mode == "120000":
                raise ClientError(ErrorCode.PAYLOAD_INVALID, "Review refuses symlinks")
            if mode in {"100644", "100755"}:
                records.append((path, oid, int(mode, 8)))
        unknown = {oid for _, oid, _ in records if oid not in cursor.known}
        sizes = {oid: pair[1] for oid, pair in cursor.known.items()}
        if unknown:
            try:
                result = subprocess.run(
                    ["git", "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
                    cwd=repo,
                    input="\n".join(sorted(unknown)) + "\n",
                    text=True,
                    capture_output=True,
                    timeout=60,
                    check=True,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise _stale() from exc
            for line in result.stdout.splitlines():
                oid, kind, size = line.split()
                if oid not in unknown or kind != "blob":
                    raise _stale()
                sizes[oid] = int(size)
        if any(oid not in sizes for _, oid, _ in records):
            raise _stale()
        return [(path, oid, mode, sizes[oid]) for path, oid, mode in records]
    if not source:
        return []
    rows = []
    for record in _git(repo, "ls-tree", "-rlz", "--full-tree", source).split(b"\0"):
        if not record:
            continue
        meta, raw_path = record.split(b"\t", 1)
        mode, kind, oid, size = meta.decode("ascii").split()
        path = os.fsdecode(raw_path)
        if path.split("/", 1)[0] in {".git", ".claude", ".lc-worktrees", ".lemoncrow"}:
            continue
        if mode == "120000":
            raise ClientError(ErrorCode.PAYLOAD_INVALID, "Review refuses symlinks")
        if kind == "blob" and mode in {"100644", "100755"}:
            rows.append((path, oid, int(mode, 8), int(size)))
    return rows


def snapshot(repo: Path, source: str, cursor: DeltaCursor, *, working_tree: bool = False) -> DeltaSnapshot:
    """Git tracks clean bytes; only staged/dirty overlays need new hashes."""
    repo = repo.resolve()
    rows = _rows(repo, source, cursor)
    if len(rows) > _MAX_FILES:
        raise ClientError(ErrorCode.REQUEST_TOO_LARGE, "Review exceeds 200k files")
    pending: dict[str, tuple[str, int]] = {}
    for path, oid, _mode, size in rows:
        if not _safe(path):
            raise ClientError(ErrorCode.PAYLOAD_INVALID, f"Review refuses sensitive source path: {path}")
        if size > _MAX_BLOB:
            raise ClientError(ErrorCode.REQUEST_TOO_LARGE, f"Review file exceeds 64 MiB: {path}")
        if oid not in cursor.known or cursor.known[oid][1] != size:
            pending[oid] = (path, size)
    for batch in object_batches(repo, {oid: item[1] for oid, item in pending.items()}):
        for oid, data in batch.items():
            _protect(data, pending[oid][0])
            cursor.known[oid] = (sha256_hex(data), len(data))
    entries: dict[str, ManifestEntry] = {}
    objects: dict[str, tuple[str, int, str]] = {}
    for path, oid, mode, size in rows:
        digest = cursor.known[oid][0]
        entries[path] = ManifestEntry(path, digest, profile_for_path(path), mode, size)
        objects[digest] = (oid, size, path)
    changed: dict[str, str] = {}
    if working_tree:
        dirty = dirty_paths(repo, max_files=_MAX_FILES)
        untracked = {
            os.fsdecode(path)
            for path in _git(repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
            if path
        }
        for path in sorted(untracked & dirty):
            if not _untracked_allowed(path):
                raise ClientError(
                    ErrorCode.PAYLOAD_INVALID,
                    f"Review untracked source requires explicit path allowlist: {path}",
                )
        for path in dirty:
            if not _safe(path):
                raise ClientError(ErrorCode.PAYLOAD_INVALID, f"Review refuses sensitive source path: {path}")
            candidate = repo / path
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                info = None
            if info is not None and (
                not candidate.resolve().is_relative_to(repo)
                or stat.S_ISLNK(info.st_mode)
                or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
            ):
                raise ClientError(ErrorCode.PAYLOAD_INVALID, f"Review refuses special source path: {path}")
            entries.pop(path, None)
        for path in sorted(dirty):
            candidate = repo / path
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(info.st_mode):
                continue
            data, before = _read_regular(repo, path)
            _protect(data, path)  # Reject secrets before announcing the manifest.
            entry = ManifestEntry(
                path,
                sha256_hex(data),
                profile_for_path(path),
                0o100755 if before.st_mode & stat.S_IXUSR else 0o100644,
                len(data),
            )
            entries[path] = entry
            changed[entry.content_digest] = path
    return DeltaSnapshot(entries, objects, changed, repo)

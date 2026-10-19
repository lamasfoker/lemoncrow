"""Ordered file locks shared by editing runtime features.

``workspace_path_locks`` are process-local thread locks. ``cross_process_path_locks``
adds advisory OS locks (``flock`` on POSIX, ``msvcrt.locking`` on Windows) on lock
files under a store directory, so every *cooperating* process that names the same
store -- Verified Local Work promotion and Undo, wherever they run -- excludes the
others on the same live paths.

Platform limitation: these are advisory locks. A writer that does not take them
(the host agent's native Edit, the user's editor, ``git checkout``) is not held
out; callers keep their own content-hash checks for that case. Acquisition never
waits forever: past ``timeout_s`` it fails closed with :class:`PathLockUnavailable`
and the caller refuses the operation rather than writing unlocked.
"""

from __future__ import annotations

import errno
import hashlib
import os
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import IO, Any

_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()

#: Fixed number of lock files per store; paths hash onto them, so the lock
#: directory never grows and needs no cleanup.
LOCK_STRIPES = 256
#: Lock files live here, relative to the store root a caller passes.
LOCK_DIR_NAME = "workspace_path_locks"


class PathLockUnavailable(RuntimeError):
    """The path locks could not be taken; the caller must not write.

    ``reason`` is ``path_locked`` (another holder past the deadline) or
    ``path_lock_failed`` (the lock files could not be opened).
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def _path_key(path: Path) -> str:
    return str(path.expanduser().resolve())


def workspace_path_locks(paths: list[Path]) -> list[threading.Lock]:
    """Return stable locks for paths, ordered by resolved path to avoid deadlock."""
    keys = sorted({_path_key(path) for path in paths})
    with _PATH_LOCKS_GUARD:
        return [_PATH_LOCKS.setdefault(key, threading.Lock()) for key in keys]


def path_lock_stripe(path: Path) -> int:
    """The lock-file stripe a path hashes onto (stable across processes)."""
    digest = hashlib.sha256(_path_key(path).encode("utf-8", "surrogatepass")).digest()
    return int.from_bytes(digest[:4], "big") % LOCK_STRIPES


#: ``msvcrt.locking(LK_NBLCK)`` reports a byte another handle holds as EACCES (or
#: EDEADLOCK, the CRT's name for EDEADLK); any other errno is a real failure.
_WINDOWS_CONTENDED = frozenset({errno.EACCES, errno.EDEADLK, getattr(errno, "EDEADLOCK", errno.EDEADLK)})


def _try_os_lock(handle: IO[bytes]) -> bool:
    """Take the lock without waiting: ``True`` taken, ``False`` held elsewhere.

    Any other OS error propagates, and the caller reports ``path_lock_failed``.
    """
    if os.name == "nt":
        import msvcrt

        api: Any = msvcrt
        handle.seek(0)
        try:
            api.locking(handle.fileno(), api.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in _WINDOWS_CONTENDED:
                return False
            raise
        return True
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _os_unlock(handle: IO[bytes]) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            api: Any = msvcrt
            handle.seek(0)
            api.locking(handle.fileno(), api.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass  # closing the handle releases the lock as well


@contextmanager
def cross_process_path_locks(
    paths: Iterable[Path],
    *,
    store_root: Path,
    timeout_s: float = 10.0,
    poll_s: float = 0.02,
) -> Iterator[None]:
    """Hold every path's lock in this process and across processes, or raise.

    Order is fixed to stay deadlock-free: this process's thread locks by
    resolved path first, then the stripe lock files by stripe number. Raises
    :class:`PathLockUnavailable` (fail closed) when ``timeout_s`` passes first.
    """
    keys = sorted({_path_key(path) for path in paths})
    deadline = time.monotonic() + max(0.0, timeout_s)
    lock_dir = Path(store_root).expanduser() / LOCK_DIR_NAME
    with ExitStack() as stack:
        for lock in workspace_path_locks([Path(key) for key in keys]):
            if not lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
                raise PathLockUnavailable("path_locked", "another writer in this process holds the path")
            stack.callback(lock.release)
        # lc-debt: paths share LOCK_STRIPES lock files, so two unrelated paths on one
        # stripe serialize (false sharing, bounded by the timeout). Upgrade: byte-range
        # locks (fcntl.lockf) on one file keyed by the full path hash.
        stripes = sorted({path_lock_stripe(Path(key)) for key in keys})
        try:
            lock_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PathLockUnavailable("path_lock_failed", f"cannot create {lock_dir}: {exc}") from exc
        for stripe in stripes:
            lock_path = lock_dir / f"stripe-{stripe:03d}.lock"
            try:
                handle = lock_path.open("a+b")
            except OSError as exc:
                raise PathLockUnavailable("path_lock_failed", f"cannot open {lock_path}: {exc}") from exc
            stack.callback(handle.close)
            if os.name == "nt":
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
            while True:
                try:
                    if _try_os_lock(handle):
                        break
                except OSError as exc:  # e.g. ENOLCK on a filesystem without locks
                    raise PathLockUnavailable("path_lock_failed", f"cannot lock {lock_path}: {exc}") from exc
                if time.monotonic() >= deadline:
                    raise PathLockUnavailable("path_locked", f"another process holds {lock_path.name}")
                time.sleep(poll_s)
            stack.callback(_os_unlock, handle)
        yield


__all__ = [
    "LOCK_DIR_NAME",
    "LOCK_STRIPES",
    "PathLockUnavailable",
    "cross_process_path_locks",
    "path_lock_stripe",
    "workspace_path_locks",
]

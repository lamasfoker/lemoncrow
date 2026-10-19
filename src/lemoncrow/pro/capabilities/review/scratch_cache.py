"""One closer for Review's derived scratch caches.

Every directory these helpers manage is a cache. A Snapshot's exact bytes live
in the Review store (base commit + frozen changed files), so any materialized
tree, build workspace, or rendered output can be rebuilt on demand. Entries are
content-keyed directories touched on every use; the closer bounds them by count
and by idle time.

Deletion never blocks a request: an entry is renamed into a ``.trash-*`` sibling
(atomic, same filesystem) and removed by one background worker. Trash a crashed
process left behind is swept by the next prune of that directory.
"""

from __future__ import annotations

import os
import queue
import secrets
import shutil
import threading
import time
from pathlib import Path

# Whole source trees and build workspaces: cheap to rebuild, expensive to keep.
HEAVY_TTL_SECONDS = 3 * 3600
# Slim results (static build output, screenshots, rendered artifacts) that are
# slow to rebuild but small: keep them for revisits across a few days.
LIGHT_TTL_SECONDS = 3 * 24 * 3600
# An entry used this recently may still be read by a running build or surface,
# so the count cap alone never removes it.
IDLE_GRACE_SECONDS = 3600

TRASH_PREFIX = ".trash-"

_QUEUE: queue.Queue[Path] = queue.Queue()
_PENDING: set[str] = set()
_GUARD = threading.Lock()
_WORKER: threading.Thread | None = None


def _reset_after_fork() -> None:
    # A forked child inherits the queue but not the worker thread.
    global _QUEUE, _PENDING, _GUARD, _WORKER
    _QUEUE = queue.Queue()
    _PENDING = set()
    _GUARD = threading.Lock()
    _WORKER = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


def _drain() -> None:
    while True:
        path = _QUEUE.get()
        try:
            shutil.rmtree(path, ignore_errors=True)
        finally:
            with _GUARD:
                _PENDING.discard(str(path))
            _QUEUE.task_done()


def _enqueue(path: Path) -> None:
    global _WORKER
    with _GUARD:
        if str(path) in _PENDING:
            return
        _PENDING.add(str(path))
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_drain, name="lemoncrow-review-cache-trash", daemon=True)
            _WORKER.start()
    _QUEUE.put(path)


def wait_for_retired() -> None:
    """Block until every retired tree is gone (tests and orderly shutdown)."""

    _QUEUE.join()


def touch_cache_entry(path: Path) -> None:
    """Mark a cache entry as recently used (the closer reads its mtime)."""

    try:
        path.mkdir(parents=True, exist_ok=True)
        os.utime(path)
    except OSError:
        pass


def retire_tree(path: Path, *, trash_dir: Path | None = None) -> None:
    """Remove *path* now from the caller's point of view, without waiting on ``rmtree``.

    A directory is renamed to ``.trash-*`` inside *trash_dir* (default: its own
    parent, which must be on the same filesystem) and deleted in the background.
    Files and symlinks are simply unlinked.
    """

    try:
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
            return
    except OSError:
        return
    trash = (trash_dir or path.parent) / f"{TRASH_PREFIX}{secrets.token_hex(6)}"
    try:
        trash.parent.mkdir(parents=True, exist_ok=True)
        os.rename(path, trash)
    except FileNotFoundError:
        return
    except OSError:
        shutil.rmtree(path, ignore_errors=True)
        return
    _enqueue(trash)


def prune_cache_entries(
    entries_root: Path,
    keep: Path | None,
    *,
    keep_count: int,
    idle_seconds: float,
    ttl_seconds: float | None = None,
) -> None:
    """Retire cache entries under *entries_root*.

    An entry goes when it is beyond the *keep_count* most recently used ones
    (counting *keep*) and idle for *idle_seconds*, or when it has been idle for
    *ttl_seconds* regardless of rank. *keep* (the entry in use) always stays.
    """

    try:
        children = list(entries_root.iterdir())
    except OSError:
        return
    rows: list[tuple[float, Path]] = []
    for item in children:
        if item.name.startswith(TRASH_PREFIX):
            if item.is_dir() and not item.is_symlink():
                _enqueue(item)  # left behind by a process that exited mid-delete
            continue
        try:
            if item == keep or item.is_symlink() or not item.is_dir():
                continue
            rows.append((item.stat().st_mtime, item))
        except OSError:
            continue
    rows.sort(key=lambda row: row[0], reverse=True)
    now = time.time()
    for index, (mtime, item) in enumerate(rows):
        idle = now - mtime
        over_cap = index >= max(0, keep_count - 1)
        if (over_cap and idle >= idle_seconds) or (ttl_seconds is not None and idle >= ttl_seconds):
            retire_tree(item)


__all__ = [
    "HEAVY_TTL_SECONDS",
    "IDLE_GRACE_SECONDS",
    "LIGHT_TTL_SECONDS",
    "prune_cache_entries",
    "retire_tree",
    "touch_cache_entry",
    "wait_for_retired",
]

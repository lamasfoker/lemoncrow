"""Bounded, disposable source-only Review preparation.

Each workspace retains one source packet and one Git base tree. New generations
replace old entries; entries older than a day are rebuilt on their next use,
and a workspace no capture has written for a day (a deleted worktree) is swept.
Provenance, verification evidence and mutable graph results are never cached.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .gitdiff import BlobPair, RevRange
from .live_runtime import _cross_process_lock
from .models import SCHEMA_VERSION, review_packet_from_dict
from .packet import PacketBuild

_VERSION = 1
_MAX_PACKET_BYTES = 32 * 1024 * 1024
_MAX_AGE_S = 24 * 60 * 60
# Bounded lock striping keeps unrelated workspaces independent in the usual case.
_LOCKS = tuple(threading.RLock() for _ in range(32))
_SLOT_NAME = re.compile(r"[0-9a-f]{64}")


def _slots(store_root: Path) -> Path:
    return store_root.expanduser().resolve() / "review" / "precompute"


def _root(store_root: Path, repo_root: Path) -> Path:
    key = hashlib.sha256(os.fsencode(repo_root.resolve())).hexdigest()
    return _slots(store_root) / key


def _stripe(root: Path) -> threading.RLock:
    return _LOCKS[int(root.name[:8], 16) % len(_LOCKS)]


def _newest_write(slot: Path, *, with_directory: bool) -> float:
    """When anything in *slot* was last written, ignoring its lock file."""
    newest = slot.lstat().st_mtime if with_directory else 0.0
    with os.scandir(slot) as entries:
        for entry in entries:
            if entry.name != "base.lock":
                newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
    with contextlib.suppress(FileNotFoundError):
        newest = max(newest, (slot / "base" / "identity.json").lstat().st_mtime)
    return newest


def _sweep_slot(slot: Path) -> None:
    deadline = time.time() - _MAX_AGE_S
    if _newest_write(slot, with_directory=True) > deadline:
        return
    lock_path = slot / "base.lock"
    # A busy slot raises BlockingIOError; a later capture sweeps it.
    with _cross_process_lock(lock_path, _stripe(slot), blocking=False):
        # No reader holds the base tree now. Re-check for a writer, ignoring the
        # directory's own time, which opening the lock file may just have set.
        if _newest_write(slot, with_directory=False) > deadline:
            return
        for entry in slot.iterdir():
            if entry.name == "base.lock":
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        if os.name != "nt":
            # A waiter that wins this deleted lock file retries on the path.
            lock_path.unlink()
            slot.rmdir()
    if os.name == "nt":
        # Windows refuses both while another process holds the lock file open.
        lock_path.unlink()
        slot.rmdir()


def sweep_stale_slots(store_root: Path, *, keep: Path | None = None) -> None:
    """Delete every other workspace's slot that nothing has written for a day.

    Such a slot would be rebuilt on its next use anyway, so sweeping it never
    costs a cache hit; it is what bounds the cache once a workspace stops
    capturing. A slot in use is skipped, and a failure never fails capture.
    """
    try:
        with os.scandir(_slots(store_root)) as entries:
            slots = [
                Path(entry.path)
                for entry in entries
                if _SLOT_NAME.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)
            ]
    except OSError:
        return
    for slot in slots:
        if slot != keep:
            with contextlib.suppress(OSError):
                _sweep_slot(slot)


def _identity(repo_root: Path, rng: RevRange, fingerprint: str) -> dict[str, Any]:
    return {
        "version": _VERSION,
        "schema": SCHEMA_VERSION,
        "repo": str(repo_root.resolve()),
        # JSON round-trips tuples as lists; compare in the stored form.
        "range": {**asdict(rng), "paths": list(rng.paths)},
        "source": fingerprint,
    }


def load_source_build(store_root: Path, repo_root: Path, rng: RevRange, fingerprint: str) -> PacketBuild | None:
    """Load only an exact source generation, falling back on any invalid entry."""
    path = _root(store_root, repo_root) / "source.json.gz"
    try:
        if not fingerprint or time.time() - path.stat().st_mtime > _MAX_AGE_S:
            return None
        with gzip.open(path, "rb") as handle:
            raw = handle.read(_MAX_PACKET_BYTES + 1)
        if len(raw) > _MAX_PACKET_BYTES:
            return None
        payload = json.loads(raw)
        if payload["identity"] != _identity(repo_root, rng, fingerprint):
            return None
        packet = review_packet_from_dict(payload["packet"])
        blobs = payload["blobs"]
        return PacketBuild(
            packet=packet,
            blobs=BlobPair(
                old=dict(blobs["old"]),
                new=dict(blobs["new"]),
                degraded=tuple(blobs["degraded"]),
                binary_new={
                    path: base64.b64decode(value, validate=True) for path, value in blobs["binary_new"].items()
                },
            ),
            source_fingerprint=fingerprint,
        )
    except (OSError, EOFError, ValueError, TypeError, KeyError, AttributeError):
        return None


def save_source_build(store_root: Path, repo_root: Path, rng: RevRange, build: PacketBuild) -> None:
    """Replace the single source slot atomically; cache failure never fails capture."""
    if not build.source_fingerprint:
        return
    temporary: str | None = None
    try:
        payload = {
            "identity": _identity(repo_root, rng, build.source_fingerprint),
            "packet": build.packet.to_dict(),
            "blobs": {
                "old": build.blobs.old,
                "new": build.blobs.new,
                "degraded": build.blobs.degraded,
                "binary_new": {
                    path: base64.b64encode(value).decode("ascii") for path, value in build.blobs.binary_new.items()
                },
            },
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        root = _root(store_root, repo_root)
        if len(raw) > _MAX_PACKET_BYTES:
            # The cap changes cache eligibility, never the captured Review contents.
            (root / "source.json.gz").unlink(missing_ok=True)
            return
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(dir=root, prefix=".source-", delete=False) as handle:
            temporary = handle.name
            with gzip.GzipFile(fileobj=handle, mode="wb") as compressed:
                compressed.write(raw)
        os.replace(temporary, root / "source.json.gz")
        sweep_stale_slots(store_root, keep=root)
    except (OSError, ValueError, TypeError):
        pass
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                Path(temporary).unlink(missing_ok=True)


@contextlib.contextmanager
def base_snapshot(
    store_root: Path,
    repo_root: Path,
    rng: RevRange,
    materialize: Callable[[Path, RevRange, Path], None],
) -> Iterator[Path]:
    """Reuse one immutable base tree while serializing readers and replacement.

    The consumer must only read the yielded tree. Upload still validates the
    complete manifest and negotiates missing blobs with the current server.
    """
    root = _root(store_root, repo_root)
    materialized = False
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(_cross_process_lock(root / "base.lock", _stripe(root)))
            entry = root / "base"
            marker = entry / "identity.json"
            identity = {"version": _VERSION, "base_sha": rng.base_sha}
            valid = False
            try:
                valid = (
                    time.time() - marker.stat().st_mtime <= _MAX_AGE_S
                    and json.loads(marker.read_text(encoding="utf-8")) == identity
                    and (entry / "tree").is_dir()
                )
            except (OSError, ValueError):
                pass
            if not valid:
                # Also reclaim staging trees left by an interrupted materialization.
                for abandoned in root.glob(".base-*"):
                    shutil.rmtree(abandoned)
                with tempfile.TemporaryDirectory(dir=root, prefix=".base-") as temporary:
                    pending = Path(temporary)
                    materialize(repo_root, rng, pending / "tree")
                    (pending / "identity.json").write_text(json.dumps(identity), encoding="utf-8")
                    if entry.exists():
                        shutil.rmtree(entry)
                    os.replace(pending, entry)
                materialized = True
            tree = entry / "tree"
        except OSError:
            # Read-only/full cache storage must preserve the uncached path.
            temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix="lemoncrow-review-base-"))
            tree = Path(temporary)
            materialize(repo_root, rng, tree)
        yield tree
    if materialized:
        # This slot just grew: reclaim the ones no capture has written for a day.
        sweep_stale_slots(store_root, keep=root)

"""Bounded undo records for promoted Verified Local Work repairs.

Every promotion into a live checkout first writes a *pending* record holding
the exact pre-repair bytes and mode of every file it will touch -- the live
bytes, so uncommitted and untracked edits are captured, not ``HEAD``. Only
after that write succeeds are candidate bytes written; after the live verifier
passes the record is committed with the observed post-repair hashes. If the
record cannot be written or committed, the promotion is refused or rolled back.

``undo_repair`` restores a record only while every touched file still matches
its post-repair hash; otherwise it refuses and names the conflicting paths.

Bounds (documented contract, enforced by ``prune_repairs`` after every commit
and on every list/undo):

* ``MAX_RECORDS`` newest records are kept (older ones are deleted);
* records older than ``MAX_AGE_S`` (30 days) are deleted;
* one record holds at most ``MAX_RECORD_BYTES`` of pre-repair content; a
  larger promotion is refused rather than left without an undo record.

Records live under ``<store root>/verified_work_repairs/<id>/`` next to the
other Verified Local Work state files (no SQLite table is used).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.workspace_locks import PathLockUnavailable, cross_process_path_locks
from lemoncrow.core.foundation.paths import default_store_root
from lemoncrow.pro.capabilities.model_setup.transport import active_store_root

from .outcomes import _atomic_write, _cross_process_lock
from .promote import _atomic_replace

_SCHEMA_VERSION = 1
_DIR_NAME = "verified_work_repairs"
_RECORD = "record.json"
_ID_RE = re.compile(r"^[0-9a-f]{12}$")

MAX_RECORDS = 50
MAX_AGE_S = 30 * 24 * 60 * 60
MAX_RECORD_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RepairMeta:
    workspace: str
    repo_key: str
    repo_root: str
    session_id: str
    host: str
    model: str
    kind: str
    verifier_command: str


@dataclass(frozen=True, slots=True)
class RepairFile:
    path: str
    pre_sha256: str
    pre_mode: int
    pre_size: int
    post_sha256: str
    blob: str


@dataclass(frozen=True, slots=True)
class RepairRecord:
    id: str
    status: str
    created_at: float
    applied_at: float | None
    undone_at: float | None
    undone_by: str
    repo_key: str
    repo_root: str
    workspace: str
    session_id: str
    host: str
    model: str
    kind: str
    verifier_command: str
    files: tuple[RepairFile, ...]


@dataclass(frozen=True, slots=True)
class UndoResult:
    ok: bool
    reason: str
    record_id: str = ""
    restored_paths: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()


def _root(root: Path | None = None) -> Path:
    return Path(root or active_store_root() or default_store_root()).expanduser()


def repairs_dir(root: Path | None = None) -> Path:
    return _root(root) / _DIR_NAME


def _store_lock(root: Path | None) -> AbstractContextManager[None]:
    return _cross_process_lock(repairs_dir(root) / "store")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str:
    try:
        return _sha(path.read_bytes())
    except OSError:
        return ""


def _write_blob(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _record_from(raw: Any) -> RepairRecord | None:
    if not isinstance(raw, dict):
        return None
    record_id = str(raw.get("id") or "")
    created_at = _float(raw.get("created_at"))
    rows = raw.get("files")
    if not _ID_RE.match(record_id) or created_at is None or not isinstance(rows, list) or not rows:
        return None
    files: list[RepairFile] = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        try:
            files.append(
                RepairFile(
                    path=str(row["path"]),
                    pre_sha256=str(row["pre_sha256"]),
                    pre_mode=int(row["pre_mode"]),
                    pre_size=int(row.get("pre_size") or 0),
                    post_sha256=str(row["post_sha256"]),
                    blob=str(row["blob"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            return None
    return RepairRecord(
        id=record_id,
        status=str(raw.get("status") or ""),
        created_at=created_at,
        applied_at=_float(raw.get("applied_at")),
        undone_at=_float(raw.get("undone_at")),
        undone_by=str(raw.get("undone_by") or ""),
        repo_key=str(raw.get("repo_key") or ""),
        repo_root=str(raw.get("repo_root") or ""),
        workspace=str(raw.get("workspace") or ""),
        session_id=str(raw.get("session_id") or ""),
        host=str(raw.get("host") or ""),
        model=str(raw.get("model") or ""),
        kind=str(raw.get("kind") or ""),
        verifier_command=str(raw.get("verifier_command") or ""),
        files=tuple(files),
    )


def _read_raw(folder: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads((folder / _RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


class RepairJournal:
    """Write-ahead undo record for one promotion (see module docstring)."""

    def __init__(
        self,
        meta: RepairMeta,
        *,
        root: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.meta = meta
        self.root = root
        self.clock = clock

    @property
    def store_root(self) -> Path:
        """The store whose path locks promotion and Undo share."""
        return _root(self.root)

    def begin(
        self,
        originals: Mapping[str, tuple[bytes, int]],
        candidates: Mapping[str, bytes],
    ) -> tuple[str | None, str]:
        """Persist pre-repair bytes before any live write. Returns (id, reason)."""
        rels = list(originals)
        if not rels or any(rel not in candidates for rel in rels):
            return None, "repair_record_failed"
        if sum(len(data) for data, _ in originals.values()) > MAX_RECORD_BYTES:
            return None, "repair_record_too_large"
        base = repairs_dir(self.root)
        folder: Path | None = None
        try:
            with _store_lock(self.root):
                record_id = uuid.uuid4().hex[:12]
                while (base / record_id).exists():
                    record_id = uuid.uuid4().hex[:12]
                folder = base / record_id
                folder.mkdir(parents=True)
                files: list[dict[str, Any]] = []
                for index, rel in enumerate(rels):
                    data, mode = originals[rel]
                    blob = f"pre/{index}"
                    _write_blob(folder / blob, data)
                    files.append(
                        {
                            "path": rel,
                            "pre_sha256": _sha(data),
                            "pre_mode": stat.S_IMODE(mode),
                            "pre_size": len(data),
                            # Expected until commit replaces it with the observed hash.
                            "post_sha256": _sha(candidates[rel]),
                            "blob": blob,
                        }
                    )
                meta = self.meta
                _atomic_write(
                    folder / _RECORD,
                    {
                        "schema_version": _SCHEMA_VERSION,
                        "id": record_id,
                        "status": "pending",
                        "created_at": float(self.clock()),
                        "applied_at": None,
                        "undone_at": None,
                        "undone_by": "",
                        "repo_key": meta.repo_key,
                        "repo_root": meta.repo_root,
                        "workspace": meta.workspace,
                        "session_id": meta.session_id,
                        "host": meta.host,
                        "model": meta.model,
                        "kind": meta.kind,
                        "verifier_command": meta.verifier_command,
                        "files": files,
                    },
                )
        except OSError:
            if folder is not None:
                shutil.rmtree(folder, ignore_errors=True)
            return None, "repair_record_failed"
        return record_id, "repair_record_pending"

    def commit(self, record_id: str, post_hashes: Mapping[str, str], *, prune: bool = True) -> bool:
        folder = repairs_dir(self.root) / record_id
        try:
            with _store_lock(self.root):
                raw = _read_raw(folder)
                if raw is None or _record_from(raw) is None:
                    return False
                for row in raw["files"]:
                    observed = post_hashes.get(str(row["path"]))
                    if not observed:
                        return False
                    row["post_sha256"] = observed
                raw["status"] = "applied"
                raw["applied_at"] = float(self.clock())
                _atomic_write(folder / _RECORD, raw)
        except OSError:
            return False
        if prune:
            prune_repairs(self.root)
        return True

    def abort(self, record_id: str) -> None:
        if not _ID_RE.match(record_id):
            return
        with _store_lock(self.root):
            shutil.rmtree(repairs_dir(self.root) / record_id, ignore_errors=True)


def load_repair(record_id: str, root: Path | None = None) -> RepairRecord | None:
    if not _ID_RE.match(record_id or ""):
        return None
    return _record_from(_read_raw(repairs_dir(root) / record_id))


def _scan(root: Path | None) -> tuple[list[RepairRecord], list[Path]]:
    base = repairs_dir(root)
    records: list[RepairRecord] = []
    broken: list[Path] = []
    if not base.is_dir():
        return records, broken
    for folder in base.iterdir():
        if not folder.is_dir() or not _ID_RE.match(folder.name):
            continue
        record = _record_from(_read_raw(folder))
        if record is None or record.id != folder.name:
            broken.append(folder)
        else:
            records.append(record)
    records.sort(key=lambda row: (row.created_at, row.id), reverse=True)
    return records, broken


def prune_repairs(root: Path | None = None, *, now: float | None = None) -> int:
    """Enforce the count and age bounds. Returns the number of records removed."""
    if not repairs_dir(root).is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - MAX_AGE_S
    removed = 0
    with _store_lock(root):
        records, broken = _scan(root)
        # Under the store lock a folder without a valid record is a write that
        # crashed before its record existed; it never guarded a live write.
        for folder in broken:
            shutil.rmtree(folder, ignore_errors=True)
        fresh = [record for record in records if record.created_at >= cutoff]
        doomed = [record for record in records if record.created_at < cutoff] + fresh[MAX_RECORDS:]
        for record in doomed:
            shutil.rmtree(repairs_dir(root) / record.id, ignore_errors=True)
            removed += 1
    return removed


def list_repairs(
    root: Path | None = None,
    *,
    repo_key: str | None = None,
    limit: int | None = None,
    prune: bool = True,
) -> list[RepairRecord]:
    """Records newest first, optionally for one repository."""
    if prune:
        prune_repairs(root)
    records, _ = _scan(root)
    if repo_key is not None:
        records = [record for record in records if record.repo_key == repo_key]
    return records if limit is None else records[: max(0, limit)]


def _mark_undone(root: Path | None, record_id: str, actor: str) -> None:
    folder = repairs_dir(root) / record_id
    with _store_lock(root):
        raw = _read_raw(folder)
        if raw is None:
            return
        raw["status"] = "undone"
        raw["undone_at"] = time.time()
        raw["undone_by"] = actor
        _atomic_write(folder / _RECORD, raw)


def _undo_targets(record: RepairRecord, root: Path | None) -> list[tuple[RepairFile, Path, Path]] | UndoResult:
    """(row, live file, pre-repair blob) for every file *record* touched, or why not."""
    workspace = Path(record.workspace).expanduser().resolve()
    if not record.workspace or not workspace.is_dir():
        return UndoResult(False, "workspace_missing", record.id)
    folder = (repairs_dir(root) / record.id).resolve()
    targets: list[tuple[RepairFile, Path, Path]] = []
    for row in record.files:
        live = (workspace / row.path).resolve()
        blob = (folder / row.blob).resolve()
        try:
            live.relative_to(workspace)
            blob.relative_to(folder)
        except ValueError:
            return UndoResult(False, "path_escape", record.id)
        targets.append((row, live, blob))
    return targets


def undo_repair(
    record_id: str | None = None,
    *,
    repo_key: str | None = None,
    root: Path | None = None,
    actor: str | None = None,
    lock_timeout_s: float = 10.0,
) -> UndoResult:
    """Restore one repair's pre-repair bytes, or refuse without changing anything.

    ``record_id=None`` selects the newest applied record (of ``repo_key`` when
    given). The restore happens only when every touched file still has its
    post-repair hash; any other content is reported as a conflict.

    The hash check and the restore run under the store's cross-process path
    locks, shared with :func:`promote_candidate` in any process. When another
    holder keeps them past ``lock_timeout_s`` Undo changes nothing and reports
    ``path_locked`` (retry later). A writer that takes no lock (an editor, the
    host's native Edit) is caught only by the hash check, not excluded.
    """
    from .consent import local_actor

    if record_id is None:
        candidates = [
            record for record in list_repairs(root, repo_key=repo_key) if record.status in {"applied", "pending"}
        ]
        if not candidates:
            return UndoResult(False, "not_found")
        record: RepairRecord | None = candidates[0]
    else:
        prune_repairs(root)
        record = load_repair(record_id, root=root)
    if record is None:
        return UndoResult(False, "not_found", record_id or "")
    if record.status == "undone":
        return UndoResult(False, "already_undone", record.id)

    targets = _undo_targets(record, root)
    if isinstance(targets, UndoResult):
        return targets

    with ExitStack() as stack:
        try:
            stack.enter_context(
                cross_process_path_locks(
                    [live for _, live, _ in targets], store_root=_root(root), timeout_s=lock_timeout_s
                )
            )
        except PathLockUnavailable as exc:
            return UndoResult(False, exc.reason, record.id)

        # The record read above chose which paths to lock; decide from the record as it
        # stands under the locks (an Undo or a commit may have finished in between).
        fresh = load_repair(record.id, root=root)
        if fresh is None:
            return UndoResult(False, "not_found", record.id)
        if fresh.status == "undone":
            return UndoResult(False, "already_undone", record.id)
        refreshed = _undo_targets(fresh, root)
        if isinstance(refreshed, UndoResult):
            return refreshed
        if sorted(str(live) for _, live, _ in refreshed) != sorted(str(live) for _, live, _ in targets):
            return UndoResult(False, "record_corrupt", record.id)
        record, targets = fresh, refreshed

        current = {row.path: _file_sha(live) for row, live, _ in targets}
        if all(current[row.path] == row.pre_sha256 for row, _, _ in targets):
            _mark_undone(root, record.id, actor or local_actor())
            return UndoResult(True, "already_restored", record.id)
        # A pending record means the promotion was interrupted mid-write: files
        # it never reached still hold their pre-repair bytes and are not drift.
        interrupted = record.status == "pending"
        conflicts = tuple(
            row.path
            for row, _, _ in targets
            if current[row.path] != row.post_sha256 and not (interrupted and current[row.path] == row.pre_sha256)
        )
        if conflicts:
            return UndoResult(False, "drift", record.id, conflicts=conflicts)
        targets = [target for target in targets if current[target[0].path] != target[0].pre_sha256]

        pre: dict[str, bytes] = {}
        for row, _, blob in targets:
            try:
                data = blob.read_bytes()
            except OSError:
                return UndoResult(False, "record_corrupt", record.id)
            if _sha(data) != row.pre_sha256:
                return UndoResult(False, "record_corrupt", record.id)
            pre[row.path] = data

        backups = {row.path: (live.read_bytes(), live.stat().st_mode) for row, live, _ in targets}
        written: list[tuple[RepairFile, Path]] = []
        try:
            for row, live, _ in targets:
                _atomic_replace(live, pre[row.path], row.pre_mode)
                written.append((row, live))
        except OSError:
            # Each write is atomic, so the failing file is untouched. Put back
            # the files already restored; any that cannot be put back are left
            # at their pre-repair bytes and reported, never silently.
            stranded: list[str] = []
            for row, live in written:
                try:
                    _atomic_replace(live, *backups[row.path])
                except OSError:
                    stranded.append(row.path)
            if stranded:
                return UndoResult(False, "restore_partial", record.id, restored_paths=tuple(stranded))
            return UndoResult(False, "restore_failed", record.id)
        _mark_undone(root, record.id, actor or local_actor())
    return UndoResult(True, "restored", record.id, tuple(row.path for row, _, _ in targets))


def promotion_notice(changed_paths: tuple[str, ...], *, attempts: int, record_id: str) -> str:
    """The host-visible line after a promotion: what changed and how to undo it."""
    files = ", ".join(changed_paths) or "verified files"
    undo = f"lc rt savings undo {record_id}" if record_id else "lc rt savings undo --last"
    return (
        "LemonCrow verified local repair applied.\n"
        f"Verifier passed after repair · {attempts} attempt(s) · {files}\n"
        f"Undo: {undo}\n"
    )


__all__ = [
    "MAX_AGE_S",
    "MAX_RECORDS",
    "MAX_RECORD_BYTES",
    "RepairFile",
    "RepairJournal",
    "RepairMeta",
    "RepairRecord",
    "UndoResult",
    "list_repairs",
    "load_repair",
    "promotion_notice",
    "prune_repairs",
    "repairs_dir",
    "undo_repair",
]

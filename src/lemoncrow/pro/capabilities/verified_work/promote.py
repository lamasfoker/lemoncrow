"""Transactional promotion of a verified candidate into the live checkout."""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import tempfile
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lemoncrow.core.capabilities.verified_work.models import VerifiedWorkOpportunity
from lemoncrow.core.capabilities.workspace_locks import PathLockUnavailable, cross_process_path_locks

if TYPE_CHECKING:
    from .repairs import RepairJournal


@dataclass(frozen=True, slots=True)
class PromotionResult:
    promoted: bool
    reason: str
    changed_paths: tuple[str, ...] = ()
    record_id: str = ""


def _sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _atomic_replace(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.lc-vw-", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, stat.S_IMODE(mode))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def promote_candidate(
    opportunity: VerifiedWorkOpportunity,
    *,
    candidate_workspace: Path,
    changed_paths: tuple[str, ...],
    post_verify: Callable[[], bool] | None = None,
    journal: RepairJournal | None = None,
    lock_timeout_s: float = 10.0,
) -> PromotionResult:
    """Promote all changed files or none, guarded by original content hashes.

    ``post_verify`` runs while the shared path locks remain held. If it returns
    false the original bytes are restored before locks are released.

    ``journal`` is mandatory: the pre-repair bytes of every touched file are
    persisted before the first live write, and the record is committed with
    the observed post-repair hashes before the promotion is reported. Without
    a journal, or when the record cannot be written, nothing is promoted.

    The path locks are shared across processes through the journal's store
    (Undo takes the same ones); when they cannot be taken within
    ``lock_timeout_s`` nothing is promoted (``path_locked``).
    """
    if journal is None:
        return PromotionResult(False, "repair_record_required")
    live_workspace = Path(opportunity.workspace).resolve()
    expected = dict(opportunity.base_file_hashes)
    rels = tuple(dict.fromkeys(changed_paths))
    if not rels:
        return PromotionResult(False, "no_changed_paths")
    if any(rel not in expected for rel in rels):
        return PromotionResult(False, "changed_path_not_in_opportunity")

    live_paths = [(live_workspace / rel).resolve() for rel in rels]
    for path in live_paths:
        try:
            path.relative_to(live_workspace)
        except ValueError:
            return PromotionResult(False, "path_escape")

    with ExitStack() as stack:
        try:
            stack.enter_context(
                cross_process_path_locks(live_paths, store_root=journal.store_root, timeout_s=lock_timeout_s)
            )
        except PathLockUnavailable as exc:
            return PromotionResult(False, exc.reason)

        for rel, path in zip(rels, live_paths, strict=True):
            if _sha(path) != expected.get(rel, ""):
                return PromotionResult(False, "base_hash_conflict")

        originals: dict[str, tuple[bytes, int]] = {}
        candidates: dict[str, bytes] = {}
        try:
            for rel, path in zip(rels, live_paths, strict=True):
                originals[rel] = (path.read_bytes(), path.stat().st_mode)
                candidate = (candidate_workspace / rel).resolve()
                candidate.relative_to(candidate_workspace.resolve())
                candidates[rel] = candidate.read_bytes()
        except (OSError, ValueError):
            return PromotionResult(False, "promotion_write_failed")

        record_id, record_reason = journal.begin(originals, candidates)
        if record_id is None:
            return PromotionResult(False, record_reason)

        try:
            for rel, path in zip(rels, live_paths, strict=True):
                original_mode = originals[rel][1]
                _atomic_replace(path, candidates[rel], original_mode)

            if post_verify is not None and not bool(post_verify()):
                raise RuntimeError("post_promotion_verifier_failed")
            # The observed hashes (after the live verifier) are what Undo checks.
            post_hashes = {rel: _sha(path) for rel, path in zip(rels, live_paths, strict=True)}
            if not journal.commit(record_id, post_hashes):
                raise RuntimeError("repair_record_failed")
        except Exception as exc:
            # Roll back every path from the captured originals while locks are held.
            for rel, path in zip(rels, live_paths, strict=True):
                original = originals.get(rel)
                if original is None:
                    continue
                try:
                    _atomic_replace(path, original[0], original[1])
                except OSError:
                    pass
            with contextlib.suppress(OSError):
                journal.abort(record_id)
            reason = (
                str(exc)
                if str(exc) in {"post_promotion_verifier_failed", "repair_record_failed"}
                else "promotion_write_failed"
            )
            return PromotionResult(False, reason)

    return PromotionResult(True, "promoted_verified_candidate", rels, record_id)


__all__ = ["PromotionResult", "promote_candidate"]

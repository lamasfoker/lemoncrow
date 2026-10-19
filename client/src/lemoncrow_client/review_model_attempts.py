"""Durable, exclusive, one-shot local budget reservation for Review model calls.

An operator supplies the Review-only state directory. Per signed job/attempt,
repository, machine, task and pinned route, create one owner-only, O_EXCL record
*before* any provider API POST. A timeout, process crash, or HTTP uncertainty
consumes the grant; only a NEW signed lease attempt can spend again. No source,
prompt, model output, bearer or provider token is persisted.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from .review_worker_client import ReviewWorkerClient, WorkerLease


class ModelAttemptAlreadySpent(ValueError):
    """The signed job attempt has been dispatched or its history is unsafe."""


class ProviderAttemptLedger:
    def __init__(self, review_state_dir: Path) -> None:
        if not isinstance(review_state_dir, Path) or not review_state_dir.is_absolute():
            raise ModelAttemptAlreadySpent("model attempt state requires an absolute local Review state directory")
        self.review_state_dir = review_state_dir

    @staticmethod
    def _private(path: Path) -> None:
        if path.is_symlink():
            raise ModelAttemptAlreadySpent("model budget state may not be a symlink")
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ModelAttemptAlreadySpent("model budget state must be owner-only")

    def consume(self, client: ReviewWorkerClient, lease: WorkerLease, task_id: str, route_id: str) -> None:
        """Atomic CAS across threads/processes/restarts; never a best-effort lock."""
        client._require_lease(lease)
        base = self.review_state_dir
        if not base.is_dir():
            raise ModelAttemptAlreadySpent("Review state directory must already exist")
        self._private(base)
        directory = base / "model-attempts"
        if directory.is_symlink():
            raise ModelAttemptAlreadySpent("model attempt directory may not be redirected")
        directory.mkdir(mode=0o700, exist_ok=True)
        self._private(directory)
        binding = client.binding
        identity = {
            "org_id": binding.org_id,
            "repo_id": binding.repo_id,
            "worker_id": binding.worker_id,
            "job_id": lease.job_id,
            "attempt": lease.attempt,
            "task_id": task_id,
            "route_id": route_id,
        }
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            name = f"{digest}.spent"
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
            except FileExistsError as exc:
                raise ModelAttemptAlreadySpent("this signed job attempt has already consumed its model call") from exc
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(b"provider-attempt-reserved-v1\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.fsync(dir_fd)
            except BaseException:
                # Fail closed: leave the spend marker in place on uncertain
                # write/fsync rather than permitting a potentially duplicate POST.
                raise
        finally:
            os.close(dir_fd)


__all__ = ["ModelAttemptAlreadySpent", "ProviderAttemptLedger"]

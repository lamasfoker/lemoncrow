"""Cross-process concurrency and per-session budgets for Verified Local Work."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.verified_work.settings import VerifiedWorkSettings
from lemoncrow.core.foundation.paths import default_store_root
from lemoncrow.pro.capabilities.model_setup.transport import active_store_root

_SCHEMA_VERSION = 1
_FILE_NAME = "verified_work_runtime_state.json"
_MAX_SESSION_ROWS = 256
_PROCESS_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class WorkReservation:
    token: str
    session_id: str
    started_monotonic: float


def _root(root: Path | None = None) -> Path:
    return Path(root or active_store_root() or default_store_root()).expanduser()


def state_path(root: Path | None = None) -> Path:
    return _root(root) / _FILE_NAME


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _PROCESS_LOCK:
        handle = lock_path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                api: Any = msvcrt
                api.locking(handle.fileno(), api.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    api_unlock: Any = msvcrt
                    api_unlock.locking(handle.fileno(), api_unlock.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": _SCHEMA_VERSION, "active": [], "sessions": {}}
    if not isinstance(raw, dict):
        return {"schema_version": _SCHEMA_VERSION, "active": [], "sessions": {}}
    return {
        "schema_version": _SCHEMA_VERSION,
        "active": raw.get("active") if isinstance(raw.get("active"), list) else [],
        "sessions": raw.get("sessions") if isinstance(raw.get("sessions"), dict) else {},
    }


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _pid_live(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _prune(payload: dict[str, Any], *, stale_after_s: float) -> None:
    now = time.time()
    active: list[dict[str, Any]] = []
    for row in payload.get("active", []):
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row.get("pid") or 0)
            started = float(row.get("started_at") or 0.0)
        except (TypeError, ValueError):
            continue
        if _pid_live(pid) and started > 0 and now - started <= stale_after_s:
            active.append(row)
    payload["active"] = active

    sessions = payload.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
        payload["sessions"] = sessions
    if len(sessions) > _MAX_SESSION_ROWS:
        ordered = sorted(
            sessions.items(),
            key=lambda item: (
                (float((item[1] or {}).get("updated_at") or 0.0), str(item[0]))
                if isinstance(item[1], dict)
                else (0.0, str(item[0]))
            ),
        )
        for key, _ in ordered[: len(sessions) - _MAX_SESSION_ROWS]:
            sessions.pop(key, None)


def reserve_work(
    session_id: str,
    settings: VerifiedWorkSettings,
    *,
    root: Path | None = None,
) -> tuple[WorkReservation | None, str]:
    """Atomically reserve one worker slot and consume one session repair count."""
    path = state_path(root)
    stale_after_s = max(300.0, float(settings.timeout_s * settings.max_attempts * 2 + 30))
    with _locked(path):
        payload = _read(path)
        _prune(payload, stale_after_s=stale_after_s)
        active = payload["active"]
        sessions = payload["sessions"]
        row = sessions.get(session_id)
        if not isinstance(row, dict):
            row = {"repairs": 0, "seconds": 0.0, "updated_at": time.time()}
            sessions[session_id] = row
        repairs = max(0, int(row.get("repairs") or 0))
        seconds = max(0.0, float(row.get("seconds") or 0.0))
        if len(active) >= settings.max_parallel:
            return None, "parallel_budget_exhausted"
        if repairs >= settings.max_repairs_session:
            return None, "session_repair_budget_exhausted"
        if seconds >= settings.max_seconds_session:
            return None, "session_time_budget_exhausted"

        token = uuid.uuid4().hex
        now = time.time()
        active.append({"token": token, "session_id": session_id, "pid": os.getpid(), "started_at": now})
        row["repairs"] = repairs + 1
        row["updated_at"] = now
        _write(path, payload)
        return WorkReservation(token=token, session_id=session_id, started_monotonic=time.monotonic()), "reserved"


def release_work(reservation: WorkReservation, *, root: Path | None = None) -> None:
    path = state_path(root)
    elapsed = max(0.0, time.monotonic() - reservation.started_monotonic)
    with _locked(path):
        payload = _read(path)
        payload["active"] = [
            row
            for row in payload.get("active", [])
            if not isinstance(row, dict) or str(row.get("token") or "") != reservation.token
        ]
        sessions = payload.get("sessions")
        if not isinstance(sessions, dict):
            sessions = {}
            payload["sessions"] = sessions
        row = sessions.get(reservation.session_id)
        if not isinstance(row, dict):
            row = {"repairs": 1, "seconds": 0.0}
            sessions[reservation.session_id] = row
        row["seconds"] = max(0.0, float(row.get("seconds") or 0.0)) + elapsed
        row["updated_at"] = time.time()
        _write(path, payload)


__all__ = ["WorkReservation", "release_work", "reserve_work", "state_path"]

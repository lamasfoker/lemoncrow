"""Durable exact-session inbox for review feedback.

Some coding hosts can accept a message directly into an already-running session;
others cannot. The inbox is the safe fallback for the latter: Review can hand
feedback to the exact author session without spawning a copy, and the host's
normal hook consumes it on that session's next model turn.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.paths import safe_segment


@dataclass(frozen=True, slots=True)
class SessionInboxMessage:
    id: str
    host: str
    session_id: str
    message: str
    created_at: float


_INBOX_PROCESS_LOCK = threading.RLock()


@contextlib.contextmanager
def _session_inbox_lock(directory: Path) -> Iterator[None]:
    """Serialize enqueue/claim for one exact-session inbox across processes."""

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".lock"
    with _INBOX_PROCESS_LOCK:
        handle = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt_api: Any = msvcrt
                msvcrt_api.locking(handle.fileno(), msvcrt_api.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt_unlock_api: Any = msvcrt
                    msvcrt_unlock_api.locking(handle.fileno(), msvcrt_unlock_api.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def _inbox_dir(root: Path | str, host: str, session_id: str) -> Path:
    return (
        Path(root)
        / "review"
        / "session-inbox"
        / safe_segment(host.strip().lower(), field="host")
        / safe_segment(session_id.strip(), field="session_id")
    )


def enqueue_session_message(
    root: Path | str,
    *,
    host: str,
    session_id: str,
    message: str,
) -> SessionInboxMessage:
    """Persist one exact-session message idempotently by semantic payload."""

    host = host.strip().lower()
    session_id = session_id.strip()
    if not host or not session_id or not message.strip():
        raise ValueError("host, session_id and message are required")
    digest = hashlib.sha256(f"{host}\0{session_id}\0{message}".encode()).hexdigest()[:32]
    record = SessionInboxMessage(
        id=f"msg-{digest}",
        host=host,
        session_id=session_id,
        message=message,
        created_at=time.time(),
    )
    directory = _inbox_dir(root, host, session_id)
    with _session_inbox_lock(directory):
        pending = directory / "pending"
        inflight = directory / "inflight"
        claimed = directory / "claimed"
        pending.mkdir(parents=True, exist_ok=True)
        target = pending / f"{record.id}.json"
        inflight_target = inflight / target.name
        claimed_target = claimed / target.name
        # Semantic idempotency survives consumption. The enqueue check and the
        # agent's claim share this lock, so a retry cannot recreate pending work
        # after the first copy moved to claimed/.
        if target.exists() or inflight_target.exists() or claimed_target.exists():
            return record
        payload = {
            "id": record.id,
            "host": host,
            "session_id": session_id,
            "message": message,
            "created_at": record.created_at,
        }
        tmp = pending / f".{record.id}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    return record


def _read_message(path: Path, *, host: str, session_id: str) -> SessionInboxMessage | None:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        message = str(raw.get("message") or "")
        if not message:
            return None
        return SessionInboxMessage(
            id=str(raw.get("id") or path.stem),
            host=str(raw.get("host") or host),
            session_id=str(raw.get("session_id") or session_id),
            message=message,
            created_at=float(raw.get("created_at") or 0.0),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def lease_session_messages(
    root: Path | str,
    *,
    host: str,
    session_id: str,
    limit: int = 8,
    lease_timeout_s: float = 300.0,
) -> tuple[SessionInboxMessage, ...]:
    """Lease pending messages without declaring them consumed yet.

    Stale inflight leases are returned to pending so a hook crash cannot make
    reviewer feedback disappear forever. Call :func:`ack_session_messages` only
    after the host hook has successfully emitted the injected model context.
    """

    directory = _inbox_dir(root, host, session_id)
    with _session_inbox_lock(directory):
        pending = directory / "pending"
        inflight = directory / "inflight"
        claimed = directory / "claimed"
        pending.mkdir(parents=True, exist_ok=True)
        inflight.mkdir(parents=True, exist_ok=True)
        claimed.mkdir(parents=True, exist_ok=True)
        now = time.time()
        cutoff = now - max(1.0, lease_timeout_s)
        for leased in tuple(inflight.glob("msg-*.json")):
            try:
                if leased.stat().st_mtime > cutoff:
                    continue
                if (claimed / leased.name).exists():
                    leased.unlink(missing_ok=True)
                elif not (pending / leased.name).exists():
                    os.replace(leased, pending / leased.name)
                else:
                    leased.unlink(missing_ok=True)
            except OSError:
                continue
        try:
            paths = sorted(pending.glob("msg-*.json"), key=lambda path: (path.stat().st_mtime_ns, path.name))
        except OSError:
            return ()
        out: list[SessionInboxMessage] = []
        for source in paths[: max(1, limit)]:
            destination = inflight / source.name
            try:
                os.replace(source, destination)
                os.utime(destination, None)
            except OSError:
                continue
            message = _read_message(destination, host=host, session_id=session_id)
            if message is not None:
                out.append(message)
            else:
                with contextlib.suppress(OSError):
                    os.replace(destination, claimed / destination.name)
        return tuple(out)


def ack_session_messages(
    root: Path | str,
    *,
    host: str,
    session_id: str,
    message_ids: Iterable[str],
) -> None:
    """Acknowledge leased messages after model context was emitted."""

    directory = _inbox_dir(root, host, session_id)
    with _session_inbox_lock(directory):
        inflight = directory / "inflight"
        claimed = directory / "claimed"
        claimed.mkdir(parents=True, exist_ok=True)
        acknowledged: list[str] = []
        for message_id in message_ids:
            safe_id = safe_segment(str(message_id), field="message_id")
            name = f"{safe_id}.json"
            source = inflight / name
            destination = claimed / name
            try:
                if destination.exists():
                    source.unlink(missing_ok=True)
                    acknowledged.append(safe_id)
                elif source.exists():
                    os.replace(source, destination)
                    acknowledged.append(safe_id)
            except OSError:
                continue
    if acknowledged:
        # Hook output has already been flushed when this function is called, so
        # this is the first point at which Review may truthfully say the exact
        # session consumed the queued context. Fail-open: the claimed inbox file
        # remains the durable receipt even if the Review DB is temporarily busy.
        try:
            from lemoncrow.pro.capabilities.review.store import ReviewStore

            ReviewStore(root).acknowledge_delivery_remote_refs(
                tuple(f"inbox:{message_id}" for message_id in acknowledged)
            )
        except Exception:
            # Delivery acknowledgement is a secondary projection of the claimed
            # inbox receipt. Never fail the host hook after context was emitted;
            # a later Review read can reconcile the durable inbox state.
            pass


def release_session_messages(
    root: Path | str,
    *,
    host: str,
    session_id: str,
    message_ids: Iterable[str],
) -> None:
    """Return leased messages to pending after a known hook failure."""

    directory = _inbox_dir(root, host, session_id)
    with _session_inbox_lock(directory):
        pending = directory / "pending"
        inflight = directory / "inflight"
        claimed = directory / "claimed"
        pending.mkdir(parents=True, exist_ok=True)
        for message_id in message_ids:
            name = f"{safe_segment(str(message_id), field='message_id')}.json"
            source = inflight / name
            destination = pending / name
            try:
                if (claimed / name).exists():
                    source.unlink(missing_ok=True)
                elif source.exists() and not destination.exists():
                    os.replace(source, destination)
            except OSError:
                continue


def claim_session_messages(
    root: Path | str,
    *,
    host: str,
    session_id: str,
    limit: int = 8,
) -> tuple[SessionInboxMessage, ...]:
    """Compatibility helper: lease and acknowledge immediately.

    Production host hooks use the two-phase lease/ack API so a hook crash does
    not silently lose feedback. Tests and legacy callers that intentionally
    treat claiming as consumption may keep using this helper.
    """

    messages = lease_session_messages(root, host=host, session_id=session_id, limit=limit)
    if messages:
        ack_session_messages(
            root,
            host=host,
            session_id=session_id,
            message_ids=(item.id for item in messages),
        )
    return messages


__all__ = [
    "SessionInboxMessage",
    "ack_session_messages",
    "claim_session_messages",
    "enqueue_session_message",
    "lease_session_messages",
    "release_session_messages",
]

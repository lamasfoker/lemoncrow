#!/usr/bin/env python3
"""Import-free exact-session Review inbox helpers for Copilot CLI hooks."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_MAX_MESSAGES = 8
_MAX_CONTEXT_CHARS = 64_000


def _root() -> Path:
    return Path(os.environ.get("LEMONCROW_ROOT", "") or Path.home() / ".lemoncrow").expanduser().resolve()


def _safe_segment(value: object) -> str:
    text = str(value or "").strip()
    if text in {"", ".", ".."} or not _SAFE_SEGMENT.fullmatch(text):
        raise ValueError("unsafe exact-session id")
    return text


def _inbox_dirs(session_id: str) -> tuple[str, Path, Path, Path, Path]:
    sid = _safe_segment(session_id)
    directory = _root() / "review" / "session-inbox" / "copilot" / sid
    return sid, directory / "pending", directory / "inflight", directory / "claimed", directory


def _read_payload(path: Path, *, sid: str) -> tuple[str, str] | None:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        if str(raw.get("host") or "").strip().lower() != "copilot":
            return None
        if str(raw.get("session_id") or "").strip() != sid:
            return None
        message = str(raw.get("message") or "").strip()
        message_id = str(raw.get("id") or path.stem).strip()
        if not message or not message_id:
            return None
        return message_id, message
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def lease_feedback(session_id: str, *, lease_timeout_s: float = 300.0) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Lease exact-session Copilot feedback without declaring it consumed."""

    sid, pending, inflight, claimed, _directory = _inbox_dirs(session_id)
    try:
        pending.mkdir(parents=True, exist_ok=True)
        inflight.mkdir(parents=True, exist_ok=True)
        claimed.mkdir(parents=True, exist_ok=True)
    except OSError:
        return (), ()

    import time

    cutoff = time.time() - max(1.0, lease_timeout_s)
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
        return (), ()
    messages: list[str] = []
    ids: list[str] = []
    for source in paths[:_MAX_MESSAGES]:
        destination = inflight / source.name
        try:
            os.replace(source, destination)
            os.utime(destination, None)
        except OSError:
            continue
        parsed = _read_payload(destination, sid=sid)
        if parsed is None:
            try:
                os.replace(destination, claimed / destination.name)
            except OSError:
                pass
            continue
        message_id, message = parsed
        ids.append(message_id)
        messages.append(message)
    return tuple(messages), tuple(ids)


def ack_feedback(session_id: str, message_ids: tuple[str, ...]) -> None:
    """Acknowledge leased Copilot feedback after hook output was flushed."""

    _sid, _pending, inflight, claimed, _directory = _inbox_dirs(session_id)
    try:
        claimed.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for message_id in message_ids:
        try:
            name = f"{_safe_segment(message_id)}.json"
        except ValueError:
            continue
        source = inflight / name
        destination = claimed / name
        try:
            if destination.exists():
                source.unlink(missing_ok=True)
            elif source.exists():
                os.replace(source, destination)
        except OSError:
            continue


def claim_feedback(session_id: str) -> tuple[str, ...]:
    """Compatibility helper: lease and acknowledge immediately."""

    messages, ids = lease_feedback(session_id)
    if ids:
        ack_feedback(session_id, ids)
    return messages


def feedback_context(messages: tuple[str, ...], *, continue_now: bool) -> str:
    """Bound and label exact-session reviewer feedback for model context."""

    if not messages:
        return ""
    body = "\n\n---\n\n".join(messages)
    if len(body) > _MAX_CONTEXT_CHARS:
        body = body[:_MAX_CONTEXT_CHARS] + "\n\n…(additional Review feedback remains in LemonCrow Review)"
    context = '<lemoncrow_review_feedback source="exact-session-inbox">\n' f"{body}\n" "</lemoncrow_review_feedback>"
    if continue_now:
        context += (
            "\n\nContinue this same authoring task now. Apply the Review feedback, "
            "run the relevant verification, and report what changed."
        )
    return context

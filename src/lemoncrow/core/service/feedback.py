"""Product wishes ("Make a wish") submitted from the app.

A wish is free text a person typed into the Review feedback dialog and then
asked to send. It is **not telemetry**: it is a deliberate, single, labelled
action, so it deliberately bypasses the remote-telemetry switch
(``resolve_destination`` / ``DO_NOT_TRACK`` / ``LEMONCROW_TELEMETRY=off``) and
always goes to the LemonCrow sink. Gating it there would silently throw away
the one thing the person actually asked to send. What it does obey is
``docs/setup/privacy.md``: the payload carries the message, an email only if
one was typed, the surface, and the version -- never code, paths, review
contents, install or account identifiers.

The forward is synchronous and single-shot (one retry on a transient failure):
a wish is a deliberate single user action, not a stream, so there is nothing to
batch and no backlog to close. Nothing is queued locally either -- a failed
forward is reported so the UI can offer its mailto fallback rather than claim a
wish was delivered. The sink is the LemonCrow-operated ``/api/feedback``
endpoint (``FEEDBACK_ENDPOINT``); its implementation is not part of this
repository.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal

from lemoncrow.core.service.telemetry.event_rollup import post_json

__all__ = ["FeedbackError", "FeedbackResult", "feedback_endpoint", "submit_feedback"]

# The sink is the LemonCrow Worker, not the telemetry destination: a wish is
# sent there whatever the telemetry configuration says.
FEEDBACK_ENDPOINT = "https://lemoncrow.com/api/feedback"
# Full-URL override for local development only.
OVERRIDE_ENV = "LEMONCROW_FEEDBACK_ENDPOINT"

# The service rejects longer messages; keep in step with its MAX_MESSAGE_CHARS.
MAX_MESSAGE_CHARS = 4000
MAX_EMAIL_CHARS = 254
FORWARD_TIMEOUT_SECONDS = 10.0
FORWARD_ATTEMPTS = 2

# The sink allowlists these; anything else collapses to "unknown" there, so
# normalising here keeps the row honest about what was sent.
SOURCES = frozenset({"review", "app", "docs", "landing", "other"})

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

Delivered = Literal["remote", "failed"]


class FeedbackError(ValueError):
    """A rejection the sender can fix (empty message, malformed email)."""


@dataclass(frozen=True)
class FeedbackResult:
    delivered: Delivered
    status: int = 0
    detail: str = ""


def normalize_message(value: Any) -> str:
    if not isinstance(value, str):
        raise FeedbackError("message must be a string")
    message = value.strip()
    if not message:
        raise FeedbackError("message is required")
    if len(message) > MAX_MESSAGE_CHARS:
        raise FeedbackError(f"message must be at most {MAX_MESSAGE_CHARS} characters")
    return message


def normalize_email(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise FeedbackError("email must be a string")
    email = value.strip().lower()
    if not email:
        return None
    if len(email) > MAX_EMAIL_CHARS or not _EMAIL_RE.match(email):
        raise FeedbackError("email must be a valid address")
    return email


def normalize_source(value: Any) -> str:
    source = value.strip() if isinstance(value, str) else ""
    return source if source in SOURCES else "unknown"


def _service_version() -> str:
    try:
        return version("lemoncrow")
    except PackageNotFoundError:
        return "0.1.0"


def feedback_endpoint() -> str:
    """The sink a wish is sent to: the LemonCrow Worker, or a development
    override. Never the telemetry destination -- a wish is not telemetry."""
    return os.environ.get(OVERRIDE_ENV, "").strip() or FEEDBACK_ENDPOINT


def submit_feedback(
    *,
    message: Any,
    email: Any = None,
    source: Any = "review",
) -> FeedbackResult:
    """Send one validated wish to the sink, whatever telemetry is set to."""
    body = normalize_message(message)
    contact = normalize_email(email)
    label = normalize_source(source)

    status = post_json(
        feedback_endpoint(),
        {
            "message": body,
            "email": contact,
            "source": label,
            "lemoncrow_version": _service_version(),
        },
        timeout_s=FORWARD_TIMEOUT_SECONDS,
        attempts=FORWARD_ATTEMPTS,
    )
    if 200 <= status < 300:
        return FeedbackResult(delivered="remote", status=status)
    return FeedbackResult(delivered="failed", status=status, detail=f"sink_status_{status}")

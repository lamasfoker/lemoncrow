"""Review-specific exception types kept interpreted for mypyc compatibility."""

from __future__ import annotations


class AnnotationVersionConflict(RuntimeError):
    """A semantic comment changed after the caller last read its version."""


class ReviewArtifactReclaimed(RuntimeError):
    """An artifact a new Review row references was reclaimed as an orphan before the row committed.

    Raised by a hosted Snapshot (its packet, blobs, media or submodule artifacts)
    or evidence row (its uploaded bytes) whose writer stalled past the orphan GC
    grace. Nothing was committed; repeating the request writes the artifacts
    again, so routes answer it as a retryable conflict (HTTP 409, ``Retry-After``).
    """


class HostedAttachmentConflict(RuntimeError):
    """A local<->hosted attachment write lost to the current attachment state (G5-SYNC).

    ``code`` is stable and machine-readable: ``not_attached``,
    ``already_attached``, ``hosted_review_taken``, ``identity_changed``,
    ``unresolved_actions``, ``queue_full``, ``duplicate_action``,
    ``action_unknown`` or ``action_state_changed``.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class HostedActionRefused(ValueError):
    """A local action cannot be queued for hosted replay; ``code`` names why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class HostedUnavailable(RuntimeError):
    """The hosted Review could not be reached; pending actions stay pending."""


class HostedReplayRefused(RuntimeError):
    """Hosted refused a replay request outright: ``forbidden`` stops the loop, ``rejected`` ends one action."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


__all__ = [
    "AnnotationVersionConflict",
    "HostedActionRefused",
    "HostedAttachmentConflict",
    "HostedReplayRefused",
    "HostedUnavailable",
    "ReviewArtifactReclaimed",
]

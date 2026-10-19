"""Small in-memory facts about automatic client recovery decisions.

The thin client is intentionally standard-library-only and does not import the
main LemonCrow runtime/telemetry stack. These facts are ephemeral; the only
persistent record is the per-call runtime trace (:mod:`.trace`), which copies
the recovery facts of the call it describes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

RecoveryKind = Literal[
    "session_reopen",
    "revision_retry",
    "write_replay",
    "blob_fill",
    "reuse_reject",
    "reconnect_retry",
]
RecoveryOutcome = Literal["retrying", "recovered", "failed"]


@dataclass(frozen=True, slots=True)
class RecoveryEvent:
    """One content-free recovery fact."""

    kind: RecoveryKind
    outcome: RecoveryOutcome
    error_code: str = ""
    tool: str = ""
    client_seq: int = 0
    from_revision: int | None = None
    to_revision: int | None = None
    count: int = 0
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


__all__ = ["RecoveryEvent", "RecoveryKind", "RecoveryOutcome"]

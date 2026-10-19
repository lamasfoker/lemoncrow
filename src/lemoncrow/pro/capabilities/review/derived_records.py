"""Versioned JSON records stored as ``review_derived`` rows, moved only by compare-and-set.

P12-AGENT keeps its split requests and agent-action approval requests the
same way P12-FIX keeps Review actions (:class:`ReviewActionLedger`): one row per
record under the Snapshot it was made against, created insert-if-absent on its
id and changed only by a store-level compare-and-set on the row stamp, so two
server processes (or two hosted replicas) racing the same record agree on one
winner. No schema: SQLite and hosted PostgreSQL both carry ``review_derived``.

Writer: :meth:`DerivedRecordLedger.create` / :meth:`DerivedRecordLedger.update`.
Closer: the Snapshot/Review deletion cascade owns retention, exactly as for
Review actions; nothing here deletes a row.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final

_CAS_ATTEMPTS: Final[int] = 8


class RecordConflict(ValueError):
    """The record is not in a state that allows this change, or kept changing concurrently."""


class DerivedRecordLedger:
    """One record kind in ``review_derived``; payloads carry ``id`` and ``version``."""

    def __init__(self, store: Any, kind: str, *, stamp_prefix: str = "v1") -> None:
        self._store = store
        self.kind = kind
        self._prefix = stamp_prefix

    def _stamp(self, version: int) -> str:
        return f"{self._prefix}:{version}"

    def _current(self, stamp: str) -> bool:
        return stamp.split(":", 1)[0] == self._prefix

    def _row(self, review_id: str, revision_id: str, payload: Mapping[str, Any]) -> Any:
        from lemoncrow.pro.capabilities.review.store import DerivedResult

        return DerivedResult(
            review_id=review_id,
            revision_id=revision_id,
            kind=self.kind,
            scope_key=str(payload["id"]),
            stamp=self._stamp(int(payload.get("version") or 1)),
            payload=dict(payload),
        )

    def rows(self, revision_ids: Iterable[str]) -> list[tuple[str, str, dict[str, Any]]]:
        """``(revision_id, stamp, payload)`` of every current-format record on these Snapshots."""

        found = self._store.derived_results(self.kind, list(revision_ids))
        return [
            (revision_id, stamp, payload)
            for (revision_id, _scope), (stamp, payload) in found.items()
            if self._current(stamp) and isinstance(payload, dict) and payload.get("id")
        ]

    def get(self, revision_ids: Iterable[str], record_id: str) -> tuple[str, dict[str, Any]] | None:
        """``(revision_id, payload)`` of one record, or ``None``."""

        return next(
            (
                (revision_id, payload)
                for revision_id, _stamp, payload in self.rows(revision_ids)
                if payload["id"] == record_id
            ),
            None,
        )

    def create(self, review_id: str, revision_id: str, payload: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
        """Store a record once; an existing id is returned as stored (idempotent replay)."""

        record = {**payload, "version": int(payload.get("version") or 1)}
        created, _stamp, stored = self._store.insert_derived_result(self._row(review_id, revision_id, record))
        return created, (record if created else dict(stored))

    def update(
        self,
        review_id: str,
        revision_id: str,
        record_id: str,
        change: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        """Apply *change* to the stored record by compare-and-set; *change* raises to refuse.

        A concurrent writer that moved the record first makes this attempt re-read,
        and *change* then judges the newer state instead of overwriting it.
        """

        for _attempt in range(_CAS_ATTEMPTS):
            found = next(
                (
                    (stamp, payload)
                    for stored_revision, stamp, payload in self.rows((revision_id,))
                    if stored_revision == revision_id and payload["id"] == record_id
                ),
                None,
            )
            if found is None:
                raise LookupError(f"no such {self.kind} record {record_id!r}")
            stamp, current = found
            updated = change(dict(current))
            updated = {**updated, "id": record_id, "version": int(current.get("version") or 1) + 1}
            if self._store.compare_and_set_derived_result(
                self._row(review_id, revision_id, updated), expected_stamp=stamp
            ):
                return updated
        raise RecordConflict(f"{self.kind} record is changing concurrently; reload and try again")


__all__ = ["DerivedRecordLedger", "RecordConflict"]

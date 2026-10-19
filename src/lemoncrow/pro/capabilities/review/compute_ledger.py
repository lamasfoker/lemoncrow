"""Local compute ledger: metered execution time per Snapshot, catalog entry, executor and stage.

Hosted execution is metered from durable job leases. Executions that run in this
process (local runtime validation, surface runs) are metered here, so a local
Review shows compute minutes too and per-Review minute ceilings see them.

Rows live in ``review_derived`` (kind ``compute_usage``, one row per entry,
executor and stage) and are deleted with their Snapshot or Review. Executors
report through the typed ``ComputeRecorder.record_compute`` hook; per-stage
minutes from the hermetic execution contract plug in there.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final, Protocol

from lemoncrow.pro.capabilities.review.store import DerivedResult

COMPUTE_KIND: Final[str] = "compute_usage"
_STAMP: Final[str] = "v1"
_MAX_FIELD: Final[int] = 256


class ComputeRecorder(Protocol):
    """Where an executor reports metered compute for one Snapshot."""

    def record_compute(self, entry_id: str, executor_class: str, stage: str, seconds: float) -> None: ...


@dataclass(frozen=True, slots=True)
class ComputeRow:
    entry_id: str
    executor_class: str
    stage: str
    seconds: float
    runs: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "executor": self.executor_class,
            "stage": self.stage,
            "compute_minutes": round(self.seconds / 60.0, 3),
            "runs": self.runs,
        }


def _field(name: str, value: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > _MAX_FIELD:
        raise ValueError(f"compute {name} must be 1..{_MAX_FIELD} characters")
    return text


class ComputeLedger:
    """Per-Snapshot compute rows in one Review store."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def record(
        self,
        *,
        review_id: str,
        revision_id: str,
        entry_id: str,
        executor_class: str,
        stage: str,
        seconds: float,
    ) -> None:
        value = float(seconds)
        if not math.isfinite(value) or value < 0:
            raise ValueError("compute seconds must be a finite, non-negative number")
        entry, executor, step = _field("entry_id", entry_id), _field("executor", executor_class), _field("stage", stage)
        # One additive store-side write: atomic across threads, processes and
        # hosted replicas sharing the store (no read-modify-write here).
        self._store.accumulate_derived_result(
            DerivedResult(
                review_id=review_id,
                revision_id=revision_id,
                kind=COMPUTE_KIND,
                scope_key=f"{entry}|{executor}|{step}",
                stamp=_STAMP,
                payload={"entry_id": entry, "executor": executor, "stage": step},
            ),
            increments={"seconds": value, "runs": 1},
        )

    def recorder(self, review_id: str, revision_id: str) -> ComputeRecorder:
        return _SnapshotRecorder(self, review_id, revision_id)

    def rows(self, revision_ids: Iterable[str]) -> tuple[ComputeRow, ...]:
        """Compute per entry, executor and stage, summed over these Snapshots."""

        totals: dict[tuple[str, str, str], list[float]] = {}
        for (_revision_id, _scope), (stamp, payload) in self._store.derived_results(
            COMPUTE_KIND, list(revision_ids)
        ).items():
            if stamp != _STAMP:
                continue
            key = (
                str(payload.get("entry_id") or ""),
                str(payload.get("executor") or ""),
                str(payload.get("stage") or ""),
            )
            bucket = totals.setdefault(key, [0.0, 0.0])
            bucket[0] += float(payload.get("seconds") or 0.0)
            bucket[1] += int(payload.get("runs") or 0)
        return tuple(
            ComputeRow(entry_id=entry, executor_class=executor, stage=stage, seconds=seconds, runs=int(runs))
            for (entry, executor, stage), (seconds, runs) in sorted(totals.items())
        )

    def projection(self, revision_ids: Iterable[str]) -> dict[str, Any]:
        rows = self.rows(revision_ids)
        return {
            "rows": [row.to_wire() for row in rows],
            "totals": {
                "compute_minutes": round(sum(row.seconds for row in rows) / 60.0, 3),
                "runs": sum(row.runs for row in rows),
            },
            "source": "local",
        }

    def minutes(self, revision_ids: Iterable[str]) -> float:
        return sum(row.seconds for row in self.rows(revision_ids)) / 60.0


@dataclass(frozen=True, slots=True)
class _SnapshotRecorder:
    ledger: ComputeLedger
    review_id: str
    revision_id: str

    def record_compute(self, entry_id: str, executor_class: str, stage: str, seconds: float) -> None:
        self.ledger.record(
            review_id=self.review_id,
            revision_id=self.revision_id,
            entry_id=entry_id,
            executor_class=executor_class,
            stage=stage,
            seconds=seconds,
        )


__all__ = ["COMPUTE_KIND", "ComputeLedger", "ComputeRecorder", "ComputeRow"]

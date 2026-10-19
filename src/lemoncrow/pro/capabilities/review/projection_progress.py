"""Bounded, process-local feedback for Brief preparation; never Review authority."""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from typing import Any

BRIEF_STAGES = 10


class ProjectionProgress:
    def __init__(self, limit: int = 128) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._rows: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def start(self, revision_id: str) -> str:
        token = uuid.uuid4().hex
        with self._lock:
            self._rows[revision_id] = {
                "request_id": token,
                "state": "running",
                "completed": 0,
                "total": BRIEF_STAGES,
                "detail": "Reading review plan",
                "started_at": time.time(),
            }
            self._rows.move_to_end(revision_id)
            while len(self._rows) > self._limit:
                self._rows.popitem(last=False)
        return token

    def update(self, revision_id: str, token: str, completed: int, detail: str, *, state: str = "running") -> None:
        with self._lock:
            row = self._rows.get(revision_id)
            # A superseded request or an evicted Snapshot cannot re-enter.
            if row is not None and row["request_id"] == token:
                row.update(completed=completed, detail=detail, state=state)

    def get(self, revision_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._rows.get(revision_id)
            return (
                {key: value for key, value in row.items() if key != "request_id"}
                if row
                else {
                    "state": "pending",
                    "completed": 0,
                    "total": BRIEF_STAGES,
                    "detail": "Waiting for Brief preparation",
                }
            )

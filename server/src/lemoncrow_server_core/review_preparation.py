"""Small, bounded startup jobs independent of a Review's source and database."""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from typing import Any

_STEPS = (
    ("browser", "Opening Review Reader"),
    ("range", "Resolving review range"),
    ("capture", "Starting source preparation"),
    ("packet_diff", "Reading changed files and diff"),
    ("packet_source", "Loading exact old/new source snapshots"),
    ("packet_ranking", "Ranking the human review order"),
    ("packet_symbols", "Identifying changed symbols for stable Review targets"),
    ("packet_ready", "Preparing change packet"),
    ("connect", "Connecting to Review server"),
    ("sync_current", "Synchronizing current source"),
    ("publish", "Publishing reviewable source revision"),
    ("state_overview", "Loading review overview"),
    ("state_units", "Loading review targets and marks"),
    ("state_annotations", "Loading review comments"),
)


class ReviewPreparationJobs:
    """Ephemeral UI feedback; never source authority or durable review state."""

    def __init__(self) -> None:
        self._jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def create(self) -> dict[str, Any]:
        now = time.time()
        for key, row in tuple(self._jobs.items()):
            if now - row["updated_at"] > 3600:
                del self._jobs[key]
        job_id = uuid.uuid4().hex
        row = {
            "id": job_id,
            "state": "running",
            "detail": "",
            "review_url": "",
            "created_at": now,
            "updated_at": now,
            "steps": [{"key": key, "label": label, "state": "pending", "elapsed": None} for key, label in _STEPS],
        }
        self._jobs[job_id] = row
        while len(self._jobs) > 128:
            self._jobs.popitem(last=False)
        return row

    def get(self, job_id: str) -> dict[str, Any] | None:
        row = self._jobs.get(job_id)
        if row is not None and row["state"] == "running" and time.time() - row["updated_at"] > 600:
            self.update(job_id, stage="failed", detail="Preparation stopped. Run lc review again to retry.")
        return row

    def update(self, job_id: str, *, stage: str = "", detail: str = "", review_url: str = "") -> dict[str, Any] | None:
        row = self._jobs.get(job_id)
        if row is None:
            return None
        if row["state"] != "running":
            return row
        now = time.time()
        row["updated_at"] = now
        if review_url:
            row["review_url"] = review_url
        if stage in {"ready", "failed"}:
            row["state"] = stage
            row["detail"] = detail
            for step in row["steps"]:
                if step["state"] == "running":
                    step["state"] = "failed" if stage == "failed" else "done"
                    step["elapsed"] = now - step["started_at"]
                elif step["state"] == "pending":
                    step["state"] = "skipped"
            return row
        if not stage:
            return row
        done = stage.endswith("_done")
        key = stage.removesuffix("_done")
        step = next((item for item in row["steps"] if item["key"] == key), None)
        if step is None:
            if len(row["steps"]) >= 48:
                return row
            step = {"key": key, "label": detail, "state": "pending", "elapsed": None}
            row["steps"].append(step)
        step["label"] = detail or step["label"]
        if done:
            step["state"] = "done"
            step["elapsed"] = now - step["started_at"] if "started_at" in step else None
        else:
            if step["state"] != "running":
                step["started_at"] = now
            step["state"] = "running"
            step["elapsed"] = None
        if key == "packet_ready" and done:
            for item in row["steps"]:
                if item["key"].startswith("packet_") and item["state"] == "pending":
                    item["state"] = "skipped"
        return row

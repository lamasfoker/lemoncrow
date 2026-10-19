"""Low-cardinality indexing/search scalability telemetry.

The counters here deliberately avoid tenant/repository/view labels. They are
process-level operational evidence for scale work, not an audit stream. The
same object is shared by repository-partitioned leaves so one local server has
one coherent set of measurements.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

__all__ = ["IndexOpsMetrics", "IndexOpsSnapshot"]

_QUERY_CHANNELS: Final[frozenset[str]] = frozenset({"lexical", "symbol", "relations", "semantic", "code_search"})
_LAYER_NAMES: Final[frozenset[str]] = frozenset({"link", "semantic"})
_LAYER_STATES: Final[frozenset[str]] = frozenset({"cold", "building", "ready"})
_OBJECT_OPS: Final[frozenset[str]] = frozenset({"get", "put", "delete", "missing"})
_BUNDLE_OPS: Final[frozenset[str]] = frozenset({"publish", "load", "hit", "miss", "invalid", "stale"})


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@dataclass(frozen=True, slots=True)
class IndexOpsSnapshot:
    counters: Mapping[tuple[str, tuple[tuple[str, str], ...]], float]
    gauges: Mapping[tuple[str, tuple[tuple[str, str], ...]], float]

    def value(self, name: str, **labels: str) -> float:
        key = (name, tuple(sorted(labels.items())))
        return float(self.counters.get(key, self.gauges.get(key, 0.0)))


class IndexOpsMetrics:
    """Thread-safe scale telemetry shared by query, storage and workers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

    @staticmethod
    def _key(name: str, labels: Mapping[str, str]) -> tuple[str, tuple[tuple[str, str], ...]]:
        return name, tuple(sorted(labels.items()))

    def _add(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            self._counters[self._key(name, labels)] += float(value)

    def _set(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            self._gauges[self._key(name, labels)] = float(value)

    def membership(self, *, operation: str, entries: int, duration_s: float) -> None:
        operation = operation if operation in _QUERY_CHANNELS else "code_search"
        labels = {"operation": operation}
        self._add("lemoncrow_index_membership_build_seconds_count", 1, **labels)
        self._add("lemoncrow_index_membership_build_seconds_sum", max(0.0, duration_s), **labels)
        self._add("lemoncrow_index_membership_entries_total", max(0, entries), **labels)

    def query_plan(self, *, intent: str, channels: Sequence[str]) -> None:
        safe_intent = (
            intent
            if intent
            in {
                "pipe_alternatives",
                "regex",
                "exact_symbol",
                "path",
                "impact",
                "definition",
                "natural_language",
                "hybrid",
            }
            else "hybrid"
        )
        self._add("lemoncrow_index_query_plans_total", 1, intent=safe_intent)
        for channel in channels:
            if channel in {"path", "symbol", "lexical", "relations", "semantic", "regex"}:
                self._add("lemoncrow_index_query_plan_channels_total", 1, channel=channel)

    def retrieval(self, *, channel: str, candidates: int, hits: int, duration_s: float) -> None:
        channel = channel if channel in _QUERY_CHANNELS else "code_search"
        labels = {"channel": channel}
        self._add("lemoncrow_index_retrieval_seconds_count", 1, **labels)
        self._add("lemoncrow_index_retrieval_seconds_sum", max(0.0, duration_s), **labels)
        self._add("lemoncrow_index_retrieval_candidates_total", max(0, candidates), **labels)
        self._add("lemoncrow_index_retrieval_hits_total", max(0, hits), **labels)

    def semantic_scan(self, *, vectors: int, duration_s: float) -> None:
        self._add("lemoncrow_index_semantic_scan_seconds_count", 1)
        self._add("lemoncrow_index_semantic_scan_seconds_sum", max(0.0, duration_s))
        self._add("lemoncrow_index_semantic_vectors_scored_total", max(0, vectors))
        self._set("lemoncrow_index_semantic_vectors_last", max(0, vectors))

    def semantic_ann(self, *, total_units: int, candidates: int, duration_s: float) -> None:
        self._add("lemoncrow_index_semantic_ann_seconds_count", 1)
        self._add("lemoncrow_index_semantic_ann_seconds_sum", max(0.0, duration_s))
        self._add("lemoncrow_index_semantic_ann_candidates_total", max(0, candidates))
        self._set("lemoncrow_index_semantic_ann_candidates_last", max(0, candidates))
        self._set("lemoncrow_index_semantic_ann_units_last", max(0, total_units))

    def metadata_read(self, *, rows: int, bytes_read: int) -> None:
        self._add("lemoncrow_index_metadata_rows_read_total", max(0, rows))
        self._add("lemoncrow_index_metadata_bytes_read_total", max(0, bytes_read))

    def metadata_pool(
        self,
        *,
        wait_s: float,
        size: int,
        available: int,
        waiting: int,
        acquired: bool = True,
    ) -> None:
        """Record bounded PostgreSQL-pool pressure without tenant labels."""
        metric = (
            "lemoncrow_index_metadata_pool_acquires_total"
            if acquired
            else "lemoncrow_index_metadata_pool_timeouts_total"
        )
        self._add(metric, 1)
        self._add("lemoncrow_index_metadata_pool_wait_seconds_sum", max(0.0, wait_s))
        self._set("lemoncrow_index_metadata_pool_size", max(0, size))
        self._set("lemoncrow_index_metadata_pool_available", max(0, available))
        self._set("lemoncrow_index_metadata_pool_waiting", max(0, waiting))

    def metadata_transaction(self, *, duration_s: float, outcome: str) -> None:
        """Record PostgreSQL transaction latency and commit/rollback outcome."""
        if outcome not in {"committed", "rolled_back", "failed"}:
            outcome = "failed"
        labels = {"outcome": outcome}
        self._add("lemoncrow_index_metadata_transactions_total", 1, **labels)
        self._add("lemoncrow_index_metadata_transaction_seconds_sum", max(0.0, duration_s), **labels)

    def generation_bundle(self, *, operation: str, bytes_transferred: int = 0) -> None:
        operation = operation if operation in _BUNDLE_OPS else "invalid"
        self._add("lemoncrow_index_generation_bundle_operations_total", 1, operation=operation)
        self._add(
            "lemoncrow_index_generation_bundle_bytes_total",
            max(0, bytes_transferred),
            operation=operation,
        )

    def object_store(self, *, operation: str, operations: int = 1, bytes_transferred: int = 0) -> None:
        operation = operation if operation in _OBJECT_OPS else "get"
        labels = {"operation": operation}
        self._add("lemoncrow_index_object_store_operations_total", max(0, operations), **labels)
        self._add("lemoncrow_index_object_store_bytes_total", max(0, bytes_transferred), **labels)

    def layer(self, *, layer: str, state: str, revision_lag: int, covered_paths: int) -> None:
        layer = layer if layer in _LAYER_NAMES else "link"
        state = state if state in _LAYER_STATES else "building"
        for candidate in _LAYER_STATES:
            self._set(
                "lemoncrow_index_layer_state",
                int(candidate == state),
                layer=layer,
                state=candidate,
            )
        self._set("lemoncrow_index_layer_revision_lag", max(0, revision_lag), layer=layer)
        self._set("lemoncrow_index_layer_covered_paths", max(0, covered_paths), layer=layer)

    def worker_queue(self, *, queued: int, running: int) -> None:
        self._set("lemoncrow_index_worker_jobs", max(0, queued), state="queued")
        self._set("lemoncrow_index_worker_jobs", max(0, running), state="running")

    def worker_outcome(self, *, layer: str, outcome: str, duration_s: float) -> None:
        layer = layer if layer in _LAYER_NAMES else "link"
        labels = {"layer": layer, "outcome": outcome}
        self._add("lemoncrow_index_worker_jobs_total", 1, **labels)
        self._add("lemoncrow_index_worker_duration_seconds_count", 1, **labels)
        self._add("lemoncrow_index_worker_duration_seconds_sum", max(0.0, duration_s), **labels)

    def snapshot(self) -> IndexOpsSnapshot:
        with self._lock:
            return IndexOpsSnapshot(counters=dict(self._counters), gauges=dict(self._gauges))

    def render_prometheus(self) -> str:
        snapshot = self.snapshot()
        lines = [
            "# HELP lemoncrow_index_metrics_info Index scalability telemetry is enabled.",
            "# TYPE lemoncrow_index_metrics_info gauge",
            "lemoncrow_index_metrics_info 1",
        ]
        for source in (snapshot.counters, snapshot.gauges):
            for (name, labels), value in sorted(source.items()):
                label_text = ""
                if labels:
                    label_text = "{" + ",".join(f'{key}="{_label(val)}"' for key, val in labels) + "}"
                lines.append(f"{name}{label_text} {value:g}")
        return "\n".join(lines) + "\n"

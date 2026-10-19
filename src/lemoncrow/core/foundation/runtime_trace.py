"""One runtime trace per tool call: request, decisions, retrieval, execution, outcome.

A trace is identified by an ``lt_<16 hex>`` id that the *first* layer to see a
call mints (normally the thin client) and every later layer reuses, so the
client record, the server record and the in-process runtime record for one
call share one id.  Layers nest through a context variable: an inner layer that
opens a trace with the id of the active trace joins it instead of writing a
second record.

Privacy
-------
``raw`` (local default) keeps queries, paths and line ranges so a trace can be
replayed against a different ranker.  Bulk source text (edit bodies, file
contents, diffs) is *never* stored in any mode: it is reduced to a length +
digest.  ``redacted`` hashes every sensitive leaf with the same rules as
:mod:`runtime_decisions` and is what shared/hosted deployments use.  ``off``
records nothing.

Storage
-------
Append-only JSONL under ``<root>/traces/<YYYY-MM-DD>/<writer>-<pid>.jsonl``.
One file per writer process means concurrent writers never interleave lines.
Retention: day directories older than ``LEMONCROW_TRACE_RETENTION_DAYS``
(default 14) are removed, then the oldest days are removed until the store is
under ``LEMONCROW_TRACE_MAX_MB`` (default 256).  Pruning runs at most once an
hour per process, on write.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from lemoncrow.core.foundation.runtime_decisions import RuntimeDecisionEvent, _sanitize_value

TraceMode = Literal["raw", "redacted", "off"]

TRACE_SCHEMA_VERSION = 1
TRACE_ID_PREFIX = "lt_"
TRACE_ID_RE = re.compile(r"^lt_[0-9a-f]{16}$")

#: Argument keys whose values are bulk source/content. Never stored verbatim.
BULK_CONTENT_KEYS = frozenset(
    {"content", "new", "old", "new_string", "old_string", "text", "source", "diff", "patch", "body", "input"}
)

_MAX_DECISIONS = 64
_MAX_CANDIDATES = 32
_MAX_ARG_CHARS = 2048
_MAX_ARG_ITEMS = 64
_MAX_DEPTH = 5
_PRUNE_INTERVAL_S = 3600.0


def new_trace_id() -> str:
    """Mint one opaque trace id."""
    return f"{TRACE_ID_PREFIX}{secrets.token_hex(8)}"


def is_trace_id(value: object) -> bool:
    return isinstance(value, str) and bool(TRACE_ID_RE.match(value))


def trace_mode_from_env(default: TraceMode = "raw") -> TraceMode:
    raw = os.environ.get("LEMONCROW_RUNTIME_TRACE", "").strip().lower()
    if raw in {"off", "0", "false", "disabled", "none"}:
        return "off"
    if raw in {"redacted", "redact", "hashed"}:
        return "redacted"
    if raw in {"raw", "on", "1", "true"}:
        return "raw"
    return default


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:20]


def _bulk_stub(value: Any) -> dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value, default=str, sort_keys=True)
    return {"omitted": "bulk_content", "chars": len(text), "sha256": _digest(text)}


def _raw_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if key.lower() in BULK_CONTENT_KEYS and value is not None:
        return _bulk_stub(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value if len(value) <= _MAX_ARG_CHARS else value[:_MAX_ARG_CHARS] + f"…[+{len(value) - _MAX_ARG_CHARS}]"
    if depth >= _MAX_DEPTH:
        return {"truncated": "max_depth"}
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for index, (item_key, item_value) in enumerate(value.items()):
            if index >= _MAX_ARG_ITEMS:
                out["…"] = f"+{len(value) - _MAX_ARG_ITEMS} keys"
                break
            name = str(item_key)[:64]
            out[name] = _raw_value(item_value, key=name, depth=depth + 1)
        return out
    if isinstance(value, list | tuple | set | frozenset):
        items = list(value)
        bounded = [_raw_value(item, depth=depth + 1) for item in items[:_MAX_ARG_ITEMS]]
        if len(items) > _MAX_ARG_ITEMS:
            bounded.append(f"…+{len(items) - _MAX_ARG_ITEMS}")
        return bounded
    return str(value)[:_MAX_ARG_CHARS]


def _redacted_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Default-deny view of an argument: every string leaf is hashed.

    Tool arguments are open-ended (``symbol``, ``url``, ``target``, ``pattern``,
    ``cwd`` ... and whatever a tool adds next), so a list of "sensitive" keys
    cannot be complete. Only structure and non-string scalars (numbers, bools,
    ``None``) survive; bulk-content keys keep their length + digest stub.
    """
    if key.lower() in BULK_CONTENT_KEYS and value is not None:
        return _bulk_stub(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return {"redacted_sha256": _digest(value)}
    if depth >= _MAX_DEPTH:
        return {"truncated": "max_depth"}
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for index, (item_key, item_value) in enumerate(value.items()):
            if index >= _MAX_ARG_ITEMS:
                out["…"] = f"+{len(value) - _MAX_ARG_ITEMS} keys"
                break
            name = str(item_key)[:64]
            out[name] = _redacted_value(item_value, key=name, depth=depth + 1)
        return out
    if isinstance(value, list | tuple | set | frozenset):
        items = list(value)
        bounded = [_redacted_value(item, depth=depth + 1) for item in items[:_MAX_ARG_ITEMS]]
        if len(items) > _MAX_ARG_ITEMS:
            bounded.append(f"…+{len(items) - _MAX_ARG_ITEMS}")
        return bounded
    return {"redacted_sha256": _digest(str(value))}


def sanitize_arguments(arguments: Mapping[str, Any] | None, mode: TraceMode) -> dict[str, Any]:
    """Arguments as stored in a trace for ``mode`` (bulk content never kept)."""
    if not arguments or mode == "off":
        return {}
    value = _redacted_value(dict(arguments)) if mode == "redacted" else _raw_value(dict(arguments))
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True, slots=True)
class TraceCandidate:
    """One retrieval candidate as seen by the ranker (pointer, never source)."""

    path: str
    score: float
    rank: int
    start_line: int | None = None
    end_line: int | None = None
    features: Mapping[str, float] = field(default_factory=dict)
    symbols: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"path": self.path, "score": round(float(self.score), 6), "rank": self.rank}
        if self.symbols:
            payload["symbols"] = [str(s)[:128] for s in self.symbols[:16]]
        if self.start_line is not None:
            payload["start_line"] = self.start_line
        if self.end_line is not None:
            payload["end_line"] = self.end_line
        if self.features:
            payload["features"] = {str(k)[:48]: round(float(v), 6) for k, v in list(self.features.items())[:24]}
        return payload


@dataclass(slots=True)
class RuntimeTrace:
    """Mutable in-flight trace. Finalized into one JSON line on close."""

    trace_id: str
    writer: str
    tool: str
    mode: TraceMode
    session_id: str | None = None
    workspace_id: str | None = None
    workspace_revision: str | int | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    _started_perf: float = field(default_factory=time.perf_counter)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    dropped_decisions: int = 0
    retrieval: dict[str, Any] | None = None
    execution: dict[str, Any] = field(default_factory=dict)
    outcome: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- recording --------------------------------------------------------- #

    def record_decision(self, event: RuntimeDecisionEvent) -> None:
        if self.mode == "off":
            return
        payload = event.to_payload()
        payload.pop("session_id", None)
        if self.mode == "redacted":
            payload = _redact_decision(payload)
        with self._lock:
            # One event is often fanned out to several sinks (ledger +
            # optimization recorder); the trace keeps it once.
            if any(existing.get("id") == event.id for existing in self.decisions):
                return
            if len(self.decisions) >= _MAX_DECISIONS:
                self.dropped_decisions += 1
                return
            self.decisions.append(payload)

    def record_retrieval(
        self,
        *,
        query: str,
        candidates: Sequence[TraceCandidate],
        selected: Sequence[str] = (),
        channel: str = "",
        ranker: str = "",
        limit: int | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        if self.mode == "off":
            return
        stored_query: Any = query if self.mode == "raw" else {"redacted_sha256": _digest(query)}
        rows = [candidate.to_payload() for candidate in list(candidates)[:_MAX_CANDIDATES]]
        if self.mode == "redacted":
            for row in rows:
                row["path"] = {"redacted_sha256": _digest(str(row["path"]))}
                row.pop("symbols", None)
            chosen: list[Any] = [{"redacted_sha256": _digest(p)} for p in selected]
        else:
            chosen = list(selected)[:_MAX_CANDIDATES]
        retrieval: dict[str, Any] = {
            "query": stored_query,
            "channel": channel,
            "ranker": ranker,
            "candidates": rows,
            "candidate_total": len(candidates),
            "selected": chosen,
        }
        if limit is not None:
            retrieval["limit"] = int(limit)
        if extra:
            retrieval["extra"] = self._bounded_extra(extra)
        with self._lock:
            self.retrieval = retrieval

    def finalize_retrieval(self, *, selected: Sequence[str], extra: Mapping[str, Any] | None = None) -> None:
        """Replace the selected set after post-ranking policy (expansion etc.)."""
        if self.mode == "off":
            return
        with self._lock:
            if self.retrieval is None:
                return
            if self.mode == "redacted":
                self.retrieval["selected"] = [{"redacted_sha256": _digest(p)} for p in selected]
            else:
                self.retrieval["selected"] = list(selected)[:_MAX_CANDIDATES]
            if extra:
                merged = dict(self.retrieval.get("extra") or {})
                merged.update(self._bounded_extra(extra))
                self.retrieval["extra"] = merged

    def _bounded_extra(self, extra: Mapping[str, Any]) -> dict[str, Any]:
        if self.mode == "redacted":
            value = _redact_targets(_sanitize_value({k: v for k, v in extra.items() if k != "scopes"}))
        else:
            value = _raw_value(dict(extra))
        return dict(value) if isinstance(value, Mapping) else {}

    def note(self, section: Literal["execution", "outcome", "metadata"], **values: Any) -> None:
        bounded = _sanitize_value(values) if self.mode == "redacted" else _raw_value(values)
        if not isinstance(bounded, Mapping):
            return
        with self._lock:
            getattr(self, section).update(bounded)

    # -- finalization ------------------------------------------------------ #

    def elapsed_ms(self) -> int:
        return max(0, round((time.perf_counter() - self._started_perf) * 1000))

    def to_record(self) -> dict[str, Any]:
        with self._lock:
            execution = dict(self.execution)
            execution.setdefault("duration_ms", self.elapsed_ms())
            record: dict[str, Any] = {
                "schema_version": TRACE_SCHEMA_VERSION,
                "trace_id": self.trace_id,
                "writer": self.writer,
                "mode": self.mode,
                "tool": self.tool,
                "started_at": self.started_at.isoformat(),
                "session_id": self.session_id if self.mode == "raw" else _redact_opt(self.session_id),
                "workspace": {
                    "id": self.workspace_id if self.mode == "raw" else _redact_opt(self.workspace_id),
                    "revision": self.workspace_revision,
                },
                "request": {"arguments": self.arguments},
                "decisions": list(self.decisions),
                "execution": execution,
                "outcome": dict(self.outcome),
            }
            if self.dropped_decisions:
                record["dropped_decisions"] = self.dropped_decisions
            if self.retrieval is not None:
                record["retrieval"] = self.retrieval
            if self.metadata:
                record["metadata"] = dict(self.metadata)
        return record


_DECISION_TARGET_KEYS = frozenset({"target", "target_path", "symbol", "related_to"})


def _redact_targets(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            k: ({"redacted_sha256": _digest(str(v))} if k in _DECISION_TARGET_KEYS and v else _redact_targets(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_targets(v) for v in value]
    return value


def _redact_decision(payload: dict[str, Any]) -> dict[str, Any]:
    """Shared-deployment view of a decision: refs and targets hashed."""
    out = dict(payload)
    out["evidence_refs"] = [_digest(str(ref)) for ref in payload.get("evidence_refs") or ()]
    for key in ("proposed", "actual", "metrics"):
        if key in out:
            out[key] = _redact_targets(out[key])
    return out


def _redact_opt(value: str | None) -> dict[str, str] | None:
    return None if value is None else {"redacted_sha256": _digest(value)}


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


def _env_int(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    if raw:
        with contextlib.suppress(ValueError):
            return max(low, min(high, int(raw)))
    return default


class TraceStore:
    """Append-only, retention-bounded JSONL store for runtime traces."""

    def __init__(
        self,
        root: Path,
        *,
        retention_days: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self.dir = Path(root) / "traces"
        self.retention_days = (
            retention_days if retention_days is not None else _env_int("LEMONCROW_TRACE_RETENTION_DAYS", 14, 1, 365)
        )
        self.max_bytes = (
            max_bytes if max_bytes is not None else _env_int("LEMONCROW_TRACE_MAX_MB", 256, 1, 65536) * 1024 * 1024
        )
        self._lock = threading.Lock()
        self._last_prune = 0.0

    # -- write ------------------------------------------------------------- #

    def append(self, record: Mapping[str, Any]) -> Path | None:
        writer = re.sub(r"[^a-z0-9_-]", "_", str(record.get("writer") or "runtime").lower())[:32]
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        target = self.dir / day / f"{writer}-{os.getpid()}.jsonl"
        line = json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
        try:
            with self._lock:
                # Owner-only: raw traces hold queries and paths.
                self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.parent.mkdir(mode=0o700, exist_ok=True)
                fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as handle:
                    handle.write(line)
        except OSError:
            return None
        self._maybe_prune()
        return target

    # -- read -------------------------------------------------------------- #

    def _day_dirs(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        return sorted(
            (p for p in self.dir.iterdir() if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)),
            reverse=True,
        )

    def iter_records(self, *, days: int | None = None) -> Iterator[dict[str, Any]]:
        """Newest day first; within a day, file order then line order."""
        for index, day_dir in enumerate(self._day_dirs()):
            if days is not None and index >= days:
                return
            for path in sorted(day_dir.glob("*.jsonl")):
                try:
                    handle = path.open(encoding="utf-8")
                except OSError:
                    continue
                with handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(record, dict):
                            yield record

    def find(self, trace_id: str) -> list[dict[str, Any]]:
        """Every layer's record for one trace id (client, server, runtime)."""
        needle = f'"trace_id":"{trace_id}"'
        found: list[dict[str, Any]] = []
        for day_dir in self._day_dirs():
            for path in sorted(day_dir.glob("*.jsonl")):
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                if needle not in text:
                    continue
                for line in text.splitlines():
                    if needle in line:
                        with contextlib.suppress(json.JSONDecodeError):
                            record = json.loads(line)
                            if isinstance(record, dict) and record.get("trace_id") == trace_id:
                                found.append(record)
            if found:
                break
        return found

    def recent(self, limit: int = 20, *, tool: str | None = None) -> list[dict[str, Any]]:
        rows = [r for r in self.iter_records(days=self.retention_days) if tool is None or r.get("tool") == tool]
        rows.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
        return rows[: max(0, limit)]

    # -- retention --------------------------------------------------------- #

    def _maybe_prune(self) -> None:
        now = time.monotonic()
        if self._last_prune and now - self._last_prune < _PRUNE_INTERVAL_S:
            return
        self._last_prune = now
        with contextlib.suppress(OSError):
            self.prune()

    def prune(self, *, today: datetime | None = None) -> list[str]:
        """Apply age then size retention. Returns removed day names."""
        removed: list[str] = []
        cutoff = ((today or datetime.now(UTC)) - timedelta(days=self.retention_days)).strftime("%Y-%m-%d")
        days = self._day_dirs()
        for day_dir in days:
            if day_dir.name < cutoff:
                shutil.rmtree(day_dir, ignore_errors=True)
                removed.append(day_dir.name)
        kept = [d for d in days if d.name not in removed]
        sizes = {d: sum(f.stat().st_size for f in d.glob("*.jsonl") if f.is_file()) for d in kept}
        total = sum(sizes.values())
        # Oldest first; never delete the newest (active) day.
        for day_dir in reversed(kept[1:]):
            if total <= self.max_bytes:
                break
            shutil.rmtree(day_dir, ignore_errors=True)
            total -= sizes[day_dir]
            removed.append(day_dir.name)
        return removed


_STORES: dict[Path, TraceStore] = {}
_STORES_LOCK = threading.Lock()


def trace_store(root: Path | None = None) -> TraceStore:
    """Process-shared store for ``root`` (default: the LemonCrow store root)."""
    if root is None:
        from lemoncrow.core.foundation.paths import default_store_root

        root = default_store_root()
    resolved = Path(root)
    with _STORES_LOCK:
        store = _STORES.get(resolved)
        if store is None:
            store = _STORES[resolved] = TraceStore(resolved)
        return store


# --------------------------------------------------------------------------- #
# Active-trace context
# --------------------------------------------------------------------------- #

_ACTIVE: ContextVar[RuntimeTrace | None] = ContextVar("lemoncrow_runtime_trace", default=None)


def active_trace() -> RuntimeTrace | None:
    return _ACTIVE.get()


def record_decision(event: RuntimeDecisionEvent) -> None:
    """Attach a decision to the active trace, if any. Never raises."""
    trace = _ACTIVE.get()
    if trace is not None:
        with contextlib.suppress(Exception):
            trace.record_decision(event)


@dataclass(slots=True)
class TraceHandle:
    """A begun trace: activate it around execution, commit it once finalized.

    ``owned`` is False when the call joined an outer layer's trace; such a
    handle records into the outer trace and never writes a record itself.
    """

    trace: RuntimeTrace
    owned: bool
    store: TraceStore | None = None
    committed: bool = False

    @contextlib.contextmanager
    def active(self) -> Iterator[RuntimeTrace]:
        if not self.owned or _ACTIVE.get() is self.trace:
            yield self.trace
            return
        token = _ACTIVE.set(self.trace)
        try:
            yield self.trace
        finally:
            _ACTIVE.reset(token)

    def commit(self) -> None:
        """Persist once. Idempotent; a no-op for joined or ``off`` traces."""
        if not self.owned or self.committed:
            return
        self.committed = True
        if self.trace.mode == "off":
            return
        with contextlib.suppress(Exception):
            (self.store or trace_store()).append(self.trace.to_record())


def begin_trace(
    *,
    trace_id: str | None,
    writer: str,
    tool: str,
    arguments: Mapping[str, Any] | None = None,
    mode: TraceMode | None = None,
    session_id: str | None = None,
    workspace_id: str | None = None,
    workspace_revision: str | int | None = None,
    store: TraceStore | None = None,
) -> TraceHandle:
    """Begin the trace for one call, or join the active one for the same call.

    Joining: when a trace is active and ``trace_id`` is absent or equal to its
    id, the caller is an inner layer of the same call.
    """
    outer = _ACTIVE.get()
    if outer is not None and (trace_id is None or trace_id == outer.trace_id):
        return TraceHandle(trace=outer, owned=False)
    env_mode = trace_mode_from_env()
    # The operator's ``off`` always wins over a deployment/call default.
    resolved_mode: TraceMode = "off" if env_mode == "off" else (mode if mode is not None else env_mode)
    trace = RuntimeTrace(
        trace_id=trace_id if trace_id is not None and is_trace_id(trace_id) else new_trace_id(),
        writer=writer,
        tool=tool,
        mode=resolved_mode,
        session_id=session_id,
        workspace_id=workspace_id,
        workspace_revision=workspace_revision,
        arguments=sanitize_arguments(arguments, resolved_mode),
    )
    return TraceHandle(trace=trace, owned=True, store=store)


@contextlib.contextmanager
def open_trace(
    *,
    trace_id: str | None,
    writer: str,
    tool: str,
    arguments: Mapping[str, Any] | None = None,
    mode: TraceMode | None = None,
    session_id: str | None = None,
    workspace_id: str | None = None,
    workspace_revision: str | int | None = None,
    store: TraceStore | None = None,
) -> Iterator[RuntimeTrace]:
    """Begin/join, activate, and commit one trace around a block."""
    handle = begin_trace(
        trace_id=trace_id,
        writer=writer,
        tool=tool,
        arguments=arguments,
        mode=mode,
        session_id=session_id,
        workspace_id=workspace_id,
        workspace_revision=workspace_revision,
        store=store,
    )
    try:
        with handle.active() as trace:
            yield trace
    except BaseException as exc:
        if handle.owned:
            handle.trace.note("outcome", is_error=True, error_type=type(exc).__name__)
        raise
    finally:
        handle.commit()


__all__ = [
    "BULK_CONTENT_KEYS",
    "TRACE_ID_RE",
    "TRACE_SCHEMA_VERSION",
    "RuntimeTrace",
    "TraceCandidate",
    "TraceHandle",
    "TraceMode",
    "TraceStore",
    "active_trace",
    "begin_trace",
    "is_trace_id",
    "new_trace_id",
    "open_trace",
    "record_decision",
    "sanitize_arguments",
    "trace_mode_from_env",
    "trace_store",
]

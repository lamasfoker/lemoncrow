"""Disk layer for the imported-session list payloads behind GET /v1/sessions.

Building one payload re-parses the session's raw transcripts; the in-memory
cache in ``create_app`` made repeat polls cheap, but every server start began
empty and the first Runs load re-parsed ~100 transcripts (12 s measured).
Rows are keyed by session id and match only when the caller's staleness
fingerprint *and* the code key agree, so a cached row can never outlive its
inputs: the fingerprint covers the trace and its artifacts and savings files,
and the code key covers the modules and pricing table that turn them into a
payload. The table is capped; the oldest rows are pruned. The cache is
best-effort: any SQLite failure reads as a miss and never fails a request.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_payloads (
    session_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL,
    touched_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS session_payloads_touched ON session_payloads(touched_at);
"""

#: Rows kept; one per session id. The list endpoint reads at most ~450 per
#: request, so this holds every recent session with room for the detail view.
MAX_ROWS = 4000
#: Prune at most once per this many writes.
_PRUNE_EVERY = 128


def code_key(module_files: Iterable[str | Path | None], extra: Iterable[str] = ()) -> str:
    """Digest of the code and data that shape a payload; a change invalidates every row."""
    digest = hashlib.sha256()
    for name in module_files:
        if name is None:
            continue
        try:
            digest.update(Path(name).read_bytes())
        except OSError:
            digest.update(str(name).encode())
    for item in extra:
        digest.update(item.encode())
    return digest.hexdigest()[:32]


class SessionPayloadDiskCache:
    def __init__(self, path: Path, *, key: str | Callable[[], str], max_rows: int = MAX_ROWS) -> None:
        self._path = path
        self._key_source = key
        self._key: str | None = key if isinstance(key, str) else None
        self._max_rows = max_rows
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._writes = 0
        self._broken = False

    def _connection(self) -> sqlite3.Connection | None:
        if self._conn is None and not self._broken:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(self._path, check_same_thread=False, timeout=1.0)
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
                conn.executescript(_SCHEMA)
                self._conn = conn
            except (OSError, sqlite3.Error) as exc:
                logger.debug("session payload cache unavailable at %s: %s", self._path, exc)
                self._broken = True
        return self._conn

    def _stamp(self, fingerprint: tuple[Any, ...]) -> str:
        if self._key is None:
            source = self._key_source
            self._key = source if isinstance(source, str) else source()
        return json.dumps([self._key, fingerprint], separators=(",", ":"))

    def get(self, session_id: str, fingerprint: tuple[Any, ...]) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connection()
            if conn is None:
                return None
            try:
                row = conn.execute(
                    "SELECT fingerprint, payload FROM session_payloads WHERE session_id=?", (session_id,)
                ).fetchone()
            except sqlite3.Error as exc:
                logger.debug("session payload cache read failed: %s", exc)
                return None
        if row is None or row[0] != self._stamp(fingerprint):
            return None
        try:
            payload = json.loads(row[1])
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def put(self, session_id: str, fingerprint: tuple[Any, ...], payload: dict[str, Any]) -> None:
        try:
            encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            return
        with self._lock:
            conn = self._connection()
            if conn is None:
                return
            try:
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO session_payloads VALUES (?, ?, ?, ?)",
                        (session_id, self._stamp(fingerprint), encoded, time.time()),
                    )
                    self._writes += 1
                    if self._writes % _PRUNE_EVERY == 1:
                        conn.execute(
                            "DELETE FROM session_payloads WHERE session_id IN ("
                            "SELECT session_id FROM session_payloads ORDER BY touched_at DESC LIMIT -1 OFFSET ?)",
                            (self._max_rows,),
                        )
            except sqlite3.Error as exc:
                logger.debug("session payload cache write failed: %s", exc)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

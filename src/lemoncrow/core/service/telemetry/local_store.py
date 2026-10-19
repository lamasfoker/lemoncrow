"""Local SQLite product telemetry store."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

RETENTION_DAYS = 30
# Hard row ceiling on top of the age cutoff: a noisy emitter must not be able to
# grow the file without bound inside the retention window (~250 B/row).
MAX_EVENTS = 400_000
# SQLite leaves a WAL at its high-water size after a checkpoint; cap what it
# keeps on disk (a VACUUM or burst of writes used to leave ~1 GB behind).
_JOURNAL_SIZE_LIMIT_BYTES = 64 * 1024 * 1024
# Reclaim file space only when the free list is large enough to matter.
_VACUUM_MIN_FREE_BYTES = 32 * 1024 * 1024

# Retention pruning is housekeeping, not a per-event obligation: running the
# DELETE inside every write_event() put a table scan + extra WAL churn on the
# tool-call hot path. The interval is enforced across processes through the
# ``meta`` table (every short-lived CLI run used to prune + VACUUM on its first
# write); the dict is only a per-process fast path that skips even that read.
_PRUNE_INTERVAL_S = 6 * 60 * 60
_last_prune_ts: dict[str, float] = {}

# Events whose props feed a summary panel. Any other row reads props only for
# ``agent_host``; the instr() probe finds those exactly because every row is
# written by json.dumps, which never escapes that ASCII key.
_PROPS_EVENTS: tuple[str, ...] = (
    "cli_command_invoked",
    "cli_command_completed",
    "playbook_applied",
    "playbook_retrieved",
    "frustration_signal_behavioral",
    "frustration_signal_lexical",
    "value_estimate",
)
_COMMAND_EVENTS = frozenset({"cli_command_invoked", "cli_command_completed"})
_NEEDS_PROPS = f"(event IN ({', '.join('?' * len(_PROPS_EVENTS))}) OR instr(props_json, '\"agent_host\"') > 0)"


def default_db_path() -> Path:
    from lemoncrow.core.foundation.paths import default_store_root

    return Path(os.environ.get("LEMONCROW_TELEMETRY_DB", default_store_root() / "telemetry.db"))


class LocalTelemetryStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or default_db_path()

    def write_event(
        self,
        *,
        event: str,
        props: dict[str, Any],
        exported: bool,
        ts: float | None = None,
    ) -> int:
        timestamp = time.time() if ts is None else ts
        session_id = props.get("session_id") if isinstance(props.get("session_id"), str) else None
        with self._connect() as conn:
            self._init(conn)
            self._maybe_prune(conn, timestamp)
            cur = conn.execute(
                """
                INSERT INTO events (ts, event, session_id, props_json, exported)
                VALUES (?, ?, ?, ?, ?)
                """,
                (timestamp, event, session_id, json.dumps(props, sort_keys=True), int(exported)),
            )
            return int(cur.lastrowid or 0)

    def write_events(self, events: list[dict[str, Any]]) -> None:
        """Persist a batch of already-validated+scrubbed events in one transaction.

        Used by the async telemetry worker: the whole batch shares one
        connection (no per-event connect + DDL), and prune/VACUUM runs here —
        off the hot path — never inside an emit call.
        """
        if not events:
            return
        now = time.time()
        rows: list[tuple[float, str, str | None, str, int]] = []
        for item in events:
            props = item.get("props") or {}
            session_id = props.get("session_id") if isinstance(props.get("session_id"), str) else None
            rows.append(
                (
                    float(item.get("ts", now)),
                    str(item["event"]),
                    session_id,
                    json.dumps(props, sort_keys=True),
                    int(bool(item.get("exported", False))),
                )
            )
        with self._connect() as conn:
            self._init(conn)
            self._maybe_prune(conn, now)
            conn.executemany(
                "INSERT INTO events (ts, event, session_id, props_json, exported) VALUES (?, ?, ?, ?, ?)",
                rows,
            )

    def _where_and_params(
        self,
        *,
        since: float | None,
        event: str | None,
        host: str | None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        if event:
            clauses.append("event = ?")
            params.append(event)
        if host:
            clauses.append("""
                (
                  (event = 'session_start' AND json_extract(props_json, '$.agent_host') = ?)
                  OR session_id IN (
                    SELECT session_id
                    FROM events
                    WHERE event = 'session_start'
                      AND json_extract(props_json, '$.agent_host') = ?
                      AND session_id IS NOT NULL
                  )
                )
                """)
            params.extend([host, host])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return where, params

    def list_events(
        self,
        *,
        since: float | None = None,
        event: str | None = None,
        host: str | None = None,
        limit: int | None = 500,
    ) -> list[dict[str, Any]]:
        if limit is not None:
            limit = max(1, min(limit, 5000))
        where, params = self._where_and_params(since=since, event=event, host=host)
        with self._connect() as conn:
            self._init(conn)
            query = f"""
                SELECT id, ts, event, session_id, props_json, exported
                FROM events{where}
                ORDER BY ts DESC
                """
            query_params = list(params)
            if limit is not None:
                query += "\n                LIMIT ?"
                query_params.append(limit)
            rows = conn.execute(query, query_params).fetchall()
        return [_row_to_event(row) for row in rows]

    def summary(
        self,
        *,
        since: float | None = None,
        event: str | None = None,
        host: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate the filtered window, parsing only rows whose props matter.

        Counts, sessions and time bounds are SQLite aggregates; rows that only
        contribute their session's host are grouped per session. Only the rows
        whose props feed a panel are ``json.loads``-ed (a full-window parse took
        seconds while the telemetry page polls every few seconds).
        """
        where, params = self._where_and_params(since=since, event=event, host=host)
        # Without ANALYZE stats SQLite would rather walk a whole index that yields
        # GROUP BY/DISTINCT order than range-scan a short window (~120 ms vs ~1 ms).
        source = "events INDEXED BY events_ts" if since is not None and not event else "events"
        props_params = [*params, *_PROPS_EVENTS]
        commands_by_day: Counter[str] = Counter()
        top_commands: Counter[str] = Counter()
        host_counts: Counter[str] = Counter()
        host_last_ts: dict[str, float] = {}
        top_playbooks: Counter[str] = Counter()
        playbook_domains: dict[str, str] = {}
        retrieval_scores: Counter[str] = Counter()
        frustration_behavioral: Counter[str] = Counter()
        frustration_lexical: Counter[str] = Counter()
        value = {
            "tokens_saved_estimate": 0,
            "cache_hits": 0,
            "total_tool_calls": 0,
            "cache_hit_rate": 0.0,
            "blocks_applied": 0,
        }

        with self._connect() as conn:
            self._init(conn)
            # Ordered by latest occurrence: the order a newest-first scan meets them.
            event_rows = conn.execute(
                f"SELECT event, count(*), min(ts), max(ts) FROM {source}{where} GROUP BY event ORDER BY max(ts) DESC",
                params,
            ).fetchall()
            active_sessions = int(
                conn.execute(
                    f"SELECT count(DISTINCT session_id) FROM {source}"
                    + _and(where, "typeof(session_id) = 'text' AND session_id <> ''"),
                    params,
                ).fetchone()[0]
            )
            session_hosts, start_hosts = self._session_hosts(
                conn, f"{source}{where}", params, windowed=since is not None
            )
            prop_rows = conn.execute(
                f"SELECT id, ts, event, session_id, props_json FROM {source}{_and(where, _NEEDS_PROPS)}"
                " ORDER BY ts DESC",
                props_params,
            )
            for row_id, ts, name, session_id, raw in prop_rows:
                if row_id in start_hosts:
                    # A session_start already parsed for session_hosts; it feeds
                    # no panel but agent_hosts.
                    host_name = start_hosts[row_id]
                else:
                    props = _load_props(raw)
                    host_name = props.get("agent_host")
                    if name in _COMMAND_EVENTS:
                        commands_by_day[time.strftime("%Y-%m-%d", time.localtime(float(ts)))] += 1
                        command = props.get("command_name")
                        if isinstance(command, str):
                            top_commands[command] += 1
                    elif name in {"playbook_applied", "playbook_retrieved"}:
                        if name == "playbook_applied":
                            block_hash = props.get("block_id_hash")
                            if isinstance(block_hash, str):
                                top_playbooks[block_hash] += 1
                                domain = props.get("domain")
                                if isinstance(domain, str):
                                    playbook_domains[block_hash] = domain
                        score = props.get("retrieval_score")
                        if isinstance(score, (int, float)):
                            retrieval_scores[_score_bucket(float(score))] += 1
                    elif name == "frustration_signal_behavioral":
                        signal = props.get("signal_type")
                        if isinstance(signal, str):
                            frustration_behavioral[signal] += 1
                    elif name == "frustration_signal_lexical":
                        category = props.get("category")
                        if isinstance(category, str):
                            frustration_lexical[category] += 1
                    elif name == "value_estimate":
                        for key in ("tokens_saved_estimate", "cache_hits", "total_tool_calls", "blocks_applied"):
                            raw_value = props.get(key)
                            if isinstance(raw_value, int) and not isinstance(raw_value, bool):
                                value[key] += raw_value
                if not isinstance(host_name, str):
                    host_name = session_hosts.get(session_id or "")
                if host_name:
                    host_counts[host_name] += 1
                    # Rows arrive newest first, so the first sighting is the latest.
                    host_last_ts.setdefault(host_name, float(ts))
            # Every other row has no agent_host of its own: it counts toward its
            # session's host, so SQLite groups it per session.
            for session_id, count, last_ts in conn.execute(
                f"SELECT session_id, count(*), max(ts) FROM {source}"
                + _and(where, f"NOT {_NEEDS_PROPS}")
                + " GROUP BY session_id",
                props_params,
            ):
                host_name = session_hosts.get(session_id or "")
                if host_name:
                    host_counts[host_name] += int(count)
                    host_last_ts[host_name] = max(float(last_ts), host_last_ts.get(host_name, float(last_ts)))

        event_counts = {str(name): int(count) for name, count, _first, _last in event_rows}
        total_tool_calls = int(value["total_tool_calls"])
        cache_hits = int(value["cache_hits"])
        value["cache_hit_rate"] = round(cache_hits / total_tool_calls, 3) if total_tool_calls > 0 else 0.0
        # most_common() breaks count ties by insertion order, i.e. the host seen first
        # by a newest-first scan.
        agent_hosts = Counter(
            {name: host_counts[name] for name in sorted(host_counts, key=host_last_ts.__getitem__, reverse=True)}
        )

        return {
            "events_total": sum(event_counts.values()),
            "unique_event_types": len(event_counts),
            "active_sessions": active_sessions,
            "first_event_ts": min((float(row[2]) for row in event_rows), default=None),
            "last_event_ts": max((float(row[3]) for row in event_rows), default=None),
            "event_counts": event_counts,
            "commands_by_day": _counter_series(commands_by_day),
            "top_commands": _counter_items(top_commands),
            "agent_hosts": _counter_items(agent_hosts),
            "top_playbooks": [
                {"block_id_hash": key, "count": count, "domain": playbook_domains.get(key, "")}
                for key, count in top_playbooks.most_common(10)
            ],
            "retrieval_score_distribution": _counter_items(retrieval_scores),
            "plan_checks": {name: count for name, count in event_counts.items() if name.startswith("plan_check_")},
            "frustration_behavioral": _counter_items(frustration_behavioral),
            "frustration_lexical": _counter_items(frustration_lexical),
            "value_estimate": value,
        }

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        # WAL mode allows concurrent readers + one writer without locking conflicts
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(f"PRAGMA journal_size_limit={_JOURNAL_SIZE_LIMIT_BYTES}")
        except Exception:
            pass  # ignore if already set or fails
        return conn

    def _init(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts REAL NOT NULL,
              event TEXT NOT NULL,
              session_id TEXT,
              props_json TEXT NOT NULL,
              exported INTEGER NOT NULL DEFAULT 0
            )
            """)
        conn.execute("CREATE INDEX IF NOT EXISTS events_ts ON events(ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS events_event_ts ON events(event, ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS events_session ON events(session_id)")
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value REAL NOT NULL)")

    def _session_hosts(
        self, conn: sqlite3.Connection, window: str, params: list[Any], *, windowed: bool
    ) -> tuple[dict[str, str], dict[int, Any]]:
        """Latest string ``agent_host`` per session the ``FROM {window}`` rows reference.

        Also returns each parsed session_start row's own ``agent_host`` by row id,
        so the summary does not parse those rows twice.
        """
        # A time window references few sessions: look each up. Unwindowed, the
        # planner's scan of every session_start is cheaper than 45k lookups.
        index = " INDEXED BY events_session" if windowed else ""
        rows = conn.execute(
            f"""
            SELECT id, session_id, props_json
            FROM events{index}
            WHERE event = 'session_start'
              AND session_id IN (SELECT COALESCE(session_id, '') FROM {window})
            ORDER BY ts DESC
            """,
            params,
        )
        hosts: dict[str, str] = {}
        start_hosts: dict[int, Any] = {}
        for row_id, session_id, raw in rows:
            host = start_hosts[row_id] = _load_props(raw).get("agent_host")
            if isinstance(session_id, str) and isinstance(host, str):
                hosts.setdefault(session_id, host)
        return hosts, start_hosts

    def _maybe_prune(self, conn: sqlite3.Connection, now: float) -> None:
        key = str(self.db_path)
        if now - _last_prune_ts.get(key, 0.0) < _PRUNE_INTERVAL_S:
            return
        _last_prune_ts[key] = now
        row = conn.execute("SELECT value FROM meta WHERE key = 'last_prune_ts'").fetchone()
        if row is not None and now - float(row[0]) < _PRUNE_INTERVAL_S:
            _last_prune_ts[key] = float(row[0])
            return
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_prune_ts', ?)", (now,))
        self._prune(conn, now)

    def _prune(self, conn: sqlite3.Connection, now: float) -> None:
        cutoff = now - RETENTION_DAYS * 24 * 60 * 60
        conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        conn.execute(
            "DELETE FROM events WHERE id <= (SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (MAX_EVENTS,),
        )
        conn.commit()
        try:
            free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            total_pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
            # Deleted pages are reused by later inserts, so only a large free
            # list (a one-off shrink, e.g. after the row cap first applies) is
            # worth a VACUUM; it rewrites the file through the WAL.
            if free_pages * page_size >= _VACUUM_MIN_FREE_BYTES and free_pages * 4 >= total_pages:
                conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass


def _and(where: str, clause: str) -> str:
    return f"{where} AND {clause}" if where else f" WHERE {clause}"


def _load_props(raw: Any) -> dict[str, Any]:
    try:
        props = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return props if isinstance(props, dict) else {}


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    try:
        props = json.loads(row["props_json"])
    except json.JSONDecodeError:
        props = {}
    return {
        "id": row["id"],
        "ts": row["ts"],
        "event": row["event"],
        "session_id": row["session_id"],
        "props": props,
        "exported": bool(row["exported"]),
    }


def _counter_items(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"name": key, "count": count} for key, count in counter.most_common(20)]


def _counter_series(counter: Counter[str]) -> list[dict[str, Any]]:
    by_day: defaultdict[str, int] = defaultdict(int)
    by_day.update(counter)
    return [{"day": key, "count": by_day[key]} for key in sorted(by_day)]


def _score_bucket(value: float) -> str:
    if value < 0.25:
        return "0-0.25"
    if value < 0.5:
        return "0.25-0.5"
    if value < 0.75:
        return "0.5-0.75"
    return "0.75-1.0"

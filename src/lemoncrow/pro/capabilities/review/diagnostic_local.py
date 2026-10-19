"""Local collectors for the diagnostic bundle (``lc review diagnostics``).

Read-only by construction: each Review partition of the loopback server is
opened with ``mode=ro`` and ``PRAGMA query_only``, and every query names only
the columns the bundle's allowlist needs. Titles, paths, packets, model
output, prompts and comment bodies are never selected, so they never leave the
database file. The shared builder is
:func:`lemoncrow.pro.capabilities.review.diagnostic_bundle.build_diagnostic_bundle`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from lemoncrow.pro.capabilities.review.diagnostic_bundle import (
    DEFAULT_LIMITS,
    DataFlowFacts,
    DiagnosticFacts,
    DiagnosticLimits,
    IntelligenceJobRecord,
    OperationsFacts,
    ProtocolFacts,
    ProviderOperationRecord,
    RecordSource,
    SessionRow,
    SnapshotRecord,
    VersionFacts,
    WebhookDeliveryRecord,
    collect_review_records,
    installed_versions,
    order_by_recency,
)
from lemoncrow.pro.capabilities.review.diagnostic_state import gate_states, release_state

if TYPE_CHECKING:
    from lemoncrow_client.config import ClientConfig

LOCAL_DISTRIBUTIONS: Final[tuple[str, ...]] = ("lemoncrow", "lemoncrow-client", "lemoncrow-server")
DISCOVERY_PATH: Final[str] = "/.well-known/lemoncrow-server.json"
DISCOVERY_TIMEOUT_S: Final[float] = 2.0
_DB_NAME: Final[str] = "lemoncrow_reviews.db"
_UNREADABLE_DEGRADED: Final[str] = "degraded reasons could not be read"


class LocalStoreReader:
    """Bounded, read-only reads of one loopback-server Review partition."""

    __slots__ = ("_db", "_repo_id")

    def __init__(self, repo_id: str, db_path: Path) -> None:
        self._repo_id = repo_id
        self._db = db_path

    def _rows(self, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
        conn = sqlite3.connect(f"{self._db.as_uri()}?mode=ro", uri=True, timeout=1.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            return list(conn.execute(sql, params).fetchall())
        finally:
            conn.close()

    def recent_sessions(self, limit: int) -> list[SessionRow]:
        rows = self._rows(
            "SELECT id, status, current_revision_id, updated_at FROM review_sessions "
            "ORDER BY updated_at DESC, id DESC LIMIT ?",
            (max(1, limit),),
        )
        return [
            SessionRow(str(row["id"]), str(row["status"]), str(row["current_revision_id"]), str(row["updated_at"]))
            for row in rows
        ]

    def snapshot(self, session: SessionRow) -> SnapshotRecord:
        columns = "id, revision_number, capture_kind, packet_schema_version, created_at, degraded_json"
        rows: list[sqlite3.Row] = []
        if session.current_revision_id:
            rows = self._rows(
                f"SELECT {columns} FROM review_revisions WHERE review_id = ? AND id = ?",
                (session.review_id, session.current_revision_id),
            )
        if not rows:
            rows = self._rows(
                f"SELECT {columns} FROM review_revisions WHERE review_id = ? ORDER BY revision_number DESC LIMIT 1",
                (session.review_id,),
            )
        base = SnapshotRecord(
            review_id=session.review_id,
            repo_id=self._repo_id,
            status=session.status,
            review_updated_at=session.updated_at,
        )
        if not rows:
            return base
        row = rows[0]
        try:
            raw = json.loads(str(row["degraded_json"] or "[]"))
            degraded = tuple(str(item) for item in raw) if isinstance(raw, list) else (_UNREADABLE_DEGRADED,)
        except ValueError:
            degraded = (_UNREADABLE_DEGRADED,)
        return SnapshotRecord(
            review_id=session.review_id,
            repo_id=self._repo_id,
            status=session.status,
            snapshot_id=str(row["id"]),
            snapshot_number=int(row["revision_number"]),
            capture_kind=str(row["capture_kind"]),
            packet_schema_version=int(row["packet_schema_version"]),
            snapshot_created_at=str(row["created_at"]),
            review_updated_at=session.updated_at,
            degraded=degraded,
        )

    def intelligence_jobs(self, review_id: str, limit: int) -> list[IntelligenceJobRecord]:
        runs = self._rows(
            "SELECT id, revision_id, capability, driver, model, status, model_calls, created_at, completed_at, error "
            "FROM review_intelligence_runs WHERE review_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (review_id, max(1, limit)),
        )
        syntheses = self._rows(
            "SELECT id, revision_id, model, status, created_at, completed_at, error "
            "FROM review_plan_syntheses WHERE review_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (review_id, max(1, limit)),
        )
        jobs = [
            IntelligenceJobRecord(
                job_id=str(row["id"]),
                kind="intelligence",
                review_id=review_id,
                repo_id=self._repo_id,
                snapshot_id=str(row["revision_id"]),
                capability=str(row["capability"]),
                driver=str(row["driver"]),
                state=str(row["status"]),
                model_calls=int(row["model_calls"]),
                created_at=str(row["created_at"]),
                completed_at=str(row["completed_at"]),
                model=str(row["model"]),
                error=str(row["error"]),
            )
            for row in runs
        ]
        jobs.extend(
            IntelligenceJobRecord(
                job_id=str(row["id"]),
                kind="synthesis",
                review_id=review_id,
                repo_id=self._repo_id,
                snapshot_id=str(row["revision_id"]),
                capability="plan_synthesis",
                state=str(row["status"]),
                created_at=str(row["created_at"]),
                completed_at=str(row["completed_at"]),
                model=str(row["model"]),
                error=str(row["error"]),
            )
            for row in syntheses
        )
        return jobs

    def provider_operations(self, review_id: str, limit: int) -> list[ProviderOperationRecord]:
        """Local feedback deliveries: one record per delivery, ``operation_id`` groups one publish."""

        rows = self._rows(
            "SELECT d.id, d.operation_id, d.target_type, d.state, d.revision_id, d.created_at, d.updated_at, "
            "d.last_error FROM deliveries d JOIN annotations a ON a.id = d.annotation_id "
            "WHERE a.review_id = ? ORDER BY d.updated_at DESC, d.id DESC LIMIT ?",
            (review_id, max(1, limit)),
        )
        return [
            ProviderOperationRecord(
                operation_id=str(row["operation_id"] or row["id"]),
                review_id=review_id,
                repo_id=self._repo_id,
                snapshot_id=str(row["revision_id"]),
                provider=str(row["target_type"]),
                kind="feedback_delivery",
                state=str(row["state"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
                error=str(row["last_error"]),
            )
            for row in rows
        ]

    def webhook_deliveries(self, limit: int) -> list[WebhookDeliveryRecord]:
        return []

    def latest_updated_at(self) -> str:
        """The durable update time of this store's most recently updated Review (``""`` when it holds none)."""

        rows = self._rows("SELECT MAX(updated_at) AS newest FROM review_sessions", ())
        return str(rows[0]["newest"] or "") if rows else ""

    def schema_markers(self) -> tuple[str, ...]:
        """``session:<n>`` and ``migration:<latest applied>`` of this store."""

        markers: list[str] = []
        for row in self._rows("SELECT value FROM review_meta WHERE key = 'schema_version'", ()):
            markers.append(f"session:{row['value']}")
        for row in self._rows("SELECT name FROM _schema_migrations ORDER BY name DESC LIMIT 1", ()):
            markers.append(f"migration:{str(row['name']).removesuffix('.sql')}")
        return tuple(markers)


_REPOSITORY_ID_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def local_store_readers(
    review_root: Path, *, limits: DiagnosticLimits = DEFAULT_LIMITS, repository_id: str = ""
) -> tuple[list[tuple[str, LocalStoreReader]], int]:
    """The partitions to read under *review_root* and how many exist.

    An explicit *repository_id* selects that partition alone. Otherwise the
    partitions whose Review was durably updated most recently come first
    (``order_by_recency`` over each store's own newest ``updated_at``), at most
    ``limits.repositories`` of them. File times play no part.
    """

    if not review_root.is_dir():
        return [], 0
    partitions: dict[str, Path] = {}
    for entry in review_root.iterdir():
        db = entry / _DB_NAME
        if entry.is_dir() and db.is_file():
            partitions[entry.name] = db
    if repository_id:
        if not _REPOSITORY_ID_RE.fullmatch(repository_id) or repository_id not in partitions:
            return [], len(partitions)
        chosen = [repository_id]
    else:
        # lc-debt: one MAX(updated_at) read per partition, O(partitions); upgrade path: a
        # repository-activity index in the loopback server.
        activity: list[tuple[str, str]] = []
        for name, db in partitions.items():
            try:
                activity.append((name, LocalStoreReader(name, db).latest_updated_at()))
            except sqlite3.Error:
                activity.append((name, ""))
        chosen = order_by_recency(activity)[: limits.repositories]
    return [(name, LocalStoreReader(name, partitions[name])) for name in chosen], len(partitions)


def discover_server(config: ClientConfig) -> Mapping[str, Any]:
    """The configured server's unauthenticated discovery document."""

    from lemoncrow_client.transport import HttpTransport

    transport = HttpTransport(config.url, timeout_s=DISCOVERY_TIMEOUT_S)
    try:
        return transport.get(DISCOVERY_PATH, timeout_s=DISCOVERY_TIMEOUT_S).require()
    finally:
        transport.close()


def local_protocol_facts(
    cwd: Path, *, discover: Callable[[ClientConfig], Mapping[str, Any]] | None = None
) -> ProtocolFacts:
    """This client's protocol window and what the configured server declares."""

    from lemoncrow_client.config import load_config
    from lemoncrow_client.errors import ClientError
    from lemoncrow_client.protocol import protocol_window

    window = protocol_window()
    try:
        config = load_config(cwd=cwd)
    except ClientError:
        return ProtocolFacts(client_window=window, server_reason="server_not_configured")
    try:
        identity = (discover or discover_server)(config)
    except Exception:
        # The transport error may name the configured URL: never carried.
        return ProtocolFacts(client_window=window, server_reason="server_unreachable")
    if not isinstance(identity, Mapping):
        return ProtocolFacts(client_window=window, server_reason="server_unreachable")
    return ProtocolFacts(client_window=window, server_identity=identity)


def local_diagnostic_facts(
    *,
    review_root: Path,
    protocol: ProtocolFacts,
    data_flow: DataFlowFacts,
    limits: DiagnosticLimits = DEFAULT_LIMITS,
    repository_id: str = "",
) -> DiagnosticFacts:
    readers, total = local_store_readers(review_root, limits=limits, repository_id=repository_id)
    records = collect_review_records(
        list(readers), limits=limits, webhooks=False, unavailable="" if readers else "no_review_store"
    )
    markers: set[str] = set()
    for _repo_id, reader in readers:
        try:
            markers.update(reader.schema_markers())
        except sqlite3.Error:
            continue
    return DiagnosticFacts(
        surface="local",
        versions=VersionFacts(components=installed_versions(LOCAL_DISTRIBUTIONS), store_schemas=tuple(sorted(markers))),
        protocol=protocol,
        data_flow=data_flow,
        records=records,
        queue_jobs=RecordSource(unavailable="no_durable_queue"),
        repositories_total=total,
        operations=OperationsFacts(gates=gate_states(), release=release_state()),
    )


__all__ = [
    "DISCOVERY_PATH",
    "LOCAL_DISTRIBUTIONS",
    "LocalStoreReader",
    "discover_server",
    "local_diagnostic_facts",
    "local_protocol_facts",
    "local_store_readers",
]

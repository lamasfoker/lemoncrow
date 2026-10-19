"""Saved Inbox filters: per-user, server-persisted, validated, bounded (Phase 9 item 13, P9-INBOX).

A saved filter is a named :class:`~lemoncrow.pro.capabilities.review.inbox_views.InboxSelection`.
It is *scope*, never authority: the Inbox applies it after repository access
is resolved for the viewer at query time, so a filter naming a repository the
viewer cannot read simply matches nothing there. Nothing a filter stores can
open a store, widen a view or reveal a count.

Validation rejects unknown fields and out-of-vocabulary values instead of
dropping them, bounds every list and string, and bounds the number of filters
one user may keep (:data:`MAX_SAVED_FILTERS`); creating one more is refused,
never a silent eviction.

Storage: one ``review_saved_filters`` table keyed by organization, principal
and filter id. Closers: :meth:`SavedFilterStore.delete_principal` (user
deletion) and :meth:`SavedFilterStore.forget_org` (organization deletion); the
file-backed store also lives inside the Review state root that organization
and local-state deletion remove.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Callable, Container, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from lemoncrow.infra.storage.ids import make_uuid7
from lemoncrow.pro.capabilities.review.inbox_views import (
    INBOX_MAX_QUERY,
    INBOX_STATUSES,
    INBOX_VIEWS,
    InboxSelection,
    parse_query,
)
from lemoncrow.pro.capabilities.review.review_triage import PRIORITY_LEVELS
from lemoncrow.pro.capabilities.review.store import utc_now
from lemoncrow.pro.capabilities.review.workflow_state import WORKFLOW_STATES

MAX_SAVED_FILTERS: Final[int] = 50
"""Filters one user may keep. Creating one more is refused (409), never an eviction."""
MAX_FILTER_NAME: Final[int] = 80
MAX_FILTER_LIST: Final[int] = 50
MAX_SCOPE_ID: Final[int] = 200
SAVED_FILTER_FIELDS: Final[frozenset[str]] = frozenset(
    {"view", "status", "repos", "teams", "workflow_states", "priorities", "query"}
)
#: Repository ids are opaque server identifiers (the server's own ``[A-Za-z0-9_.:-]{1,128}``).
_REPO_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class SavedFilterInvalid(ValueError):
    """A rejected saved-filter document; ``field`` names the offending field."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class SavedFilterLimit(RuntimeError):
    """The user already keeps :data:`MAX_SAVED_FILTERS` filters."""


class SavedFilterConflict(RuntimeError):
    """Another filter of this user already has the name."""


@dataclass(frozen=True, slots=True)
class SavedFilter:
    id: str
    org_id: str
    principal_id: str
    name: str
    selection: InboxSelection
    created_at: str = ""
    updated_at: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "definition": selection_wire(self.selection),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def selection_wire(selection: InboxSelection) -> dict[str, Any]:
    return {
        "view": selection.view,
        "status": selection.status,
        "repos": list(selection.repos),
        "teams": list(selection.teams),
        "workflow_states": list(selection.workflow_states),
        "priorities": list(selection.priorities),
        "query": selection.query,
    }


def _choice(raw: Mapping[str, Any], key: str, allowed: tuple[str, ...], default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or value not in allowed:
        raise SavedFilterInvalid(key, f"{key} must be one of {', '.join(allowed)}")
    return value


def _values(
    raw: Mapping[str, Any], key: str, *, allowed: tuple[str, ...] | None = None, names: bool = False
) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SavedFilterInvalid(key, f"{key} must be a list of strings")
    if len(value) > MAX_FILTER_LIST:
        raise SavedFilterInvalid(key, f"{key} accepts at most {MAX_FILTER_LIST} entries")
    out: list[str] = []
    for item in value:
        if allowed is not None:
            if item not in allowed:
                raise SavedFilterInvalid(key, f"{key} entry {item[:40]!r} is not one of {', '.join(allowed)}")
        elif names:
            # Team (directory group) names: printable text, bounded; matched exactly, never as a pattern.
            if not item.strip() or len(item) > MAX_SCOPE_ID or any(ord(char) < 32 for char in item):
                raise SavedFilterInvalid(key, f"{key} entries must be names of at most {MAX_SCOPE_ID} characters")
        elif not _REPO_ID.match(item):
            raise SavedFilterInvalid(key, f"{key} entries must be repository identifiers of at most 128 characters")
        if item not in out:
            out.append(item)
    return tuple(out)


def validate_selection(raw: object, *, teams_supported: bool = True) -> InboxSelection:
    """Strict decode of a filter definition: unknown fields and values are errors."""

    if not isinstance(raw, Mapping):
        raise SavedFilterInvalid("definition", "definition must be an object")
    unknown = sorted(str(key) for key in raw if key not in SAVED_FILTER_FIELDS)
    if unknown:
        raise SavedFilterInvalid(unknown[0], f"unknown saved filter field {unknown[0][:40]!r}")
    query = raw.get("query", "")
    if not isinstance(query, str) or len(query.strip()) > INBOX_MAX_QUERY:
        raise SavedFilterInvalid("query", f"query must be a string of at most {INBOX_MAX_QUERY} characters")
    teams = _values(raw, "teams", names=True)
    if teams and not teams_supported:
        raise SavedFilterInvalid("teams", "team scoping needs team identities, which this Review server does not have")
    return InboxSelection(
        view=_choice(raw, "view", ("all", *INBOX_VIEWS), "all"),
        status=_choice(raw, "status", INBOX_STATUSES, "all"),
        repos=_values(raw, "repos"),
        teams=teams,
        workflow_states=_values(raw, "workflow_states", allowed=WORKFLOW_STATES),
        priorities=_values(raw, "priorities", allowed=PRIORITY_LEVELS),
        query=query.strip().lower(),
    )


def validate_name(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise SavedFilterInvalid("name", "name is required")
    name = " ".join(raw.split())
    if len(name) > MAX_FILTER_NAME or any(ord(char) < 32 for char in name):
        raise SavedFilterInvalid("name", f"name must be at most {MAX_FILTER_NAME} printable characters")
    return name


def validate_document(raw: object, *, teams_supported: bool = True) -> tuple[str, InboxSelection]:
    """``{"name": ..., "definition": {...}}``; any other top-level key is an error."""

    if not isinstance(raw, Mapping):
        raise SavedFilterInvalid("body", "body must be an object")
    unknown = sorted(str(key) for key in raw if key not in {"name", "definition"})
    if unknown:
        raise SavedFilterInvalid(unknown[0], f"unknown field {unknown[0][:40]!r}")
    return validate_name(raw.get("name")), validate_selection(
        raw.get("definition", {}), teams_supported=teams_supported
    )


#: Query parameters of ``GET /v1/reviews`` that select the Inbox view mode (local and hosted).
VIEW_MODE_PARAMS: Final[frozenset[str]] = frozenset({"view", "filter", "repo", "workflow", "priority", "team"})
#: Repeated request parameter -> selection field.
_REQUEST_LISTS: Final[tuple[tuple[str, str], ...]] = (
    ("repo", "repos"),
    ("workflow", "workflow_states"),
    ("priority", "priorities"),
    ("team", "teams"),
)
_NONE: Final[str] = "\0none"


def wants_view_mode(params: Container[str]) -> bool:
    return any(name in params for name in VIEW_MODE_PARAMS)


def request_selection(values: Callable[[str], Sequence[str]], *, teams_supported: bool) -> InboxSelection:
    """The request's own selection from (repeated) query parameters; an error names the parameter.

    Raises :class:`InboxQueryError` for an oversized search and
    :class:`SavedFilterInvalid` for anything :func:`validate_selection` refuses.
    """

    def first(name: str, default: str) -> str:
        items = values(name)
        return (items[0].strip() if items else "") or default

    raw: dict[str, Any] = {"view": first("view", "all"), "status": first("status", "all"), "query": first("q", "")}
    for param, key in _REQUEST_LISTS:
        items = [item.strip() for item in values(param) if item.strip()]
        if items:
            raw[key] = items
    parse_query(str(raw["query"]))
    try:
        return validate_selection(raw, teams_supported=teams_supported)
    except SavedFilterInvalid as exc:
        param = {key: name for name, key in _REQUEST_LISTS}.get(exc.field, exc.field)
        raise SavedFilterInvalid(param, str(exc)) from exc


def selection_view(selection: InboxSelection) -> dict[str, Any]:
    """The applied selection on the wire; an empty intersection is explicit, never an internal sentinel."""

    wire = selection_wire(selection)
    empty = False
    for key in ("view", "status"):
        if wire[key] == _NONE:
            wire[key], empty = "none", True
    for key in ("repos", "teams", "workflow_states", "priorities"):
        if _NONE in wire[key]:
            wire[key], empty = [], True
    wire["matches_nothing"] = empty
    return wire


def _decode_selection(raw: str) -> InboxSelection:
    try:
        return validate_selection(json.loads(raw))
    except (ValueError, SavedFilterInvalid):
        # A row that no longer validates (vocabulary shrank) matches nothing rather than everything.
        return InboxSelection(view=_NONE)


_SQLITE_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS review_saved_filters (
    org_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (org_id, principal_id, id),
    UNIQUE (org_id, principal_id, name)
);
"""


class SavedFilterStore:
    """Shared SQL over a ``_transaction(org_id)`` yielding ``execute(sql, params)`` with mapping rows."""

    def _transaction(self, org_id: str) -> AbstractContextManager[Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    @staticmethod
    def _row(row: Mapping[str, Any]) -> SavedFilter:
        return SavedFilter(
            id=str(row["id"]),
            org_id=str(row["org_id"]),
            principal_id=str(row["principal_id"]),
            name=str(row["name"]),
            selection=_decode_selection(str(row["definition_json"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def list(self, org_id: str, principal_id: str) -> tuple[SavedFilter, ...]:
        with self._transaction(org_id) as conn:
            rows = conn.execute(
                "SELECT * FROM review_saved_filters WHERE org_id = ? AND principal_id = ? ORDER BY name, id",
                (org_id, principal_id),
            ).fetchall()
        return tuple(self._row(row) for row in rows)

    def get(self, org_id: str, principal_id: str, filter_id: str) -> SavedFilter | None:
        with self._transaction(org_id) as conn:
            row = conn.execute(
                "SELECT * FROM review_saved_filters WHERE org_id = ? AND principal_id = ? AND id = ?",
                (org_id, principal_id, filter_id),
            ).fetchone()
        return None if row is None else self._row(row)

    def create(self, org_id: str, principal_id: str, name: str, selection: InboxSelection) -> SavedFilter:
        now = utc_now()
        record = SavedFilter(
            id=f"rsf-{make_uuid7()}",
            org_id=org_id,
            principal_id=principal_id,
            name=name,
            selection=selection,
            created_at=now,
            updated_at=now,
        )
        with self._transaction(org_id) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM review_saved_filters WHERE org_id = ? AND principal_id = ?",
                (org_id, principal_id),
            ).fetchone()
            if count is not None and int(count["n"]) >= MAX_SAVED_FILTERS:
                raise SavedFilterLimit(f"at most {MAX_SAVED_FILTERS} saved filters per user")
            self._require_unique_name(conn, org_id, principal_id, name, exclude="")
            conn.execute(
                "INSERT INTO review_saved_filters"
                " (org_id, principal_id, id, name, definition_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    org_id,
                    principal_id,
                    record.id,
                    name,
                    json.dumps(selection_wire(selection), sort_keys=True),
                    now,
                    now,
                ),
            )
        return record

    @staticmethod
    def _require_unique_name(conn: Any, org_id: str, principal_id: str, name: str, *, exclude: str) -> None:
        clash = conn.execute(
            "SELECT id FROM review_saved_filters WHERE org_id = ? AND principal_id = ? AND name = ? AND id <> ?",
            (org_id, principal_id, name, exclude),
        ).fetchone()
        if clash is not None:
            raise SavedFilterConflict("a saved filter with this name already exists")

    def update(
        self, org_id: str, principal_id: str, filter_id: str, name: str, selection: InboxSelection
    ) -> SavedFilter | None:
        now = utc_now()
        with self._transaction(org_id) as conn:
            row = conn.execute(
                "SELECT * FROM review_saved_filters WHERE org_id = ? AND principal_id = ? AND id = ?",
                (org_id, principal_id, filter_id),
            ).fetchone()
            if row is None:
                return None
            self._require_unique_name(conn, org_id, principal_id, name, exclude=filter_id)
            conn.execute(
                "UPDATE review_saved_filters SET name = ?, definition_json = ?, updated_at = ?"
                " WHERE org_id = ? AND principal_id = ? AND id = ?",
                (name, json.dumps(selection_wire(selection), sort_keys=True), now, org_id, principal_id, filter_id),
            )
            created_at = str(row["created_at"])
        return SavedFilter(
            id=filter_id,
            org_id=org_id,
            principal_id=principal_id,
            name=name,
            selection=selection,
            created_at=created_at,
            updated_at=now,
        )

    def delete(self, org_id: str, principal_id: str, filter_id: str) -> bool:
        with self._transaction(org_id) as conn:
            cursor = conn.execute(
                "DELETE FROM review_saved_filters WHERE org_id = ? AND principal_id = ? AND id = ?",
                (org_id, principal_id, filter_id),
            )
            return int(cursor.rowcount or 0) > 0

    def delete_principal(self, org_id: str, principal_id: str) -> int:
        """Closer for user deletion: every filter the principal kept in the organization."""

        with self._transaction(org_id) as conn:
            cursor = conn.execute(
                "DELETE FROM review_saved_filters WHERE org_id = ? AND principal_id = ?", (org_id, principal_id)
            )
            return int(cursor.rowcount or 0)

    def forget_org(self, org_id: str) -> int:
        """Closer for organization deletion."""

        with self._transaction(org_id) as conn:
            cursor = conn.execute("DELETE FROM review_saved_filters WHERE org_id = ?", (org_id,))
            return int(cursor.rowcount or 0)


class SqliteSavedFilterStore(SavedFilterStore):
    """File-backed store; ``path_for(org_id)`` names the database for one organization."""

    def __init__(self, path_for: Path | Any) -> None:
        self._path_for = path_for
        self._lock = threading.Lock()

    def path(self, org_id: str) -> Path:
        resolved = self._path_for(org_id) if callable(self._path_for) else self._path_for
        return Path(resolved)

    @contextmanager
    def _transaction(self, org_id: str) -> Iterator[sqlite3.Connection]:
        path = self.path(org_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            conn = sqlite3.connect(path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            try:
                conn.executescript(_SQLITE_DDL)
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()


__all__ = [
    "MAX_FILTER_LIST",
    "MAX_FILTER_NAME",
    "MAX_SAVED_FILTERS",
    "SAVED_FILTER_FIELDS",
    "VIEW_MODE_PARAMS",
    "SavedFilter",
    "SavedFilterConflict",
    "SavedFilterInvalid",
    "SavedFilterLimit",
    "SavedFilterStore",
    "SqliteSavedFilterStore",
    "request_selection",
    "selection_view",
    "selection_wire",
    "validate_document",
    "validate_name",
    "validate_selection",
    "wants_view_mode",
]

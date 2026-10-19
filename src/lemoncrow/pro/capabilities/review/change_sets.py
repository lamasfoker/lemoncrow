"""Multi-repository change sets (Phase 9R item 42).

Linked Changes in different repositories are reviewed as one set. Each
repository keeps its own Review and Snapshot. The set is built from the
relationship store: every member holds one ``change_set`` row per member it was
linked to (written on both sides), and membership is the bounded walk over
those rows. Members are resolved through a composition-supplied
:class:`ChangeSetDirectory` that returns only members the current viewer may
read, in the viewer's own tenant.

On top of the members' own Reviews the set adds:

* **set readiness**: the conjunction of every member's own readiness and the
  set-level checks below. No member reports ready while another is blocked:
  each member's readiness carries ``change-set:*`` conditions naming the
  blocker.
* **contract compatibility between heads** (deterministic, on the review host):
  a contract one member's head removes (a served route or OpenAPI path, a
  table, a column, a message type) that another member's head still uses is an
  incompatibility.
* **deploy order**: providers before consumers, migrations before code, and a
  consumer that stops using a contract before the provider removes it. A cycle
  is ``needs_decision``.

The heads also execute together: a member's ``compat.mixed_version``
persistence run probes the other members' head SQL against its migrated schema
(:func:`set_heads`), so that run's failure blocks the member and with it the
set.

An unreadable member (unknown to the viewer, or not readable) makes the set
``incomplete``. It is counted, never described.

Membership changes by :func:`link`, :func:`unlink` and :func:`replace` (point
this Change's link in one repository at another Change). Each needs manage
permission in both repositories, and each checks and writes under the
tenant's change-set write lock, so concurrent requests never interleave.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import heapq
import json
import os
import re
import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, Protocol

from lemoncrow.pro.capabilities.review import compat_mixed_version as compat
from lemoncrow.pro.capabilities.review import migration_execution
from lemoncrow.pro.capabilities.review.analysis_common import bounded, sha256_hex
from lemoncrow.pro.capabilities.review.contracts import _MIGRATION_PATH, inventory_side_effects
from lemoncrow.pro.capabilities.review.readiness import (
    ReadinessCondition,
    ReadinessInputs,
    ReadinessSources,
    RegisteredCondition,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewRelationship

CHANGE_SET_KIND: Final[str] = "change_set"
CHANGE_SET_RELATION: Final[str] = "member"
CHANGE_SET_PROVIDER: Final[str] = "lemoncrow"
#: The readiness registration (between the stack and organization policy conditions).
CONDITION_KEY: Final[str] = "change_set"
CONDITION_ORDER: Final[int] = 102

# lc-debt: a change set holds at most 8 Changes; the walk stops there and the set is
# ``incomplete`` (never ready). Upgrade path: a durable set record with server-side
# membership instead of walking member links on every evaluation.
MAX_MEMBERS: Final[int] = 8
# lc-debt: per member at most 300 changed files are read for contract surfaces, and at most
# 400 head code files (512 KiB each) are scanned for uses of a contract another member
# removes; past either bound the contract check is ``unknown``, never compatible.
# Upgrade path: an indexed route/table/symbol usage query per repository.
MAX_CHANGED_FILES: Final[int] = 300
MAX_SCAN_FILES: Final[int] = compat.MAX_CODE_FILES
MAX_SCAN_BYTES: Final[int] = compat.MAX_CODE_BYTES
# lc-debt: surfaces and head scans are memoized in process (an LRU of 64 per kind) keyed by
# immutable Snapshot ids. Upgrade path: persist them as review_derived rows.
_CACHE_MAX: Final[int] = 64

SetState = Literal["ready", "blocked", "needs_decision", "incomplete"]
Phase = Literal["migrations", "code"]
_PHASE_RANK: Final[Mapping[str, int]] = {"migrations": 0, "code": 1}


# --------------------------------------------------------------------------- #
# members and the directory
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True, order=True)
class MemberRef:
    repository: str
    review_id: str

    @property
    def key(self) -> str:
        return f"{self.repository}#{self.review_id}"


def alias(ref: MemberRef) -> str:
    """A stable label that does not name the member (records on one member never name another's repository)."""

    return "m-" + hashlib.sha256(ref.key.encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True)
class MemberSnapshot:
    """One readable member, as its own repository's Reader sees it (latest Snapshot)."""

    repository: str
    #: The Reader identity of the member's repository (cache scope, never shown).
    identity: str
    review_id: str
    title: str
    status: str
    revision_id: str
    snapshot_number: int
    changed_files: Callable[[], Sequence[tuple[str, str, str]]]
    read_side: Callable[[str, str], str | None]
    head_paths: Callable[[], Iterable[str] | None]
    #: The member's readiness without its change-set conditions (no recursion).
    own_readiness: Callable[[], Mapping[str, Any]]
    links: Callable[[], Sequence[MemberRef]]
    link: Callable[[MemberRef], object]
    #: Remove this member's own row naming another member (``None``: this member cannot be written).
    unlink: Callable[[MemberRef], object] | None = None
    #: Revalidate this member's armed landing now (``None``: nothing to revalidate here).
    revalidate_landing: Callable[[], object] | None = None

    @property
    def ref(self) -> MemberRef:
        return MemberRef(self.repository, self.review_id)


class ChangeSetDirectory(Protocol):
    """How a composition resolves set members for the current viewer.

    ``member`` returns ``None`` both for an unknown member and for one the
    viewer may not read (the two are indistinguishable by design), and only
    ever resolves within the viewer's own tenant. ``may_link`` is whether the
    acting principal may manage Reviews in *repository*. ``write_lock`` is the
    tenant's one change-set write lock, exclusive across threads, processes
    and replicas; a membership change checks and writes while holding it.
    """

    @property
    def repository(self) -> str: ...

    def member(self, ref: MemberRef) -> MemberSnapshot | None: ...

    def may_link(self, repository: str) -> bool: ...

    # lc-debt: one write lock per tenant serializes every change-set write in it; key the lock on the touched sets if write volume ever matters.
    def write_lock(self) -> AbstractContextManager[None]: ...


def link_row(review_id: str, ref: MemberRef) -> ReviewRelationship:
    """The relationship row *review_id* holds for linked member *ref*."""

    return ReviewRelationship(
        id="",
        review_id=review_id,
        kind=CHANGE_SET_KIND,
        relation=CHANGE_SET_RELATION,
        provider=CHANGE_SET_PROVIDER,
        external_id=ref.key,
        source="explicit",
        metadata_json=json.dumps({"repository": ref.repository, "review_id": ref.review_id}, sort_keys=True),
    )


def unlink_row(store: Any, review_id: str, ref: MemberRef) -> bool:
    """Remove the relationship row *review_id* holds for linked member *ref* (``False``: none held)."""

    row = link_row(review_id, ref)
    return bool(
        store.delete_relationship(
            review_id,
            kind=row.kind,
            relation=row.relation,
            provider=row.provider,
            external_id=row.external_id,
        )
    )


def member_links(store: Any, review_id: str) -> tuple[MemberRef, ...]:
    """The members one Change's own rows link it to."""

    out: set[MemberRef] = set()
    for row in store.list_relationships(review_id, kind=CHANGE_SET_KIND):
        try:
            meta = json.loads(row.metadata_json or "{}")
        except ValueError:
            continue
        if not isinstance(meta, Mapping):
            continue
        repository, target = str(meta.get("repository") or ""), str(meta.get("review_id") or "")
        if repository and target:
            out.add(MemberRef(repository, target))
    return tuple(sorted(out))


@dataclass(frozen=True)
class Discovery:
    """The members reachable from *start* that the viewer can read, and how many it cannot."""

    start: MemberSnapshot
    members: tuple[MemberSnapshot, ...]
    unreadable: int = 0
    truncated: bool = False

    @property
    def linked(self) -> bool:
        return len(self.members) > 1 or self.unreadable > 0 or self.truncated

    @property
    def complete(self) -> bool:
        return not self.unreadable and not self.truncated

    @property
    def others(self) -> tuple[MemberSnapshot, ...]:
        return self.members[1:]


def discover(start: MemberSnapshot, resolve: Callable[[MemberRef], MemberSnapshot | None]) -> Discovery:
    """Walk member links breadth-first from *start*, at most :data:`MAX_MEMBERS` members.

    A member *resolve* does not return (unknown or unreadable), or returns as
    a different Change, is counted and not walked further: links behind it are
    unknown, so the set is incomplete.

    A link is a pair of rows, one on each side. A readable member that does
    not link back (it unlinked, or a link write stopped half way) is not a
    member through that row: the row is dangling and is ignored -- before the
    bound is applied, so a dangling row never makes a full set look truncated.
    """

    seen: set[MemberRef] = {start.ref}
    members: list[MemberSnapshot] = [start]
    unreadable = 0
    truncated = False
    queue: deque[MemberSnapshot] = deque([start])
    while queue:
        current = queue.popleft()
        for ref in current.links():
            if ref in seen:
                continue
            member = resolve(ref)
            readable = member is not None and member.ref == ref
            if readable and member is not None and current.ref not in member.links():
                # Dangling: not a member through this row (another member may still link it).
                continue
            if len(seen) >= MAX_MEMBERS:
                truncated = True
                continue
            seen.add(ref)
            if member is None or not readable:
                unreadable += 1
                continue
            members.append(member)
            queue.append(member)
    return Discovery(start, tuple(members), unreadable, truncated)


# --------------------------------------------------------------------------- #
# contract surfaces (host side; reads Snapshot text, never executes it)
# --------------------------------------------------------------------------- #

Route = tuple[str, ...]
_PARAM = re.compile(r"\{[^}]*\}|<[^>]*>|^:\w+$|\$\{[^}]*\}|%[sd]|\*")
_URL = re.compile(r"(?:https?://[^/\s]+)?(/[^\s?#\"'`]*)(?:[?#]\S*)?")
_NAME = re.compile(r"\b[A-Z][A-Za-z0-9_]{2,}\b")
_WORD = re.compile(r"[A-Za-z_]\w*")
_IDENT = r"""[`"\[]?([A-Za-z_][\w.]*)[`"\]]?"""
_SQL_CREATE = re.compile(rf"\bCREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{_IDENT}", re.I)
_SQL_DROP = re.compile(rf"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?{_IDENT}", re.I)
_SQL_ALTER = re.compile(rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?{_IDENT}([^;]*)", re.I)
_SQL_DROP_COLUMN = re.compile(rf"\bDROP\s+(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?{_IDENT}", re.I)
_SQL_RENAME_COLUMN = re.compile(rf"\bRENAME\s+(?:COLUMN\s+)?{_IDENT}\s+TO\b", re.I)
_SQL_RENAME_TABLE = re.compile(rf"^\s*RENAME\s+TO\s+{_IDENT}", re.I)
_NOT_COLUMNS: Final[frozenset[str]] = frozenset(
    {"constraint", "index", "default", "not", "primary", "foreign", "check", "unique", "to", "key", "identity"}
)
_ALEMBIC = re.compile(
    r"""\bop\.(create_table|drop_table|add_column|drop_column|alter_column|rename_table)\(\s*"""
    r"""["']([\w.]+)["'](?:\s*,\s*(?:sa\.Column\(\s*)?["']([\w.]+)["'])?([^\n]*)"""
)
_RAILS = re.compile(
    r"""\b(create_table|drop_table|add_column|remove_column|rename_column|rename_table|change_column)\b"""
    r"""\s*\(?\s*[:"']([\w.]+)["']?(?:\s*,\s*[:"']([\w.]+)["']?)?"""
)


def route_segments(path: str, *, open_end: bool = False) -> Route:
    """A route path as comparable segments; parameters (``{id}``, ``:id``, ``${id}``) become ``*``."""

    parts = [part for part in path.split("/") if part and part != "."]
    segments = tuple("*" if _PARAM.search(part) else part.lower() for part in parts)
    return (*segments, "*") if open_end else segments


def route_matches(served: Route, called: Route) -> bool:
    """Whether a call to *called* reaches *served* (the served path may sit under a mount prefix)."""

    if not any(segment != "*" for segment in served) or len(called) < len(served):
        return False
    tail = called[len(called) - len(served) :]
    return all(a == b or a == "*" or b == "*" for a, b in zip(served, tail, strict=True))


def _route_label(route: Route) -> str:
    return "/" + "/".join(route)


def _table(name: str) -> str:
    return name.lower().rsplit(".", 1)[-1]


@dataclass(frozen=True)
class Uses:
    """Contracts code uses: called route paths, tables (and the words its SQL names), message types."""

    routes: frozenset[Route] = frozenset()
    tables: frozenset[str] = frozenset()
    columns: frozenset[tuple[str, str]] = frozenset()
    names: frozenset[str] = frozenset()

    def merged(self, other: Uses) -> Uses:
        return Uses(
            self.routes | other.routes,
            self.tables | other.tables,
            self.columns | other.columns,
            self.names | other.names,
        )


def code_uses(path: str, text: str) -> Uses:
    """What one code file uses (pure; literal route paths, literal SQL, ORM tables, type names)."""

    routes: set[Route] = set()
    for match in compat._LITERAL.finditer(text):
        body = next((match.group(name) for name in "abcde" if match.group(name) is not None), "")
        if not body or len(body) > 300 or not body.lstrip().startswith(("/", "http://", "https://")):
            continue
        url = _URL.fullmatch(body.strip())
        if url is None:
            continue
        segments = route_segments(url.group(1), open_end=url.group(1).endswith("/") and len(url.group(1)) > 1)
        if any(segment != "*" for segment in segments):
            routes.add(segments)
    tables: set[str] = set()
    columns: set[tuple[str, str]] = set()
    for statement in compat.extract_statements(path, text):
        words = {word.lower() for word in _WORD.findall(statement.sql)}
        for table in statement.tables:
            tables.add(table)
            columns |= {(table, word) for word in words}
    for fact in inventory_side_effects(path, text):
        if fact.category == "db_table":
            tables.add(_table(fact.name))
        elif fact.category == "route":
            # The path a handler declares is served here, not called.
            routes.discard(route_segments(fact.name.split(" ", 1)[-1]))
    return Uses(frozenset(routes), frozenset(tables), frozenset(columns), frozenset(_NAME.findall(text)))


@dataclass(frozen=True)
class Surface:
    """What one member's Change provides and uses, from its changed files (base vs head)."""

    routes_added: frozenset[Route] = frozenset()
    routes_removed: frozenset[Route] = frozenset()
    tables_migrated: frozenset[str] = frozenset()
    tables_removed: frozenset[str] = frozenset()
    columns_removed: frozenset[tuple[str, str]] = frozenset()
    messages_added: frozenset[str] = frozenset()
    messages_removed: frozenset[str] = frozenset()
    migrations: bool = False
    uses_head: Uses = field(default_factory=Uses)
    uses_base: Uses = field(default_factory=Uses)
    notes: tuple[str, ...] = ()
    complete: bool = True

    @property
    def removes(self) -> bool:
        return bool(self.routes_removed or self.tables_removed or self.columns_removed or self.messages_removed)


def _migration_ops(path: str, text: str) -> list[tuple[str, str, str]]:
    """``(op, table, column)`` of one migration: ``create``, ``alter``, ``drop``, ``drop_column``."""

    ops: list[tuple[str, str, str]] = []
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".sql":
        body = migration_execution._COMMENTS.sub(" ", text)
        ops += [("create", _table(name), "") for name in _SQL_CREATE.findall(body)]
        ops += [("drop", _table(name), "") for name in _SQL_DROP.findall(body)]
        for name, rest in _SQL_ALTER.findall(body):
            table = _table(name)
            renamed = _SQL_RENAME_TABLE.match(rest)
            if renamed:
                ops += [("drop", table, ""), ("create", _table(renamed.group(1)), "")]
                continue
            ops.append(("alter", table, ""))
            for column in (*_SQL_DROP_COLUMN.findall(rest), *_SQL_RENAME_COLUMN.findall(rest)):
                if column.lower() not in _NOT_COLUMNS:
                    ops.append(("drop_column", table, column.lower()))
    elif suffix == ".py":
        for op, first, second, rest in _ALEMBIC.findall(text):
            table = _table(first)
            if op == "create_table":
                ops.append(("create", table, ""))
            elif op == "drop_table":
                ops.append(("drop", table, ""))
            elif op == "rename_table":
                ops += [("drop", table, ""), *([("create", _table(second), "")] if second else [])]
            elif op == "drop_column" or (op == "alter_column" and "new_column_name" in rest):
                ops += [("alter", table, ""), *([("drop_column", table, second.lower())] if second else [])]
            else:
                ops.append(("alter", table, ""))
    elif suffix == ".rb":
        for op, first, second in _RAILS.findall(text):
            table = _table(first)
            if op == "create_table":
                ops.append(("create", table, ""))
            elif op in {"drop_table", "rename_table"}:
                ops += [("drop", table, ""), *([("create", _table(second), "")] if second else [])]
            elif op in {"remove_column", "rename_column"}:
                ops += [("alter", table, ""), *([("drop_column", table, second.lower())] if second else [])]
            else:
                ops.append(("alter", table, ""))
    return ops


def _message_names(path: str, text: str) -> set[str]:
    lowered = path.lower()
    if lowered.endswith(".proto"):
        return {name.rsplit(".", 1)[-1] for name in compat.proto_messages(text)}
    doc = compat._load_document(path, text)
    if lowered.endswith(".avsc"):
        return {name.rsplit(".", 1)[-1] for name in compat.avro_records(doc, compat._Budget())}
    title = doc.get("title") if isinstance(doc, Mapping) else None
    return {str(title)} if isinstance(title, str) and title else {PurePosixPath(path).name.split(".", 1)[0]}


def _served_routes(path: str, text: str | None) -> set[Route]:
    if text is None:
        return set()
    return {
        route_segments(fact.name.split(" ", 1)[-1])
        for fact in inventory_side_effects(path, text)
        if fact.category == "route"
    }


def _openapi_routes(path: str, text: str | None) -> set[Route]:
    if not text:
        return set()
    doc = compat._load_document(path, text)
    return {route_segments(key.split(" ", 1)[-1]) for key in compat._operations(doc, compat._Budget())}


def surface(member: MemberSnapshot) -> Surface:
    """The contract surface of one member's Change (memoized per immutable Snapshot)."""

    key = (member.identity, member.revision_id)
    cached = _SURFACES.get(key)
    if cached is not None:
        return cached
    computed = _surface(member)
    _SURFACES.put(key, computed)
    return computed


def _surface(member: MemberSnapshot) -> Surface:
    notes: list[str] = []
    complete = True
    files = sorted(member.changed_files())
    if len(files) > MAX_CHANGED_FILES:
        complete = False
        notes.append(f"only the first {MAX_CHANGED_FILES} of {len(files)} changed files were read for contracts")
        files = files[:MAX_CHANGED_FILES]
    served: dict[str, set[Route]] = {"old": set(), "new": set()}
    messages: dict[str, set[str]] = {"old": set(), "new": set()}
    migrated: set[str] = set()
    dropped: set[str] = set()
    dropped_columns: set[tuple[str, str]] = set()
    migrations = False
    uses = {"old": Uses(), "new": Uses()}
    for path, old_path, status in files:
        texts = {
            "old": None if status == "added" else member.read_side(old_path or path, "old"),
            "new": None if status == "deleted" else member.read_side(path, "new"),
        }
        if any(text is not None and len(text) > MAX_SCAN_BYTES for text in texts.values()):
            complete = False
            notes.append(f"{path} is over {MAX_SCAN_BYTES // 1024} KiB and was not read for contracts")
            continue
        try:
            if compat._OPENAPI.search(path):
                for side in ("old", "new"):
                    served[side] |= _openapi_routes(path, texts[side])
                continue
            if compat._MESSAGE_DOCS.search(path):
                for side in ("old", "new"):
                    text = texts[side]
                    messages[side] |= _message_names(path, text) if text else set()
                continue
        except (ValueError, TypeError, AttributeError, KeyError, RecursionError) as exc:
            complete = False
            notes.append(f"{path} cannot be read as a contract: {bounded(exc, 160)}")
            continue
        if _MIGRATION_PATH.search(path) and path.lower().endswith((".sql", ".py", ".rb")):
            text = texts["new"]
            if text is None:
                continue
            migrations = True
            ops = _migration_ops(path, text)
            if not ops:
                named = compat.migration_tables(text)
                if not named:
                    complete = False
                    notes.append(f"{path} declares no table operation this check recognizes")
                migrated |= named
            for op, table, column in ops:
                if op == "drop":
                    dropped.add(table)
                elif op == "drop_column":
                    dropped_columns.add((table, column))
                else:
                    migrated.add(table)
            continue
        if PurePosixPath(path).suffix.lower() not in compat._CODE_SUFFIXES or compat._VENDORED.search(path):
            continue
        for side in ("old", "new"):
            text = texts[side]
            if text is None:
                continue
            served[side] |= _served_routes(path, text)
            if not compat._TEST_PATH.search(path):
                uses[side] = uses[side].merged(code_uses(path, text))
    return Surface(
        routes_added=frozenset(served["new"] - served["old"]),
        routes_removed=frozenset(served["old"] - served["new"]),
        tables_migrated=frozenset(migrated - dropped),
        tables_removed=frozenset(dropped - migrated),
        columns_removed=frozenset(dropped_columns),
        messages_added=frozenset(messages["new"] - messages["old"]),
        messages_removed=frozenset(messages["old"] - messages["new"]),
        migrations=migrations,
        uses_head=uses["new"],
        uses_base=uses["old"],
        notes=tuple(notes),
        complete=complete,
    )


@dataclass(frozen=True)
class HeadScan:
    """Where one member's whole head uses the contracts another member removes."""

    hits: Mapping[str, tuple[str, ...]]
    complete: bool
    notes: tuple[str, ...] = ()


def _removed_tokens(provider: Surface, uses: Uses) -> set[str]:
    found = {
        f"route {_route_label(served)}"
        for served in provider.routes_removed
        if any(route_matches(served, called) for called in uses.routes)
    }
    found |= {f"table {table}" for table in provider.tables_removed & uses.tables}
    found |= {f"column {table}.{column}" for table, column in provider.columns_removed & uses.columns}
    found |= {f"message {name}" for name in provider.messages_removed & uses.names}
    return found


def scan_head(consumer: MemberSnapshot, provider: Surface) -> HeadScan:
    """Search *consumer*'s head code (changed files first) for the contracts *provider* removes."""

    wanted = sha256_hex(
        [
            sorted(map(list, provider.routes_removed)),
            sorted(provider.tables_removed),
            sorted(map(list, provider.columns_removed)),
            sorted(provider.messages_removed),
        ]
    )
    key = (consumer.identity, consumer.revision_id, wanted)
    cached = _SCANS.get(key)
    if cached is not None:
        return cached
    listing = consumer.head_paths()
    if listing is None:
        result = HeadScan({}, False, ("the head tree is not readable, so its uses were not scanned",))
        _SCANS.put(key, result)
        return result
    changed = {path for path, _old, _status in consumer.changed_files()}
    candidates = sorted(
        path
        for path in listing
        if PurePosixPath(path).suffix.lower() in compat._CODE_SUFFIXES
        and not compat._VENDORED.search(path)
        and not compat._TEST_PATH.search(path)
        and not _MIGRATION_PATH.search(path)
    )
    ordered = [path for path in candidates if path in changed] + [path for path in candidates if path not in changed]
    notes: list[str] = []
    complete = True
    if len(ordered) > MAX_SCAN_FILES:
        complete = False
        notes.append(f"the head scan stops at {MAX_SCAN_FILES} of {len(ordered)} code files")
    hits: dict[str, list[str]] = {}
    for path in ordered[:MAX_SCAN_FILES]:
        text = consumer.read_side(path, "new")
        if text is None:
            continue
        if len(text) > MAX_SCAN_BYTES:
            complete = False
            notes.append(f"{path} is over {MAX_SCAN_BYTES // 1024} KiB and was not scanned")
            continue
        for token in _removed_tokens(provider, code_uses(path, text)):
            hits.setdefault(token, []).append(path)
    result = HeadScan({token: tuple(paths[:5]) for token, paths in sorted(hits.items())}, complete, tuple(notes))
    _SCANS.put(key, result)
    return result


# --------------------------------------------------------------------------- #
# contracts between heads and the deploy order
# --------------------------------------------------------------------------- #

Node = tuple[MemberRef, str]


@dataclass(frozen=True)
class ContractIssue:
    provider: MemberRef
    consumer: MemberRef
    contract: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class OrderEdge:
    before: Node
    after: Node
    reason: str


@dataclass(frozen=True)
class Contracts:
    state: str
    issues: tuple[ContractIssue, ...] = ()
    edges: tuple[OrderEdge, ...] = ()
    nodes: tuple[Node, ...] = ()
    notes: tuple[str, ...] = ()


def check_contracts(members: Sequence[MemberSnapshot]) -> Contracts:
    """Contract compatibility between the members' heads, and the ordering edges it implies."""

    surfaces = {member.ref: surface(member) for member in members}
    issues: list[ContractIssue] = []
    edges: list[OrderEdge] = []
    nodes: list[Node] = []
    notes: list[str] = []
    complete = all(item.complete for item in surfaces.values())
    for member in members:
        own = surfaces[member.ref]
        notes.extend(f"{alias(member.ref)}: {note}" for note in own.notes)
        nodes.append((member.ref, "code"))
        if own.migrations:
            nodes.append((member.ref, "migrations"))
            edges.append(OrderEdge((member.ref, "migrations"), (member.ref, "code"), "migrations before code"))
    touched = False
    for provider in members:
        offered = surfaces[provider.ref]
        migrations: Node = (provider.ref, "migrations" if offered.migrations else "code")
        for consumer in members:
            if consumer.ref == provider.ref:
                continue
            used = surfaces[consumer.ref]
            needs: list[tuple[Node, str]] = []
            for served in sorted(offered.routes_added):
                if any(route_matches(served, called) for called in used.uses_head.routes):
                    needs.append(((provider.ref, "code"), f"calls {_route_label(served)}, which it starts serving"))
            for table in sorted(offered.tables_migrated & used.uses_head.tables):
                needs.append((migrations, f"uses table {table}, which it migrates"))
            for name in sorted(offered.messages_added & used.uses_head.names):
                needs.append(((provider.ref, "code"), f"uses message {name}, which it introduces"))
            for before, why in needs:
                touched = True
                edges.append(
                    OrderEdge(before, (consumer.ref, "code"), f"{alias(consumer.ref)} {why} ({alias(provider.ref)})")
                )
            if not offered.removes:
                continue
            touched = True
            scan = scan_head(consumer, offered)
            complete = complete and scan.complete
            notes.extend(f"{alias(consumer.ref)}: {note}" for note in scan.notes)
            for token, paths in scan.hits.items():
                issues.append(ContractIssue(provider.ref, consumer.ref, token, paths))
            # A consumer that stops using a contract deploys before the provider removes it.
            for token in sorted(_removed_tokens(offered, used.uses_base) - set(scan.hits)):
                target: Node = migrations if token.startswith(("table ", "column ")) else (provider.ref, "code")
                edges.append(
                    OrderEdge(
                        (consumer.ref, "code"),
                        target,
                        f"{alias(consumer.ref)} stops using {token} before {alias(provider.ref)} removes it",
                    )
                )
    if issues:
        state = "incompatible"
    elif not complete:
        state = "unknown"
    elif touched:
        state = "compatible"
    else:
        state = "not_applicable"
    return Contracts(state, tuple(issues), tuple(edges), tuple(nodes), tuple(notes))


@dataclass(frozen=True)
class DeployOrder:
    steps: tuple[Node, ...]
    after: Mapping[Node, tuple[OrderEdge, ...]]
    cycle: tuple[Node, ...] = ()


def _node_key(node: Node) -> tuple[int, str, str]:
    return (_PHASE_RANK[node[1]], node[0].repository, node[0].review_id)


def deploy_order(nodes: Sequence[Node], edges: Sequence[OrderEdge]) -> DeployOrder:
    """Kahn's topological order of the deploy steps; ties go migrations first, then by repository.

    Whatever cannot be ordered (a cycle, and everything waiting behind it) is
    returned as ``cycle``: the order needs a human decision.
    """

    known = set(nodes)
    successors: dict[Node, set[Node]] = {node: set() for node in known}
    indegree: dict[Node, int] = dict.fromkeys(known, 0)
    incoming: dict[Node, list[OrderEdge]] = {node: [] for node in known}
    for edge in edges:
        if edge.before not in known or edge.after not in known or edge.before == edge.after:
            continue
        incoming[edge.after].append(edge)
        if edge.after not in successors[edge.before]:
            successors[edge.before].add(edge.after)
            indegree[edge.after] += 1
    heap = [(_node_key(node), node) for node in known if indegree[node] == 0]
    heapq.heapify(heap)
    steps: list[Node] = []
    while heap:
        _key, node = heapq.heappop(heap)
        steps.append(node)
        for successor in successors[node]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                heapq.heappush(heap, (_node_key(successor), successor))
    cycle = tuple(sorted((node for node in known if node not in steps), key=_node_key))
    after = {node: tuple(sorted(rows, key=lambda edge: _node_key(edge.before))) for node, rows in incoming.items()}
    return DeployOrder(tuple(steps), after, cycle)


# --------------------------------------------------------------------------- #
# the set assessment, its readiness conditions and its wire form
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MemberState:
    member: MemberSnapshot
    #: ``None``: the member's own readiness could not be read.
    ready: bool | None
    blockers: int = 0
    error: str = ""


@dataclass(frozen=True)
class Assessment:
    discovery: Discovery
    #: Own readiness of the members evaluated (every member, or every member but the start).
    states: tuple[MemberState, ...]
    contracts: Contracts
    order: DeployOrder | None
    state: SetState
    #: Repositories with more than one Change in the set, and those Changes: a broken set.
    duplicates: tuple[tuple[str, tuple[MemberRef, ...]], ...] = ()


def _duplicates(members: Sequence[MemberSnapshot]) -> tuple[tuple[str, tuple[MemberRef, ...]], ...]:
    by_repository: dict[str, list[MemberRef]] = {}
    for member in members:
        by_repository.setdefault(member.repository, []).append(member.ref)
    return tuple(
        (repository, tuple(sorted(refs))) for repository, refs in sorted(by_repository.items()) if len(refs) > 1
    )


def _member_state(member: MemberSnapshot) -> MemberState:
    try:
        readiness = member.own_readiness()
    except Exception as exc:  # a member that cannot be judged is never ready
        return MemberState(member, None, 0, bounded(exc, 200) or exc.__class__.__name__)
    return MemberState(member, readiness.get("ready") is True, int(readiness.get("blocker_count") or 0))


def assess(discovery: Discovery, *, include_start: bool) -> Assessment:
    """The set's state: the conjunction of the members' own readiness and the set-level checks.

    ``include_start`` is ``False`` for the start member's own readiness
    condition (its own blockers are already its other conditions).
    """

    evaluated = discovery.members if include_start else discovery.others
    states = tuple(_member_state(member) for member in evaluated)
    # Two Changes of one repository (a replace that died half way): never ready, whatever else holds.
    duplicates = _duplicates(discovery.members)
    if not discovery.complete:
        state_now: SetState = "blocked" if duplicates else "incomplete"
        return Assessment(discovery, states, Contracts("unknown"), None, state_now, duplicates)
    contracts = check_contracts(discovery.members)
    order = deploy_order(contracts.nodes, contracts.edges)
    members_ready = all(item.ready is True for item in states)
    state: SetState
    if duplicates or not members_ready or contracts.state in {"incompatible", "unknown"}:
        state = "blocked"
    elif order.cycle:
        state = "needs_decision"
    else:
        state = "ready"
    return Assessment(discovery, states, contracts, order, state, duplicates)


def _ref_label(ref: MemberRef) -> str:
    return f"r/{ref.review_id} in {ref.repository}"


def conditions(assessment: Assessment) -> list[ReadinessCondition]:
    """The set blockers one member's readiness shows (``change-set:*``)."""

    discovery = assessment.discovery
    extra = {"set_state": assessment.state}
    if assessment.duplicates:
        named = "; ".join(
            f"{repository}: " + ", ".join(f"r/{ref.review_id}" for ref in refs)
            for repository, refs in assessment.duplicates
        )
        return [
            ReadinessCondition(
                id="change-set:members",
                label="Change set members",
                state="fail",
                detail=bounded(
                    f"This change set holds more than one Change of one repository ({named}). "
                    "Unlink all but one of them.",
                    600,
                ),
                category="change_set",
                reason="set_duplicate_repository",
                extra=extra,
            )
        ]
    if not discovery.complete:
        hidden = discovery.unreadable
        detail = (
            f"{hidden} member(s) of this change set cannot be read here, so the set cannot be judged."
            if hidden
            else f"The change set links more than {MAX_MEMBERS} Changes, so it cannot be judged."
        )
        # Unknown and transient: it blocks arming but never withdraws an armed
        # landing (a request-free revalidation has no viewer to read members as).
        return [
            ReadinessCondition(
                id="change-set:members",
                label="Change set members",
                state="unknown",
                detail=detail,
                category="change_set",
                reason="evaluation_error",
                extra=extra,
            )
        ]
    unread = [item for item in assessment.states if item.ready is None]
    blocked = [item for item in assessment.states if item.ready is False]
    if unread:
        members = ReadinessCondition(
            id="change-set:members",
            label="Change set members",
            state="unknown",
            detail="The readiness of " + ", ".join(_ref_label(item.member.ref) for item in unread) + " is unavailable.",
            category="change_set",
            reason="evaluation_error",
            extra=extra,
        )
    elif blocked:
        members = ReadinessCondition(
            id="change-set:members",
            label="Change set members",
            state="pending",
            detail="Blocked by "
            + "; ".join(f"{_ref_label(item.member.ref)} ({item.blockers} blocker(s))" for item in blocked)
            + ".",
            category="change_set",
            reason="set_member_blocked",
            extra=extra,
        )
    else:
        members = ReadinessCondition(
            id="change-set:members",
            label="Change set members",
            state="pass",
            detail=f"Every other member of the change set ({len(assessment.states)}) is ready.",
            category="change_set",
            reason="satisfied",
            extra=extra,
        )
    contracts = assessment.contracts
    contract_state = {"incompatible": "fail", "unknown": "unknown", "not_applicable": "not_applicable"}.get(
        contracts.state, "pass"
    )
    if contracts.issues:
        detail = "; ".join(
            f"{_ref_label(item.provider)} removes {item.contract}, which {_ref_label(item.consumer)} still uses"
            for item in contracts.issues[:5]
        )
    elif contracts.state == "unknown":
        detail = "Contracts between the heads could not be fully checked: " + "; ".join(contracts.notes[:3])
    elif contracts.state == "not_applicable":
        detail = "No member provides or removes a contract another member uses."
    else:
        detail = "Every contract one member provides or removes matches the other members' heads."
    rows = [
        members,
        ReadinessCondition(
            id="change-set:contracts",
            label="Change set contracts",
            state=contract_state,
            detail=bounded(detail, 600),
            category="change_set",
            reason={"fail": "contract_incompatible", "unknown": "limited_by_budget"}.get(contract_state, "satisfied"),
            extra=extra,
        ),
    ]
    order = assessment.order
    if order is not None and order.cycle:
        rows.append(
            ReadinessCondition(
                id="change-set:deploy-order",
                label="Change set deploy order",
                state="pending",
                detail="The deploy order has a cycle ("
                + ", ".join(f"{_ref_label(ref)} {phase}" for ref, phase in order.cycle)
                + "): decide the order before landing.",
                category="change_set",
                reason="needs_decision",
                extra=extra,
            )
        )
    elif order is not None:
        rows.append(
            ReadinessCondition(
                id="change-set:deploy-order",
                label="Change set deploy order",
                state="pass",
                detail="Deploy order: " + " → ".join(f"{_ref_label(ref)} {phase}" for ref, phase in order.steps) + ".",
                category="change_set",
                reason="satisfied",
                extra=extra,
            )
        )
    return rows


def readiness_condition(gather: Callable[[ReadinessSources], object]) -> RegisteredCondition:
    """The ``change_set`` readiness registration; *gather* returns an :class:`Assessment` or ``None``."""

    def evaluate(inputs: ReadinessInputs) -> list[ReadinessCondition]:
        fact = inputs.fact(CONDITION_KEY)
        return conditions(fact) if isinstance(fact, Assessment) else []

    return RegisteredCondition(key=CONDITION_KEY, order=CONDITION_ORDER, evaluate=evaluate, gather=gather)


def _member_wire(item: MemberState | MemberSnapshot, *, start: MemberRef) -> dict[str, Any]:
    member = item.member if isinstance(item, MemberState) else item
    row: dict[str, Any] = {
        "alias": alias(member.ref),
        "readable": True,
        "repository": member.repository,
        "review_id": member.review_id,
        "ref": f"r/{member.review_id}",
        "title": member.title,
        "status": member.status,
        "snapshot_number": member.snapshot_number,
        "current": member.ref == start,
    }
    if isinstance(item, MemberState):
        row.update({"ready": item.ready, "blockers": item.blockers, "error": item.error})
    return row


def to_wire(assessment: Assessment) -> dict[str, Any]:
    """The set as the viewer may see it: unreadable members are counted, never named."""

    discovery = assessment.discovery
    start = discovery.start.ref
    states = {item.member.ref: item for item in assessment.states}
    members = [_member_wire(states.get(member.ref, member), start=start) for member in discovery.members]
    members.extend({"alias": "", "readable": False} for _ in range(discovery.unreadable))
    contracts = assessment.contracts
    order = assessment.order
    steps: list[dict[str, Any]] = []
    if order is not None:
        for position, (ref, phase) in enumerate(order.steps, 1):
            steps.append(
                {
                    "position": position,
                    "alias": alias(ref),
                    "repository": ref.repository,
                    "review_id": ref.review_id,
                    "phase": phase,
                    "after": [
                        {"alias": alias(edge.before[0]), "phase": edge.before[1], "reason": edge.reason}
                        for edge in order.after.get((ref, phase), ())
                    ],
                }
            )
    return {
        "available": True,
        "linked": discovery.linked,
        "state": assessment.state,
        "members": members,
        "member_count": len(members),
        "unreadable": discovery.unreadable,
        "truncated": discovery.truncated,
        "max_members": MAX_MEMBERS,
        "duplicate_repositories": [repository for repository, _refs in assessment.duplicates],
        "ready_members": sum(1 for item in assessment.states if item.ready is True),
        "contracts": {
            "state": contracts.state,
            "issues": [
                {
                    "provider": alias(item.provider),
                    "consumer": alias(item.consumer),
                    "contract": item.contract,
                    "paths": list(item.paths),
                }
                for item in contracts.issues[:50]
            ],
            "notes": list(contracts.notes[:10]),
        },
        "deploy_order": {
            "state": "unknown" if order is None else ("needs_decision" if order.cycle else "ordered"),
            "steps": steps,
            "cycle": (
                [{"alias": alias(ref), "phase": phase} for ref, phase in order.cycle] if order is not None else []
            ),
        },
    }


def unlinked_wire(*, available: bool) -> dict[str, Any]:
    """The wire form of a Change in no set (``available``: this Reader can link sets)."""

    return {
        "available": available,
        "linked": False,
        "state": "not_linked",
        "members": [],
        "member_count": 0,
        "unreadable": 0,
        "truncated": False,
        "max_members": MAX_MEMBERS,
        "ready_members": 0,
        "contracts": {"state": "not_applicable", "issues": [], "notes": []},
        "deploy_order": {"state": "unknown", "steps": [], "cycle": []},
    }


# --------------------------------------------------------------------------- #
# linking, and the heads-together run
# --------------------------------------------------------------------------- #


class LinkRefused(Exception):
    """A link request the set cannot take; ``status`` is the HTTP status to answer with.

    Every refusal is raised before the request writes anything: a refused
    request changed nothing.
    """

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


#: The refusal a lock that cannot be taken in time answers with (409): nothing was written.
LOCK_BUSY: Final[str] = "another change to this change set is in progress; nothing was changed, retry"

_FILE_LOCKS: dict[str, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def file_write_lock(path: Path) -> Iterator[None]:
    """Exclusive change-set write lock held in *path*, across threads and processes.

    For stores that live in files (the local Reader, the hosted SQLite
    backend): every process that writes change sets for one tenant takes the
    same lock file. The file stays (unlinking a lock file another process has
    open lets two holders appear).
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    with _FILE_LOCKS_GUARD:
        local = _FILE_LOCKS.setdefault(key, threading.Lock())
    with local:
        handle = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt_api: Any = msvcrt
                msvcrt_api.locking(handle.fileno(), msvcrt_api.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt_unlock: Any = msvcrt
                    msvcrt_unlock.locking(handle.fileno(), msvcrt_unlock.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def _target(directory: ChangeSetDirectory, start: MemberSnapshot, repository: str, review_ref: str) -> MemberSnapshot:
    """The other repository's Change a set request names, readable and manageable on both sides."""

    repository, review_ref = repository.strip(), review_ref.strip()
    if not repository or not review_ref:
        raise LinkRefused(422, "a change-set link needs repository and review")
    if repository == start.repository:
        raise LinkRefused(422, "a change set links Changes in different repositories; use a stack within one")
    target = directory.member(MemberRef(repository, review_ref.removeprefix("r/")))
    if target is None:
        # Unreadable and unknown are one answer: existence is never confirmed.
        raise LinkRefused(404, "related Change not found")
    if not (directory.may_link(start.repository) and directory.may_link(target.repository)):
        raise LinkRefused(403, "changing a change set needs manage permission in both repositories")
    return target


def _refuse_union(ours: Discovery, theirs: Discovery) -> None:
    """Refuse joining two sets that are not whole for the caller, too large, or share a repository."""

    if not ours.complete or not theirs.complete:
        raise LinkRefused(409, "the change set has members you cannot read, so it cannot be extended")
    union = {member.ref for member in (*ours.members, *theirs.members)}
    if len(union) > MAX_MEMBERS:
        raise LinkRefused(422, f"a change set holds at most {MAX_MEMBERS} Changes")
    if any(
        member.repository == other.repository and member.ref != other.ref
        for member in ours.members
        for other in theirs.members
    ):
        raise LinkRefused(422, "a change set holds one Change per repository")


class _Writes:
    """The row writes of one request, undone in reverse if a later write raises.

    Only used under the directory's write lock, so nothing else changed the
    rows in between and the undo restores exactly the state before the request.
    A process that dies mid-request undoes nothing; the write order makes that
    state fail closed (a dangling row joins nothing; two Changes of one
    repository make the set ``blocked``).
    """

    def __init__(self) -> None:
        self._undo: list[Callable[[], object]] = []

    def add(self, member: MemberSnapshot, ref: MemberRef) -> None:
        if ref in member.links():
            return  # already held (a dangling row a retry completes): nothing to write or undo
        member.link(ref)
        if member.unlink is not None:
            unlink = member.unlink
            self._undo.append(lambda: unlink(ref))

    def drop(self, member: MemberSnapshot, ref: MemberRef) -> None:
        if member.unlink is None or ref not in member.links():
            return
        member.unlink(ref)
        self._undo.append(lambda: member.link(ref))

    def rollback(self) -> None:
        for undo in reversed(self._undo):
            with contextlib.suppress(Exception):
                undo()


@contextlib.contextmanager
def _applying(directory: ChangeSetDirectory) -> Iterator[_Writes]:
    """Check-and-write atomically: the tenant's change-set write lock is held throughout."""

    with directory.write_lock():
        writes = _Writes()
        try:
            yield writes
        except LinkRefused:
            raise  # refusals happen before any write
        except BaseException:
            writes.rollback()
            raise


def link(directory: ChangeSetDirectory, start: MemberSnapshot, repository: str, review_ref: str) -> MemberSnapshot:
    """Link *start* and another repository's Change into one set (rows on both sides).

    Needs manage permission in both repositories and a readable target; an
    unreadable target is "not found" (never "forbidden": that would confirm it
    exists). A set with members the caller cannot read is never extended.

    The check and the writes run under the tenant's change-set write lock
    (:meth:`ChangeSetDirectory.write_lock`), so two requests never interleave:
    a refused request changed nothing and an accepted one is never undone by
    another request.
    """

    target = _target(directory, start, repository, review_ref)
    if target.status == "archived":
        raise LinkRefused(409, "the related Change is discarded")
    with _applying(directory) as writes:
        if target.ref in start.links() and start.ref in target.links():
            return target  # already linked: nothing to write
        _refuse_union(discover(start, directory.member), discover(target, directory.member))
        # The caller's own side last: a write that stops half way leaves a
        # dangling row on the target, which joins nothing.
        writes.add(target, start.ref)
        writes.add(start, target.ref)
    return target


def unlink(directory: ChangeSetDirectory, start: MemberSnapshot, repository: str, review_ref: str) -> MemberSnapshot:
    """Remove the link between *start* and another repository's Change (rows on both sides).

    Unlinking relaxes set readiness, so it needs the authority linking needs:
    manage permission in both repositories and a readable target. Unlike
    linking it does not need the rest of the set to be readable: it removes
    only the pair the caller can read and manage, never extends a set and
    reveals nothing about other members (it is also how a broken set -- two
    Changes of one repository -- is repaired). It runs under the same write
    lock and needs no check after the write: removing a pair cannot break an
    invariant. The other side's row goes first; a write that stops half way
    leaves a dangling row on *start*, which joins nothing.
    """

    target = _target(directory, start, repository, review_ref)
    if start.unlink is None or target.unlink is None:
        raise LinkRefused(409, "this change set cannot be changed here")
    with _applying(directory) as writes:
        if target.ref not in start.links() and start.ref not in target.links():
            raise LinkRefused(404, "this Change is not linked to that Change")
        writes.drop(target, start.ref)
        writes.drop(start, target.ref)
    return target


def replace(directory: ChangeSetDirectory, start: MemberSnapshot, repository: str, review_ref: str) -> MemberSnapshot:
    """Update the set: the Change *start* links in *repository* becomes *review_ref*.

    Checked and written under the tenant's change-set write lock, so the
    result is either the whole update or (a refusal) nothing. The new link is
    written before the old one is removed: a process that dies in between
    leaves both Changes of that repository linked, which :func:`assess` rates
    ``blocked`` ("two Changes of one repository") until one is unlinked --
    never a set that is ready with the wrong Change.
    """

    target = _target(directory, start, repository, review_ref)
    if target.status == "archived":
        raise LinkRefused(409, "the related Change is discarded")
    with _applying(directory) as writes:
        current = [ref for ref in start.links() if ref.repository == target.repository]
        if not current:
            raise LinkRefused(409, "this Change links no Change in that repository; link it instead")
        if current == [target.ref] and start.ref in target.links():
            return target
        previous = [directory.member(ref) for ref in current if ref != target.ref]
        if any(member is None for member in previous):
            raise LinkRefused(409, "the change set has members you cannot read, so it cannot be extended")
        old = tuple(member for member in previous if member is not None)
        if start.unlink is None or target.unlink is None or any(member.unlink is None for member in old):
            raise LinkRefused(409, "this change set cannot be changed here")
        replaced = {member.ref for member in old}
        view = dataclasses.replace(start, links=lambda: tuple(ref for ref in start.links() if ref not in replaced))

        def resolve(ref: MemberRef) -> MemberSnapshot | None:
            return view if ref == start.ref else directory.member(ref)

        _refuse_union(discover(view, resolve), discover(target, resolve))
        writes.add(target, start.ref)
        writes.add(start, target.ref)
        for member in old:
            writes.drop(member, start.ref)
            writes.drop(start, member.ref)
    return target


def set_heads(discovery: Discovery | None) -> compat.SetHeads | None:
    """The other members' heads for the heads-together persistence run (``None``: no set)."""

    if discovery is None or not discovery.linked:
        return None
    heads = tuple(_set_head(member) for member in discovery.others)
    return compat.SetHeads(heads, unreadable=discovery.unreadable, truncated=discovery.truncated)


def _set_head(member: MemberSnapshot) -> compat.SetHead:
    def changed() -> list[str]:
        return [path for path, _old, _status in member.changed_files()]

    def read(path: str) -> str | None:
        return member.read_side(path, "new")

    return compat.SetHead(alias=alias(member.ref), paths=member.head_paths, changed=changed, read=read)


class _Lru:
    def __init__(self) -> None:
        self._rows: OrderedDict[Any, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        with self._lock:
            value = self._rows.get(key)
            if value is not None:
                self._rows.move_to_end(key)
            return value

    def put(self, key: Any, value: Any) -> None:
        with self._lock:
            self._rows[key] = value
            while len(self._rows) > _CACHE_MAX:
                self._rows.popitem(last=False)


_SURFACES = _Lru()
_SCANS = _Lru()


__all__ = [
    "CHANGE_SET_KIND",
    "CONDITION_KEY",
    "LOCK_BUSY",
    "MAX_MEMBERS",
    "Assessment",
    "ChangeSetDirectory",
    "Contracts",
    "DeployOrder",
    "Discovery",
    "LinkRefused",
    "MemberRef",
    "MemberSnapshot",
    "OrderEdge",
    "Surface",
    "alias",
    "assess",
    "check_contracts",
    "code_uses",
    "conditions",
    "deploy_order",
    "discover",
    "file_write_lock",
    "link",
    "link_row",
    "member_links",
    "readiness_condition",
    "replace",
    "route_matches",
    "route_segments",
    "set_heads",
    "surface",
    "to_wire",
    "unlink",
    "unlink_row",
    "unlinked_wire",
]

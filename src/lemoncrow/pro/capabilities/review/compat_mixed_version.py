"""Mixed-version compatibility (Phase 9R item 41, ``compat.mixed_version``).

During a rolling deploy old and new code run side by side against one database,
one API and one message bus. For the persistence, API and message-format
changes a Change makes -- found deterministically from the trusted-base
contract surfaces and the R3 inventory of the changed files -- this analysis
constructs each mixed pairing and checks it:

* **persistence** (executed in the sandbox, building on ``migration_execution``):
  the old code's literal SQL against the migrated schema, and the reverse (the
  new code's SQL against the old schema). Both schemas are built on SQLite from
  the trusted base's migrations (or schema fixture) plus the added migrations;
  every statement is prepared (``EXPLAIN``) on both, never run.
* **api** (evaluated on the review host from contract documents): an old client
  against the new API -- removed operations, new required parameters or request
  fields, removed or retyped response fields in the OpenAPI documents, and
  routes the R3 inventory sees removed from the changed code -- and the reverse
  (new operations an old API does not serve).
* **messages** (evaluated on the review host): a new consumer against old-format
  messages (proto, Avro, JSON Schema) and the reverse.

A pairing is ``compatible``, ``incompatible``, ``unknown`` (something relevant
could not be verified), ``not_available`` (the pairing cannot be constructed:
an unsupported framework or protocol, no literal SQL to pair, an unparseable
contract, a migration that does not apply), ``limited_by_budget`` or
``not_applicable`` (the Change touches nothing that pairing covers). Only
``compatible`` and ``not_applicable`` pairings can pass; a forward
incompatibility (old code, old client, new consumer) fails. A reverse
incompatibility is a deploy-order requirement (migrate first, deploy the API
before its clients, consumers before producers): a behavior target, not a
failure.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Any, Final, Literal

from lemoncrow.pro.capabilities.review import migration_execution
from lemoncrow.pro.capabilities.review.analysis_common import (
    COMPAT_ENTRY,
    AnalysisSpec,
    Evaluation,
    Finding,
    Observation,
    SideRun,
    bounded,
    build_driver,
    join_path,
    relative_path,
    sha256_hex,
    valid_spec_id,
)
from lemoncrow.pro.capabilities.review.contracts import _MIGRATION_PATH, inventory_side_effects

PairingState = Literal["compatible", "incompatible", "unknown", "not_available", "not_applicable", "limited_by_budget"]

KINDS: Final[tuple[str, ...]] = ("persistence", "api", "messages")
STATEMENTS_FILE: Final[str] = ".lemoncrow-compat/statements.json"
#: Pairing id -> (label, direction). ``forward`` incompatibilities fail; ``reverse`` ones order the deploy.
PAIRINGS: Final[Mapping[str, tuple[str, str]]] = {
    "old_code_new_schema": ("Old code vs migrated schema", "forward"),
    "new_code_old_schema": ("New code vs old schema", "reverse"),
    "old_client_new_api": ("Old client vs new API", "forward"),
    "new_client_old_api": ("New client vs old API", "reverse"),
    "new_consumer_old_messages": ("New consumer vs old-format messages", "forward"),
    "old_consumer_new_messages": ("Old consumer vs new-format messages", "reverse"),
    # Phase 9R item 42: the other change-set members' heads run against this head's schema.
    "set_heads_new_schema": ("Change-set members' heads vs migrated schema", "forward"),
}
KIND_PAIRINGS: Final[Mapping[str, tuple[str, str]]] = {
    "persistence": ("old_code_new_schema", "new_code_old_schema"),
    "api": ("old_client_new_api", "new_client_old_api"),
    "messages": ("new_consumer_old_messages", "old_consumer_new_messages"),
}
ORDER_HINT: Final[Mapping[str, str]] = {
    "new_code_old_schema": "run the migration before deploying the new code",
    "new_client_old_api": "deploy the API before the clients that call it",
    "old_consumer_new_messages": "deploy the consumers before the producers",
}

# lc-debt: the old-code scan reads at most 400 base code files (512 KiB each) and pairs at
# most 300 statements per side; past either bound the forward pairing is ``unknown``, never
# compatible. Upgrade path: scan the base tree inside the sandbox run instead of on the host.
MAX_CODE_FILES: Final[int] = 400
MAX_CODE_BYTES: Final[int] = 512 * 1024
MAX_STATEMENTS: Final[int] = 300
MAX_DOCUMENTS: Final[int] = 32
# lc-debt: contract documents are walked with a 50,000-node budget per document (YAML aliases
# can make a small file a huge graph); past it the document is not_available. Upgrade path:
# a streaming OpenAPI/JSON-Schema walker with per-reference memoization.
MAX_NODES: Final[int] = 50_000

_CODE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rb", ".java", ".kt", ".php", ".rs", ".cs", ".scala"}
)
_VENDORED = re.compile(r"(?:^|/)(?:node_modules|vendor|third_party|\.git|dist|build)/")
_TEST_PATH = re.compile(
    r"(?:^|/)(?:tests?|spec|__tests__|testdata|fixtures)/|(?:^|/)test_[^/]*$|_test\.[a-z]+$|\.(?:test|spec)\.[a-z]+$",
    re.I,
)
_OPENAPI = re.compile(r"(?:^|/)(?:openapi|swagger)[^/]*\.(?:ya?ml|json)$", re.I)
_GRAPHQL = re.compile(r"\.(?:graphql|gql)$", re.I)
_MESSAGE_DOCS = re.compile(r"\.proto$|\.avsc$|\.schema\.json$", re.I)
_THRIFT = re.compile(r"\.thrift$", re.I)


# --------------------------------------------------------------------------- #
# the persistence driver (stdlib only; runs inside the sandbox)
# --------------------------------------------------------------------------- #

_MAIN: Final[str] = r"""
def connection():
    con = sqlite3.connect(":memory:", isolation_level=None)
    deny = (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH)
    con.set_authorizer(lambda action, *rest: sqlite3.SQLITE_DENY if action in deny else sqlite3.SQLITE_OK)
    con.set_progress_handler(lambda: 1 if time.monotonic() - STARTED > TIMEOUT - 3 else 0, 20000)
    return con


def probe(con, sql):
    # Prepared, never executed: EXPLAIN compiles the statement against the schema.
    try:
        con.execute("EXPLAIN " + sql).fetchall()
        return ""
    except (sqlite3.Error, sqlite3.Warning, ValueError) as exc:
        return (str(exc) or type(exc).__name__)[:300]


def main():
    root = os.path.normpath(os.path.join(CWD, P.get("dir") or "."))
    if not os.path.isdir(root):
        finish(2, "the migration directory %s is missing on this side" % P.get("dir"))
    dialect = str(P.get("dialect") or "postgres")
    base = P.get("base") or {}
    migrations = load(root)
    if len(migrations) > 2000:
        finish(2, "more than 2000 migrations; narrow the persistence spec")
    for row in migrations:
        known = [p for p in row["paths"] if p in base]
        if not known:
            row["state"] = "added"
        elif any(sha(os.path.join(CWD, p)) != base[p] for p in known):
            row["state"] = "edited"
        else:
            row["state"] = "base"
    raw = text(os.path.join(TOP, str(P.get("statements") or "")), 8 << 20)
    try:
        given = json.loads(raw) if raw else None
    except ValueError:
        given = None
    if not isinstance(given, dict):
        finish(2, "the dispatcher's statement file is missing on this side")
    fixture = str(P.get("fixture") or "")
    added = [row for row in migrations if row["state"] == "added"]
    cons = {}
    for name in ("old", "new"):
        con = connection()
        if fixture:
            body = text(os.path.join(CWD, fixture), 8 << 20)
            if body is None:
                finish(2, "the schema fixture %s is missing" % fixture)
            try:
                con.executescript(body)
            except sqlite3.Error as exc:
                finish(2, "the trusted-base schema fixture does not load on SQLite: %s" % exc)
        else:
            for row in migrations:
                if row["state"] == "added" or row["up"] is None:
                    continue
                try:
                    execute(con, row["up"], dialect)
                except sqlite3.Error as exc:
                    finish(2, "the base migration %s does not apply on the SQLite schema fixture (%s); "
                              "the old schema cannot be constructed" % (row["id"], exc))
        cons[name] = con
    applied, failed = [], None
    for row in added:
        if row["up"] is None:
            continue
        try:
            execute(cons["new"], row["up"], dialect)
            applied.append(row["id"])
        except sqlite3.Error as exc:
            failed = {"id": row["id"], "error": str(exc)[:1000]}
            break
    R.update({"dialect": dialect, "fixture": fixture, "applied": applied, "failed": failed,
              "edited": [r["id"] for r in migrations if r["state"] == "edited"][:50],
              "schema_old": schema(cons["old"]), "schema_new": schema(cons["new"])})
    if failed is not None:
        finish(2, "the new migration %s does not apply (%s); the migrated schema cannot be constructed"
                  % (failed["id"], failed["error"]))
    probes = {}
    for side in ("old", "new", "set"):
        rows = []
        for item in (given.get(side) or [])[:400]:
            sql = str(item.get("sql") or "") if isinstance(item, dict) else ""
            row = {"k": str(item.get("k") or "") if isinstance(item, dict) else "",
                   "on_old": probe(cons["old"], sql), "on_new": probe(cons["new"], sql)}
            if side == "set":
                # Another repository's statement: whether it prepares, never the error text.
                row = {"k": row["k"], "on_old": "fails" if row["on_old"] else "",
                       "on_new": "fails" if row["on_new"] else ""}
            rows.append(row)
        probes[side] = rows
    R["probes"] = probes
    finish(0)
"""

DRIVER: Final[str] = build_driver(migration_execution.MIGRATION_LIB + _MAIN)


# --------------------------------------------------------------------------- #
# trusted-base specs
# --------------------------------------------------------------------------- #


def _documents_spec(
    spec_id: str, kind: str, documents: Sequence[str], *, origin: str, source: str, title: str = ""
) -> AnalysisSpec:
    return AnalysisSpec(
        entry_id=COMPAT_ENTRY,
        id=spec_id,
        tool="openapi" if kind == "api" else "schemas",
        root=".",
        title=title or ("API contract" if kind == "api" else "Message formats"),
        origin=origin,  # type: ignore[arg-type]
        source=source,
        options={"kind": kind, "documents": sorted(documents)[:MAX_DOCUMENTS]},
        timeout=60,
    )


def _unsupported(
    spec_id: str, kind: str, tool: str, documents: Sequence[str], *, origin: str, source: str
) -> AnalysisSpec:
    label = {"graphql": "GraphQL", "thrift": "Thrift"}.get(tool, tool)
    return AnalysisSpec(
        entry_id=COMPAT_ENTRY,
        id=spec_id,
        tool=tool,
        root=".",
        title=f"{label} contract",
        origin=origin,  # type: ignore[arg-type]
        source=source,
        options={"kind": kind, "documents": sorted(documents)[:MAX_DOCUMENTS]},
        executable=False,
        reason=f"{label} contract compatibility is not supported in this build, so its pairings cannot be constructed",
        setup_hint=f"Mixed-version pairings cover OpenAPI, proto, Avro and JSON Schema contracts; {label} is not checked.",
    )


def _persistence(migration: AnalysisSpec) -> AnalysisSpec:
    """A migration spec (``migration_execution``) as the persistence pairing spec."""

    directory = join_path(migration.root, str(migration.options.get("dir") or "."))
    out = replace(
        migration,
        entry_id=COMPAT_ENTRY,
        id=f"persistence-{migration.id}"[:64],
        title=f"Persistence · {directory}",
        options={**migration.options, "kind": "persistence"},
    )
    if not migration.executable:
        out = replace(
            out,
            reason=(
                f"{migration.tool.capitalize()} migrations cannot execute in this build's sandbox, "
                "so the migrated schema the old code would meet cannot be constructed"
            ),
        )
    return out


def parse_specs(rows: Sequence[Any], errors: list[str]) -> list[AnalysisSpec]:
    """``analyses.compatibility`` rows: ``{id, kind: persistence|api|messages, ...}``."""

    specs: list[AnalysisSpec] = []
    for index, row in enumerate(rows):
        where = f"analyses.compatibility[{index}]"
        if not isinstance(row, Mapping):
            errors.append(f"{where} must be a mapping")
            continue
        kind = str(row.get("kind") or "")
        if kind not in KINDS:
            errors.append(f"{where}.kind must be one of {', '.join(KINDS)}")
            continue
        if kind == "persistence":
            inner: list[str] = []
            body = {key: value for key, value in row.items() if key != "kind"}
            parsed = migration_execution.parse_specs([body], inner)
            errors.extend(item.replace("analyses.migrations[0]", where) for item in inner)
            specs.extend(replace(_persistence(spec), id=spec.id, source=where) for spec in parsed)
            continue
        unknown = sorted(set(row) - {"id", "kind", "title", "documents"})
        if unknown:
            errors.append(f"{where} has unknown keys: {', '.join(unknown)}")
            continue
        spec_id = row.get("id")
        raw_docs = row.get("documents")
        documents = [relative_path(item, allow_root=False) for item in raw_docs] if isinstance(raw_docs, list) else []
        if not valid_spec_id(spec_id):
            errors.append(f"{where}.id must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}")
        elif not documents or any(item is None for item in documents) or len(documents) > MAX_DOCUMENTS:
            errors.append(f"{where}.documents must list 1-{MAX_DOCUMENTS} repository-relative contract files")
        else:
            docs = [str(item) for item in documents]
            unsupported = [doc for doc in docs if _GRAPHQL.search(doc) or _THRIFT.search(doc)]
            if unsupported:
                tool = "graphql" if _GRAPHQL.search(unsupported[0]) else "thrift"
                specs.append(_unsupported(str(spec_id), kind, tool, docs, origin="configured", source=where))
            else:
                specs.append(
                    _documents_spec(
                        str(spec_id), kind, docs, origin="configured", source=where, title=str(row.get("title") or "")
                    )
                )
    return specs


def discover_specs(paths: Sequence[str], read_base: Callable[[str], str | None]) -> list[AnalysisSpec]:
    """Contract surfaces the trusted base contains: migration directories, API contracts, message schemas."""

    specs = [_persistence(spec) for spec in migration_execution.discover_specs(paths, read_base)]
    openapi: list[str] = []
    graphql: list[str] = []
    messages: list[str] = []
    thrift: list[str] = []
    for path in paths:
        if _VENDORED.search(path) or _TEST_PATH.search(path):
            continue
        if _OPENAPI.search(path):
            openapi.append(path)
        elif _GRAPHQL.search(path):
            graphql.append(path)
        elif _MESSAGE_DOCS.search(path):
            messages.append(path)
        elif _THRIFT.search(path):
            thrift.append(path)
    if openapi:
        specs.append(_documents_spec("api", "api", openapi, origin="discovered", source=sorted(openapi)[0]))
    if graphql:
        specs.append(
            _unsupported("api-graphql", "api", "graphql", graphql, origin="discovered", source=sorted(graphql)[0])
        )
    if messages:
        specs.append(_documents_spec("messages", "messages", messages, origin="discovered", source=sorted(messages)[0]))
    if thrift:
        specs.append(
            _unsupported("messages-thrift", "messages", "thrift", thrift, origin="discovered", source=sorted(thrift)[0])
        )
    return specs


def attach_base_files(spec: AnalysisSpec, paths: Sequence[str]) -> AnalysisSpec:
    if spec.options.get("kind") != "persistence":
        return spec
    return migration_execution.attach_base_files(spec, paths)


# --------------------------------------------------------------------------- #
# host-side preparation (pure; reads Snapshot text, never executes it)
# --------------------------------------------------------------------------- #

_LITERAL = re.compile(
    r'(?P<prefix>\b[fFrRbBuU]{1,2})?(?:"""(?P<a>.*?)"""|\'\'\'(?P<b>.*?)\'\'\'|`(?P<c>[^`]*)`'
    r'|"(?P<d>(?:[^"\\\n]|\\.)*)"|\'(?P<e>(?:[^\'\\\n]|\\.)*)\')',
    re.S,
)
_DML = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b", re.I)
_DML_SHAPE = re.compile(r"\b(?:FROM|INTO|SET)\b", re.I)
_TABLE_REF = re.compile(r"""\b(?:FROM|JOIN|INTO|UPDATE)\s+(?:ONLY\s+)?[`"\[]?([A-Za-z_][\w.]*)""", re.I)
_INSERT_COLUMNS = re.compile(r"""^\s*INSERT\s+INTO\s+[`"\[]?([A-Za-z_][\w.]*)[`"\]]?\s*\(([^)]*)\)""", re.I)
_INTERPOLATION = re.compile(r"\$\{|\{[A-Za-z_][^{}]{0,80}\}|#\{|\{\}|%\(\w+\)[^s]")
_CONCATENATED = re.compile(r"\s*(?:\+|\.\.|\|\||%\s*[(\w])")
_CAST = re.compile(r"::\s*[A-Za-z_]\w*(?:\[\])?")
_PLACEHOLDERS = re.compile(r"%\(\w+\)s|%s|\$\d+|(?<![:\w]):[A-Za-z_]\w*|\?|@[A-Za-z_]\w*")
_MIGRATION_TABLES = re.compile(
    r"""\b(?:TABLE|INTO|ON|UPDATE|FROM|JOIN|REFERENCES|TO)\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?(?:ONLY\s+)?"""
    r"""[`"\[]?([A-Za-z_][\w.]*)""",
    re.I,
)
_SQL_KEYWORDS: Final[frozenset[str]] = frozenset(
    {"select", "table", "column", "index", "constraint", "exists", "not", "only", "set", "default", "null"}
)


def _table(name: str) -> str:
    return name.lower().rsplit(".", 1)[-1]


@dataclass(frozen=True, slots=True)
class Statement:
    """One literal SQL statement of the old or new code."""

    key: str
    sql: str
    path: str
    line: int
    tables: tuple[str, ...]
    verb: str
    columns: tuple[str, ...] = ()
    dynamic: bool = False

    def to_wire(self) -> dict[str, Any]:
        return {
            "k": self.key,
            "path": self.path,
            "line": self.line,
            "tables": list(self.tables),
            "verb": self.verb,
            "sql": bounded(self.sql, 300),
            "dynamic": self.dynamic,
        }


def extract_statements(path: str, text: str) -> list[Statement]:
    """Literal SQL statements in one code file (deterministic; placeholders become ``NULL``).

    A literal built by interpolation (f-strings, template strings, string
    concatenation or formatting) is kept as ``dynamic``: it names its tables
    but cannot be prepared, so it never counts as verified. Keys are assigned
    by the caller.
    """

    out: list[Statement] = []
    for match in _LITERAL.finditer(text):
        body = next((match.group(name) for name in "abcde" if match.group(name) is not None), "")
        verb = _DML.match(body) if body and len(body) <= 4000 else None
        if verb is None or not _DML_SHAPE.search(body):
            continue
        sql = " ".join(body.replace("\\n", " ").replace("\\t", " ").split()).rstrip(";")
        prefix = (match.group("prefix") or "").lower()
        dynamic = (
            "f" in prefix
            or bool(_INTERPOLATION.search(sql))
            or bool(_CONCATENATED.match(text, match.end()))
            # A string closed and reopened around a value: "... " + name + " ..."
            or bool(re.search(r"(?:\+|\|\|)\s*$", text[max(0, match.start() - 20) : match.start()]))
        )
        insert = _INSERT_COLUMNS.match(sql)
        out.append(
            Statement(
                key="",
                sql=_PLACEHOLDERS.sub("NULL", _CAST.sub("", sql)),
                path=path,
                line=text.count("\n", 0, match.start()) + 1,
                tables=tuple(dict.fromkeys(_table(name) for name in _TABLE_REF.findall(sql))),
                verb=verb.group(1).upper(),
                columns=(
                    tuple(part.strip().strip('`"[]').lower() for part in insert.group(2).split(",") if part.strip())
                    if insert
                    else ()
                ),
                dynamic=dynamic,
            )
        )
    return out


def migration_tables(text: str) -> set[str]:
    """Tables a migration's SQL names (a prefilter; the executed schema diff decides)."""

    body = migration_execution._COMMENTS.sub(" ", text)
    return {_table(name) for name in _MIGRATION_TABLES.findall(body) if name.lower() not in _SQL_KEYWORDS}


def _code_file(path: str) -> bool:
    return (
        PurePosixPath(path).suffix.lower() in _CODE_SUFFIXES
        and not _VENDORED.search(path)
        and not _TEST_PATH.search(path)
        and not _MIGRATION_PATH.search(path)
    )


@dataclass(slots=True)
class Pairing:
    id: str
    state: PairingState
    reason: str
    checked: int = 0
    incompatible: int = 0

    @property
    def direction(self) -> str:
        return PAIRINGS[self.id][1]

    def to_wire(self) -> dict[str, Any]:
        label, direction = PAIRINGS[self.id]
        return {
            "pairing": self.id,
            "label": label,
            "direction": direction,
            "state": self.state,
            "reason": bounded(self.reason, 600),
            "checked": self.checked,
            "incompatible": self.incompatible,
            **({"order": ORDER_HINT[self.id]} if direction == "reverse" else {}),
        }


@dataclass(frozen=True, slots=True)
class SetHead:
    """Another change-set member's head, read through the viewer's own authority."""

    alias: str
    paths: Callable[[], Iterable[str] | None]
    changed: Callable[[], Sequence[str]]
    read: Callable[[str], str | None]


@dataclass(frozen=True, slots=True)
class SetHeads:
    """The heads the persistence pairing runs together with this one (Phase 9R item 42)."""

    heads: tuple[SetHead, ...]
    unreadable: int = 0
    truncated: bool = False


@dataclass(slots=True)
class Prepared:
    """What the host found before (or instead of) executing one spec."""

    kind: str
    #: Whether a sandboxed run is needed (persistence with an added or edited migration).
    execute: bool = False
    pairings: list[Pairing] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    overlay: tuple[tuple[str, str], ...] = ()
    statements: dict[str, list[Statement]] = field(default_factory=dict)
    #: Tables the old-code scan looked for (``None``: every statement was kept).
    scanned: frozenset[str] | None = None
    #: Table -> old code files that name it without a literal statement over it.
    mentions: dict[str, list[str]] = field(default_factory=dict)
    #: Changed code file -> tables its R3 inventory names (ORM models, table facts).
    orm_tables: dict[str, set[str]] = field(default_factory=dict)
    changed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    truncated: bool = False
    #: Whether the Change is in a change set (the heads-together pairing applies).
    set_scope: bool = False
    #: Why the heads-together pairing cannot be constructed (``""``: it can).
    set_problem: str = ""
    #: Set statement key -> member alias (never the member's repository or paths).
    set_aliases: dict[str, str] = field(default_factory=dict)
    #: Table -> aliases of members whose head names it without literal SQL over it.
    set_mentions: dict[str, list[str]] = field(default_factory=dict)
    set_truncated: bool = False


def prepare(
    spec: AnalysisSpec,
    *,
    changed_paths: Sequence[str],
    read_side: Callable[[str, str], str | None],
    base_paths: Callable[[], Iterable[str] | None],
    set_heads: Callable[[], SetHeads | None] = lambda: None,
) -> Prepared:
    """Scope one spec to what the Change touches and build every host-side pairing."""

    kind = str(spec.options.get("kind") or "")
    if kind == "persistence":
        prepared = _prepare_persistence(spec, changed_paths, read_side, base_paths)
        if prepared.execute:
            _prepare_set_heads(prepared, set_heads())
        return prepared
    return _prepare_documents(spec, kind, changed_paths, read_side)


def _prepare_set_heads(prepared: Prepared, heads: SetHeads | None) -> None:
    """The other change-set members' head SQL over the tables this Change migrates (host-extracted)."""

    if heads is None:
        return
    prepared.set_scope = True
    if heads.unreadable or heads.truncated:
        prepared.set_problem = (
            "a member of the change set is not readable by whoever started this run, so its head cannot be paired"
        )
        return
    tables = set(prepared.scanned or ())
    words = {table: re.compile(rf"\b{re.escape(table)}\b", re.I) for table in tables}
    found: list[tuple[str, Statement]] = []
    for head in heads.heads:
        listing = head.paths()
        if listing is None:
            prepared.set_problem = f"the head tree of set member {head.alias} is not readable"
            return
        changed = set(head.changed())
        candidates = sorted(path for path in listing if _code_file(path))
        ordered = [path for path in candidates if path in changed] + [
            path for path in candidates if path not in changed
        ]
        if len(ordered) > MAX_CODE_FILES:
            prepared.set_truncated = True
            prepared.notes.append(f"set member {head.alias}: the head scan stops at {MAX_CODE_FILES} code files")
        for path in ordered[:MAX_CODE_FILES]:
            text = head.read(path)
            if text is None:
                continue
            if len(text) > MAX_CODE_BYTES:
                prepared.set_truncated = True
                continue
            rows = extract_statements(path, text)
            literal = {table for row in rows if not row.dynamic for table in row.tables}
            for table, word in words.items():
                if (
                    table not in literal
                    and word.search(text)
                    and head.alias not in prepared.set_mentions.get(table, [])
                ):
                    prepared.set_mentions.setdefault(table, []).append(head.alias)
            found.extend((head.alias, row) for row in rows)
    relevant = [(name, row) for name, row in found if not tables or set(row.tables) & tables]
    if len(relevant) > MAX_STATEMENTS:
        prepared.set_truncated = True
        prepared.notes.append(f"only the first {MAX_STATEMENTS} of {len(relevant)} set-member statements were paired")
    rows_out: list[Statement] = []
    for index, (name, row) in enumerate(relevant[:MAX_STATEMENTS], 1):
        keyed = replace(row, key=f"s{index}")
        prepared.set_aliases[keyed.key] = name
        rows_out.append(keyed)
    prepared.statements["set"] = rows_out
    document = {
        side: [{"k": row.key, "sql": row.sql} for row in rows if not row.dynamic]
        for side, rows in prepared.statements.items()
    }
    prepared.overlay = ((STATEMENTS_FILE, json.dumps(document, sort_keys=True, separators=(",", ":"))),)


def _not_applicable(kind: str, reason: str) -> list[Pairing]:
    return [Pairing(pairing, "not_applicable", reason) for pairing in KIND_PAIRINGS[kind]]


def _prepare_persistence(
    spec: AnalysisSpec,
    changed_paths: Sequence[str],
    read_side: Callable[[str, str], str | None],
    base_paths: Callable[[], Iterable[str] | None],
) -> Prepared:
    directory = join_path(spec.root, str(spec.options.get("dir") or "."))
    prefix = "" if directory in {"", "."} else directory.rstrip("/") + "/"
    suffixes = (".sql",) if spec.tool == "sql" else (".py", ".rb", ".sql")
    touched = sorted(path for path in changed_paths if path.startswith(prefix) and path.lower().endswith(suffixes))
    prepared = Prepared(kind="persistence", changed=touched)
    if not touched:
        prepared.pairings = _not_applicable("persistence", f"the Change adds or edits no migration in {directory}")
        return prepared
    if not spec.executable:
        prepared.pairings = [
            Pairing(pairing, "not_available", spec.reason or "not executable in this build")
            for pairing in KIND_PAIRINGS["persistence"]
        ]
        return prepared
    tables: set[str] = set()
    for path in touched:
        text = read_side(path, "new")
        if text is not None:
            tables |= migration_tables(text)
    prepared.scanned = frozenset(tables) if tables else None
    changed = set(changed_paths)
    listing = base_paths()
    candidates = sorted(path for path in (listing or ()) if _code_file(path))
    ordered = [path for path in candidates if path in changed] + [path for path in candidates if path not in changed]
    if listing is None:
        prepared.truncated = True
        prepared.notes.append("the trusted base tree is not readable, so the old code could not be scanned")
    if len(ordered) > MAX_CODE_FILES:
        prepared.truncated = True
        prepared.notes.append(
            f"the old-code scan stops at {MAX_CODE_FILES} of {len(ordered)} code files; the rest were not paired"
        )
    words = {table: re.compile(rf"\b{re.escape(table)}\b", re.I) for table in tables}
    old: list[Statement] = []
    for path in ordered[:MAX_CODE_FILES]:
        text = read_side(path, "old")
        if text is None:
            continue
        if len(text) > MAX_CODE_BYTES:
            prepared.truncated = True
            prepared.notes.append(f"{path} is over {MAX_CODE_BYTES // 1024} KiB and was not scanned")
            continue
        found = extract_statements(path, text)
        literal = {table for row in found if not row.dynamic for table in row.tables}
        for table, word in words.items():
            if table not in literal and word.search(text):
                prepared.mentions.setdefault(table, []).append(path)
        old.extend(found)
    new: list[Statement] = []
    for path in sorted(changed):
        if not _code_file(path):
            continue
        text = read_side(path, "new")
        if text is None or len(text) > MAX_CODE_BYTES:
            continue
        new.extend(extract_statements(path, text))
        named = {
            _table(fact.name)
            for fact in inventory_side_effects(path, text)
            if fact.category == "db_table" or (fact.category == "db_drop" and ":" in fact.name)
        }
        if named:
            prepared.orm_tables[path] = named
    for side, rows in (("old", old), ("new", new)):
        relevant = [row for row in rows if not tables or set(row.tables) & tables]
        if len(relevant) > MAX_STATEMENTS:
            prepared.truncated = True
            prepared.notes.append(
                f"only the first {MAX_STATEMENTS} of {len(relevant)} {side}-code statements were paired"
            )
        prepared.statements[side] = [
            replace(row, key=f"{side[0]}{index}") for index, row in enumerate(relevant[:MAX_STATEMENTS], 1)
        ]
    document = {
        side: [{"k": row.key, "sql": row.sql} for row in rows if not row.dynamic]
        for side, rows in prepared.statements.items()
    }
    prepared.overlay = ((STATEMENTS_FILE, json.dumps(document, sort_keys=True, separators=(",", ":"))),)
    prepared.execute = True
    return prepared


def payload(spec: AnalysisSpec, *, read_base: Callable[[str], str | None]) -> tuple[dict[str, Any], str]:
    document, problem = migration_execution.payload(spec, read_base=read_base)
    return {**document, "statements": STATEMENTS_FILE}, problem


# --------------------------------------------------------------------------- #
# persistence evaluation
# --------------------------------------------------------------------------- #


def _finding(
    spec: AnalysisSpec,
    pairing: str,
    rule: str,
    target: str,
    title: str,
    summary: str,
    *,
    paths: Sequence[str] = (),
    facts: Sequence[tuple[str, str]] = (),
    base: Observation | None = None,
    head: Observation | None = None,
) -> Finding:
    label, direction = PAIRINGS[pairing]
    forward = direction == "forward"
    return Finding(
        rule=rule,
        category="incompatible" if forward else "deploy_order",
        subject=f"{spec.id}::{pairing}::{rule}::{target}"[:300],
        title=f"{title} · {target}"[:200],
        summary=bounded(summary if forward else f"{summary} Deploy order: {ORDER_HINT[pairing]}."),
        severity="high" if forward else "medium",
        critical=forward,
        paths=tuple(dict.fromkeys(path for path in paths if path))[:10],
        facts=(("Pairing", label), ("Direction", direction), *facts)[:11],
        base=base,
        head=head
        or Observation("incompatible" if forward else "order", bounded(summary, 200), sha256_hex([rule, target])),
    )


def _evaluate_persistence(spec: AnalysisSpec, prepared: Prepared, head: SideRun | None) -> Evaluation:
    if not prepared.execute:
        return _verdict(spec, prepared, list(prepared.pairings), [])
    if head is None or head.state in {"not_run", "unavailable", "error", "failed"}:
        reason = (head.reason if head is not None else "") or "the persistence pairing did not run"
        state: PairingState = "not_available"
        if reason.startswith("Limited by budget"):
            state = "limited_by_budget"
        elif head is not None and head.state in {"error", "failed"}:
            state = "unknown"
        ids = [*KIND_PAIRINGS["persistence"], *(["set_heads_new_schema"] if prepared.set_scope else [])]
        rows = [Pairing(pairing, state, reason) for pairing in ids]
        return _verdict(spec, prepared, rows, [])
    report = head.report
    old_schema = report.get("schema_old") if isinstance(report.get("schema_old"), Mapping) else {}
    new_schema = report.get("schema_new") if isinstance(report.get("schema_new"), Mapping) else {}
    assert isinstance(old_schema, Mapping) and isinstance(new_schema, Mapping)
    old_tables, new_tables = migration_execution._tables(old_schema), migration_execution._tables(new_schema)
    diff = migration_execution.schema_diff(old_schema, new_schema)
    altered: set[str] = {name.lower() for name in diff["tables_removed"]}
    for key in ("columns_removed", "columns_changed"):
        altered |= {item.split(".", 1)[0].lower() for item in diff[key]}
    required_added: dict[str, set[str]] = {}
    for table in set(old_tables) & set(new_tables):
        for name in set(new_tables[table]) - set(old_tables[table]):
            column = new_tables[table][name]
            if column.get("notnull") and column.get("default") is None and not column.get("pk"):
                required_added.setdefault(table.lower(), set()).add(name.lower())
                altered.add(table.lower())
    # What the new code may newly rely on: added tables, and tables that gained columns.
    added_tables = {name.lower() for name in diff["tables_added"]}
    added_tables |= {item.split(".", 1)[0].lower() for item in diff["columns_added"]}
    probes = report.get("probes") if isinstance(report.get("probes"), Mapping) else {}
    assert isinstance(probes, Mapping)
    results: dict[str, Mapping[str, Any]] = {
        str(row.get("k")): row
        for side in ("old", "new", "set")
        for row in probes.get(side) or ()
        if isinstance(row, Mapping)
    }
    findings: list[Finding] = []
    migrated = ", ".join(map(str, report.get("applied") or ())) or "no new migration"

    # Forward: the old code against the migrated schema.
    forward = Pairing("old_code_new_schema", "compatible", "")
    relevant = [row for row in prepared.statements.get("old", ()) if set(row.tables) & altered]
    unverifiable = 0
    for row in relevant:
        probe = results.get(row.key)
        if row.dynamic or probe is None:
            unverifiable += 1
            continue
        forward.checked += 1
        on_old, on_new = str(probe.get("on_old") or ""), str(probe.get("on_new") or "")
        target = f"{row.path}:{row.line}"
        if on_old:
            unverifiable += 1  # it does not even prepare on the schema it was written for
        elif on_new:
            forward.incompatible += 1
            findings.append(
                _finding(
                    spec,
                    "old_code_new_schema",
                    "old_statement_fails",
                    target,
                    "Old code breaks on the migrated schema",
                    f"The old code's statement prepares on the old schema but fails on the migrated one: {on_new}.",
                    paths=(row.path, *prepared.changed[:3]),
                    facts=(
                        ("Statement", bounded(row.sql, 200)),
                        ("Error on migrated schema", bounded(on_new, 200)),
                        ("Migrated with", bounded(migrated, 200)),
                    ),
                    base=Observation("prepares", "prepares on the old schema", sha256_hex([row.sql, "old"])),
                    head=Observation("fails", bounded(on_new, 200), sha256_hex([row.sql, on_new])),
                )
            )
        elif row.verb == "INSERT" and row.columns and row.tables:
            missing = sorted(required_added.get(row.tables[0], set()) - set(row.columns))
            if missing:
                forward.incompatible += 1
                findings.append(
                    _finding(
                        spec,
                        "old_code_new_schema",
                        "old_insert_misses_required_column",
                        target,
                        "Old code's INSERT omits a new required column",
                        f"The migrated {row.tables[0]} requires {', '.join(missing)} (NOT NULL, no default); "
                        "the old code's INSERT does not set it and fails at runtime.",
                        paths=(row.path, *prepared.changed[:3]),
                        facts=(("Statement", bounded(row.sql, 200)), ("Missing", ", ".join(missing))),
                        base=Observation("inserts", "inserts on the old schema", sha256_hex([row.sql, "old"])),
                    )
                )
    mentioned = sorted(table for table in altered if prepared.mentions.get(table))
    unscanned = sorted(altered - prepared.scanned) if prepared.scanned is not None else []
    if forward.incompatible:
        forward.state = "incompatible"
        forward.reason = f"{forward.incompatible} old-code statement(s) break on the migrated schema"
    elif not altered:
        forward.reason = "the migrations change no existing table, so the old code meets the schema it expects"
    elif unverifiable:
        forward.state = "unknown"
        forward.reason = (
            f"{unverifiable} old-code statement(s) over {', '.join(sorted(altered))} could not be prepared "
            "(dialect-specific, or built at runtime)"
        )
    elif mentioned:
        forward.state = "not_available"
        files = sorted({path for table in mentioned for path in prepared.mentions[table]})
        forward.reason = (
            f"the old code references {', '.join(mentioned)} ({', '.join(files[:3])}) without literal SQL "
            "that can be paired (ORM or runtime-built queries)"
        )
    elif unscanned or prepared.truncated:
        forward.state = "unknown"
        forward.reason = "; ".join(
            [*([f"the old code was not scanned for {', '.join(unscanned)}"] if unscanned else []), *prepared.notes]
        )
    elif not relevant:
        forward.reason = f"no old code references {', '.join(sorted(altered))}"
    else:
        forward.reason = f"{forward.checked} old-code statement(s) prepare on the migrated schema"

    # Reverse: the new code against the old schema.
    reverse = Pairing("new_code_old_schema", "compatible", "")
    scope = altered | added_tables
    news = [row for row in prepared.statements.get("new", ()) if set(row.tables) & scope]
    new_unverifiable = 0
    for row in news:
        probe = results.get(row.key)
        if row.dynamic or probe is None:
            new_unverifiable += 1
            continue
        reverse.checked += 1
        on_old, on_new = str(probe.get("on_old") or ""), str(probe.get("on_new") or "")
        if on_new:
            new_unverifiable += 1
        elif on_old:
            reverse.incompatible += 1
            findings.append(
                _finding(
                    spec,
                    "new_code_old_schema",
                    "new_statement_needs_migration",
                    f"{row.path}:{row.line}",
                    "New code needs the migration first",
                    f"The new code's statement fails on the old schema: {on_old}.",
                    paths=(row.path,),
                    facts=(("Statement", bounded(row.sql, 200)), ("Error on old schema", bounded(on_old, 200))),
                    base=Observation("fails", bounded(on_old, 200), sha256_hex([row.sql, on_old])),
                )
            )
    orm = sorted(path for path, named in prepared.orm_tables.items() if named & scope)
    if reverse.incompatible:
        reverse.state = "incompatible"
        reverse.reason = f"{reverse.incompatible} new-code statement(s) need the migrated schema"
    elif new_unverifiable:
        reverse.state = "unknown"
        reverse.reason = f"{new_unverifiable} new-code statement(s) could not be prepared"
    elif not news and orm:
        reverse.state = "not_available"
        reverse.reason = (
            f"the changed code ({', '.join(orm[:3])}) maps the changed tables without literal SQL that can be paired"
        )
    elif not news:
        reverse.state = "not_applicable"
        reverse.reason = "the Change's code issues no SQL over the changed tables"
    else:
        reverse.reason = f"{reverse.checked} new-code statement(s) prepare on the old schema"
    pairings = [forward, reverse]
    if prepared.set_scope:
        pairings.append(_set_pairing(spec, prepared, results, altered, added_tables, findings))
    edited = [str(item) for item in report.get("edited") or ()]
    if edited:
        prepared.notes.append(
            "the Change edits already-applied migration(s) "
            + ", ".join(edited[:5])
            + "; the old schema was rebuilt from their edited text"
        )
    evaluation = _verdict(spec, prepared, pairings, findings)
    if edited and evaluation.status == "PASS":
        evaluation.status = "UNKNOWN"
    evaluation.summary.update(
        {
            "schema_diff": {key: values[:50] for key, values in diff.items()},
            "altered_tables": sorted(altered)[:50],
            # Set members' statements stay out of this record: it never carries another repository's code.
            "statements": {
                side: [row.to_wire() for row in rows[:50]]
                for side, rows in prepared.statements.items()
                if side != "set"
            },
            "set_statements": len(prepared.statements.get("set", ())),
            "engine": f"SQLite (rules: {report.get('dialect') or 'postgres'})",
            "fixture": str(report.get("fixture") or ""),
        }
    )
    return evaluation


def _set_pairing(
    spec: AnalysisSpec,
    prepared: Prepared,
    results: Mapping[str, Mapping[str, Any]],
    altered: set[str],
    added_tables: set[str],
    findings: list[Finding],
) -> Pairing:
    """The other change-set members' head SQL, prepared on this head's migrated schema.

    A finding names the member by its alias and the statement by a digest:
    the record never carries another repository's paths, SQL or error text.
    """

    pairing = Pairing("set_heads_new_schema", "compatible", "")
    if prepared.set_problem:
        pairing.state, pairing.reason = "not_available", prepared.set_problem
        return pairing
    scope = altered | added_tables
    rows = [row for row in prepared.statements.get("set", ()) if set(row.tables) & scope]
    unverifiable = 0
    for row in rows:
        probe = results.get(row.key)
        if row.dynamic or probe is None:
            unverifiable += 1
            continue
        pairing.checked += 1
        on_old, on_new = str(probe.get("on_old") or ""), str(probe.get("on_new") or "")
        if not on_new:
            continue
        if on_old and not set(row.tables) & added_tables:
            unverifiable += 1  # it prepares on neither schema: dialect-specific, not this Change
            continue
        member = prepared.set_aliases.get(row.key, "set member")
        digest = sha256_hex([member, row.sql])[:16]
        pairing.incompatible += 1
        findings.append(
            _finding(
                spec,
                "set_heads_new_schema",
                "set_member_statement_fails",
                f"{member}:{digest}",
                "Another change-set member breaks on this schema",
                f"A literal statement in change-set member {member}'s head does not prepare on this head's "
                "migrated schema; deploying the set together fails.",
                paths=tuple(prepared.changed[:3]),
                facts=(("Member", member), ("Statement digest", digest)),
                head=Observation("fails", "does not prepare on the migrated schema", sha256_hex([row.sql, "set"])),
            )
        )
    mentioned = sorted({name for table in scope for name in prepared.set_mentions.get(table, ())})
    if pairing.incompatible:
        pairing.state = "incompatible"
        pairing.reason = f"{pairing.incompatible} statement(s) of other set members break on the migrated schema"
    elif unverifiable:
        pairing.state = "unknown"
        pairing.reason = f"{unverifiable} statement(s) of other set members could not be prepared"
    elif mentioned:
        pairing.state = "not_available"
        pairing.reason = (
            f"set member(s) {', '.join(mentioned)} use the changed tables without literal SQL that can be paired"
        )
    elif prepared.set_truncated:
        pairing.state = "unknown"
        pairing.reason = "the set members' heads were not fully scanned"
    elif not scope:
        pairing.reason = "the migrations change no table another set member could use"
    elif not rows:
        pairing.reason = f"no other set member's head issues SQL over {', '.join(sorted(scope))}"
    else:
        pairing.reason = f"{pairing.checked} statement(s) of other set members prepare on the migrated schema"
    return pairing


# --------------------------------------------------------------------------- #
# API and message-format evaluation (host side, from contract documents)
# --------------------------------------------------------------------------- #


class _Budget:
    def __init__(self) -> None:
        self.left = MAX_NODES

    def spend(self) -> None:
        self.left -= 1
        if self.left < 0:
            raise ValueError(f"the document is larger than the {MAX_NODES}-node walk bound")


def _load_document(path: str, text: str) -> Any:
    if len(text) > 2 << 20:
        raise ValueError("the document is over 2 MiB")
    if path.lower().endswith((".json", ".avsc")):
        return json.loads(text)
    import yaml

    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML: {bounded(exc, 160)}") from exc


def _ref(doc: Any, node: Any, budget: _Budget) -> Any:
    depth = 0
    while isinstance(node, Mapping) and isinstance(node.get("$ref"), str) and depth < 16:
        budget.spend()
        ref = str(node["$ref"])
        if not ref.startswith("#/"):
            raise ValueError(f"external reference {ref[:120]} cannot be resolved")
        target: Any = doc
        for part in ref[2:].split("/"):
            target = target.get(part.replace("~1", "/").replace("~0", "~")) if isinstance(target, Mapping) else None
        node, depth = target, depth + 1
    return node


def _properties(doc: Any, schema: Any, budget: _Budget, depth: int = 0) -> tuple[dict[str, str], set[str], bool]:
    """(property -> type, required names, closed) of an object schema (``allOf`` merged)."""

    schema = _ref(doc, schema, budget)
    props: dict[str, str] = {}
    required: set[str] = set()
    closed = False
    if not isinstance(schema, Mapping) or depth > 8:
        return props, required, closed
    for part in schema.get("allOf") or ():
        sub_props, sub_required, sub_closed = _properties(doc, part, budget, depth + 1)
        props.update(sub_props)
        required |= sub_required
        closed = closed or sub_closed
    raw = schema.get("properties")
    if isinstance(raw, Mapping):
        for name, value in raw.items():
            budget.spend()
            props[str(name)] = _type(doc, value, budget)
    required |= {str(item) for item in schema.get("required") or () if isinstance(item, str)}
    closed = closed or schema.get("additionalProperties") is False
    return props, required, closed


def _type(doc: Any, value: Any, budget: _Budget, depth: int = 0) -> str:
    value = _ref(doc, value, budget)
    if not isinstance(value, Mapping) or depth > 8:
        return "any"
    kind = value.get("type")
    if isinstance(kind, list):
        kind = "|".join(sorted(str(item) for item in kind))
    if kind == "array":
        return f"array<{_type(doc, value.get('items'), budget, depth + 1)}>"
    return str(kind or ("object" if "properties" in value else "any"))


_METHODS: Final[tuple[str, ...]] = ("get", "put", "post", "delete", "patch", "options", "head")


def _operations(doc: Any, budget: _Budget) -> dict[str, Mapping[str, Any]]:
    paths = doc.get("paths") if isinstance(doc, Mapping) else None
    if not isinstance(paths, Mapping):
        raise ValueError("the contract has no paths object")
    out: dict[str, Mapping[str, Any]] = {}
    for route, raw in paths.items():
        budget.spend()
        item = _ref(doc, raw, budget)
        if not isinstance(item, Mapping):
            continue
        raw_shared = item.get("parameters")
        shared: list[Any] = list(raw_shared) if isinstance(raw_shared, list) else []
        for method in _METHODS:
            op = item.get(method)
            if isinstance(op, Mapping):
                raw_own = op.get("parameters")
                own: list[Any] = list(raw_own) if isinstance(raw_own, list) else []
                out[f"{method.upper()} {route}"] = {**op, "parameters": [*shared, *own]}
    return out


def _parameters(doc: Any, op: Mapping[str, Any], budget: _Budget) -> dict[tuple[str, str], tuple[bool, str]]:
    out: dict[tuple[str, str], tuple[bool, str]] = {}
    for raw in op.get("parameters") or ():
        param = _ref(doc, raw, budget)
        if not isinstance(param, Mapping) or not param.get("name"):
            continue
        where = str(param.get("in") or "query")
        if where == "body":  # Swagger 2 request body
            continue
        kind = _type(doc, param.get("schema"), budget) if "schema" in param else str(param.get("type") or "any")
        out[(where, str(param["name"]))] = (param.get("required") is True or where == "path", kind)
    return out


def _json_schema(content: Any) -> Any:
    if not isinstance(content, Mapping):
        return None
    for media, value in content.items():
        if "json" in str(media) and isinstance(value, Mapping):
            return value.get("schema")
    return None


def _request(doc: Any, op: Mapping[str, Any], budget: _Budget) -> tuple[bool, dict[str, str], set[str]]:
    body = _ref(doc, op.get("requestBody"), budget)
    schema: Any = None
    required = False
    if isinstance(body, Mapping):
        schema, required = _json_schema(body.get("content")), body.get("required") is True
    else:
        for raw in op.get("parameters") or ():
            param = _ref(doc, raw, budget)
            if isinstance(param, Mapping) and param.get("in") == "body":
                schema, required = param.get("schema"), param.get("required") is True
    props, names, _closed = _properties(doc, schema, budget)
    return required, props, names


def _responses(doc: Any, op: Mapping[str, Any], budget: _Budget) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    raw = op.get("responses")
    for code, value in raw.items() if isinstance(raw, Mapping) else ():
        if not str(code).startswith("2"):
            continue
        response = _ref(doc, value, budget)
        if not isinstance(response, Mapping):
            continue
        schema = _json_schema(response.get("content")) if "content" in response else response.get("schema")
        out[str(code)] = _properties(doc, schema, budget)[0]
    return out


def _api_rules(spec: AnalysisSpec, path: str, old: Any, new: Any) -> tuple[list[Finding], list[Finding], int]:
    """(forward findings, reverse findings, operations compared) of one OpenAPI document."""

    forward: list[Finding] = []
    reverse: list[Finding] = []
    budget = _Budget()
    old_ops = _operations(old, budget)
    new_ops = _operations(new, budget) if new is not None else {}

    def flag(rule: str, target: str, title: str, summary: str) -> None:
        forward.append(
            _finding(
                spec,
                "old_client_new_api",
                rule,
                target,
                title,
                summary,
                paths=(path,),
                facts=(("Contract", path),),
                base=Observation("served", f"{target} as the old client calls it", sha256_hex([path, target, "old"])),
            )
        )

    for name in sorted(set(old_ops) - set(new_ops)):
        flag("removed_operation", name, "Removes an API operation", f"{name} is gone; old clients calling it fail.")
    for name in sorted(set(old_ops) & set(new_ops)):
        before, after = old_ops[name], new_ops[name]
        old_params, new_params = _parameters(old, before, budget), _parameters(new, after, budget)
        for key, (required, kind) in sorted(new_params.items()):
            was = old_params.get(key)
            if required and (was is None or not was[0]):
                flag(
                    "new_required_parameter",
                    f"{name} {key[0]}:{key[1]}",
                    "Adds a required parameter",
                    f"{name} now requires the {key[0]} parameter {key[1]}; old clients do not send it.",
                )
            elif was is not None and was[1] != kind and "any" not in {was[1], kind}:
                flag(
                    "parameter_type_changed",
                    f"{name} {key[0]}:{key[1]}",
                    "Changes a parameter type",
                    f"The {key[1]} parameter of {name} changes from {was[1]} to {kind}.",
                )
        old_body_required, old_body, old_req = _request(old, before, budget)
        new_body_required, new_body, new_req = _request(new, after, budget)
        if new_body_required and not old_body_required:
            flag("request_body_required", name, "Requires a request body", f"{name} now requires a request body.")
        for prop in sorted(new_req - old_req):
            flag(
                "new_required_field",
                f"{name} body.{prop}",
                "Adds a required request field",
                f"{name} now requires body field {prop}; old clients do not send it.",
            )
        for prop in sorted(set(old_body) & set(new_body)):
            if old_body[prop] != new_body[prop] and "any" not in {old_body[prop], new_body[prop]}:
                flag(
                    "request_field_type_changed",
                    f"{name} body.{prop}",
                    "Changes a request field type",
                    f"Body field {prop} of {name} changes from {old_body[prop]} to {new_body[prop]}.",
                )
        old_out, new_out = _responses(old, before, budget), _responses(new, after, budget)
        if old_out and not new_out:
            for code in sorted(old_out):
                flag(
                    "removed_response",
                    f"{name} {code}",
                    "Removes a success response",
                    f"{name} no longer answers {code}.",
                )
        for code in sorted(set(old_out) & set(new_out)):
            for prop in sorted(set(old_out[code]) - set(new_out[code])):
                flag(
                    "removed_response_field",
                    f"{name} {code}.{prop}",
                    "Removes a response field",
                    f"The {code} response of {name} no longer has {prop}; old clients reading it break.",
                )
            for prop in sorted(set(old_out[code]) & set(new_out[code])):
                before_type, after_type = old_out[code][prop], new_out[code][prop]
                if before_type != after_type and "any" not in {before_type, after_type}:
                    flag(
                        "response_field_type_changed",
                        f"{name} {code}.{prop}",
                        "Changes a response field type",
                        f"Response field {prop} of {name} changes from {before_type} to {after_type}.",
                    )
    for name in sorted(set(new_ops) - set(old_ops)):
        reverse.append(
            _finding(
                spec,
                "new_client_old_api",
                "added_operation",
                name,
                "Adds an API operation",
                f"{name} is new; a new client calling it against the old API fails.",
                paths=(path,),
                facts=(("Contract", path),),
            )
        )
    return forward, reverse, len(set(old_ops) | set(new_ops))


# --- messages ---------------------------------------------------------------- #

_PROTO_BLOCK = re.compile(r"\b(message|enum|oneof|service|extend)\s+([A-Za-z_][\w.]*)\s*\{")
_PROTO_FIELD = re.compile(
    r"^\s*(optional|required|repeated)?\s*(map\s*<[^>]*>|[A-Za-z_][\w.]*)\s+([A-Za-z_]\w*)\s*=\s*(\d+)", re.M
)
_PROTO_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


def proto_messages(text: str) -> dict[str, dict[int, tuple[str, str, str]]]:
    """message -> field number -> (label, type, name); nested messages are qualified."""

    body = _PROTO_COMMENT.sub(" ", text)
    out: dict[str, dict[int, tuple[str, str, str]]] = {}

    def walk(start: int, end: int, scope: str, message: str | None) -> None:
        index = start
        own: list[str] = []
        while index < end:
            match = _PROTO_BLOCK.search(body, index, end)
            own.append(body[index : match.start() if match else end])
            if match is None:
                break
            depth, cursor = 1, match.end()
            while cursor < end and depth:
                depth += {"{": 1, "}": -1}.get(body[cursor], 0)
                cursor += 1
            kind, name = match.group(1), match.group(2)
            if kind == "message":
                qualified = f"{scope}.{name}" if scope else name
                out.setdefault(qualified, {})
                walk(match.end(), cursor - 1, qualified, qualified)
            elif kind == "oneof" and message is not None:
                for found in _PROTO_FIELD.finditer(body[match.end() : cursor - 1]):
                    out[message][int(found.group(4))] = ("optional", " ".join(found.group(2).split()), found.group(3))
            index = cursor
        if message is not None:
            for found in _PROTO_FIELD.finditer("\n".join(own)):
                if found.group(2) in {"reserved", "option", "extensions"}:
                    continue
                out[message][int(found.group(4))] = (
                    found.group(1) or "",
                    " ".join(found.group(2).split()),
                    found.group(3),
                )

    walk(0, len(body), "", None)
    return out


_AVRO_PROMOTIONS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("int", "long"),
        ("int", "float"),
        ("int", "double"),
        ("long", "float"),
        ("long", "double"),
        ("float", "double"),
        ("string", "bytes"),
        ("bytes", "string"),
    }
)


def _avro_type(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "|".join(_avro_type(item) for item in value)
    if isinstance(value, Mapping):
        kind = str(value.get("type") or "")
        return str(value.get("name") or kind) if kind in {"record", "enum", "fixed"} else kind
    return "any"


def avro_records(doc: Any, budget: _Budget) -> dict[str, dict[str, tuple[str, bool]]]:
    """record -> field -> (type, has default); nested records included."""

    out: dict[str, dict[str, tuple[str, bool]]] = {}

    def walk(node: Any, depth: int = 0) -> None:
        budget.spend()
        if depth > 16:
            return
        if isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
        elif isinstance(node, Mapping) and node.get("type") == "record":
            name = ".".join(part for part in (str(node.get("namespace") or ""), str(node.get("name") or "")) if part)
            fields: dict[str, tuple[str, bool]] = {}
            for raw in node.get("fields") or ():
                if isinstance(raw, Mapping) and raw.get("name"):
                    fields[str(raw["name"])] = (_avro_type(raw.get("type")), "default" in raw)
                    walk(raw.get("type"), depth + 1)
            out[name] = fields
        elif isinstance(node, Mapping) and isinstance(node.get("type"), (Mapping, list)):
            walk(node.get("type"), depth + 1)

    walk(doc)
    return out


def _message_rules(
    spec: AnalysisSpec, path: str, old_text: str, new_text: str | None
) -> tuple[list[Finding], list[Finding], int]:
    forward: list[Finding] = []
    reverse: list[Finding] = []

    def flag(rule: str, target: str, title: str, summary: str, *, backward: bool = True) -> None:
        pairing = "new_consumer_old_messages" if backward else "old_consumer_new_messages"
        (forward if backward else reverse).append(
            _finding(
                spec,
                pairing,
                rule,
                target,
                title,
                summary,
                paths=(path,),
                facts=(("Format", path),),
                base=Observation("old format", f"{target} as old producers write it", sha256_hex([path, target])),
            )
        )

    lower = path.lower()
    compared = 0
    if lower.endswith(".proto"):
        old, new = proto_messages(old_text), proto_messages(new_text or "")
        for message in sorted(old):
            compared += 1
            if message not in new:
                flag(
                    "removed_message",
                    message,
                    "Removes a message type",
                    f"Message {message} is gone; old-format {message} messages can no longer be read.",
                )
                continue
            for number in sorted(set(old[message]) & set(new[message])):
                (_label, old_type, old_name), (_new_label, new_type, new_name) = (
                    old[message][number],
                    new[message][number],
                )
                if old_type != new_type:
                    flag(
                        "field_type_changed",
                        f"{message}.{new_name}={number}",
                        "Changes a field's wire type",
                        f"Field {number} of {message} changes from {old_type} {old_name} to {new_type} {new_name}; "
                        "old-format messages decode wrongly.",
                    )
            for number in sorted(set(new[message]) - set(old[message])):
                label, _kind, name = new[message][number]
                if label == "required":
                    flag(
                        "new_required_field",
                        f"{message}.{name}={number}",
                        "Adds a required field",
                        f"{message} now requires {name}; old-format messages lack it.",
                    )
            for number in sorted(set(old[message]) - set(new[message])):
                label, _kind, name = old[message][number]
                if label == "required":
                    flag(
                        "removed_required_field",
                        f"{message}.{name}={number}",
                        "Stops sending a field old consumers require",
                        f"Old consumers require {message}.{name}; new-format messages omit it.",
                        backward=False,
                    )
    elif lower.endswith(".avsc"):
        budget = _Budget()
        old_records = avro_records(json.loads(old_text), budget)
        new_records = avro_records(json.loads(new_text), budget) if new_text is not None else {}
        for record in sorted(old_records):
            compared += 1
            if record not in new_records:
                flag(
                    "removed_message",
                    record,
                    "Removes a record type",
                    f"Record {record} is gone; old-format data can no longer be read.",
                )
                continue
            before, after = old_records[record], new_records[record]
            for name in sorted(set(after) - set(before)):
                if not after[name][1]:
                    flag(
                        "new_field_without_default",
                        f"{record}.{name}",
                        "Adds a field without a default",
                        f"The new reader needs {record}.{name}, which old-format data lacks and has no default for.",
                    )
            for name in sorted(set(before) & set(after)):
                if before[name][0] != after[name][0] and (before[name][0], after[name][0]) not in _AVRO_PROMOTIONS:
                    flag(
                        "field_type_changed",
                        f"{record}.{name}",
                        "Changes a field type",
                        f"{record}.{name} changes from {before[name][0]} to {after[name][0]}; "
                        "old-format data does not resolve.",
                    )
            for name in sorted(set(before) - set(after)):
                if not before[name][1]:
                    flag(
                        "removed_field_without_default",
                        f"{record}.{name}",
                        "Stops writing a field old readers need",
                        f"Old readers need {record}.{name} (no default); new-format data omits it.",
                        backward=False,
                    )
    else:  # JSON Schema
        budget = _Budget()
        old_doc = json.loads(old_text)
        new_doc = json.loads(new_text) if new_text is not None else {}
        old_props, old_required, _old_closed = _properties(old_doc, old_doc, budget)
        new_props, new_required, new_closed = _properties(new_doc, new_doc, budget)
        compared = 1
        for name in sorted(new_required - old_required):
            flag(
                "new_required_field",
                name,
                "Adds a required field",
                f"New consumers require {name}; old-format messages may lack it.",
            )
        for name in sorted(set(old_props) & set(new_props)):
            if old_props[name] != new_props[name] and "any" not in {old_props[name], new_props[name]}:
                flag(
                    "field_type_changed",
                    name,
                    "Changes a field type",
                    f"{name} changes from {old_props[name]} to {new_props[name]}; old-format messages fail validation.",
                )
        if new_closed:
            for name in sorted(set(old_props) - set(new_props)):
                flag(
                    "removed_field_closed_schema",
                    name,
                    "Rejects a field old messages carry",
                    f"The new schema forbids additional properties and drops {name}; old-format messages carrying it fail.",
                )
        for name in sorted(old_required - new_required - set(new_props)):
            flag(
                "removed_required_field",
                name,
                "Stops sending a field old consumers require",
                f"Old consumers require {name}; the new format drops it.",
                backward=False,
            )
    return forward, reverse, compared


def _prepare_documents(
    spec: AnalysisSpec, kind: str, changed_paths: Sequence[str], read_side: Callable[[str, str], str | None]
) -> Prepared:
    prepared = Prepared(kind=kind)
    documents = [str(item) for item in spec.options.get("documents") or ()]
    changed = set(changed_paths)
    touched = [doc for doc in documents if doc in changed]
    routes_removed: list[tuple[str, str]] = []
    routes_added: list[tuple[str, str]] = []
    if kind == "api" and spec.executable:
        # Endpoint-level facts of the R3 inventory: routes the changed code stops or starts serving.
        before: dict[str, str] = {}
        after: dict[str, str] = {}
        for path in sorted(changed):
            if PurePosixPath(path).suffix.lower() not in _CODE_SUFFIXES or _TEST_PATH.search(path):
                continue
            for side, sink in (("old", before), ("new", after)):
                text = read_side(path, side)
                for fact in inventory_side_effects(path, text) if text is not None else ():
                    if fact.category == "route":
                        sink.setdefault(fact.name, path)
        routes_removed = sorted((name, before[name]) for name in set(before) - set(after))
        routes_added = sorted((name, after[name]) for name in set(after) - set(before))
    prepared.changed = sorted({*touched, *(path for _name, path in (*routes_removed, *routes_added))})
    if not touched and not routes_removed and not routes_added:
        what = "API contract or served route" if kind == "api" else "message format"
        prepared.pairings = _not_applicable(
            kind, f"the Change touches no {what} ({', '.join(documents[:3]) or 'none in the trusted base'})"
        )
        return prepared
    first, second = KIND_PAIRINGS[kind]
    if not spec.executable:
        prepared.pairings = [Pairing(pairing, "not_available", spec.reason) for pairing in (first, second)]
        return prepared
    forward_findings: list[Finding] = []
    reverse_findings: list[Finding] = []
    problems: list[str] = []
    compared = 0
    for doc in touched:
        old_text, new_text = read_side(doc, "old"), read_side(doc, "new")
        if old_text is None:
            continue  # a contract the base does not have yet: nothing old to pair with
        try:
            if kind == "api":
                old_doc = _load_document(doc, old_text)
                new_doc = _load_document(doc, new_text) if new_text is not None else None
                ahead, behind, count = _api_rules(spec, doc, old_doc, new_doc)
            else:
                ahead, behind, count = _message_rules(spec, doc, old_text, new_text)
        except (ValueError, TypeError, AttributeError, KeyError, RecursionError) as exc:
            problems.append(f"{doc}: {bounded(exc, 200)}")
            continue
        forward_findings.extend(ahead)
        reverse_findings.extend(behind)
        compared += count
    for name, path in routes_removed:
        forward_findings.append(
            _finding(
                spec,
                "old_client_new_api",
                "removed_route",
                name,
                "Stops serving a route",
                f"{name} is no longer served by {path}; old clients calling it fail.",
                paths=(path,),
                facts=(("Source", "R3 inventory (route)"),),
                base=Observation("served", f"{name} in {path}", sha256_hex([name, path, "old"])),
            )
        )
    for name, path in routes_added:
        reverse_findings.append(
            _finding(
                spec,
                "new_client_old_api",
                "added_route",
                name,
                "Starts serving a route",
                f"{name} is new in {path}; a new client calling it against the old API fails.",
                paths=(path,),
                facts=(("Source", "R3 inventory (route)"),),
            )
        )
    compared += len(routes_removed) + len(routes_added)
    prepared.findings = forward_findings + reverse_findings
    for pairing, found in ((first, forward_findings), (second, reverse_findings)):
        row = Pairing(pairing, "compatible", "", checked=compared, incompatible=len(found))
        if found:
            row.state = "incompatible"
            row.reason = f"{len(found)} incompatibilit{'y' if len(found) == 1 else 'ies'}"
        elif problems:
            row.state, row.reason = "not_available", "the contract cannot be paired: " + "; ".join(problems[:3])
        else:
            row.reason = f"{compared} contract element(s) compared; nothing breaks"
        prepared.pairings.append(row)
    return prepared


# --------------------------------------------------------------------------- #
# verdict
# --------------------------------------------------------------------------- #


def _verdict(spec: AnalysisSpec, prepared: Prepared, pairings: list[Pairing], findings: list[Finding]) -> Evaluation:
    applicable = [row for row in pairings if row.state != "not_applicable"]
    open_states = {"not_available", "unknown", "limited_by_budget"}
    if any(row.direction == "forward" and row.state == "incompatible" for row in pairings):
        status = "FAIL"
    elif not applicable:
        status = "PASS"
    elif all(row.state in {"not_available", "limited_by_budget"} for row in applicable):
        status = "NOT_RUN"
    elif any(row.state in open_states for row in applicable):
        status = "UNKNOWN"
    else:
        status = "PASS"
    reasons = [f"{PAIRINGS[row.id][0]}: {row.state.replace('_', ' ')} — {row.reason}" for row in pairings]
    if any(row.state == "limited_by_budget" for row in pairings):
        reasons.insert(0, "Limited by budget: not every pairing ran")
    reasons.extend(prepared.notes[:5])
    order = sorted(
        {ORDER_HINT[row.id] for row in pairings if row.direction == "reverse" and row.state == "incompatible"}
    )
    return Evaluation(
        status,  # type: ignore[arg-type]
        reasons,
        findings,
        summary={
            "kind": prepared.kind,
            "pairings": [row.to_wire() for row in pairings],
            "deploy_order": order,
            "changed": prepared.changed[:20],
            "evaluated_on": "sandbox" if prepared.execute else "review_host",
        },
        setup_hint=spec.setup_hint if status == "NOT_RUN" else "",
    )


def evaluate(spec: AnalysisSpec, prepared: Prepared, head: SideRun | None) -> Evaluation:
    """Verdict and findings of one compatibility spec."""

    if prepared.kind == "persistence":
        return _evaluate_persistence(spec, prepared, head)
    return _verdict(spec, prepared, prepared.pairings, prepared.findings)


__all__ = [
    "DRIVER",
    "KIND_PAIRINGS",
    "MAX_CODE_FILES",
    "MAX_STATEMENTS",
    "PAIRINGS",
    "STATEMENTS_FILE",
    "Pairing",
    "Prepared",
    "SetHead",
    "SetHeads",
    "Statement",
    "attach_base_files",
    "avro_records",
    "discover_specs",
    "evaluate",
    "extract_statements",
    "migration_tables",
    "parse_specs",
    "payload",
    "prepare",
    "proto_messages",
]

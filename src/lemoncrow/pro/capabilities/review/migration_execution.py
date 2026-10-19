"""Migration execution against a schema fixture (Phase 9R item 15, ``migrations.execute``).

The driver replays the trusted base's migrations (or loads a trusted-base
schema fixture) into an in-memory SQLite database inside the sandbox, applies
the migrations the Change adds, rolls them back with their down migrations and
compares the restored schema with the base schema. The host classifies the
executed schema diff and the added migration text: destructive operations
(drop table/column, truncate, type narrowing, renames) and lock-heavy ones
(non-concurrent index on an existing table, NOT NULL without default, type
changes and other table rewrites).

Frameworks, as detected from the trusted base:

* plain SQL directories (golang-migrate ``*.up.sql``/``*.down.sql``, goose,
  dbmate and sql-migrate markers, Flyway ``V``/``U`` files, Prisma
  ``migration.sql``, ordered ``*.sql``): **executed** on SQLite. PostgreSQL
  spellings SQLite cannot parse (``CONCURRENTLY``, ``NOT VALID``, ``ONLY``)
  are normalized for execution; the classification reads the original text.
* Alembic, Django and Rails: detected, **not_available** (they need their
  framework runtime and a database driver in the sandbox).

Lock-heavy rules assume a PostgreSQL-style production engine unless the
spec says ``dialect: sqlite``.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any, Final

from lemoncrow.pro.capabilities.review.analysis_common import (
    MAX_TIMEOUT,
    MIGRATIONS_ENTRY,
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
    text_sha16,
    valid_spec_id,
)
from lemoncrow.pro.capabilities.review.contracts import _MIGRATION_PATH, _SQL_FILE_RES

DIALECTS: Final[tuple[str, ...]] = ("postgres", "mysql", "sqlite")
FRAMEWORKS: Final[tuple[str, ...]] = ("sql", "alembic", "django", "rails")
_MAX_DISCOVERED: Final[int] = 8
_MAX_BASE_FILES: Final[int] = 600
_FRAMEWORK_HINT: Final[Mapping[str, str]] = {
    "alembic": (
        "Alembic migrations need Python with alembic, SQLAlchemy and a database driver in the sandbox; "
        "this build executes plain SQL migrations only. Point analyses.migrations at a directory of "
        "SQL migrations (for example alembic's --sql output) to execute them."
    ),
    "django": (
        "Django migrations need the project's Django runtime and settings in the sandbox; this build "
        "executes plain SQL migrations only (for example sqlmigrate output configured under analyses.migrations)."
    ),
    "rails": (
        "Rails migrations need Ruby, Rails and a database adapter in the sandbox; this build executes "
        "plain SQL migrations only."
    ),
}

#: Driver helpers shared with ``compat_mixed_version`` (load, schema dump, dialect-normalized execute).
MIGRATION_LIB: Final[str] = r"""
import sqlite3

MARKS = (
    (r"^\s*--\s*\+goose\s+up\b", r"^\s*--\s*\+goose\s+down\b", "goose"),
    (r"^\s*--\s*migrate:up\b", r"^\s*--\s*migrate:down\b", "dbmate"),
    (r"^\s*--\s*\+migrate\s+up\b", r"^\s*--\s*\+migrate\s+down\b", "sql-migrate"),
)
PG = (
    (r"\bCREATE\s+(UNIQUE\s+)?INDEX\s+CONCURRENTLY\b", r"CREATE \1INDEX"),
    (r"\bDROP\s+INDEX\s+CONCURRENTLY\b", "DROP INDEX"),
    (r"\bNOT\s+VALID\b", ""),
    (r"\bALTER\s+TABLE\s+ONLY\b", "ALTER TABLE"),
)


def natural(key):
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", key) if t]


def split_marked(body):
    for up, down, name in MARKS:
        u = re.search(up, body, re.I | re.M)
        if u:
            d = re.search(down, body[u.end():], re.I | re.M)
            if d:
                return body[u.end():u.end() + d.start()], body[u.end() + d.end():], name
            return body[u.end():], None, name
    return None


def load(root):
    rows = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if not name.lower().endswith(".sql"):
                continue
            full = os.path.join(base, name)
            body = text(full, 4 << 20)
            if body is None:
                continue
            updown = re.match(r"^(.+)\.(up|down)\.sql$", name, re.I)
            flyway = re.match(r"^([VU])(\d+(?:[._]\d+)*)__(.+)\.sql$", name)
            if name == "migration.sql":
                key, part, layout = rel(base, root), "up", "prisma"
            elif updown:
                key, part, layout = rel(os.path.join(base, updown.group(1)), root), updown.group(2).lower(), "up-down"
            elif flyway:
                key = rel(os.path.join(base, "V%s__%s" % (flyway.group(2), flyway.group(3))), root)
                part, layout = ("up" if flyway.group(1) == "V" else "down"), "flyway"
            else:
                key, part, layout = rel(full[:-4], root), "file", "plain"
            row = rows.setdefault(key, {"id": key, "paths": [], "up": None, "down": None, "layout": layout})
            row["paths"].append(rel(full, CWD))
            if part == "file":
                marked = split_marked(body)
                if marked:
                    row["up"], row["down"], row["layout"] = marked
                else:
                    row["up"] = body
            else:
                row[part] = body
    return [rows[key] for key in sorted(rows, key=natural)]


def schema(con):
    out = {}
    quote = lambda name: '"' + name.replace('"', '""') + '"'
    rows = con.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    for kind, name, table, sql in rows:
        if name.startswith("sqlite_"):
            continue
        if kind == "table":
            cols = [
                {"name": r[1], "type": (r[2] or "").upper(), "notnull": bool(r[3]),
                 "default": None if r[4] == EMULATED else r[4], "pk": int(r[5])}
                for r in con.execute("PRAGMA table_xinfo(%s)" % quote(name))
            ]
            out["table:" + name] = {"kind": kind, "name": name, "columns": cols}
        elif kind == "index":
            cols = [r[2] for r in con.execute("PRAGMA index_info(%s)" % quote(name))]
            unique = bool(sql and re.search(r"\bUNIQUE\b", sql, re.I))
            out["index:" + name] = {"kind": kind, "name": name, "table": table, "columns": cols, "unique": unique}
        else:
            out[kind + ":" + name] = {"kind": kind, "name": name, "table": table, "sql": " ".join((sql or "").split())}
    return out


EMULATED = "'lc-emulated-not-null'"
ADD_NOT_NULL = re.compile(
    r"\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?[A-Za-z_][\w$]*[`\"\]]?\s+[^,;]*?\bNOT\s+NULL\b[^,;]*",
    re.I,
)


def emulate_not_null(match):
    clause = match.group(0)
    if re.search(r"\bDEFAULT\b", clause, re.I):
        return clause
    R.setdefault("emulated", []).append(" ".join(clause.split())[:200])
    return clause + " DEFAULT " + EMULATED


def execute(con, sql, dialect):
    if dialect != "sqlite":
        for pattern, repl in PG:
            sql = re.sub(pattern, repl, sql, flags=re.I)
        # PostgreSQL adds a NOT NULL column without a default to an empty table; SQLite never
        # does. The fixture's tables are empty, so a sentinel default stands in (and is hidden).
        sql = ADD_NOT_NULL.sub(emulate_not_null, sql)
    con.executescript(sql)


"""

_MAIN: Final[str] = r"""def main():
    root = os.path.normpath(os.path.join(CWD, P.get("dir") or "."))
    if not os.path.isdir(root):
        finish(2, "the migration directory %s is missing on this side" % P.get("dir"))
    dialect = str(P.get("dialect") or "postgres")
    base = P.get("base") or {}
    migrations = load(root)
    if len(migrations) > 2000:
        finish(2, "more than 2000 migrations; narrow analyses.migrations")
    for row in migrations:
        known = [p for p in row["paths"] if p in base]
        if not known:
            row["state"] = "added"
        elif any(sha(os.path.join(CWD, p)) != base[p] for p in known):
            row["state"] = "edited"
        else:
            row["state"] = "base"
    con = sqlite3.connect(":memory:", isolation_level=None)
    deny = (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH)
    con.set_authorizer(lambda action, *rest: sqlite3.SQLITE_DENY if action in deny else sqlite3.SQLITE_OK)
    con.set_progress_handler(lambda: 1 if time.monotonic() - STARTED > TIMEOUT - 3 else 0, 20000)
    fixture = str(P.get("fixture") or "")
    added = [row for row in migrations if row["state"] == "added"]
    if fixture:
        body = text(os.path.join(CWD, fixture), 8 << 20)
        if body is None:
            finish(2, "the schema fixture %s is missing" % fixture)
        try:
            con.executescript(body)
        except sqlite3.Error as exc:
            finish(2, "the trusted-base schema fixture does not load on SQLite: %s" % exc)
        R["fixture"] = fixture
    else:
        for row in migrations:
            if row["state"] == "added" or row["up"] is None:
                continue
            try:
                execute(con, row["up"], dialect)
            except sqlite3.Error as exc:
                finish(2, "the base migration %s does not apply on the SQLite schema fixture (%s); "
                          "configure a SQLite-compatible schema fixture" % (row["id"], exc))
    before = schema(con)
    R["heavy"]["schema_before.sql"] = "\n".join(con.iterdump()).replace(" DEFAULT " + EMULATED, "")
    statements = []
    con.set_trace_callback(
        lambda sql: statements.append(" ".join(sql.split())[:1000]) if len(statements) < 300 else None
    )
    applied, failed = [], None
    for row in added:
        if row["up"] is None:
            continue
        try:
            execute(con, row["up"], dialect)
            applied.append(row["id"])
        except sqlite3.Error as exc:
            failed = {"id": row["id"], "error": str(exc)[:1000]}
            break
    con.set_trace_callback(None)
    after = schema(con)
    R["heavy"]["schema_after.sql"] = "\n".join(con.iterdump()).replace(" DEFAULT " + EMULATED, "")
    rollback = {"status": "skipped"}
    if failed is None and applied:
        missing = [row["id"] for row in added if row["up"] is not None and row["down"] is None]
        if missing:
            rollback = {"status": "unverifiable", "missing_down": missing[:50]}
        else:
            rollback = {"status": "verified"}
            for row in reversed([r for r in added if r["id"] in applied]):
                try:
                    execute(con, row["down"], dialect)
                except sqlite3.Error as exc:
                    rollback = {"status": "failed", "id": row["id"], "error": str(exc)[:1000]}
                    break
            if rollback["status"] == "verified":
                restored = schema(con)
                if restored != before:
                    rollback = {"status": "mismatch", "restored": restored}
    ordered = [r for r in migrations if r["state"] != "base"] + [r for r in migrations if r["state"] == "base"]
    R["migrations"] = [
        {"id": r["id"], "paths": r["paths"][:4], "layout": r["layout"], "state": r["state"],
         "has_down": r["down"] is not None, "up": (r["up"] or "")[:16000] if r["state"] != "base" else ""}
        for r in ordered[:400]
    ]
    R["counts"] = {"total": len(migrations), "added": len(added),
                   "edited": sum(1 for r in migrations if r["state"] == "edited")}
    R.update({"dialect": dialect, "schema_before": before, "schema_after": after, "applied": applied,
              "failed": failed, "rollback": rollback, "statements": statements})
    finish(1 if failed or rollback["status"] in ("failed", "mismatch") else 0)
"""

DRIVER: Final[str] = build_driver(MIGRATION_LIB + _MAIN)


# --------------------------------------------------------------------------- #
# trusted-base specs
# --------------------------------------------------------------------------- #


def _slug(path: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", path.strip("/")).strip("-.") or "root"
    return text[-64:].lstrip("-.") or "root"


def _common_root(paths: Sequence[str]) -> str:
    parts = [PurePosixPath(path).parts for path in paths if path not in {"", "."}]
    if not parts or len(parts) != len(paths):
        return "."
    common: list[str] = []
    for column in zip(*parts, strict=False):
        if len(set(column)) != 1:
            break
        common.append(column[0])
    return "/".join(common) or "."


def _sql_spec(
    spec_id: str,
    directory: str,
    *,
    origin: str,
    source: str,
    dialect: str = "postgres",
    fixture: str = "",
    timeout: int = 120,
    title: str = "",
) -> AnalysisSpec:
    root = _common_root([directory, str(PurePosixPath(fixture).parent)]) if fixture else directory

    def inside(path: str) -> str:
        return path if root == "." else str(PurePosixPath(path).relative_to(root)) or "."

    return AnalysisSpec(
        entry_id=MIGRATIONS_ENTRY,
        id=spec_id,
        tool="sql",
        root=root,
        title=title or f"SQL migrations · {directory}",
        origin=origin,  # type: ignore[arg-type]
        source=source,
        options={"dir": inside(directory), "dialect": dialect, "fixture": inside(fixture) if fixture else ""},
        timeout=timeout,
        trusted_inputs=(fixture,) if fixture else (),
    )


def _unavailable(spec_id: str, framework: str, directory: str, *, origin: str, source: str) -> AnalysisSpec:
    return AnalysisSpec(
        entry_id=MIGRATIONS_ENTRY,
        id=spec_id,
        tool=framework,
        root=directory,
        title=f"{framework.capitalize()} migrations · {directory}",
        origin=origin,  # type: ignore[arg-type]
        source=source,
        options={"dir": "."},
        executable=False,
        reason=f"{framework.capitalize()} migrations cannot execute in this build's sandbox",
        setup_hint=_FRAMEWORK_HINT[framework],
    )


def parse_specs(rows: Sequence[Any], errors: list[str]) -> list[AnalysisSpec]:
    """``analyses.migrations`` rows; invalid rows are reported, never half-used."""

    specs: list[AnalysisSpec] = []
    for index, row in enumerate(rows):
        where = f"analyses.migrations[{index}]"
        if not isinstance(row, Mapping):
            errors.append(f"{where} must be a mapping")
            continue
        unknown = sorted(set(row) - {"id", "title", "dir", "framework", "dialect", "fixture", "timeout"})
        if unknown:
            errors.append(f"{where} has unknown keys: {', '.join(unknown)}")
            continue
        spec_id = row.get("id")
        directory = relative_path(row.get("dir"), allow_root=False)
        framework = str(row.get("framework") or "sql")
        dialect = str(row.get("dialect") or "postgres")
        fixture_raw = row.get("fixture")
        fixture = relative_path(fixture_raw, allow_root=False) if fixture_raw else ""
        try:
            timeout = max(10, min(int(row.get("timeout") or 120), MAX_TIMEOUT))
        except (TypeError, ValueError):
            errors.append(f"{where}.timeout must be a number of seconds")
            continue
        if not valid_spec_id(spec_id):
            errors.append(f"{where}.id must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}")
        elif directory is None:
            errors.append(f"{where}.dir must be a repository-relative directory")
        elif framework not in FRAMEWORKS:
            errors.append(f"{where}.framework must be one of {', '.join(FRAMEWORKS)}")
        elif dialect not in DIALECTS:
            errors.append(f"{where}.dialect must be one of {', '.join(DIALECTS)}")
        elif fixture is None:
            errors.append(f"{where}.fixture must be a repository-relative file")
        elif framework != "sql":
            specs.append(_unavailable(str(spec_id), framework, directory, origin="configured", source=where))
        else:
            specs.append(
                _sql_spec(
                    str(spec_id),
                    directory,
                    origin="configured",
                    source=where,
                    dialect=dialect,
                    fixture=fixture,
                    timeout=timeout,
                    title=str(row.get("title") or "")[:200],
                )
            )
    return specs


def discover_specs(paths: Sequence[str], read_base: Callable[[str], str | None]) -> list[AnalysisSpec]:
    """Migration frameworks the trusted base actually contains."""

    del read_base  # file names are enough; contents are read per run
    sql_dirs: dict[str, str] = {}
    other: dict[str, tuple[str, str]] = {}
    for path in paths:
        lower = path.lower()
        parent = str(PurePosixPath(path).parent)
        name = PurePosixPath(path).name
        if lower.endswith(".sql") and _MIGRATION_PATH.search(path):
            if name == "migration.sql" and "/prisma/migrations/" in f"/{lower}":
                parent = str(PurePosixPath(parent).parent)
            sql_dirs.setdefault(parent, path)
        elif lower.endswith(".py") and re.search(r"(?:^|/)alembic/versions/[^/]+\.py$", lower):
            other.setdefault(parent, ("alembic", path))
        elif lower.endswith(".py") and re.search(r"(?:^|/)migrations/0\d{3}_[^/]+\.py$", lower):
            other.setdefault(parent, ("django", path))
        elif lower.endswith(".rb") and re.search(r"(?:^|/)db/migrate/\d+_[^/]+\.rb$", lower):
            other.setdefault(parent, ("rails", path))
    # A nested folder of an already-discovered migration directory is part of it.
    sql_dirs = {
        directory: source
        for directory, source in sql_dirs.items()
        if not any(directory.startswith(outer.rstrip("/") + "/") for outer in sql_dirs if outer != directory)
    }
    specs: list[AnalysisSpec] = []
    seen: set[str] = set()
    for directory in sorted(sql_dirs)[:_MAX_DISCOVERED]:
        spec_id = _slug(directory)
        while spec_id in seen:
            spec_id = f"{spec_id[:60]}-{len(seen)}"
        seen.add(spec_id)
        specs.append(_sql_spec(spec_id, directory, origin="discovered", source=sql_dirs[directory]))
    for directory in sorted(other)[: max(0, _MAX_DISCOVERED - len(specs))]:
        if directory in sql_dirs:
            continue
        framework, source = other[directory]
        spec_id = _slug(directory)
        while spec_id in seen:
            spec_id = f"{spec_id[:60]}-{len(seen)}"
        seen.add(spec_id)
        specs.append(_unavailable(spec_id, framework, directory, origin="discovered", source=source))
    return specs


def attach_base_files(spec: AnalysisSpec, paths: Sequence[str]) -> AnalysisSpec:
    """Record which migration files the trusted base already has (names only; read per run)."""

    if spec.tool != "sql" or not spec.executable:
        return spec
    directory = join_path(spec.root, str(spec.options.get("dir") or "."))
    prefix = "" if directory in {"", "."} else directory.rstrip("/") + "/"
    files = sorted(path for path in paths if path.startswith(prefix) and path.lower().endswith(".sql"))
    if len(files) > _MAX_BASE_FILES:
        return replace(
            spec,
            executable=False,
            reason=f"more than {_MAX_BASE_FILES} base migration files",
            setup_hint="Narrow analyses.migrations to one migration directory, or squash old migrations into a schema fixture.",
        )
    return replace(spec, options={**spec.options, "base_files": files})


def payload(spec: AnalysisSpec, *, read_base: Callable[[str], str | None]) -> tuple[dict[str, Any], str]:
    """Driver input: the directory, the dialect and the trusted base's migration file keys."""

    base: dict[str, str] = {}
    for path in spec.options.get("base_files") or ():
        text = read_base(str(path))
        if text is not None:
            base[path if spec.root == "." else str(PurePosixPath(path).relative_to(spec.root))] = text_sha16(text)
    return {
        "dir": spec.options.get("dir") or ".",
        "dialect": spec.options.get("dialect") or "postgres",
        "fixture": spec.options.get("fixture") or "",
        "base": base,
    }, ""


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #

_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_IDENT = r"""[`"\[]?([A-Za-z_][\w.]*)[`"\]]?"""
_RENAME = re.compile(
    r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?"
    + _IDENT
    + r"\s+RENAME\s+(?:COLUMN\s+)?(?:"
    + _IDENT
    + r"\s+)?TO\s+",
    re.I,
)
_TYPE_CHANGE = re.compile(
    r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?"
    + _IDENT
    + r"\s+(?:ALTER|MODIFY)\s+(?:COLUMN\s+)?"
    + _IDENT
    + r"\s+(?:SET\s+DATA\s+)?TYPE\s+([^,;]+)",
    re.I,
)
_ADD_COLUMN = re.compile(
    r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?"
    + _IDENT
    + r"\s+ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
    + _IDENT
    + r"\s+([^;]*)",
    re.I,
)
_SET_NOT_NULL = re.compile(
    r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?"
    + _IDENT
    + r"\s+ALTER\s+(?:COLUMN\s+)?"
    + _IDENT
    + r"\s+SET\s+NOT\s+NULL",
    re.I,
)
_INDEX = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(?:"
    + _IDENT
    + r"\s+)?ON\s+(?:ONLY\s+)?"
    + _IDENT,
    re.I,
)
_FOREIGN_KEY = re.compile(
    r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?" + _IDENT + r"\s+ADD\s+(?:CONSTRAINT\s+\S+\s+)?FOREIGN\s+KEY",
    re.I,
)
_REWRITE = re.compile(r"\bVACUUM\s+FULL\b|\bCLUSTER\s+" + _IDENT, re.I)
_UNBOUNDED_DELETE = re.compile(r"^\s*DELETE\s+FROM\s+" + _IDENT + r"\s*$", re.I)
_INT_RANK: Final[Mapping[str, int]] = {
    "TINYINT": 1,
    "SMALLINT": 2,
    "INT2": 2,
    "MEDIUMINT": 3,
    "INT": 4,
    "INTEGER": 4,
    "INT4": 4,
    "BIGINT": 8,
    "INT8": 8,
}
_TEXT_TYPES: Final[frozenset[str]] = frozenset({"TEXT", "CLOB", "LONGTEXT", "MEDIUMTEXT", "STRING"})
_VARCHAR = re.compile(r"^(?:VAR)?CHAR(?:ACTER)?(?:\s+VARYING)?|^N?VARCHAR")


def _type_parts(value: str) -> tuple[str, tuple[int, ...]]:
    match = re.match(r"^\s*([A-Z][A-Z0-9 ]*?)\s*(?:\(([\d\s,]+)\))?\s*$", value.upper())
    if match is None:
        return value.upper().strip(), ()
    args = tuple(int(item) for item in (match.group(2) or "").replace(" ", "").split(",") if item)
    return match.group(1).strip(), args


def is_narrowing(old: str, new: str) -> bool:
    """Whether changing a column from *old* to *new* can lose values (conservative)."""

    (old_name, old_args), (new_name, new_args) = _type_parts(old), _type_parts(new)
    if (old_name, old_args) == (new_name, new_args):
        return False
    if old_name in _INT_RANK and new_name in _INT_RANK:
        return _INT_RANK[new_name] < _INT_RANK[old_name]
    old_text = old_name in _TEXT_TYPES or bool(_VARCHAR.match(old_name))
    new_text = new_name in _TEXT_TYPES or bool(_VARCHAR.match(new_name))
    if old_text and new_text:
        if new_name in _TEXT_TYPES:
            return False
        if old_name in _TEXT_TYPES:
            return True
        return bool(old_args and new_args and new_args[0] < old_args[0]) or bool(not old_args and new_args)
    if old_name in {"NUMERIC", "DECIMAL"} and new_name in {"NUMERIC", "DECIMAL"}:
        return bool(old_args and new_args and (new_args[0] < old_args[0] or new_args[1:] < old_args[1:]))
    if old_name in {"DOUBLE", "DOUBLE PRECISION", "FLOAT8"} and new_name in {"REAL", "FLOAT4", "FLOAT"}:
        return True
    # Any other cross-family change (text to integer, anything to boolean ...) can fail or truncate.
    return old_name != new_name


def _statements(text: str) -> list[str]:
    return [part.strip() for part in _COMMENTS.sub(" ", text).split(";") if part.strip()]


def _tables(schema: Mapping[str, Any]) -> dict[str, dict[str, Mapping[str, Any]]]:
    out: dict[str, dict[str, Mapping[str, Any]]] = {}
    for key, value in schema.items():
        if not key.startswith("table:") or not isinstance(value, Mapping):
            continue
        columns = value.get("columns") or ()
        out[str(value.get("name") or key[6:])] = {
            str(col.get("name")): col for col in columns if isinstance(col, Mapping) and col.get("name")
        }
    return out


def schema_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, list[str]]:
    """Readable executed-schema delta (tables, columns, indexes, views, triggers)."""

    old_tables, new_tables = _tables(before), _tables(after)
    diff: dict[str, list[str]] = {
        "tables_added": sorted(set(new_tables) - set(old_tables)),
        "tables_removed": sorted(set(old_tables) - set(new_tables)),
        "columns_added": [],
        "columns_removed": [],
        "columns_changed": [],
        "indexes_added": [],
        "indexes_removed": [],
        "other_changed": [],
    }
    for table in sorted(set(old_tables) & set(new_tables)):
        old_cols, new_cols = old_tables[table], new_tables[table]
        diff["columns_added"] += [
            f"{table}.{name} {new_cols[name].get('type') or ''}".strip()
            for name in sorted(set(new_cols) - set(old_cols))
        ]
        diff["columns_removed"] += [f"{table}.{name}" for name in sorted(set(old_cols) - set(new_cols))]
        for name in sorted(set(old_cols) & set(new_cols)):
            old, new = old_cols[name], new_cols[name]
            changes = [
                f"{label} {old.get(key)!s} -> {new.get(key)!s}"
                for key, label in (("type", "type"), ("notnull", "not null"), ("default", "default"))
                if old.get(key) != new.get(key)
            ]
            if changes:
                diff["columns_changed"].append(f"{table}.{name}: " + ", ".join(changes))
    for key in sorted(set(before) | set(after)):
        if key.startswith("table:"):
            continue
        if key.startswith("index:"):
            if key not in before:
                diff["indexes_added"].append(key[6:])
            elif key not in after:
                diff["indexes_removed"].append(key[6:])
            elif before[key] != after[key]:
                diff["other_changed"].append(key)
        elif before.get(key) != after.get(key):
            diff["other_changed"].append(key)
    return diff


def _flag(
    found: dict[tuple[str, str], Finding],
    spec: AnalysisSpec,
    rule: str,
    target: str,
    title: str,
    summary: str,
    *,
    category: str,
    severity: str,
    path: str = "",
    evidence: str = "",
) -> None:
    key = (rule, target.lower())
    if key in found:
        return
    found[key] = Finding(
        rule=rule,
        category=category,
        subject=f"{spec.id}::{rule}::{target}",
        title=f"{title} · {target}"[:200],
        summary=bounded(summary),
        severity=severity,  # type: ignore[arg-type]
        critical=category == "destructive",
        paths=(path,) if path else (),
        facts=(
            ("Operation", rule.replace("_", " ")),
            ("Class", "destructive" if category == "destructive" else "lock-heavy"),
            ("Object", target),
            *((("Evidence", evidence),) if evidence else ()),
        ),
        head=Observation(status=category, summary=bounded(summary, 200), digest=sha256_hex([rule, target])),
    )


# SQLite errors that mean "SQLite cannot express this", not "this migration is wrong".
_ENGINE_LIMITS: Final[tuple[str, ...]] = (
    "syntax error",
    "cannot add a",
    "not supported",
    "unsupported",
    "cannot drop",
)


def _engine_limited(error: object, dialect: str) -> bool:
    """A failure SQLite emulation cannot attribute to the migration (PostgreSQL/MySQL dialects only)."""

    text = str(error or "").lower()
    return dialect != "sqlite" and any(marker in text for marker in _ENGINE_LIMITS)


_DOWN_FILE = re.compile(r"(?:^|[._/-])down\.sql$|(?:^|/)U\d[^/]*__[^/]*\.sql$", re.I)


def _row_path(spec: AnalysisSpec, row: Mapping[str, Any]) -> str:
    """The repository path of a migration's up file (never its down file)."""

    paths = [str(item) for item in row.get("paths") or () if str(item)]
    chosen = next((item for item in paths if not _DOWN_FILE.search(item)), paths[0] if paths else "")
    return join_path(spec.root, chosen) if chosen else ""


def classify(
    spec: AnalysisSpec,
    report: Mapping[str, Any],
) -> list[Finding]:
    """Destructive and lock-heavy operations: executed schema diff plus the added migration text."""

    dialect = str(report.get("dialect") or spec.options.get("dialect") or "postgres")
    before = report.get("schema_before") if isinstance(report.get("schema_before"), Mapping) else {}
    after = report.get("schema_after") if isinstance(report.get("schema_after"), Mapping) else {}
    assert isinstance(before, Mapping) and isinstance(after, Mapping)
    existing = set(_tables(before))
    found: dict[tuple[str, str], Finding] = {}
    for row in report.get("migrations") or ():
        if not isinstance(row, Mapping) or row.get("state") not in {"added", "edited"}:
            continue
        path = _row_path(spec, row)
        for statement in _statements(str(row.get("up") or "")):
            snippet = bounded(statement, 160)
            for op, pattern in _SQL_FILE_RES:
                if op == "create":
                    continue
                for hit in pattern.finditer(statement):
                    table = hit.group(1)
                    if op == "drop_column":
                        target = f"{table}.{hit.group(2)}"
                        _flag(
                            found,
                            spec,
                            "drop_column",
                            target,
                            "Drops a column",
                            f"Drops column {target}; existing data in it is lost and readers of it break.",
                            category="destructive",
                            severity="high",
                            path=path,
                            evidence=snippet,
                        )
                    elif op == "drop_table":
                        _flag(
                            found,
                            spec,
                            "drop_table",
                            table,
                            "Drops a table",
                            f"Drops table {table} and all of its rows.",
                            category="destructive",
                            severity="high",
                            path=path,
                            evidence=snippet,
                        )
                    elif op == "truncate":
                        _flag(
                            found,
                            spec,
                            "truncate",
                            table,
                            "Truncates a table",
                            f"Deletes every row of {table}.",
                            category="destructive",
                            severity="high",
                            path=path,
                            evidence=snippet,
                        )
            if (match := _UNBOUNDED_DELETE.match(statement)) is not None:
                _flag(
                    found,
                    spec,
                    "delete_all_rows",
                    match.group(1),
                    "Deletes every row",
                    f"DELETE without WHERE removes every row of {match.group(1)}.",
                    category="destructive",
                    severity="high",
                    path=path,
                    evidence=snippet,
                )
            if (match := _RENAME.search(statement)) is not None:
                target = f"{match.group(1)}.{match.group(2)}" if match.group(2) else match.group(1)
                _flag(
                    found,
                    spec,
                    "rename",
                    target,
                    "Renames a schema object",
                    f"Renames {target}; code still reading the old name breaks during a rolling deploy.",
                    category="destructive",
                    severity="medium",
                    path=path,
                    evidence=snippet,
                )
            if (match := _TYPE_CHANGE.search(statement)) is not None:
                target = f"{match.group(1)}.{match.group(2)}"
                _flag(
                    found,
                    spec,
                    "type_change",
                    target,
                    "Changes a column type",
                    f"Changes the type of {target} to {match.group(3).strip()}; the table is rewritten under an exclusive lock.",
                    category="lock_heavy",
                    severity="medium",
                    path=path,
                    evidence=snippet,
                )
            if (match := _ADD_COLUMN.search(statement)) is not None:
                clause = match.group(3)
                if re.search(r"\bNOT\s+NULL\b", clause, re.I) and not re.search(r"\bDEFAULT\b", clause, re.I):
                    target = f"{match.group(1)}.{match.group(2)}"
                    _flag(
                        found,
                        spec,
                        "not_null_without_default",
                        target,
                        "Adds a NOT NULL column without a default",
                        f"Adds {target} as NOT NULL with no default: it fails on a non-empty table or forces a backfill under lock.",
                        category="lock_heavy",
                        severity="high",
                        path=path,
                        evidence=snippet,
                    )
            if (match := _SET_NOT_NULL.search(statement)) is not None:
                target = f"{match.group(1)}.{match.group(2)}"
                _flag(
                    found,
                    spec,
                    "set_not_null",
                    target,
                    "Sets NOT NULL on an existing column",
                    f"SET NOT NULL on {target} scans the whole table under an exclusive lock.",
                    category="lock_heavy",
                    severity="medium",
                    path=path,
                    evidence=snippet,
                )
            if dialect != "sqlite" and (match := _INDEX.search(statement)) is not None and not match.group(1):
                table = match.group(3)
                if table in existing:
                    _flag(
                        found,
                        spec,
                        "non_concurrent_index",
                        table,
                        "Builds an index without CONCURRENTLY",
                        f"CREATE INDEX on existing table {table} without CONCURRENTLY blocks writes for the whole build (long on large tables).",
                        category="lock_heavy",
                        severity="medium",
                        path=path,
                        evidence=snippet,
                    )
            if (
                dialect == "postgres"
                and (match := _FOREIGN_KEY.search(statement)) is not None
                and not re.search(r"\bNOT\s+VALID\b", statement, re.I)
            ):
                _flag(
                    found,
                    spec,
                    "foreign_key_validation",
                    match.group(1),
                    "Adds a validated foreign key",
                    f"Adding a foreign key to {match.group(1)} without NOT VALID validates every row under lock.",
                    category="lock_heavy",
                    severity="low",
                    path=path,
                    evidence=snippet,
                )
            if (match := _REWRITE.search(statement)) is not None:
                _flag(
                    found,
                    spec,
                    "table_rewrite",
                    match.group(1) or "database",
                    "Rewrites tables",
                    "VACUUM FULL / CLUSTER rewrites tables under an exclusive lock.",
                    category="lock_heavy",
                    severity="medium",
                    path=path,
                    evidence=snippet,
                )
    # The executed schema diff: what actually happened on the fixture.
    old_tables, new_tables = _tables(before), _tables(after)
    for table in sorted(set(old_tables) - set(new_tables)):
        _flag(
            found,
            spec,
            "drop_table",
            table,
            "Drops a table",
            f"Table {table} no longer exists after the migrations ran.",
            category="destructive",
            severity="high",
        )
    for table in sorted(set(old_tables) & set(new_tables)):
        for name in sorted(set(old_tables[table]) - set(new_tables[table])):
            _flag(
                found,
                spec,
                "drop_column",
                f"{table}.{name}",
                "Drops a column",
                f"Column {table}.{name} no longer exists after the migrations ran.",
                category="destructive",
                severity="high",
            )
        for name in sorted(set(old_tables[table]) & set(new_tables[table])):
            old, new = old_tables[table][name], new_tables[table][name]
            target = f"{table}.{name}"
            if old.get("type") != new.get("type"):
                if is_narrowing(str(old.get("type") or ""), str(new.get("type") or "")):
                    _flag(
                        found,
                        spec,
                        "type_narrowing",
                        target,
                        "Narrows a column type",
                        f"{target} changes from {old.get('type')} to {new.get('type')}; existing values can be truncated or rejected.",
                        category="destructive",
                        severity="high",
                    )
                else:
                    _flag(
                        found,
                        spec,
                        "type_change",
                        target,
                        "Changes a column type",
                        f"{target} changes from {old.get('type')} to {new.get('type')}; the table is rewritten under lock.",
                        category="lock_heavy",
                        severity="medium",
                    )
            if not old.get("notnull") and new.get("notnull"):
                if new.get("default") is None:
                    _flag(
                        found,
                        spec,
                        "not_null_without_default",
                        target,
                        "Makes a column NOT NULL without a default",
                        f"{target} becomes NOT NULL without a default: existing NULLs fail it, and the check scans the table under lock.",
                        category="lock_heavy",
                        severity="high",
                    )
                else:
                    _flag(
                        found,
                        spec,
                        "set_not_null",
                        target,
                        "Sets NOT NULL on an existing column",
                        f"{target} becomes NOT NULL; the check scans the whole table under lock.",
                        category="lock_heavy",
                        severity="medium",
                    )
    return list(found.values())


def _schema_text(schema: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for key in sorted(schema):
        value = schema[key]
        if not isinstance(value, Mapping):
            continue
        if value.get("kind") == "table":
            lines.append(f"table {value.get('name')}")
            for col in value.get("columns") or ():
                if isinstance(col, Mapping):
                    null = " NOT NULL" if col.get("notnull") else ""
                    default = f" DEFAULT {col.get('default')}" if col.get("default") is not None else ""
                    lines.append(f"  {col.get('name')} {col.get('type') or ''}{null}{default}".rstrip())
        elif value.get("kind") == "index":
            unique = "unique " if value.get("unique") else ""
            lines.append(
                f"{unique}index {value.get('name')} on {value.get('table')} ({', '.join(map(str, value.get('columns') or ()))})"
            )
        else:
            lines.append(f"{value.get('kind')} {value.get('name')}: {value.get('sql')}")
    return lines


def evaluate(spec: AnalysisSpec, head: SideRun | None, base: SideRun | None) -> Evaluation:
    """Verdict and findings of one migration spec (head side; the base is replayed inside it)."""

    del base  # the head run replays the trusted base itself (or loads its fixture)
    if head is None or head.state == "not_run":
        return Evaluation("NOT_RUN", [head.reason if head is not None else "the head side did not run"])
    if head.state == "unavailable":
        return Evaluation(
            "NOT_RUN",
            [head.reason or "migrations could not execute"],
            setup_hint=spec.setup_hint
            or "Add analyses.migrations with a SQLite-compatible schema fixture to the trusted-base .lemoncrow/review.yaml.",
        )
    if head.state == "error":
        return Evaluation("UNKNOWN", [head.reason or "the migration driver returned no usable result"])
    report = head.report
    before = report.get("schema_before") if isinstance(report.get("schema_before"), Mapping) else {}
    after = report.get("schema_after") if isinstance(report.get("schema_after"), Mapping) else {}
    assert isinstance(before, Mapping) and isinstance(after, Mapping)
    rollback = report.get("rollback") if isinstance(report.get("rollback"), Mapping) else {}
    assert isinstance(rollback, Mapping)
    failed = report.get("failed") if isinstance(report.get("failed"), Mapping) else None
    applied = [str(item) for item in report.get("applied") or ()]
    edited = [
        row for row in report.get("migrations") or () if isinstance(row, Mapping) and row.get("state") == "edited"
    ]
    findings = classify(spec, report)
    diff = schema_diff(before, after)
    status = "PASS"
    reasons: list[str] = []
    fixture = str(report.get("fixture") or "")
    origin = f"schema fixture {fixture}" if fixture else "the trusted base's migrations replayed on SQLite"
    dialect = str(report.get("dialect") or "postgres")
    if failed is not None:
        limited = _engine_limited(failed.get("error"), dialect)
        if limited:
            # SQLite cannot express this statement; that says nothing about the production engine.
            status = "UNKNOWN"
            reasons.append(
                f"migration {failed.get('id')} could not be emulated on SQLite ({failed.get('error')}); "
                "its effect on the schema is unverified"
            )
        else:
            status = "FAIL"
            reasons.append(f"migration {failed.get('id')} failed to apply: {failed.get('error')}")
        findings.append(
            Finding(
                rule="not_emulated" if limited else "apply_failed",
                category="apply",
                subject=f"{spec.id}::apply",
                title=(
                    f"Migration not executable on the SQLite emulation · {failed.get('id')}"
                    if limited
                    else f"Migration does not apply · {failed.get('id')}"
                )[:200],
                summary=bounded(f"Applying {failed.get('id')} on {origin} failed: {failed.get('error')}"),
                severity="medium" if limited else "high",
                critical=not limited,
                facts=(("Migration", str(failed.get("id"))), ("Error", bounded(failed.get("error"), 200))),
                head=Observation("failed", bounded(failed.get("error"), 200), sha256_hex(failed)),
            )
        )
    state = str(rollback.get("status") or "skipped")
    if state == "failed" and _engine_limited(rollback.get("error"), dialect):
        state = "unverifiable"
        if status == "PASS":
            status = "UNKNOWN"
        reasons.append(
            f"down migration {rollback.get('id')} could not be emulated on SQLite ({rollback.get('error')}); "
            "rollback is unverified"
        )
    elif state == "failed":
        status = "FAIL"
        reasons.append(f"down migration {rollback.get('id')} failed: {rollback.get('error')}")
    elif state == "mismatch":
        status = "FAIL"
        reasons.append("rolling back the new migrations did not restore the base schema")
    elif state == "unverifiable" and rollback.get("status") == "unverifiable":
        if status == "PASS":
            status = "UNKNOWN"
        reasons.append(
            "rollback could not be verified: no down migration for "
            + ", ".join(map(str, rollback.get("missing_down") or ()))
        )
    if state in {"failed", "mismatch", "unverifiable"}:
        restored = rollback.get("restored") if isinstance(rollback.get("restored"), Mapping) else None
        delta = schema_diff(before, restored) if restored is not None else {}
        leftover = [f"{key.replace('_', ' ')}: {', '.join(values[:6])}" for key, values in delta.items() if values]
        findings.append(
            Finding(
                rule=f"rollback_{state}",
                category="rollback",
                subject=f"{spec.id}::rollback",
                title={
                    "failed": "Rollback fails",
                    "mismatch": "Rollback does not restore the base schema",
                    "unverifiable": "Rollback not verifiable",
                }[state],
                summary=bounded(reasons[-1] + (("; left over: " + "; ".join(leftover)) if leftover else "")),
                severity="medium" if state == "unverifiable" else "high",
                critical=state != "unverifiable",
                facts=(
                    ("Rollback", state),
                    *((("Left over", bounded("; ".join(leftover), 200)),) if leftover else ()),
                ),
                base=Observation("base schema", f"{len(_tables(before))} tables", sha256_hex(before)),
                head=Observation(state, bounded(reasons[-1], 200), sha256_hex(rollback)),
            )
        )
    for row in edited:
        path = _row_path(spec, row)
        if status == "PASS":
            status = "UNKNOWN"
        reasons.append(f"the Change edits already-applied migration {row.get('id')}")
        findings.append(
            Finding(
                rule="edited_applied_migration",
                category="history",
                subject=f"{spec.id}::edited::{row.get('id')}",
                title=f"Edits an applied migration · {row.get('id')}"[:200],
                summary="Databases that already ran the base version never re-run it, so their schema diverges from this one.",
                severity="high",
                paths=(path,),
                facts=(("Migration", str(row.get("id"))),),
                head=Observation("edited", str(row.get("id")), sha256_hex([row.get("id"), row.get("up")])),
            )
        )
    changed = any(diff.values())
    if changed:
        counts = (
            ("Tables", diff["tables_added"], diff["tables_removed"]),
            ("Columns", diff["columns_added"], diff["columns_removed"]),
            ("Indexes", diff["indexes_added"], diff["indexes_removed"]),
        )
        headline = ", ".join(
            part
            for label, added, removed in counts
            for part in (
                f"+{len(added)} {label.lower()}" if added else "",
                f"-{len(removed)} {label.lower()}" if removed else "",
            )
            if part
        )
        if diff["columns_changed"]:
            headline = ", ".join(filter(None, (headline, f"{len(diff['columns_changed'])} columns changed")))
        if diff["other_changed"]:
            headline = ", ".join(filter(None, (headline, f"{len(diff['other_changed'])} other objects changed")))
        details = [
            *(f"+ table {item}" for item in diff["tables_added"]),
            *(f"- table {item}" for item in diff["tables_removed"]),
            *(f"+ column {item}" for item in diff["columns_added"]),
            *(f"- column {item}" for item in diff["columns_removed"]),
            *(f"~ column {item}" for item in diff["columns_changed"]),
            *(f"+ index {item}" for item in diff["indexes_added"]),
            *(f"- index {item}" for item in diff["indexes_removed"]),
        ]
        findings.insert(
            0,
            Finding(
                rule="schema_diff",
                category="schema",
                subject=f"{spec.id}::schema",
                title=f"Schema change · {headline}"[:200],
                summary=bounded("; ".join(details) or headline),
                severity="low",
                paths=tuple(
                    _row_path(spec, row)
                    for row in report.get("migrations") or ()
                    if isinstance(row, Mapping) and row.get("state") == "added"
                )[:10],
                facts=(
                    *((label, f"+{len(added)} / -{len(removed)}") for label, added, removed in counts),
                    ("Rollback", state),
                    ("Fixture", fixture or "base migrations replayed"),
                    ("Engine", f"SQLite (rules: {report.get('dialect') or 'postgres'})"),
                ),
                base=Observation("base schema", f"{len(_tables(before))} tables", sha256_hex(before)),
                head=Observation("migrated", f"{len(_tables(after))} tables", sha256_hex(after)),
            ),
        )
    if status == "PASS":
        reasons.append(
            f"applied {len(applied)} new migration(s) on {origin}; rollback restored the base schema"
            if applied
            else f"no new migration; the migrations apply on {origin}"
        )
    heavy: dict[str, str] = {}
    old_lines, new_lines = _schema_text(before), _schema_text(after)
    if old_lines != new_lines:
        heavy["schema.diff"] = "\n".join(
            difflib.unified_diff(old_lines, new_lines, "base-schema", "migrated-schema", lineterm="", n=2)
        )
    if head.heavy.get("schema_after.sql"):
        heavy["schema_after.sql"] = head.heavy["schema_after.sql"]
    destructive = sum(1 for item in findings if item.category == "destructive")
    lock_heavy = sum(1 for item in findings if item.category == "lock_heavy")
    if report.get("emulated"):
        reasons.append(
            "NOT NULL column(s) without a default were emulated on the empty fixture tables; "
            "on a populated production table they fail or need a backfill"
        )
    if destructive or lock_heavy:
        # Executing cleanly is not safety: flagged operations go to human judgment as behavior changes.
        reasons.append(f"{destructive} destructive and {lock_heavy} lock-heavy operation(s) flagged for review")
    return Evaluation(
        status,  # type: ignore[arg-type]
        reasons,
        findings,
        summary={
            "framework": spec.tool,
            "dialect": report.get("dialect") or "postgres",
            "fixture": fixture,
            "counts": report.get("counts") or {},
            "applied": applied[:50],
            "rollback": state,
            "schema_diff": {key: values[:50] for key, values in diff.items()},
            "destructive": destructive,
            "lock_heavy": lock_heavy,
        },
        heavy=heavy,
    )


__all__ = [
    "DRIVER",
    "attach_base_files",
    "classify",
    "discover_specs",
    "evaluate",
    "is_narrowing",
    "parse_specs",
    "payload",
    "schema_diff",
]

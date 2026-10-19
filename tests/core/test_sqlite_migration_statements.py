from __future__ import annotations

import sqlite3

from lemoncrow.core.foundation.sqlite_base import _migration_statements


def test_semicolons_in_comments_strings_and_trigger_bodies_stay_in_their_statement() -> None:
    # A `;` in a migration comment split one CREATE TABLE in two and failed the
    # upgrade of every existing store.
    script = """
-- routing; unpinning deletes the row.
CREATE TABLE t (id TEXT PRIMARY KEY, note TEXT NOT NULL DEFAULT 'a;b');
CREATE TABLE log (n INTEGER);
CREATE TRIGGER t_ins AFTER INSERT ON t BEGIN
  INSERT INTO log VALUES (1);
  INSERT INTO log VALUES (2);
END;
-- trailing comment; nothing after it
"""
    statements = list(_migration_statements(script))

    assert len(statements) == 4
    conn = sqlite3.connect(":memory:")
    for statement in statements:
        conn.execute(statement)
    conn.execute("INSERT INTO t (id) VALUES ('x')")
    assert conn.execute("SELECT note FROM t").fetchone() == ("a;b",)
    assert conn.execute("SELECT count(*) FROM log").fetchone() == (2,)


def test_plain_scripts_split_exactly_as_before() -> None:
    script = "CREATE TABLE a (x INTEGER);\nCREATE INDEX ix ON a (x);\n\n"
    assert list(_migration_statements(script)) == ["CREATE TABLE a (x INTEGER)", "CREATE INDEX ix ON a (x)"]

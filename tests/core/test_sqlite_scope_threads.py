from __future__ import annotations

import threading
from pathlib import Path

from lemoncrow.core.foundation.sqlite_base import SqliteTableStore


class _Items(SqliteTableStore):
    SCHEMA = "CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, name TEXT NOT NULL);"
    REQUIRED_TABLES = ("items",)

    def __init__(self, root: Path) -> None:
        super().__init__(root, db_name="items.sqlite3")

    def add(self, name: str) -> None:
        with self._transaction() as conn:
            conn.execute("INSERT INTO items(name) VALUES (?)", (name,))

    def names(self) -> list[str]:
        with self._transaction() as conn:
            return [str(row["name"]) for row in conn.execute("SELECT name FROM items ORDER BY id")]


def test_a_scope_open_on_one_thread_is_invisible_to_reads_on_another(tmp_path: Path) -> None:
    # A server shares one store between its event loop and worker threads. A
    # batch scope's pinned connection belongs to the thread that opened it; a
    # concurrent read elsewhere used it and failed on sqlite3's thread check.
    store = _Items(tmp_path)
    store.init()
    store.add("committed")
    seen: list[list[str]] = []
    errors: list[BaseException] = []

    def read() -> None:
        try:
            seen.append(store.names())
        except BaseException as exc:
            errors.append(exc)

    with store.batch_mode():
        store.add("uncommitted")
        reader = threading.Thread(target=read)
        reader.start()
        reader.join(10)

    assert errors == []
    assert seen == [["committed"]]
    assert store.names() == ["committed", "uncommitted"]

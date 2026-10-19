import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_server_core.index.contracts import Edge, EdgeKind, repository_generation_scope_id
from lemoncrow_server_core.index.links import LinkGraph
from lemoncrow_server_core.index.repositories import (
    KEEP_IDLE_GENERATION_GRAPHS,
    RepositoryIndex,
    _idle_generation_graphs,
)
from lemoncrow_server_core.index.sqlite import SqliteLinkGraphStore, _Db
from test_repository_index import CAP, seed


def test_reads_from_rotating_threads_share_one_pooled_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Readers were per thread for the life of the process: each maintenance
    # pass on a new executor thread opened another connection and page cache
    # per repository (+80 connections and ~100 MB per pass on one store).
    db = _Db(str(tmp_path / "index.sqlite3"))
    with db.transaction() as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def counting_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", counting_connect)
    results: list[int] = []
    for _ in range(20):
        worker = threading.Thread(target=lambda: results.append(db.scalar_int("SELECT x FROM t")))
        worker.start()
        worker.join()

    assert results == [1] * 20
    assert len(opened) == 1
    db.close()


def test_existing_link_graph_store_gains_durable_edge_version(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE link_graphs ("
            "view_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, payload TEXT NOT NULL, "
            "base TEXT NOT NULL DEFAULT '', delta TEXT NOT NULL DEFAULT '')"
        )
        connection.execute("INSERT INTO link_graphs (view_id, org_id, payload) VALUES ('view_old', 'org_a', '{}')")
        connection.commit()
    finally:
        connection.close()

    db = _Db(str(path))
    try:
        columns = {str(row[1]) for row in db._conn.execute("PRAGMA table_info(link_graphs)").fetchall()}
        assert "edge_version" in columns
        row = db.row("SELECT edge_version FROM link_graphs WHERE view_id = 'view_old'")
        assert row is not None and int(row["edge_version"]) == 0
    finally:
        db.close()


def _graph(*edges: Edge) -> LinkGraph:
    paths = sorted({path for edge in edges for path in (edge.src_path, edge.dst_path)})
    empty: dict[str, frozenset[str]] = {path: frozenset() for path in paths}
    return LinkGraph(
        at_view_revision=1,
        edges=frozenset(edges),
        snapshot={path: "d" for path in paths},
        path_modules=empty,
        path_defines=empty,
        path_refs=empty,
        path_candidates=empty,
    )


def test_marking_unreferenced_content_writes_only_rows_whose_mark_changes(tmp_path: Path) -> None:
    # Blob bytes live inline in their rows. "Mark every row, then unmark the
    # live set" rewrote nearly every blob twice on each maintenance pass.
    owner = RepositoryIndex(tmp_path, content_size_cap=CAP)
    try:
        repo, _view = seed(owner.backend, {"a.py": b"def alpha(): pass\n"})
        index = owner.index("org_a", repo)
        assert index is not None
        with index._db.transaction() as conn:
            conn.execute("DELETE FROM blobs")
            for i in range(10):
                conn.execute(
                    "INSERT INTO blobs (org_id, digest, size, data, created_at, unreferenced_since) "
                    "VALUES ('org_a', ?, 3, x'616263', 0, NULL)",
                    (f"d{i}",),
                )
        index._db._conn.execute("CREATE TEMP TABLE blob_writes (digest TEXT)")
        index._db._conn.execute(
            "CREATE TEMP TRIGGER count_blob_writes AFTER UPDATE ON main.blobs "
            "BEGIN INSERT INTO blob_writes VALUES (new.digest); END"
        )

        def mark(live: range, now: float) -> tuple[int, dict[str, float]]:
            index._db._conn.execute("DELETE FROM temp.blob_writes")
            index.backend.content.mark_unreferenced({"org_a": frozenset(f"d{i}" for i in live)}, now)
            writes = int(index._db._conn.execute("SELECT COUNT(*) FROM temp.blob_writes").fetchone()[0])
            marked = {
                str(row["digest"]): float(row["unreferenced_since"])
                for row in index._db._conn.execute(
                    "SELECT digest, unreferenced_since FROM blobs WHERE unreferenced_since IS NOT NULL"
                )
            }
            return writes, marked

        assert mark(range(8), 100.0) == (2, {"d8": 100.0, "d9": 100.0})
        assert mark(range(8), 200.0) == (0, {"d8": 100.0, "d9": 100.0})  # marks keep their age
        assert mark(range(1, 9), 300.0) == (2, {"d0": 300.0, "d9": 100.0})
    finally:
        owner.close()


def test_a_dirty_view_graph_is_stored_as_a_delta_over_its_generation_graph(tmp_path: Path) -> None:
    # Every live dirty View used to hold a full private copy of the repository
    # graph (~266k rows); 49 of them were ~8 GB of one index.
    owner = RepositoryIndex(tmp_path, content_size_cap=CAP)
    try:
        repo, view = seed(owner.backend, {"a.py": b"def alpha(): pass\n"})
        index = owner.index("org_a", repo)
        assert index is not None
        store = SqliteLinkGraphStore(index._db)
        base_key = repository_generation_scope_id("gen_" + "a" * 32, "")
        idle_key = repository_generation_scope_id("gen_" + "b" * 32, "")
        base_edges = [Edge(EdgeKind.IMPORT, f"m{i}.py", f"n{i % 7}.py", "", i) for i in range(3000)]
        store.save("org_a", base_key, _graph(*base_edges))
        store.save("org_a", idle_key, _graph(*base_edges[:5]))
        removed = base_edges[:2]
        added = [
            Edge(EdgeKind.CALL, "m0.py", "edited.py", "fresh", 9),
            Edge(EdgeKind.CALL, "edited.py", "n1.py", "x", 1),
        ]
        view_edges = {*base_edges[2:], *added}
        store.save("org_a", view, _graph(*view_edges), base=base_key)

        stored = index._db.row("SELECT base, delta FROM link_graphs WHERE view_id=?", (view,))
        assert stored is not None and stored["base"] == base_key and stored["delta"]
        assert index._db.scalar_int("SELECT COUNT(*) FROM link_edges WHERE view_id=?", (view,)) == 0
        assert set(store.edges(view)) == view_edges
        loaded = store.load("org_a", view)
        assert loaded is not None and loaded.edges == frozenset(view_edges)
        for paths in (["m0.py"], ["edited.py"], ["n1.py", "m1.py"]):
            expected = {edge for edge in view_edges if edge.src_path in paths or edge.dst_path in paths}
            assert set(store.edges_for_paths("org_a", view, paths)) == expected
        assert not set(removed) & set(store.edges_for_paths("org_a", view, ["m0.py", "m1.py"]))

        # The base outlives the delta View's generation moving on; idle graphs go.
        retired = _idle_generation_graphs(index._db, keep=0, limit=100)
        assert idle_key in retired and base_key not in retired

        # A base that is nevertheless gone makes the View graph absent, not partial.
        store.drop_view(base_key)
        assert store.load("org_a", view) is None
        assert store.edges(view) == ()
        assert store.edges_for_paths("org_a", view, ["m5.py"]) == ()
    finally:
        owner.close()


def test_a_generation_graph_that_changes_carries_every_delta_over_it(tmp_path: Path) -> None:
    import random

    rng = random.Random(9)
    owner = RepositoryIndex(tmp_path, content_size_cap=CAP)
    try:
        repo, view = seed(owner.backend, {"a.py": b"def alpha(): pass\n"})
        index = owner.index("org_a", repo)
        assert index is not None
        store = SqliteLinkGraphStore(index._db)
        universe = [Edge(EdgeKind.CALL, f"s{i % 40}.py", f"d{i % 37}.py", f"f{i}", i) for i in range(4000)]
        base_key = repository_generation_scope_id("gen_" + "d" * 32, "")
        for round_number, dropped in enumerate((0, 5, 400, 2600, 5, 400, 0, 2600)):
            old_base = set(rng.sample(universe, 3000))
            store.save("org_a", base_key, _graph(*old_base))
            expected: dict[str, set[Edge]] = {}
            for number in range(3):
                edges = (old_base - set(rng.sample(sorted(old_base), 30))) | set(rng.sample(universe, 30))
                key = f"{view}-{round_number}-{number}"
                store.save("org_a", key, _graph(*edges), base=base_key)
                expected[key] = edges
            grown = set(rng.sample(universe, 400))
            store.save("org_a", base_key, _graph(*(old_base - set(rng.sample(sorted(old_base), dropped))) | grown))
            for key, edges in expected.items():
                assert set(store.edges(key)) == edges
                loaded = store.load("org_a", key)
                assert loaded is not None and loaded.edges == frozenset(edges)
                paths = ["s3.py", "d5.py"]
                wanted = {edge for edge in edges if edge.src_path in paths or edge.dst_path in paths}
                assert set(store.edges_for_paths("org_a", key, paths)) == wanted
    finally:
        owner.close()


def test_a_view_graph_far_from_its_base_keeps_full_rows(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path, content_size_cap=CAP)
    try:
        repo, view = seed(owner.backend, {"a.py": b"def alpha(): pass\n"})
        index = owner.index("org_a", repo)
        assert index is not None
        store = SqliteLinkGraphStore(index._db)
        base_key = repository_generation_scope_id("gen_" + "c" * 32, "")
        store.save("org_a", base_key, _graph(*(Edge(EdgeKind.IMPORT, f"b{i}.py", "a.py", "", i) for i in range(10))))
        view_edges = {Edge(EdgeKind.IMPORT, f"v{i}.py", "a.py", "", i) for i in range(2500)}
        store.save("org_a", view, _graph(*view_edges), base=base_key)

        stored = index._db.row("SELECT base, delta FROM link_graphs WHERE view_id=?", (view,))
        assert stored is not None and stored["base"] == "" and stored["delta"] == ""
        assert set(store.edges(view)) == view_edges
    finally:
        owner.close()


@pytest.mark.parametrize(("changed", "as_delta"), [(1500, True), (2500, True), (3000, False)])
def test_a_view_up_to_a_quarter_away_from_its_base_is_a_delta(tmp_path: Path, changed: int, as_delta: bool) -> None:
    # A worktree with a few hundred uncommitted files sits ~12% from its
    # generation graph: at a tenth, every agent's View was stored whole.
    owner = RepositoryIndex(tmp_path, content_size_cap=CAP)
    try:
        repo, view = seed(owner.backend, {"a.py": b"def alpha(): pass\n"})
        index = owner.index("org_a", repo)
        assert index is not None
        store = SqliteLinkGraphStore(index._db)
        base_key = repository_generation_scope_id("gen_" + "d" * 32, "")
        base_edges = [Edge(EdgeKind.IMPORT, f"b{i}.py", "a.py", "", i) for i in range(20_000)]
        base = _graph(*base_edges)
        store.save("org_a", base_key, base)
        # `changed` removed plus `changed` added: 2 * changed of a 20k graph.
        view_edges = {*base_edges[changed:], *(Edge(EdgeKind.CALL, f"v{i}.py", "a.py", "f", i) for i in range(changed))}
        store.save("org_a", view, _graph(*view_edges), base=base_key, base_graph=base)

        stored = index._db.row("SELECT base, delta FROM link_graphs WHERE view_id=?", (view,))
        assert stored is not None
        assert (stored["base"] == base_key and bool(stored["delta"])) is as_delta
        assert set(store.edges(view)) == view_edges
        loaded = store.load("org_a", view, base=base_key, base_graph=base)
        assert loaded is not None and loaded.edges == frozenset(view_edges)
    finally:
        owner.close()


def test_saving_a_view_graph_again_writes_only_the_changed_edges(tmp_path: Path) -> None:
    # A dirty View's graph is saved on every revision; rewriting all of a busy
    # repository's ~266k edges per edit dominated CPU and WAL churn.
    owner = RepositoryIndex(tmp_path, content_size_cap=CAP)
    try:
        repo, view = seed(owner.backend, {"a.py": b"def alpha(): pass\n"})
        index = owner.index("org_a", repo)
        assert index is not None
        store = SqliteLinkGraphStore(index._db)
        kept = [Edge(EdgeKind.IMPORT, f"m{i}.py", "a.py", "", i) for i in range(40)]
        dropped = Edge(EdgeKind.CALL, "a.py", "b.py", "beta", 3)
        added = Edge(EdgeKind.CALL, "a.py", "c.py", "gamma", 4)
        store.save("org_a", view, _graph(*kept, dropped))

        before = index._db._conn.total_changes
        store.save("org_a", view, _graph(*kept, added))
        edge_writes = index._db._conn.total_changes - before - 1  # minus the payload upsert

        assert edge_writes == 2
        assert set(store.edges(view)) == {*kept, added}
        loaded = store.load("org_a", view)
        assert loaded is not None and loaded.edges == frozenset({*kept, added})

        store.save("org_a", view, _graph(added))  # mostly removed: one bulk rewrite
        assert set(store.edges(view)) == {added}
    finally:
        owner.close()


def test_full_graph_load_is_one_snapshot_across_a_concurrent_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A load must never pair one graph revision with another revision's edges.

    The two graphs intentionally have identical serialized metadata and the same
    edge count. That is the case payload+count cache keys cannot distinguish.
    """

    owner = RepositoryIndex(tmp_path, content_size_cap=CAP)
    try:
        repo, view = seed(owner.backend, {"a.py": b"def alpha(): pass\n"})
        index = owner.index("org_a", repo)
        assert index is not None
        store = SqliteLinkGraphStore(index._db)
        paths = ("a.py", "b.py", "c.py", "d.py")
        empty: dict[str, frozenset[str]] = {path: frozenset() for path in paths}

        def same_payload_graph(*edges: Edge) -> LinkGraph:
            return LinkGraph(
                at_view_revision=7,
                edges=frozenset(edges),
                snapshot={path: "same-digest" for path in paths},
                path_modules=empty,
                path_defines=empty,
                path_refs=empty,
                path_candidates=empty,
            )

        old = same_payload_graph(
            Edge(EdgeKind.CALL, "a.py", "b.py", "alpha", 1),
            Edge(EdgeKind.CALL, "c.py", "d.py", "beta", 2),
        )
        new = same_payload_graph(
            Edge(EdgeKind.CALL, "a.py", "d.py", "alpha", 1),
            Edge(EdgeKind.CALL, "c.py", "b.py", "beta", 2),
        )
        assert old.to_json() == new.to_json()
        store.save("org_a", view, old)
        store._full_graphs.clear()

        real_read_rows = _Db._read_rows
        interleaved = False

        def interleaving_read(
            connection: sqlite3.Connection, sql: str, parameters: tuple[Any, ...] | list[Any] = ()
        ) -> list[sqlite3.Row]:
            nonlocal interleaved
            rows = real_read_rows(connection, sql, parameters)
            if not interleaved and "FROM link_graphs g" in sql:
                interleaved = True
                writer = threading.Thread(target=lambda: store.save("org_a", view, new))
                writer.start()
                writer.join(timeout=5)
                assert not writer.is_alive(), "WAL writer was blocked by the read snapshot"
            return rows

        monkeypatch.setattr(_Db, "_read_rows", staticmethod(interleaving_read))
        loaded = store.load("org_a", view)
        assert loaded is not None
        assert loaded.to_json() == old.to_json()
        assert loaded.edges == old.edges

        latest = store.load("org_a", view)
        assert latest is not None
        assert latest.to_json() == new.to_json()
        assert latest.edges == new.edges
    finally:
        owner.close()


def test_idle_generation_graphs_keep_live_and_newest_and_retire_oldest_first(tmp_path: Path) -> None:
    # Each base tree gets a full generation graph. Keeping every one inside the
    # manifest-root retention window grew one busy repository by ~14 GiB a day.
    owner = RepositoryIndex(tmp_path, content_size_cap=CAP)
    try:
        repo, view = seed(owner.backend, {"a.py": b"def alpha(): pass\n"})
        index = owner.index("org_a", repo)
        assert index is not None
        generations = [f"gen_{age:032x}" for age in range(10)]  # index 0 is the newest
        keys = [repository_generation_scope_id(generation, "") for generation in generations]
        with index._db.transaction() as conn:
            conn.execute("UPDATE views SET generation_id=? WHERE view_id=?", (generations[9], view))
            for age, (generation, key) in enumerate(zip(generations, keys, strict=True)):
                conn.execute(
                    "INSERT INTO manifest_roots (org_id, root, complete, last_referenced_at) VALUES ('org_a', ?, 1, ?)",
                    (f"root_{age}", 1000.0 - age),
                )
                conn.execute(
                    "INSERT INTO repository_generations "
                    "(org_id, generation_id, repo_id, base_revision, root, analyzer_version, index_version) "
                    "VALUES ('org_a', ?, ?, ?, ?, 'a', 'i')",
                    (generation, repo, f"base_{age}", f"root_{age}"),
                )
                conn.execute("INSERT INTO link_graphs (view_id, org_id, payload) VALUES (?, 'org_a', '{}')", (key,))
                conn.execute("INSERT INTO link_edges VALUES (?, 'import', 'a.py', 'b.py', '', 1)", (key,))

        def stored() -> set[str]:
            graphs = {str(row["view_id"]) for row in index._db.rows("SELECT view_id FROM link_graphs")}
            edges = {str(row["view_id"]) for row in index._db.rows("SELECT DISTINCT view_id FROM link_edges")}
            assert graphs == edges
            return graphs

        # The oldest generation is live: it stays whatever its age. Of the nine
        # idle ones the newest four stay; a pass retires at most four, oldest first.
        owner.reclaim_storage()
        assert stored() == set(keys) - set(keys[5:9])
        owner.reclaim_storage()
        assert stored() == {*keys[:KEEP_IDLE_GENERATION_GRAPHS], keys[9]}
        owner.reclaim_storage()
        assert stored() == {*keys[:KEEP_IDLE_GENERATION_GRAPHS], keys[9]}
    finally:
        owner.close()


def test_retire_only_unreachable_clean_view_graphs(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path, content_size_cap=CAP)
    try:
        repo, clean = seed(owner.backend, {"a.py": b"def alpha(): pass\n"})
        _, dirty = seed(owner.backend, {"a.py": b"def alpha(): pass\n"})
        index = owner.index("org_a", repo)
        assert index is not None
        with index._db.transaction() as conn:
            conn.execute(
                "INSERT INTO overlay (view_id,path,tombstone,content_digest,parser_profile,mode,size) "
                "VALUES (?, 'a.py', 1, '', '', 0, 0)",
                (dirty,),
            )
            for key in (clean, dirty, "gscope_retained"):
                conn.execute("INSERT INTO link_graphs (view_id, org_id, payload) VALUES (?, 'org_a', '{}')", (key,))
                conn.execute(
                    "INSERT INTO link_edges VALUES (?, 'import', 'a.py', 'b.py', '', 1)",
                    (key,),
                )
        owner.reclaim_storage()
        assert index._db.row("SELECT 1 FROM link_graphs WHERE view_id=?", (clean,)) is None
        assert index._db.row("SELECT 1 FROM link_edges WHERE view_id=?", (clean,)) is None
        for key in (dirty, "gscope_retained"):
            assert index._db.row("SELECT 1 FROM link_graphs WHERE view_id=?", (key,)) is not None
            assert index._db.row("SELECT 1 FROM link_edges WHERE view_id=?", (key,)) is not None
        assert owner.backend.views.get("org_a", clean).view_id == clean
    finally:
        owner.close()


def test_a_view_graph_over_a_held_base_is_stored_and_loaded_as_over_the_stored_base(tmp_path: Path) -> None:
    # Link refresh hands the store the generation graph it already holds. That
    # must store and load exactly what re-reading the base rows does.
    owner = RepositoryIndex(tmp_path, content_size_cap=CAP)
    try:
        repo, _view = seed(owner.backend, {"a.py": b"def alpha(): pass\n"})
        index = owner.index("org_a", repo)
        assert index is not None
        store = SqliteLinkGraphStore(index._db)
        base_key = repository_generation_scope_id("gen_" + "a" * 32, "")
        base_edges = [Edge(EdgeKind.IMPORT, f"m{i}.py", f"n{i % 7}.py", "", i) for i in range(3000)]
        base_graph = _graph(*base_edges)
        store.save("org_a", base_key, base_graph)
        near = {*base_edges[2:], Edge(EdgeKind.CALL, "m0.py", "edited.py", "fresh", 9)}
        far = {*base_edges[:10], *(Edge(EdgeKind.CALL, f"x{i}.py", "y.py", "s", i) for i in range(2500))}
        for name, edges in (("near", near), ("far", far)):
            graph = _graph(*edges)
            store.save("org_a", f"view_{name}_read", graph, base=base_key)
            store.save("org_a", f"view_{name}_held", graph, base=base_key, base_graph=base_graph)
            rows = [
                index._db.row("SELECT base, delta FROM link_graphs WHERE view_id=?", (f"view_{name}_{how}",))
                for how in ("read", "held")
            ]
            assert rows[0] is not None and rows[1] is not None
            assert (rows[0]["base"], rows[0]["delta"]) == (rows[1]["base"], rows[1]["delta"])
            assert bool(rows[0]["delta"]) is (name == "near")
            assert set(store.edges(f"view_{name}_read")) == set(store.edges(f"view_{name}_held")) == edges
            for how in ("read", "held"):
                plain = store.load("org_a", f"view_{name}_{how}")
                over_held = store.load("org_a", f"view_{name}_{how}", base=base_key, base_graph=base_graph)
                assert plain is not None and over_held is not None
                assert plain.edges == over_held.edges == frozenset(edges)
    finally:
        owner.close()


def test_a_full_graph_is_decoded_once_and_never_served_after_any_rewrite(tmp_path: Path) -> None:
    # Every refresh of a dirty View re-read its generation graph (~146k edges)
    # into objects; on a live worktree that was ~40% of the server's CPU.
    db = _Db(str(tmp_path / "index.sqlite3"))
    try:
        store = SqliteLinkGraphStore(db)
        edges = [Edge(EdgeKind.IMPORT, f"m{i}.py", "n.py", "", i) for i in range(50)]
        saved = _graph(*edges)
        store.save("org_a", "gen", saved)
        assert store.load("org_a", "gen") is saved  # what was just written is not decoded again

        fresh = SqliteLinkGraphStore(db)
        first = fresh.load("org_a", "gen")
        assert first is not None and first.edges == saved.edges
        assert fresh.load("org_a", "gen") is first  # unchanged: the decoded graph is reused

        # Rewritten through another store on the same database.
        store.save("org_a", "gen", _graph(*edges[:40]))
        rewritten = fresh.load("org_a", "gen")
        assert rewritten is not None and rewritten is not first
        assert rewritten.edges == frozenset(edges[:40])

        # Rewritten behind the store's back (purge, rebase, another code path).
        with db.transaction() as conn:
            conn.execute("DELETE FROM link_edges WHERE view_id = 'gen' AND src_line = 0")
        outside = store.load("org_a", "gen")
        assert outside is not None and outside.edges == frozenset(edges[1:40])

        # Saved as a delta over a base, the view is no longer served whole.
        store.save("org_a", "base", _graph(*edges))
        store.save("org_a", "gen", _graph(*edges[1:]), base="base")
        as_delta = store.load("org_a", "gen")
        assert as_delta is not None and as_delta.edges == frozenset(edges[1:])

        with db.transaction() as conn:
            conn.execute("DELETE FROM link_edges WHERE view_id = 'gen'")
            conn.execute("DELETE FROM link_graphs WHERE view_id = 'gen'")
        assert store.load("org_a", "gen") is None
        assert store.load("org_b", "gen") is None
    finally:
        db.close()

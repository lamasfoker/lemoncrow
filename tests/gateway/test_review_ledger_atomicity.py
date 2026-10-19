"""Additive compute ledgers are atomic in the store, not behind a per-process lock (RC12-B item 2).

``ComputeLedger`` (compute spent) and ``SavingsLedger`` (compute saved) used to
read a row, add, and write it back under a ``threading.Lock``: two processes (or
two hosted replicas) sharing one store could lose an increment. Both now go
through ``ReviewStore.accumulate_derived_result``, a conditional write that only
lands on the exact value it read. The hosted PostgreSQL proof is
``enterprise/server/tests/test_review_execution_economy_postgres.py``.
"""

from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.compute_ledger import COMPUTE_KIND, ComputeLedger
from lemoncrow.pro.capabilities.review.economy import SAVED_KIND, SavingsLedger
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.store import DerivedResult, ReviewStore, new_revision_id, new_session_id

WRITES = 60


def _store(root: Path) -> tuple[ReviewStore, ReviewRevision]:
    store = ReviewStore(root)
    store.init()
    session = store.create_session(
        ReviewSession(
            id=new_session_id(), subject_type="local_change", repo_root=str(root), range_mode="working_tree", title="x"
        )
    )
    revision = store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=0,
            range_mode="working_tree",
            tree_fingerprint="tree",
            source_fingerprint="source",
            packet_schema_version=1,
            provenance_host="claude",
            provenance_model="sonnet",
        ),
        [],
    )
    return store, revision


def _writer(root: str, review_id: str, revision_id: str, start: Any) -> None:
    """One process: its own store object and connections over the same database."""

    store = ReviewStore(Path(root))
    savings, compute = SavingsLedger(store), ComputeLedger(store)
    start.wait(timeout=60)
    for _ in range(WRITES):
        savings.record(
            review_id=review_id, revision_id=revision_id, entry_id="checks.test", reason="carried_forward", seconds=1.5
        )
        compute.record(
            review_id=review_id,
            revision_id=revision_id,
            entry_id="checks.test",
            executor_class="local",
            stage="run",
            seconds=0.5,
        )


def test_two_processes_neither_lose_nor_double_count_savings_or_compute(tmp_path: Path) -> None:
    store, revision = _store(tmp_path)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    workers = [
        context.Process(target=_writer, args=(str(tmp_path), revision.review_id, revision.id, start)) for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(timeout=120)
    assert [worker.exitcode for worker in workers] == [0, 0]

    saved = SavingsLedger(store).rows([revision.id])
    assert saved == {("checks.test", "carried_forward"): (pytest.approx(2 * WRITES * 1.5), 2 * WRITES)}
    [row] = ComputeLedger(store).rows([revision.id])
    assert (row.runs, row.seconds) == (2 * WRITES, pytest.approx(2 * WRITES * 0.5))
    # One row per key: concurrency never forks the ledger into duplicate rows either.
    assert len(store.derived_results(SAVED_KIND, [revision.id])) == 1
    assert len(store.derived_results(COMPUTE_KIND, [revision.id])) == 1


def test_a_writer_holding_a_stale_read_retries_instead_of_overwriting(tmp_path: Path) -> None:
    """The conditional write is the guard: a row changed after the read is never overwritten."""

    store, revision = _store(tmp_path)
    row = DerivedResult(revision.review_id, revision.id, SAVED_KIND, "e|carried_forward", "v1", {"entry_id": "e"})
    assert store.accumulate_derived_result(row, increments={"seconds": 1.0, "runs": 1}) == {
        "entry_id": "e",
        "seconds": 1.0,
        "runs": 1,
    }
    original = store._transaction
    raced = {"done": False}

    def racing_transaction() -> Any:
        # Between this writer's read and its write, another writer adds 10 seconds.
        manager = original()
        conn = manager.__enter__()
        real_execute = conn.execute

        def execute(sql: str, params: Any = ()) -> Any:
            if sql.lstrip().startswith("UPDATE") and not raced["done"]:
                raced["done"] = True
                with original() as other:
                    other.execute(
                        "UPDATE review_derived SET payload_json = ? WHERE revision_id = ? AND kind = ?",
                        ('{"entry_id": "e", "runs": 2, "seconds": 11.0}', revision.id, SAVED_KIND),
                    )
            return real_execute(sql, params)

        class _Wrapped:
            def __enter__(self) -> Any:
                return type("Conn", (), {"execute": staticmethod(execute)})()

            def __exit__(self, *exc: Any) -> Any:
                return manager.__exit__(*exc)

        return _Wrapped()

    store._transaction = racing_transaction  # type: ignore[method-assign]
    try:
        stored = store.accumulate_derived_result(row, increments={"seconds": 1.0, "runs": 1})
    finally:
        store._transaction = original  # type: ignore[method-assign]
    assert raced["done"] is True
    assert stored == {"entry_id": "e", "seconds": 12.0, "runs": 3}
    assert SavingsLedger(store).rows([revision.id]) == {("e", ""): (12.0, 3)}


def test_accumulation_starts_over_on_a_foreign_stamp_and_rejects_identity_increments(tmp_path: Path) -> None:
    store, revision = _store(tmp_path)
    old = DerivedResult(revision.review_id, revision.id, COMPUTE_KIND, "k", "v0", {"seconds": 99.0, "runs": 9})
    store.put_derived_results([old])
    current = DerivedResult(revision.review_id, revision.id, COMPUTE_KIND, "k", "v1", {"entry_id": "k"})
    assert store.accumulate_derived_result(current, increments={"seconds": 2.0, "runs": 1}) == {
        "entry_id": "k",
        "seconds": 2.0,
        "runs": 1,
    }
    for bad in ({}, {"entry_id": 1}, {"runs": True}, {"seconds": "1"}):
        with pytest.raises(ValueError):
            store.accumulate_derived_result(current, increments=bad)  # type: ignore[arg-type]

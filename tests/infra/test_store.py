from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.history_store import HistoryStore
from lemoncrow.core.foundation.jobs_store import JobsStore
from lemoncrow.core.foundation.knowledge_store import KnowledgeStore
from lemoncrow.core.foundation.models import (
    Playbook,
    Rubric,
    Trace,
    TraceLearning,
    ValidationResult,
)
from lemoncrow.core.service.jobs import JOB_CONSOLIDATE_BLOCKS


def _block(bid: str = "b1", domain: str = "coding", title: str = "Title", **kw: object) -> Playbook:
    base: dict[str, Any] = dict(
        id=bid,
        title=title,
        domain=domain,
        situation="When doing X.",
        procedure=["Step one"],
        triggers=["foo"],
        dead_ends=["never do bar"],
    )
    base.update(kw)
    return Playbook(**base)


def test_upsert_and_get_block_roundtrip(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path)
    store.init()
    block = _block()
    store.upsert_block(block)
    fetched = store.get_block(block.id)
    assert fetched is not None
    assert fetched.title == "Title"
    assert (store.blocks_dir / f"{block.id}.md").exists()


def test_search_blocks_uses_fts(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path)
    store.init()
    store.upsert_block(_block(bid="b1", title="Shopify product handle"))
    store.upsert_block(_block(bid="b2", title="Tracker classification"))
    results = store.search_blocks("shopify")
    assert any(b.id == "b1" for b in results)


def test_search_blocks_tokenizes_multi_term_query(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path)
    store.init()
    store.upsert_block(_block(bid="b1", title="Shopify product handle"))
    store.upsert_block(_block(bid="b2", title="Tracker classification"))
    results = store.search_blocks("shop product")
    assert any(b.id == "b1" for b in results)


def test_list_filters_quarantined_and_deprecated(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path)
    store.init()
    store.upsert_block(_block(bid="active", title="A"))
    store.upsert_block(_block(bid="dep", title="B"))
    store.upsert_block(_block(bid="qua", title="C"))
    store.update_block_status("dep", "deprecated")
    store.update_block_status("qua", "quarantined")
    active = store.list_blocks()
    assert {b.id for b in active} == {"active"}
    with_dep = store.list_blocks(include_deprecated=True)
    assert {"active", "dep"}.issubset({b.id for b in with_dep})


def test_record_trace_writes_json_mirror(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    store.init()
    trace = Trace(
        id="t1",
        agent="codex",
        domain="coding",
        task="do thing",
        status="success",
    )
    store.record_trace(trace)
    assert (store.traces_dir / "t1.json").exists()
    fetched = store.get_trace("t1")
    assert fetched is not None and fetched.agent == "codex"


def test_trace_search_reindexes_existing_traces(tmp_path: Path) -> None:
    root = tmp_path / "lemoncrow"
    store = HistoryStore(root)
    store.init()
    store.record_trace(
        Trace(
            id="trace-search-1",
            session_id="run-123",
            agent="copilot",
            host="copilot",
            domain="coding",
            task="Investigate deploy timeout",
            status="failed",
            files_touched=["frontend/src/pages/Traces.tsx"],
            commands_run=["pytest tests/test_timeout.py"],
            output_summary="timeout waiting for deployment worker",
            learnings=[
                TraceLearning(
                    kind="next_rule",
                    text="Workspace fallback checks must include statusline savings.",
                    promote_to="rubric",
                )
            ],
            validation_results=[
                ValidationResult(
                    name="lint",
                    passed=False,
                    detail="timeout during lint verification",
                )
            ],
        ),
        write_json=False,
    )
    matches = store.list_traces(query="timeout")
    assert [trace.id for trace in matches] == ["trace-search-1"]
    full = store.get_trace("trace-search-1")
    assert full is not None
    assert full.learnings[0].promote_to == "rubric"
    assert full.validation_results[0].name == "lint"


def test_rubric_roundtrip(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path)
    store.init()
    r = Rubric(id="r1", domain="coding", required_checks=["a"], block_if_missing=["a"])
    store.upsert_rubric(r)
    assert (store.rubrics_dir / "r1.yaml").exists()
    fetched = store.get_rubric("r1")
    assert fetched is not None
    assert fetched.required_checks == ["a"]


def test_job_queue_roundtrip(tmp_path: Path) -> None:
    store = JobsStore(tmp_path)
    store.init()
    job_id = store.enqueue_job(JOB_CONSOLIDATE_BLOCKS, {"dry_run": True}, max_attempts=2)
    claimed = store.claim_job()
    assert claimed is not None
    assert claimed["id"] == job_id
    assert claimed["job_type"] == JOB_CONSOLIDATE_BLOCKS
    assert claimed["payload"] == {"dry_run": True}
    assert claimed["status"] == "running"
    assert claimed["attempts"] == 1
    assert store.complete_job(job_id, {"written": 0}, locked_by=claimed["locked_by"]) is True
    jobs = store.list_jobs(limit=10)
    assert jobs[0]["id"] == job_id
    assert jobs[0]["status"] == "succeeded"


def _expire_lease(store: JobsStore, job_id: str) -> None:
    """Simulate a crashed worker: backdate the job's lease so it is orphaned."""
    import sqlite3
    from datetime import UTC, datetime, timedelta

    stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE jobs SET locked_at = ? WHERE id = ?", (stale, job_id))
        conn.commit()


def test_claim_job_reaps_orphaned_running_job(tmp_path: Path) -> None:
    store = JobsStore(tmp_path)
    store.init()
    job_id = store.enqueue_job(JOB_CONSOLIDATE_BLOCKS, {"dry_run": True}, max_attempts=3)
    first = store.claim_job()
    assert first is not None and first["status"] == "running" and first["attempts"] == 1
    _expire_lease(store, job_id)
    reclaimed = store.claim_job()
    assert reclaimed is not None
    assert reclaimed["id"] == job_id
    assert reclaimed["status"] == "running"
    assert reclaimed["attempts"] == 2


def test_claim_job_dead_letters_orphan_once_attempts_exhausted(tmp_path: Path) -> None:
    store = JobsStore(tmp_path)
    store.init()
    job_id = store.enqueue_job(JOB_CONSOLIDATE_BLOCKS, {"dry_run": True}, max_attempts=1)
    claimed = store.claim_job()
    assert claimed is not None and claimed["attempts"] == 1
    _expire_lease(store, job_id)
    assert store.claim_job() is None
    job = next(j for j in store.list_jobs(limit=10) if j["id"] == job_id)
    assert job["status"] == "dead"


def test_only_the_lease_holder_completes_fails_pauses_or_heartbeats_a_job(tmp_path: Path) -> None:
    """BI2 review item 6: every outcome is fenced on ``locked_by``; a reaped worker's late result changes nothing."""

    store = JobsStore(tmp_path)
    store.init()
    job_id = store.enqueue_job(JOB_CONSOLIDATE_BLOCKS, {}, max_attempts=3)
    first = store.claim_job("worker-a")
    assert first is not None and first["locked_by"] == "worker-a"
    for wrong in ("worker-b", ""):
        assert store.heartbeat_job(job_id, locked_by=wrong) is False
        assert store.complete_job(job_id, {}, locked_by=wrong) is False
        assert store.fail_job(job_id, "x", locked_by=wrong) is False
        assert store.pause_job(job_id, "x", locked_by=wrong) is False
    assert next(j for j in store.list_jobs() if j["id"] == job_id)["status"] == "running"

    # worker-a's lease expires and worker-b reclaims the job: worker-a can no longer touch it.
    _expire_lease(store, job_id)
    second = store.claim_job("worker-b")
    assert second is not None and second["id"] == job_id and second["attempts"] == 2
    assert store.heartbeat_job(job_id, locked_by="worker-a") is False
    assert store.complete_job(job_id, {}, locked_by="worker-a") is False
    assert store.fail_job(job_id, "late", locked_by="worker-a") is False
    row = next(j for j in store.list_jobs() if j["id"] == job_id)
    assert (row["status"], row["locked_by"]) == ("running", "worker-b")
    assert store.complete_job(job_id, {}, locked_by="worker-b") is True
    assert store.complete_job(job_id, {}, locked_by="worker-b") is False  # not running any more


def test_a_heartbeat_extends_the_lease_so_a_live_job_is_not_reaped(tmp_path: Path, monkeypatch: Any) -> None:
    """BI2 review item 6: the lease the reaper reads is the last heartbeat, not the claim."""

    monkeypatch.setenv("LEMONCROW_JOB_LEASE_SECONDS", "600")
    store = JobsStore(tmp_path)
    store.init()
    job_id = store.enqueue_job(JOB_CONSOLIDATE_BLOCKS, {}, max_attempts=3)
    assert store.claim_job("worker-a") is not None
    _expire_lease(store, job_id)  # claimed an hour ago: past the 600 s lease
    assert store.heartbeat_job(job_id, locked_by="worker-a") is True
    assert store.claim_job("worker-b") is None  # still leased: not reaped, not re-run
    row = next(j for j in store.list_jobs() if j["id"] == job_id)
    assert (row["status"], row["locked_by"], row["attempts"]) == ("running", "worker-a", 1)


def test_enqueue_job_once_keeps_a_semantic_id_after_success_or_exhaustion(tmp_path: Path) -> None:
    """Repeated durable events never cause a second model run for the same identity."""

    store = JobsStore(tmp_path)
    store.init()
    assert store.enqueue_job_once(JOB_CONSOLIDATE_BLOCKS, {"n": 1}, job_id="bg-1", max_attempts=1)
    assert not store.enqueue_job_once(JOB_CONSOLIDATE_BLOCKS, {"n": 2}, job_id="bg-1")
    claimed = store.claim_job("w")
    assert claimed is not None and claimed["payload"] == {"n": 1}
    assert claimed["attempts"] == 1
    assert not store.enqueue_job_once(JOB_CONSOLIDATE_BLOCKS, {"n": 2}, job_id="bg-1")
    assert store.pause_job("bg-1", "paused: background_exhausted", locked_by="w")
    assert not store.enqueue_job_once(JOB_CONSOLIDATE_BLOCKS, {"n": 2}, job_id="bg-1")
    assert store.resume_paused_jobs(older_than_seconds=0) == 1
    assert store.claim_job("w") is not None
    assert store.complete_job("bg-1", {}, locked_by="w")
    assert not store.enqueue_job_once(JOB_CONSOLIDATE_BLOCKS, {"n": 3}, job_id="bg-1")
    row = next(j for j in store.list_jobs() if j["id"] == "bg-1")
    assert (row["status"], row["attempts"], row["payload"]) == ("succeeded", 1, {"n": 1})
    assert row["locked_by"] is None
    assert row["max_attempts"] == 1
    assert row["error"] is None

    assert store.enqueue_job_once(JOB_CONSOLIDATE_BLOCKS, {"n": 4}, job_id="bg-2", max_attempts=1)
    assert store.claim_job("w") is not None
    assert store.fail_job("bg-2", "boom", locked_by="w")
    assert not store.enqueue_job_once(JOB_CONSOLIDATE_BLOCKS, {"n": 5}, job_id="bg-2")
    assert next(j for j in store.list_jobs() if j["id"] == "bg-2")["status"] == "dead"
    assert next(j for j in store.list_jobs() if j["id"] == "bg-2")["payload"] == {"n": 4}


def test_job_queue_health_counts_stuck_running_and_dead(tmp_path: Path) -> None:
    store = JobsStore(tmp_path)
    store.init()
    running_job_id = store.enqueue_job("consolidate", {"n": 1}, max_attempts=2)
    dead_job_id = store.enqueue_job("retry", {"n": 2}, max_attempts=1)
    running_job = store.claim_job()
    dead_job = store.claim_job()
    assert running_job is not None
    assert dead_job is not None
    assert running_job["id"] == running_job_id
    assert dead_job["id"] == dead_job_id
    assert store.fail_job(dead_job_id, "boom", locked_by=dead_job["locked_by"]) is True
    stale_locked_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with store._connect() as conn:
        conn.execute(
            "UPDATE jobs SET locked_at = ?, updated_at = ? WHERE id = ?",
            (stale_locked_at, stale_locked_at, running_job_id),
        )
    assert store.job_queue_health() == {
        "pending": 0,
        "running": 1,
        "failed": 0,
        "dead": 1,
        "stuck_running": 1,
        "active": 1,
    }

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from lemoncrow.core.foundation.runtime_decisions import RuntimeDecisionEvent
from lemoncrow.core.foundation.runtime_trace import (
    TraceCandidate,
    TraceStore,
    active_trace,
    begin_trace,
    is_trace_id,
    new_trace_id,
    open_trace,
    record_decision,
    sanitize_arguments,
)


def test_trace_ids_are_opaque_lt_ids() -> None:
    tid = new_trace_id()
    assert is_trace_id(tid) and tid.startswith("lt_") and len(tid) == 19
    assert not is_trace_id("req-123") and not is_trace_id(None)


def test_raw_arguments_keep_queries_but_never_bulk_content() -> None:
    args = {"query": "authorize_review", "path": "a.py", "new": "secret source" * 10, "edits": [{"old": "x"}]}
    raw = sanitize_arguments(args, "raw")
    assert raw["query"] == "authorize_review" and raw["path"] == "a.py"
    assert raw["new"]["omitted"] == "bulk_content" and "secret" not in json.dumps(raw)
    assert raw["edits"][0]["old"]["omitted"] == "bulk_content"


def test_redacted_arguments_hash_sensitive_leaves() -> None:
    red = sanitize_arguments({"query": "authorize_review", "limit": 5, "content": "body"}, "redacted")
    assert "authorize_review" not in json.dumps(red) and "body" not in json.dumps(red)
    assert red["limit"] == 5


def test_redacted_arguments_are_default_deny_for_unlisted_keys() -> None:
    args = {
        "symbol": "AcmeGateway",
        "url": "https://internal.example/x",
        "target": "acme/core.py",
        "pattern": "SECRET_RE",
        "cwd": "/home/u/acme",
        "ref": "feat/acme",
        "opts": {"glob": "src/acme/**", "flags": ["--secret-flag"], "depth": 3, "deep": True},
    }
    red = sanitize_arguments(args, "redacted")
    dumped = json.dumps(red)
    for leaked in ("Acme", "internal.example", "SECRET_RE", "/home/u", "feat/acme", "--secret-flag", "src/acme"):
        assert leaked not in dumped, leaked
    assert red["opts"]["depth"] == 3 and red["opts"]["deep"] is True
    assert set(red["symbol"]) == {"redacted_sha256"}


def test_redacted_retrieval_extra_hashes_missing_signal_targets(tmp_path) -> None:
    store = TraceStore(tmp_path)
    with open_trace(trace_id=None, writer="server", tool="code_search", mode="redacted", store=store) as trace:
        trace.record_retrieval(query="AcmeGateway", candidates=[], channel="server_index", ranker="heuristic")
        trace.finalize_retrieval(
            selected=[],
            extra={"sufficiency": {"missing": [{"kind": "definition", "target": "AcmeGateway"}]}},
        )
    assert "Acme" not in json.dumps(list(store.iter_records()))


def test_trace_files_are_owner_only(tmp_path) -> None:
    import stat

    path = TraceStore(tmp_path).append({"writer": "runtime", "trace_id": new_trace_id()})
    assert path is not None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "traces").stat().st_mode) == 0o700


def test_redacted_mode_stubs_nested_bulk_content_before_hashing() -> None:
    args = {"edits": [{"path": "a.py", "old": "OLD-BODY", "new": "NEW-BODY"}], "files": [{"content": "FILE-BODY"}]}
    red = json.dumps(sanitize_arguments(args, "redacted"))
    assert "OLD-BODY" not in red and "NEW-BODY" not in red and "FILE-BODY" not in red and "a.py" not in red


def test_client_and_runtime_sanitizers_agree() -> None:
    """The dependency-free client restates the privacy rules; they must not drift."""
    from lemoncrow_client import trace as client_trace

    from lemoncrow.core.foundation import runtime_trace

    assert client_trace.BULK_CONTENT_KEYS == runtime_trace.BULK_CONTENT_KEYS
    assert client_trace.TRACE_SCHEMA_VERSION == runtime_trace.TRACE_SCHEMA_VERSION
    assert runtime_trace.is_trace_id(client_trace.new_trace_id())
    sample = {
        "query": "authorize_review",
        "path": "src/a.py",
        "limit": 5,
        "edits": [{"old": "x", "new": "y", "path": "b.py"}],
        "files": ["c.py:L1-L9"],
        "nested": {"text": "t", "keep": [1, 2]},
        "symbol": "AcmeGateway",
        "url": "https://internal.example/x",
        "cwd": "/home/u/acme",
        "pattern": "SECRET_RE",
    }
    for mode in ("raw", "redacted"):
        assert client_trace.sanitize_arguments(sample, mode) == sanitize_arguments(sample, mode), mode


def test_open_trace_writes_one_record_and_inner_layers_join(tmp_path) -> None:
    store = TraceStore(tmp_path)
    tid = new_trace_id()
    with open_trace(trace_id=tid, writer="server", tool="code_search", mode="raw", store=store) as outer:
        with open_trace(trace_id=tid, writer="runtime", tool="code_search", store=store) as inner:
            assert inner is outer
            event = RuntimeDecisionEvent(kind="k", phase="p", policy="x")
            record_decision(event)
            record_decision(event)  # fanned out to two sinks -> kept once
    assert active_trace() is None
    records = store.find(tid)
    assert len(records) == 1 and records[0]["writer"] == "server"
    assert [d["kind"] for d in records[0]["decisions"]] == ["k"]


def test_off_mode_and_env_kill_switch_write_nothing(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = TraceStore(tmp_path)
    with open_trace(trace_id=None, writer="runtime", tool="t", mode="off", store=store):
        pass
    monkeypatch.setenv("LEMONCROW_RUNTIME_TRACE", "off")
    with open_trace(trace_id=None, writer="server", tool="t", mode="raw", store=store):
        pass
    assert list(store.iter_records()) == []


def test_error_is_recorded_on_the_outcome(tmp_path) -> None:
    store = TraceStore(tmp_path)
    tid = new_trace_id()
    with pytest.raises(ValueError):
        with open_trace(trace_id=tid, writer="runtime", tool="t", mode="raw", store=store):
            raise ValueError("boom")
    assert store.find(tid)[0]["outcome"] == {"is_error": True, "error_type": "ValueError"}


def test_commit_is_idempotent_and_joined_handles_never_write(tmp_path) -> None:
    store = TraceStore(tmp_path)
    handle = begin_trace(trace_id=None, writer="runtime", tool="t", mode="raw", store=store)
    with handle.active():
        joined = begin_trace(trace_id=None, writer="runtime", tool="t", store=store)
        assert not joined.owned
        joined.commit()
    handle.commit()
    handle.commit()
    assert len(list(store.iter_records())) == 1


def test_redacted_retrieval_and_decisions_hash_paths(tmp_path) -> None:
    store = TraceStore(tmp_path)
    with open_trace(trace_id=None, writer="server", tool="code_search", mode="redacted", store=store) as trace:
        trace.record_retrieval(
            query="authorize_review",
            candidates=[TraceCandidate("src/secret.py", 1.0, 0, symbols=("authorize_review",))],
            selected=["src/secret.py"],
            extra={"scopes": ["src"]},
        )
        trace.finalize_retrieval(selected=["src/secret.py"], extra={"sufficiency": {"missing": [{"target": "x"}]}})
        record_decision(
            RuntimeDecisionEvent(
                kind="context.expand", phase="p", policy="x", evidence_refs=("src/secret.py",), proposed={"target": "t"}
            )
        )
    text = json.dumps(list(store.iter_records()))
    assert "src/secret.py" not in text and "authorize_review" not in text and '"target": "t"' not in text


def test_raw_retrieval_keeps_candidates_for_replay(tmp_path) -> None:
    store = TraceStore(tmp_path)
    with open_trace(trace_id=None, writer="server", tool="code_search", mode="raw", store=store) as trace:
        trace.record_retrieval(
            query="q",
            candidates=[TraceCandidate("a.py", 2.0, 0, 3, 9, {"has_definitions": 1.0}, ("fn",))],
            selected=["a.py"],
            ranker="heuristic",
            limit=5,
        )
    retrieval = next(store.iter_records())["retrieval"]
    assert retrieval["query"] == "q" and retrieval["limit"] == 5
    assert retrieval["candidates"][0] == {
        "path": "a.py",
        "score": 2.0,
        "rank": 0,
        "symbols": ["fn"],
        "start_line": 3,
        "end_line": 9,
        "features": {"has_definitions": 1.0},
    }


def test_prune_removes_old_days_then_oldest_until_under_size(tmp_path) -> None:
    store = TraceStore(tmp_path, retention_days=3, max_bytes=50)
    today = datetime(2026, 9, 23, tzinfo=UTC)
    for offset in range(6):
        day = store.dir / (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        day.mkdir(parents=True)
        (day / "runtime-1.jsonl").write_text("x" * 30)
    removed = store.prune(today=today)
    remaining = sorted(p.name for p in store.dir.iterdir())
    # Age: days older than 3 go. Size: 30B/day, cap 50B -> only the newest stays.
    assert remaining == ["2026-09-23"]
    assert set(removed) == {"2026-09-18", "2026-09-19", "2026-09-20", "2026-09-21", "2026-09-22"}


def test_recent_orders_newest_first(tmp_path) -> None:
    store = TraceStore(tmp_path)
    ids = []
    for _ in range(3):
        with open_trace(trace_id=None, writer="runtime", tool="read", mode="raw", store=store) as trace:
            ids.append(trace.trace_id)
    assert [r["trace_id"] for r in store.recent(2)] == ids[::-1][:2]
    assert [r["trace_id"] for r in store.recent(5, tool="read")] == ids[::-1]

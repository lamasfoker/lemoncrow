"""Spec §6: host-neutral author guidance — schema, capture, import, view."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.guidance import (
    AuthorGuidanceRecord,
    GuidanceError,
    _read_sidecar,
    build_guidance_view,
    guidance_view_payload,
    import_author_guidance,
    load_guidance_records,
    parse_guidance,
    payload_to_dict,
    record_author_guidance,
    record_from_row,
    referenced_paths,
)
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.session_models import AuthorGuidanceRow, ReviewUnit
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import DB_NAME, ReviewStore
from lemoncrow.pro.capabilities.review.targets import ReviewTarget, TargetSpan
from lemoncrow.pro.capabilities.review.units import file_fingerprint


def _example() -> dict[str, Any]:
    """The spec §6 illustrative payload, made concrete."""
    return {
        "schema_version": 1,
        "intent": "Keep review history isolated by branch.",
        "items": [
            {
                "key": "branch-identity",
                "title": "Branch identity",
                "targets": [{"path": "src/review/session.py", "symbol": "resolve_review"}],
                "behavior": {
                    "before": "Branch changes could reuse the wrong review context.",
                    "after": "Each branch resolves its own review context.",
                },
                "rationale": "Preserve a continuous review across commits.",
                "concerns": [{"scope": "legacy migration", "reason": "Existing persisted reviews were not exercised."}],
                "assumptions": [
                    {
                        "key": "branch-name-stability",
                        "value": "stable-within-session",
                        "statement": "Branch names remain stable during one work session.",
                        "scope": "branch identity",
                    }
                ],
                "suggested_attention": "review_first",
                "evidence_refs": ["check-1"],
            }
        ],
        "human_decisions": [
            {
                "key": "checkpoint-retention",
                "question": "How long should committed checkpoints be retained?",
                "options": [
                    {"key": "indefinite", "label": "Retain indefinitely"},
                    {"key": "ninety-days", "label": "Expire after 90 days"},
                ],
                "selected_option": "indefinite",
                "reason_human_required": "The product requirement does not specify retention.",
                "targets": [{"path": "src/review/store.py", "symbol": "prune_checkpoints"}],
                "contract_scope": ["review retention"],
                "relied_checks": ["browser:checkout-regression"],
            }
        ],
        "suggested_order": ["branch-identity"],
        "routine_candidates": [],
    }


def test_guidance_parse_and_validation_contract() -> None:
    """case: test_the_spec_example_parses_and_round_trips"""
    payload = parse_guidance(_example())
    assert payload.items[0].before.startswith("Branch changes")
    assert payload.items[0].assumptions[0].value == "stable-within-session"
    assert payload.human_decisions[0].selected_option == "indefinite"
    assert parse_guidance(payload_to_dict(payload)) == payload
    assert referenced_paths(payload) == ("src/review/session.py", "src/review/store.py")
    "case: test_invalid_guidance_is_rejected_with_the_field_named"
    for path, value, message in [
        (("schema_version",), 2, "schema_version"),
        (("items", 0, "key"), "Branch Identity", "key"),
        (("items",), [_example()["items"][0], _example()["items"][0]], "duplicate"),
        (("human_decisions", 0, "options", 1, "key"), "indefinite", "duplicate"),
        (("human_decisions", 0, "selected_option"), "forever", "selected_option"),
        (("human_decisions", 0, "options"), [{"key": "indefinite", "label": "Retain"}], "options"),
        (("human_decisions", 0, "reason_human_required"), "", "reason_human_required"),
        (("human_decisions", 0, "relied_checks"), ["ci:unit"], "relied_checks"),
        (("suggested_order",), ["nope"], "suggested_order"),
        (("routine_candidates",), ["nope"], "routine_candidates"),
        (("confidence",), 0.82, "confidence"),
        (("items", 0, "confidence"), 0.9, "confidence"),
        (("items", 0, "targets", 0, "path"), "/etc/passwd", "path"),
        (("items", 0, "targets", 0, "path"), "../outside.py", "path"),
        (("items", 0, "targets", 0, "start_line"), 9, "end_line"),
        (("items", 0, "suggested_attention"), "urgent", "suggested_attention"),
    ]:
        raw = _mutated(path, value)
        if path == ("items", 0, "targets", 0, "start_line"):
            raw["items"][0]["targets"][0]["end_line"] = 3
        with pytest.raises(GuidanceError, match=message):
            parse_guidance(raw)
    "case: test_an_internal_only_item_is_a_stated_claim_not_a_before_and_after"
    'Spec §3: "Internal implementation only" is an attributed claim the author makes.'
    raw = copy.deepcopy(_example())
    raw["items"][0]["behavior"] = {"unchanged": True}
    payload = parse_guidance(raw)
    assert payload.items[0].internal_only
    assert (payload.items[0].before, payload.items[0].after) == ("", "")
    assert payload_to_dict(payload)["items"][0]["behavior"] == {"before": "", "after": "", "unchanged": True}
    assert parse_guidance(payload_to_dict(payload)) == payload
    plain = parse_guidance(_example())
    assert not plain.items[0].internal_only
    assert "unchanged" not in payload_to_dict(plain)["items"][0]["behavior"]
    raw["items"][0]["behavior"] = {"unchanged": True, "after": "Sessions expire sooner."}
    with pytest.raises(GuidanceError, match="unchanged"):
        parse_guidance(raw)
    raw["items"][0]["behavior"] = {"unchanged": "yes"}
    with pytest.raises(GuidanceError, match="unchanged"):
        parse_guidance(raw)
    "case: test_the_view_payload_is_json_ready"
    other = _example()
    other["human_decisions"][0]["selected_option"] = "ninety-days"
    view = _view(_record(_example()), _record(other, host="codex", session="cx-1", at="2026-09-28T11:00:00+00:00"))
    payload = guidance_view_payload(view)
    assert json.loads(json.dumps(payload)) == payload
    assert payload["conflicts"][0]["key"] == "checkpoint-retention"
    assert payload["items"][0]["resolutions"][0]["outcome"] == "resolved"
    assert {record["producer_host"] for record in payload["records"]} == {"claude", "codex"}
    "case: test_oversized_item_lists_are_rejected_not_truncated"
    raw = _example()
    raw["items"] = [dict(raw["items"][0], key=f"item-{index}") for index in range(65)]
    raw["suggested_order"] = []
    with pytest.raises(GuidanceError, match="items"):
        parse_guidance(raw)


def test_guidance_resolution_contract() -> None:
    """case: test_a_current_symbol_reference_resolves_to_its_target"""
    view = _view(_record(_example()))
    resolution = _resolution(view, "item", "branch-identity")
    assert (resolution.outcome, resolution.target_ids) == ("resolved", ("t-resolve",))
    assert [definition.stale for definition in view.items + view.decisions] == [False, False]
    "case: test_a_path_the_change_deletes_is_kept_as_deleted_file"
    packet = {
        "files": [
            {"path": _SESSION, "old_path": None, "status": "modified"},
            {"path": _STORE, "old_path": _STORE, "status": "deleted"},
        ]
    }
    targets = (_TARGETS[0], _target("t-prune", _STORE, "prune_checkpoints", 1, 2, side="old"))
    view = _view(_record(_example(), prints={_STORE: ""}), targets=targets, packet=packet)
    resolution = _resolution(view, "decision", "checkpoint-retention")
    assert (resolution.outcome, resolution.target_ids) == ("deleted_file", ("t-prune",))
    assert [decision.key for decision in view.decisions] == ["checkpoint-retention"]
    "case: test_a_renamed_path_points_at_the_new_file"
    raw = _example()
    raw["items"][0]["targets"] = [{"path": "src/review/old_session.py", "symbol": "resolve_review"}]
    packet = {
        "files": [
            {"path": _SESSION, "old_path": "src/review/old_session.py", "status": "renamed"},
            {"path": _STORE, "old_path": None, "status": "modified"},
        ]
    }
    resolution = _resolution(_view(_record(raw), packet=packet), "item", "branch-identity")
    assert (resolution.outcome, resolution.target_ids) == ("renamed", ("t-resolve",))
    "case: test_a_path_outside_the_change_is_not_in_change"
    raw = _example()
    raw["items"][0]["targets"] = [{"path": "docs/notes.md"}]
    resolution = _resolution(_view(_record(raw)), "item", "branch-identity")
    assert (resolution.outcome, resolution.target_ids) == ("not_in_change", ())
    "case: test_an_unknown_symbol_is_reported_not_guessed"
    raw = _example()
    raw["items"][0]["targets"] = [{"path": _SESSION, "symbol": "missing"}]
    resolution = _resolution(_view(_record(raw)), "item", "branch-identity")
    assert (resolution.outcome, resolution.target_ids) == ("symbol_not_found", ())
    "case: test_a_symbol_named_by_two_targets_is_ambiguous"
    raw = _example()
    raw["items"][0]["targets"] = [{"path": _SESSION, "symbol": "run"}]
    targets = (_target("t-a", _SESSION, "A.run", 1, 3), _target("t-b", _SESSION, "B.run", 5, 8), _TARGETS[1])
    resolution = _resolution(_view(_record(raw), targets=targets), "item", "branch-identity")
    assert (resolution.outcome, resolution.target_ids) == ("ambiguous", ("t-a", "t-b"))
    "case: test_guidance_about_older_content_is_stale"
    view = _view(_record(_example(), prints={_SESSION: "fp-older"}))
    resolution = _resolution(view, "item", "branch-identity")
    assert (resolution.outcome, resolution.target_ids) == ("stale_content", ("t-resolve",))
    (item,) = view.items
    assert item.stale


def test_guidance_conflict_contract() -> None:
    """case: test_a_reworded_decision_from_the_same_producer_supersedes_without_conflict"""
    first = _record(_example(), at="2026-09-28T10:00:00+00:00")
    reworded = _example()
    reworded["human_decisions"][0]["question"] = "How long do we keep committed checkpoints?"
    second = _record(reworded, at="2026-09-28T11:00:00+00:00")
    view = _view(first, second)
    assert view.conflicts == ()
    (decision,) = view.decisions
    assert decision.record_id == second.id
    assert decision.decision is not None and decision.decision.question.startswith("How long do we keep")
    assert (first.id, "decision", "checkpoint-retention", second.id, "same_producer") in _sup(view)
    "case: test_incompatible_decisions_from_two_producers_are_one_conflict"
    other = _example()
    other["human_decisions"][0]["selected_option"] = "ninety-days"
    view = _view(_record(_example()), _record(other, host="codex", session="cx-1", at="2026-09-28T11:00:00+00:00"))
    assert [decision.key for decision in view.decisions] == []
    (conflict,) = view.conflicts
    assert (conflict.kind, conflict.key, conflict.reason) == (
        "decision",
        "checkpoint-retention",
        "author_guidance_conflict",
    )
    assert sorted(definition.producer for definition in conflict.definitions) == ["claude:cl-1", "codex:cx-1"]
    "case: test_identical_definitions_from_two_producers_do_not_conflict"
    later = _record(_example(), host="codex", session="cx-1", at="2026-09-28T11:00:00+00:00")
    view = _view(_record(_example()), later)
    assert view.conflicts == ()
    assert [decision.record_id for decision in view.decisions] == [later.id]
    "case: test_an_explicit_supersedes_retires_the_producers_earlier_record"
    first = _record(_example(), at="2026-09-28T10:00:00+00:00")
    replacement = _example()
    replacement["human_decisions"] = []
    replacement["supersedes"] = [first.id]
    second = _record(replacement, at="2026-09-28T11:00:00+00:00")
    view = _view(first, second)
    assert view.decisions == ()
    assert (first.id, "decision", "checkpoint-retention", second.id, "explicit") in _sup(view)
    "case: test_supersedes_cannot_retire_another_producers_guidance"
    first = _record(_example(), at="2026-09-28T10:00:00+00:00")
    replacement = _example()
    replacement["human_decisions"] = []
    replacement["supersedes"] = [first.id]
    other = _record(replacement, host="codex", session="cx-1", at="2026-09-28T11:00:00+00:00")
    view = _view(first, other)
    assert [decision.record_id for decision in view.decisions] == [first.id]
    "case: test_unattributed_documents_replace_each_other_and_say_so"
    first = _record(_example(), host="", session="", at="2026-09-28T10:00:00+00:00")
    other = _example()
    other["human_decisions"][0]["selected_option"] = "ninety-days"
    second = _record(other, host="", session="", at="2026-09-28T11:00:00+00:00")
    view = _view(first, second)
    assert view.conflicts == ()
    (decision,) = view.decisions
    assert (decision.record_id, decision.producer) == (second.id, "unattributed")
    assert len(view.limitations) == 1 and "unattributed" in view.limitations[0]


def test_guidance_history_contract() -> None:
    """case: test_incompatible_stale_definitions_still_conflict"""
    other = _example()
    other["human_decisions"][0]["selected_option"] = "ninety-days"
    view = _view(
        _record(_example(), prints={_STORE: "fp-old"}),
        _record(other, host="codex", session="cx-1", at="2026-09-28T11:00:00+00:00", prints={_STORE: "fp-old"}),
    )
    assert view.decisions == ()
    (conflict,) = view.conflicts
    assert (conflict.key, conflict.stale) == ("checkpoint-retention", True)
    "case: test_a_definition_replaced_by_one_against_newer_source_is_kept_in_history"
    older = _record(_example(), prints={_STORE: "fp-old"})
    newer = _record(_example(), host="codex", session="cx-1", at="2026-09-28T11:00:00+00:00")
    view = _view(older, newer)
    assert [decision.record_id for decision in view.decisions] == [newer.id]
    assert (older.id, "decision", "checkpoint-retention", newer.id, "newer_source") in _sup(view)
    all_stale_newer = _record(
        _example(), host="codex", session="cx-1", at="2026-09-28T11:00:00+00:00", prints={_STORE: "fp-newer"}
    )
    view = _view(older, all_stale_newer)
    assert view.conflicts == ()
    assert [decision.record_id for decision in view.decisions] == [all_stale_newer.id]
    assert (older.id, "decision", "checkpoint-retention", all_stale_newer.id, "newer_source") in _sup(view)
    "case: test_reworded_items_from_two_producers_do_not_conflict"
    reworded = _example()
    reworded["items"][0]["behavior"]["after"] = "Every branch gets its own review context."
    first = _record(_example())
    second = _record(reworded, host="codex", session="cx-1", at="2026-09-28T11:00:00+00:00")
    view = _view(first, second)
    assert [conflict.key for conflict in view.conflicts if conflict.kind == "item"] == []
    assert [item.record_id for item in view.items] == [second.id]
    assert (first.id, "item", "branch-identity", second.id, "same_meaning") in _sup(view)
    retargeted = _example()
    retargeted["items"][0]["targets"] = [{"path": _STORE, "symbol": "prune_checkpoints"}]
    view = _view(first, _record(retargeted, host="codex", session="cx-1", at="2026-09-28T11:00:00+00:00"))
    assert [conflict.key for conflict in view.conflicts if conflict.kind == "item"] == ["branch-identity"]
    "case: test_an_unreadable_stored_row_is_skipped_and_disclosed"
    good = _record(_example())
    rows = [
        AuthorGuidanceRow(
            review_id="r",
            id=good.id,
            revision_id="rev-1",
            producer_host=good.producer_host,
            producer_session_id=good.producer_session_id,
            producer_model="",
            attribution=good.attribution,
            repo_root=good.repo_root,
            schema_version=1,
            source_identity_json=json.dumps([list(pair) for pair in good.source_identity]),
            payload_json=json.dumps(payload_to_dict(good.payload)),
            created_at=good.created_at,
        ),
        AuthorGuidanceRow(
            review_id="r",
            id="gdn-future",
            revision_id="rev-1",
            producer_host="codex",
            producer_session_id="cx-1",
            producer_model="",
            attribution="exact",
            repo_root="/repo",
            schema_version=2,
            source_identity_json="[]",
            payload_json='{"schema_version": 2}',
            created_at="2026-09-28T12:00:00+00:00",
        ),
    ]
    records, unreadable = load_guidance_records(rows)
    assert ([record.id for record in records], unreadable) == ([good.id], 1)
    view = build_guidance_view(records, targets=_TARGETS, units=_UNITS, packet=_PACKET, unreadable=unreadable)
    assert any("1 stored guidance record" in text for text in view.limitations)


def _mutated(path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    raw = copy.deepcopy(_example())
    node: Any = raw
    for step in path[:-1]:
        node = node[step]
    if value is _DELETE:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return raw


_DELETE = object()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src/review").mkdir(parents=True)
    (root / "src/review/session.py").write_text("def resolve_review():\n    return 1\n", encoding="utf-8")
    (root / "src/review/store.py").write_text("def prune_checkpoints():\n    return []\n", encoding="utf-8")
    return root


def test_recording_with_an_exact_session_writes_an_attributed_sidecar_only(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    store_root = tmp_path / "store"
    record = record_author_guidance(
        store_root, repo, host="codex", session_id="cx-1", model="gpt-x", guidance=_example()
    )
    assert record.attribution == "exact"
    assert (record.producer_host, record.producer_session_id, record.producer_model) == ("codex", "cx-1", "gpt-x")
    assert record.id.startswith("gdn-")
    paths = dict(record.source_identity)
    assert paths["src/review/session.py"] == file_fingerprint(
        "src/review/session.py", "def resolve_review():\n    return 1\n"
    )
    sidecars = list(store_root.glob("sessions/*/*/*/codex/cx-1/review_guidance.jsonl"))
    assert len(sidecars) == 1
    assert not (store_root / DB_NAME).exists(), "capturing guidance must not create or advance review state"
    again = record_author_guidance(
        store_root, repo, host="codex", session_id="cx-1", model="gpt-x", guidance=_example()
    )
    assert again.id == record.id
    assert len(sidecars[0].read_text(encoding="utf-8").splitlines()) == 1


def test_recording_without_identity_is_unattributed_not_invented(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    record = record_author_guidance(tmp_path / "store", repo, host="", session_id="", guidance=_example())
    assert record.attribution == "unattributed"
    assert (record.producer_host, record.producer_session_id) == ("", "")
    assert list((tmp_path / "store").glob("sessions/*/*/*/unattributed/*/review_guidance.jsonl"))


def test_a_referenced_path_that_does_not_exist_is_recorded_as_absent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "src/review/store.py").unlink()
    record = record_author_guidance(tmp_path / "store", repo, host="claude", session_id="s", guidance=_example())
    assert dict(record.source_identity)["src/review/store.py"] == ""


def test_an_impossible_range_is_rejected_at_capture(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    raw = _example()
    raw["items"][0]["targets"] = [{"path": "src/review/session.py", "start_line": 40, "end_line": 41}]
    with pytest.raises(GuidanceError, match="past the end"):
        record_author_guidance(tmp_path / "store", repo, host="claude", session_id="s", guidance=raw)


def _git_repo(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.invalid"
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    signature = pygit2.Signature("Fixture Tester", "fixture@example.invalid", 1700000000, 0)
    repo.create_commit("HEAD", signature, signature, "base", tree, [])
    (root / "src/review/session.py").write_text("def resolve_review():\n    return 2\n", encoding="utf-8")
    (root / "src/review/store.py").write_text("def prune_checkpoints():\n    return [1]\n", encoding="utf-8")
    return root


def _capture(store_root: Path, repo_root: Path) -> tuple[ReviewStore, Any, Any]:
    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, working_tree=True)
    build = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, repo_root, rng, title="guidance")
    revision = snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)
    return (store, session, revision)


def test_guidance_recorded_before_a_review_is_imported_on_capture(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    store_root = tmp_path / "store"
    record = record_author_guidance(store_root, repo, host="codex", session_id="cx-1", guidance=_example())
    assert not (store_root / DB_NAME).exists()
    store, session, revision = _capture(store_root, repo)
    rows = store.list_author_guidance(session.id)
    assert [(row.id, row.producer_host, row.producer_session_id, row.attribution) for row in rows] == [
        (record.id, "codex", "cx-1", "exact")
    ]
    assert rows[0].revision_id == revision.id
    assert record_from_row(rows[0]) == record
    assert revision.revision_number == 1
    _capture(store_root, repo)
    assert len(store.list_author_guidance(session.id)) == 1, "re-capture must be idempotent"


def test_two_agents_guidance_survives_independently(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    store_root = tmp_path / "store"
    first = _example()
    second = _example()
    second["items"][0]["key"] = "api-compatibility"
    second["suggested_order"] = ["api-compatibility"]
    second["human_decisions"] = []
    record_author_guidance(store_root, repo, host="claude", session_id="cl-1", guidance=first)
    record_author_guidance(store_root, repo, host="codex", session_id="cx-1", guidance=second)
    store, session, _ = _capture(store_root, repo)
    producers = sorted((row.producer_host, row.producer_session_id) for row in store.list_author_guidance(session.id))
    assert producers == [("claude", "cl-1"), ("codex", "cx-1")]


def test_guidance_about_other_content_is_not_bound_to_this_change(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    store_root = tmp_path / "store"
    record_author_guidance(store_root, repo, host="claude", session_id="cl-1", guidance=_example())
    (repo / "src/review/session.py").write_text("def resolve_review():\n    return 3\n", encoding="utf-8")
    (repo / "src/review/store.py").write_text("def prune_checkpoints():\n    return [3]\n", encoding="utf-8")
    store, session, _ = _capture(store_root, repo)
    assert store.list_author_guidance(session.id) == ()


def test_deleting_the_change_deletes_its_guidance(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    store_root = tmp_path / "store"
    record_author_guidance(store_root, repo, host="claude", session_id="cl-1", guidance=_example())
    store, session, _ = _capture(store_root, repo)
    assert store.list_author_guidance(session.id)
    with store._transaction() as conn:
        conn.execute("DELETE FROM review_sessions WHERE id = ?", (session.id,))
    assert store.list_author_guidance(session.id) == ()


_SESSION = "src/review/session.py"
_STORE = "src/review/store.py"
_PRINTS = {_SESSION: "fp-session", _STORE: "fp-store"}


def _target(
    target_id: str, path: str, symbol: str = "", start: int = 1, end: int = 5, side: str = "new"
) -> ReviewTarget:
    return ReviewTarget(
        target_id=target_id,
        unit_key=f"symbol:{path}:{symbol}",
        kind="symbol",
        path=path,
        label=symbol or path,
        symbol=symbol,
        start_line=start,
        end_line=end,
        hunk_ordinals=(0,),
        spans=(TargetSpan(side, start, end, 0),),
        state="unreviewed",
        changed_since_mark=False,
        reviewed_revision_id="",
        attention_rank=0,
        attention_level="normal",
        reasons=(),
        additions=1,
        deletions=0,
        fingerprint_method="exact",
    )


def _file_unit(path: str, fingerprint: str) -> ReviewUnit:
    return ReviewUnit(
        revision_id="rev-1", unit_key=f"file:{path}", kind="file", path=path, content_fingerprint=fingerprint
    )


_TARGETS = (
    _target("t-resolve", _SESSION, "resolve_review", 1, 2),
    _target("t-prune", _STORE, "prune_checkpoints", 1, 2),
)
_UNITS = tuple((_file_unit(path, fingerprint) for path, fingerprint in _PRINTS.items()))
_PACKET: dict[str, Any] = {
    "files": [
        {"path": _SESSION, "old_path": None, "status": "modified"},
        {"path": _STORE, "old_path": None, "status": "modified"},
    ]
}


def _record(
    raw: dict[str, Any],
    *,
    host: str = "claude",
    session: str = "cl-1",
    at: str = "2026-09-28T10:00:00+00:00",
    prints: dict[str, str] | None = None,
    record_id: str = "",
) -> AuthorGuidanceRecord:
    payload = parse_guidance(raw)
    captured = {**_PRINTS, **(prints or {})}
    exact = bool(host and session)
    return AuthorGuidanceRecord(
        id=record_id or f"gdn-{host or 'none'}-{session or 'none'}-{at[11:19]}",
        producer_host=host,
        producer_session_id=session,
        producer_model="",
        attribution="exact" if exact else "unattributed",
        repo_root="/repo",
        source_identity=tuple((path, captured.get(path, "")) for path in referenced_paths(payload)),
        payload=payload,
        created_at=at,
    )


def _view(*records: AuthorGuidanceRecord, targets: Any = _TARGETS, units: Any = _UNITS, packet: Any = _PACKET) -> Any:
    return build_guidance_view(records, targets=targets, units=units, packet=packet)


def _resolution(view: Any, kind: str, key: str) -> Any:
    definitions = view.items if kind == "item" else view.decisions
    (definition,) = [item for item in definitions if item.key == key]
    return definition.resolutions[0]


_API_PORT = 45997
_API_BASE = f"http://127.0.0.1:{_API_PORT}"
_API_SECRET = "guidance-api-test-secret"


def _mcp(monkeypatch: pytest.MonkeyPatch, repo: Path, store_root: Path, identity: tuple[str, str]) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(store_root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(repo))
    monkeypatch.setattr(mcp_server, "_resolved_host_session", lambda: identity)
    monkeypatch.setattr(mcp_server, "_get_mcp_model", lambda: "model-test")
    monkeypatch.setattr(mcp_server, "_session_worktree_root", lambda _root: None)


def test_the_guidance_tool_records_exact_guidance_for_any_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _git_repo(tmp_path)
    store_root = tmp_path / "store"
    _mcp(monkeypatch, repo, store_root, ("cx-1", "codex"))
    result = mcp_server.TOOLS["review_guidance"]["handler"]({"guidance": _example()})
    assert result["status"] == "recorded"
    assert (result["attribution"], result["items"], result["human_decisions"]) == ("exact", 1, 1)
    assert result["guidance_id"].startswith("gdn-")
    assert list(store_root.glob("sessions/*/*/*/codex/cx-1/review_guidance.jsonl"))
    assert not (store_root / DB_NAME).exists()


def test_the_guidance_tool_without_identity_records_unattributed_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    store_root = tmp_path / "store"
    _mcp(monkeypatch, repo, store_root, ("", ""))
    result = mcp_server.TOOLS["review_guidance"]["handler"]({"guidance": _example()})
    assert result["attribution"] == "unattributed"
    assert "attribution unavailable" in result["note"]


def test_rationale_accepts_any_host_with_an_exact_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _git_repo(tmp_path)
    _mcp(monkeypatch, repo, tmp_path / "store", ("cx-1", "codex"))
    result = mcp_server.TOOLS["review_rationale"]["handler"](
        {"entries": [{"path": "src/review/session.py", "body": "Branch scoped on purpose."}]}
    )
    assert (result["status"], result["session_id"]) == ("recorded", "cx-1")


@pytest.mark.parametrize("tool", ["review_rationale", "review_evidence"])
def test_author_capture_without_a_session_names_the_requirement(
    tool: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path)
    _mcp(monkeypatch, repo, tmp_path / "store", ("", ""))
    with pytest.raises(ValueError, match="requires an exact host session"):
        mcp_server.TOOLS[tool]["handler"]({"entries": [{"path": "src/review/session.py", "body": "x"}]})


def test_the_guidance_api_reports_items_conflicts_and_outcomes(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    store_root = tmp_path / "store"
    other = _example()
    other["human_decisions"][0]["selected_option"] = "ninety-days"
    record_author_guidance(store_root, repo, host="claude", session_id="cl-1", guidance=_example())
    record_author_guidance(store_root, repo, host="codex", session_id="cx-1", guidance=other)
    store, session, revision = _capture(store_root, repo)
    app = FastAPI()
    register_review_api(app, store, auth_dependency=make_token_dependency(_API_SECRET), repo_root=repo, port=_API_PORT)
    client = TestClient(app, base_url=_API_BASE)
    headers = {"Authorization": f"Bearer {_API_SECRET}"}
    payload = client.get(f"/api/reviews/{session.id}/guidance", headers=headers).json()
    assert (payload["revision_id"], payload["historical"]) == (revision.id, False)
    assert [conflict["key"] for conflict in payload["conflicts"]] == ["checkpoint-retention"]
    assert payload["decisions"] == []
    (item,) = payload["items"]
    (resolution,) = item["resolutions"]
    assert resolution["outcome"] == "resolved" and resolution["target_ids"]
    assert "repo_root" not in json.dumps(payload)
    historical = client.get(f"/api/reviews/{session.id}/revisions/0/guidance", headers=headers).json()
    assert historical["historical"] is True
    assert historical["conflicts"] == payload["conflicts"]
    assert client.get(f"/api/reviews/{session.id}/guidance").status_code == 403


def _sup(view: Any) -> set[tuple[str, str, str, str, str]]:
    return {(item.record_id, item.kind, item.key, item.by_record_id, item.reason) for item in view.superseded}


def test_re_recording_an_earlier_position_makes_it_current_again(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    store_root = tmp_path / "store"
    changed = _example()
    changed["human_decisions"][0]["selected_option"] = "ninety-days"
    first = record_author_guidance(store_root, repo, host="codex", session_id="cx-1", guidance=_example())
    second = record_author_guidance(store_root, repo, host="codex", session_id="cx-1", guidance=changed)
    third = record_author_guidance(store_root, repo, host="codex", session_id="cx-1", guidance=_example())
    assert third.id not in {first.id, second.id}
    assert third.created_at > second.created_at
    (sidecar,) = store_root.glob("sessions/*/*/*/codex/cx-1/review_guidance.jsonl")
    assert [record.id for record in _read_sidecar(sidecar)] == [first.id, second.id, third.id]
    again = record_author_guidance(store_root, repo, host="codex", session_id="cx-1", guidance=_example())
    assert again.id == third.id


def test_an_exact_session_imports_only_guidance_about_this_change(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    store_root = tmp_path / "store"
    store, session, revision = _capture(store_root, repo)
    other_change = _example()
    other_change["items"][0]["targets"] = [{"path": "src/feature_a.py"}]
    other_change["human_decisions"][0]["targets"] = [{"path": "src/feature_a.py"}]
    unrelated = record_author_guidance(store_root, repo, host="claude", session_id="cl-1", guidance=other_change)
    about_this = record_author_guidance(store_root, repo, host="claude", session_id="cl-1", guidance=_example())
    exact = replace(revision, provenance_certainty="exact", provenance_host="claude", provenance_session_id="cl-1")
    import_author_guidance(store, session, exact, store_root=store_root, repo_root=repo)
    ids = {row.id for row in store.list_author_guidance(session.id)}
    assert about_this.id in ids
    assert unrelated.id not in ids


def test_a_line_separator_in_guidance_text_survives_the_sidecar(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    store_root = tmp_path / "store"
    raw = _example()
    raw["intent"] = "Keep history\u2028isolated by branch."
    record = record_author_guidance(store_root, repo, host="codex", session_id="cx-1", guidance=raw)
    (sidecar,) = store_root.glob("sessions/*/*/*/codex/cx-1/review_guidance.jsonl")
    assert [item.id for item in _read_sidecar(sidecar)] == [record.id]
    record_author_guidance(store_root, repo, host="codex", session_id="cx-1", guidance=raw)
    assert len(_read_sidecar(sidecar)) == 1


def test_the_historical_guidance_view_shows_what_that_snapshot_had(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    store_root = tmp_path / "store"
    record_author_guidance(store_root, repo, host="claude", session_id="cl-1", guidance=_example())
    store, session, first = _capture(store_root, repo)
    (repo / "src/review/store.py").write_text("def prune_checkpoints():\n    return [9]\n", encoding="utf-8")
    changed = _example()
    changed["human_decisions"][0]["selected_option"] = "ninety-days"
    record_author_guidance(store_root, repo, host="claude", session_id="cl-1", guidance=changed)
    _, _, second = _capture(store_root, repo)
    assert second.id != first.id
    app = FastAPI()
    register_review_api(app, store, auth_dependency=make_token_dependency(_API_SECRET), repo_root=repo, port=_API_PORT)
    client = TestClient(app, base_url=_API_BASE)
    headers = {"Authorization": f"Bearer {_API_SECRET}"}
    current = client.get(f"/api/reviews/{session.id}/guidance", headers=headers).json()
    historical = client.get(f"/api/reviews/{session.id}/revisions/0/guidance", headers=headers).json()
    assert [item["decision"]["selected_option"] for item in current["decisions"]] == ["ninety-days"]
    assert [(item["decision"]["selected_option"], item["stale"]) for item in historical["decisions"]] == [
        ("indefinite", False)
    ]

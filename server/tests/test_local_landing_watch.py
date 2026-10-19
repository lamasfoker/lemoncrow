"""The running local server keeps armed GitHub landing intents moving."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_server_core.local_reviews import LocalReviewRepository

from lemoncrow.pro.capabilities.review.landing import landing_metadata, landing_relationship, record_landing_state
from lemoncrow.pro.capabilities.review.session_models import ReviewRelationship, ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.sources import github as github_source
from lemoncrow.pro.capabilities.review.store import new_revision_id, new_session_id

HEAD = "b" * 40


class _MergeableGitHub:
    """Every pull request is open, green and at ``HEAD``; records each ``gh`` call."""

    def __init__(self, *, failing: frozenset[str] = frozenset()) -> None:
        self.calls: list[list[str]] = []
        self.merged: set[str] = set()
        self.failing = failing

    def __call__(self, args: Sequence[str], stdin: str | None = None, **_: Any) -> str:
        call = list(args)
        self.calls.append(call)
        if call[:2] == ["pr", "merge"]:
            self.merged.add(call[2])
            return ""
        if call[:2] == ["api", "graphql"]:
            number = next(item.split("=", 1)[1] for item in call if item.startswith("number="))
            if number in self.failing:
                raise RuntimeError(f"unexpected crash for #{number}")
            pull = {
                "id": f"PR_{number}",
                "state": "MERGED" if number in self.merged else "OPEN",
                "isDraft": False,
                "headRefOid": HEAD,
                "mergeStateStatus": "CLEAN",
                "isInMergeQueue": False,
                "autoMergeRequest": None,
                "url": f"https://github.com/acme/widgets/pull/{number}",
            }
            repository = {
                "pullRequest": pull,
                "viewerDefaultMergeMethod": "MERGE",
                "mergeCommitAllowed": True,
                "squashMergeAllowed": False,
                "rebaseMergeAllowed": False,
            }
            return json.dumps({"data": {"repository": repository}})
        raise AssertionError(f"unexpected gh call: {call}")

    def merged_numbers(self) -> list[str]:
        return [call[2] for call in self.calls if call[:2] == ["pr", "merge"]]


def _change(
    repository: LocalReviewRepository, repo_id: str, source_ref: str, *, landing: str, repo_root: str = ""
) -> ReviewSession:
    store = repository.store(repo_id)
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="pull_request",
            repo_root=repo_root or f"/repo/{repo_id}",
            range_mode="commit_range",
            source_ref=source_ref,
        )
    )
    revision = store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=0,
            range_mode="commit_range",
            tree_fingerprint="tree-1",
            packet_schema_version=1,
            head_sha=HEAD,
        ),
        [],
    )
    store.upsert_relationship(
        ReviewRelationship(
            id="",
            review_id=session.id,
            kind="landing",
            relation="merge_when_ready",
            provider="github",
            external_id=source_ref,
            title="Merge when ready",
            status=landing,
            source="human",
            metadata_json=json.dumps({"revision_id": revision.id, "tree_fingerprint": "tree-1"}),
        )
    )
    return session


def _status(repository: LocalReviewRepository, repo_id: str, session: ReviewSession) -> str:
    landing = landing_relationship(repository.store(repo_id), session.id)
    assert landing is not None
    return landing.status


def test_the_watch_merges_only_armed_github_intents_across_repositories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github = _MergeableGitHub()
    monkeypatch.setattr(github_source, "run_gh", github)
    repository = LocalReviewRepository(tmp_path / "reviews")
    armed = _change(repository, "repo-a", "github:acme/widgets#7", landing="armed")
    cancelled = _change(repository, "repo-a", "github:acme/widgets#8", landing="cancelled")
    elsewhere = _change(repository, "repo-b", "github:acme/widgets#9", landing="armed")
    local_only = _change(repository, "repo-b", "named:refs/heads/main...refs/heads/topic", landing="armed")

    repository.reconcile_landings()

    assert sorted(github.merged_numbers()) == ["7", "9"]
    assert _status(repository, "repo-a", armed) == "merged"
    assert _status(repository, "repo-b", elsewhere) == "merged"
    assert _status(repository, "repo-a", cancelled) == "cancelled"
    assert _status(repository, "repo-b", local_only) == "armed"

    github.calls.clear()
    repository.reconcile_landings()

    assert github.calls == []  # settled intents are never polled again


def test_the_watch_runs_enabled_post_merge_rules_once_after_the_provider_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P12-AUTO item 7: a provider-observed merge triggers the operator's post-merge rules, once."""

    from lemoncrow.pro.capabilities.review.post_merge_actions import POST_MERGE_ROW_KIND

    root = tmp_path / "lemoncrow-root"
    root.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    rule = {
        "id": "notify-on-merge",
        "trigger": "merge_observed",
        "action": "post_merge.notification",
        "enabled": True,
        "enabled_by": "ops@acme",
        "acts_as": {"subject": "owner", "role": "owner"},
    }
    (root / "review-automation.json").write_text(json.dumps({"version": 1, "rules": [rule]}), encoding="utf-8")
    github = _MergeableGitHub()
    monkeypatch.setattr(github_source, "run_gh", github)
    repository = LocalReviewRepository(tmp_path / "reviews")
    armed = _change(repository, "repo-a", "github:acme/widgets#7", landing="armed")
    waiting = _change(repository, "repo-a", "github:acme/widgets#8", landing="cancelled")

    repository.reconcile_landings()
    repository.reconcile_landings()

    store = repository.store("repo-a")
    revision = store.latest_revision(armed.id)
    assert revision is not None
    rows = store.derived_results(POST_MERGE_ROW_KIND, [revision.id])
    records = [payload for (_stamp, payload) in rows.values()]
    assert [(item["kind"], item["rule_id"], item["status"]) for item in records] == [
        ("notification", "notify-on-merge", "completed")
    ]
    assert records[0]["readiness_unchanged"] is True
    events = store.list_activity_events(armed.id, kinds=("post_merge.action",))
    assert len(events) == 1 and json.loads(events[0].detail_json)["rule_id"] == "notify-on-merge"
    # Nothing merged, nothing runs.
    assert store.list_activity_events(waiting.id, kinds=("post_merge.action",)) == ()


def test_one_broken_intent_never_stops_the_watch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    github = _MergeableGitHub(failing=frozenset({"7"}))
    monkeypatch.setattr(github_source, "run_gh", github)
    repository = LocalReviewRepository(tmp_path / "reviews")
    broken = _change(repository, "repo-a", "github:acme/widgets#7", landing="armed")
    healthy = _change(repository, "repo-a", "github:acme/widgets#8", landing="armed")

    repository.reconcile_landings()

    assert github.merged_numbers() == ["8"]
    assert _status(repository, "repo-a", broken) == "armed"
    assert _status(repository, "repo-a", healthy) == "merged"


def test_the_watch_retries_a_cancel_still_owed_on_a_stale_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github = _MergeableGitHub()
    monkeypatch.setattr(github_source, "run_gh", github)
    repository = LocalReviewRepository(tmp_path / "reviews")
    stale = _change(repository, "repo-a", "github:acme/widgets#7", landing="stale")
    store = repository.store("repo-a")
    landing = landing_relationship(store, stale.id)
    assert landing is not None
    record_landing_state(store, landing, status="stale", metadata_updates={"provider_cancel": "pending"})

    repository.reconcile_landings()

    assert any(call[:2] == ["api", "graphql"] for call in github.calls)
    assert github.merged_numbers() == []
    assert _status(repository, "repo-a", stale) == "stale"
    settled = landing_relationship(store, stale.id)
    assert settled is not None and landing_metadata(settled)["provider_cancel"] == "done"

    github.calls.clear()
    repository.reconcile_landings()

    assert github.calls == []


def test_the_local_inbox_keeps_an_owed_provider_cancellation_visible_on_an_archived_review(tmp_path: Path) -> None:
    """P12-AUTO item 12: a discard's owed provider cancellation stays visible until the provider confirms."""

    import asyncio

    from aiohttp import ClientSession
    from lemoncrow_server_core.bootstrap import build_local_server
    from lemoncrow_server_core.config import LocalServerConfig

    async def _exercise() -> None:
        server, state = build_local_server(
            LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=False)
        )
        repository = state.review_repository
        owed = _change(repository, "repo-a", "github:acme/widgets#7", landing="delegated")
        settled = _change(repository, "repo-a", "github:acme/widgets#8", landing="cancelled")
        store = repository.store("repo-a")
        for session in (owed, settled):
            store.update_session(session.id, status="archived")
        landing = landing_relationship(store, owed.id)
        assert landing is not None
        record_landing_state(
            store,
            landing,
            status="delegated",
            metadata_updates={"provider_cancel": "pending", "detail": "Cancellation FAILED: GitHub is unavailable"},
        )
        url = await server.start()
        try:
            async with ClientSession() as client:
                response = await client.get(f"{url}/v1/reviews?status=archived")
                assert response.status == 200, await response.text()
                rows = {row["id"]: row for row in (await response.json())["reviews"]}
        finally:
            await server.stop()
        assert rows[owed.id]["owed_cancellation"]["provider"] == "github"
        assert "GitHub is unavailable" in rows[owed.id]["owed_cancellation"]["detail"]
        assert "owed_cancellation" not in rows[settled.id]

    asyncio.run(_exercise())


def test_the_local_server_maintenance_pass_runs_the_landing_watch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow_server_core.bootstrap import build_local_server
    from lemoncrow_server_core.config import LocalServerConfig
    from lemoncrow_server_core.local_maintenance import LocalMaintenance

    watched: list[Path] = []
    monkeypatch.setattr(LocalReviewRepository, "reconcile_landings", lambda self: watched.append(self.root))
    monkeypatch.setattr(LocalMaintenance, "_maybe_import_sessions", lambda self: None)
    _server, state = build_local_server(
        LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=False)
    )
    try:
        state.maintenance.run_once()
    finally:
        state.index_worker.close()

    assert watched == [state.review_repository.root]


def test_a_readiness_rules_change_is_revalidated_through_the_reader_never_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow_server_core.local_reviews import LocalReviewReader

    from lemoncrow.pro.capabilities.review import readiness as readiness_module
    from lemoncrow.pro.capabilities.review.readiness import built_in_policy_layer, policy_generation

    github = _MergeableGitHub()
    monkeypatch.setattr(github_source, "run_gh", github)
    repository = LocalReviewRepository(tmp_path / "reviews")
    armed = _change(repository, "repo-a", "github:acme/widgets#7", landing="armed")
    store = repository.store("repo-a")
    landing = landing_relationship(store, armed.id)
    assert landing is not None
    layers = [built_in_policy_layer()]
    record_landing_state(
        store,
        landing,
        status="armed",
        metadata_updates={
            "policy_generation": policy_generation(layers).digest,
            "policy_layers": [layer.to_wire() for layer in layers],
        },
    )
    monkeypatch.setattr(readiness_module, "READINESS_RULES_VERSION", 2)  # LemonCrow upgraded its rules
    handed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        LocalReviewReader, "revalidate_landing", lambda self, repo_id, review_id: handed.append((repo_id, review_id))
    )
    LocalReviewReader(repository, listen_port=0)  # the Reader composed over this repository wires itself in

    repository.reconcile_landings()

    assert handed == [("repo-a", armed.id)]
    assert github.merged_numbers() == []
    assert _status(repository, "repo-a", armed) == "armed"


# --------------------------------------------------------------------------- #
# P10-SIGN: the review record of a merge the watch observes
# --------------------------------------------------------------------------- #


def _watched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, key: bool) -> tuple[Any, Any, Path | None]:
    from lemoncrow_server_core.local_reviews import LocalReviewReader

    from lemoncrow.core.settings import write_setting
    from lemoncrow.pro.capabilities.review.record_signing import USER_KEY_SETTING, generate_user_key

    settings_root = tmp_path / "lemoncrow-root"
    settings_root.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(settings_root))
    key_path = None
    if key:
        key_path = tmp_path / "keys" / "user-key.pem"  # outside any repository
        generate_user_key(key_path)
        write_setting(settings_root, USER_KEY_SETTING, str(key_path))
    monkeypatch.setattr(github_source, "run_gh", _MergeableGitHub())
    repository = LocalReviewRepository(tmp_path / "reviews")
    reader = LocalReviewReader(repository, listen_port=0)
    return repository, reader, key_path


def test_an_observed_merge_emits_one_record_signed_with_the_local_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.record_signing import (
        key_set_document,
        list_records,
        load_user_key,
        public_key_entry,
        user_key_id,
    )
    from lemoncrow.pro.capabilities.review.record_verify import parse_key_set, verify_record

    repository, reader, key_path = _watched(tmp_path, monkeypatch, key=True)
    assert key_path is not None
    armed = _change(repository, "repo-a", "github:acme/widgets#7", landing="armed", repo_root="local:repo-a")
    store = repository.store("repo-a")

    repository.reconcile_landings()

    assert _status(repository, "repo-a", armed) == "merged"
    [record] = list_records(store, armed.id)
    predicate = record.statement["predicate"]
    assert record.signing_state == "signed" and record.signing_mode == "user_key"
    assert predicate["exported_by"] == {"id": "lemoncrow-merge-watch", "type": "system"}
    assert predicate["decision"]["kind"] == "provider_merge" and predicate["decision"]["outcome"] == "merged"
    public = load_user_key(key_path).public_key()
    kid = user_key_id(public)
    keys = parse_key_set(key_set_document([public_key_entry(public, key_id=kid, issuer=kid)], issuer=kid))
    assert verify_record(record.envelope, keys=keys).status == "verified"

    # Re-observing the same merge (a second watcher, a retried pass) never emits again.
    assert reader.record_merge("repo-a", armed.id) == {"status": "already_recorded", "record_id": record.record_id}
    repository.reconcile_landings()
    assert [item.record_id for item in list_records(store, armed.id)] == [record.record_id]
    assert [event.kind for event in store.list_activity_events(armed.id, kinds=("record.exported",))] == [
        "record.exported"
    ]


def test_without_a_local_key_an_observed_merge_emits_nothing_and_notes_the_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.record_signing import list_records

    repository, reader, _key = _watched(tmp_path, monkeypatch, key=False)
    armed = _change(repository, "repo-a", "github:acme/widgets#7", landing="armed", repo_root="local:repo-a")
    store = repository.store("repo-a")

    repository.reconcile_landings()

    assert _status(repository, "repo-a", armed) == "merged"
    assert list_records(store, armed.id) == ()  # never an automatic unsigned record
    [noted] = store.list_activity_events(armed.id, kinds=("record.not_emitted",))
    assert json.loads(noted.detail_json) == {"code": "not_configured", "trigger": "provider_merge"}
    assert "No review record was emitted for the merge" in noted.summary

    assert reader.record_merge("repo-a", armed.id) == {"status": "already_noted"}
    assert len(store.list_activity_events(armed.id, kinds=("record.not_emitted",))) == 1
    assert list_records(store, armed.id) == ()


def test_a_merge_record_needs_a_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review.record_signing import list_records

    repository, reader, _key = _watched(tmp_path, monkeypatch, key=True)
    undecided = _change(repository, "repo-a", "github:acme/widgets#7", landing="cancelled", repo_root="local:repo-a")
    store = repository.store("repo-a")

    assert reader.record_merge("repo-a", undecided.id) == {"status": "no_decision"}
    assert list_records(store, undecided.id) == ()
    assert store.list_activity_events(undecided.id, kinds=("record.exported", "record.not_emitted")) == ()

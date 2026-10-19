"""Local change sets span this machine's repositories (Phase 9R item 42)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp import web
from fastapi.testclient import TestClient
from lemoncrow_server_core.local_reviews import LocalReviewReader, LocalReviewRepository, _ResolvedReader

from lemoncrow.pro.capabilities.review.change_sets import CHANGE_SET_KIND
from lemoncrow.pro.capabilities.review.session_models import ReviewRelationship, ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.store import new_revision_id, new_session_id


def _change(repository: LocalReviewRepository, repo_id: str) -> ReviewSession:
    store = repository.store(repo_id)
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="pull_request",
            repo_root=f"local:{repo_id}",
            range_mode="commit_range",
            source_ref=f"named:{repo_id}",
            title=f"{repo_id} change",
        )
    )
    store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=0,
            range_mode="commit_range",
            tree_fingerprint="tree-1",
            packet_schema_version=1,
            head_sha="b" * 40,
        ),
        [],
    )
    return session


def _client(reader: LocalReviewReader, repository: LocalReviewRepository, repo_id: str, review_id: str) -> TestClient:
    return TestClient(reader._app(_ResolvedReader(repo_id, repository.store(repo_id), review_id)))


def test_a_local_change_set_links_reviews_in_two_repositories(tmp_path: Path) -> None:
    repository = LocalReviewRepository(tmp_path / "reviews")
    api, web_change = _change(repository, "repo-api"), _change(repository, "repo-web")
    reader = LocalReviewReader(repository, listen_port=7420)
    client = _client(reader, repository, "repo-web", web_change.id)

    response = client.post(
        f"/api/reviews/{web_change.id}/change-set", json={"repository": "repo-api", "review": api.id}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["linked"] is True
    assert {(row["repository"], row["review_id"]) for row in payload["members"]} == {
        ("repo-api", api.id),
        ("repo-web", web_change.id),
    }
    for repo_id, session, other in (("repo-web", web_change, api), ("repo-api", api, web_change)):
        rows = repository.store(repo_id).list_relationships(session.id, kind=CHANGE_SET_KIND)
        assert [json.loads(row.metadata_json)["review_id"] for row in rows] == [other.id]

    # The other repository's Reader shows the same set and carries the set blocker.
    other = _client(reader, repository, "repo-api", api.id)
    readiness = other.get(f"/api/reviews/{api.id}/readiness").json()
    rows = {row["id"]: row for row in readiness["conditions"] if row["category"] == "change_set"}
    assert rows["change-set:members"]["state"] == "pending"
    assert f"r/{web_change.id} in repo-web" in rows["change-set:members"]["detail"]
    assert readiness["ready"] is False

    # A Change in the same repository is never a set member (that is a stack).
    same = _change(repository, "repo-web")
    refused = client.post(
        f"/api/reviews/{web_change.id}/change-set", json={"repository": "repo-web", "review": same.id}
    )
    assert refused.status_code == 422


def test_a_cached_read_of_a_set_member_misses_after_another_member_store_changes(tmp_path: Path) -> None:
    repository = LocalReviewRepository(tmp_path / "reviews")
    api, web_change = _change(repository, "repo-api"), _change(repository, "repo-web")
    reader = LocalReviewReader(repository, listen_port=7420)
    web_store, api_store = repository.store("repo-web"), repository.store("repo-api")
    app = reader._app(_ResolvedReader("repo-web", web_store, web_change.id))
    path = f"/api/reviews/{web_change.id}/readiness"
    computed: list[int] = []

    async def compute() -> web.StreamResponse:
        computed.append(1)
        return web.Response(status=200, body=b"{}")

    def read() -> None:
        asyncio.run(reader._cached_get(app, web_store, path, "", compute))

    def touch_api() -> None:
        api_store.upsert_relationship(
            ReviewRelationship(
                id="", review_id=api.id, kind="work_item", relation="relates_to", external_id=f"T-{len(computed)}"
            )
        )

    # Not in a set: another repository's writes do not touch this Review's cached reads.
    read()
    touch_api()
    read()
    assert len(computed) == 1

    client = TestClient(app)
    linked = client.post(f"/api/reviews/{web_change.id}/change-set", json={"repository": "repo-api", "review": api.id})
    assert linked.status_code == 200, linked.text
    read()
    read()
    assert len(computed) == 2
    # In a set: the member's readiness reads the other store, so its write must miss the cache.
    touch_api()
    read()
    assert len(computed) == 3


def test_a_local_set_member_is_replaced_then_unlinked_and_cached_reads_follow(tmp_path: Path) -> None:
    repository = LocalReviewRepository(tmp_path / "reviews")
    first, web_change = _change(repository, "repo-api"), _change(repository, "repo-web")
    second = _change(repository, "repo-api")
    reader = LocalReviewReader(repository, listen_port=7420)
    client = _client(reader, repository, "repo-web", web_change.id)
    web_store, api_store = repository.store("repo-web"), repository.store("repo-api")

    def members(payload: dict[str, object]) -> set[str]:
        rows = payload["members"]
        assert isinstance(rows, list)
        return {str(row["review_id"]) for row in rows}

    linked = client.post(
        f"/api/reviews/{web_change.id}/change-set", json={"repository": "repo-api", "review": first.id}
    )
    assert linked.status_code == 200, linked.text
    assert members(linked.json()) == {first.id, web_change.id}

    # Update: the web Change's api member becomes the second api Change.
    replaced = client.post(
        f"/api/reviews/{web_change.id}/change-set/replace", json={"repository": "repo-api", "review": second.id}
    )
    assert replaced.status_code == 200, replaced.text
    assert members(replaced.json()) == {second.id, web_change.id}
    assert api_store.list_relationships(first.id, kind=CHANGE_SET_KIND) == ()
    assert len(api_store.list_relationships(second.id, kind=CHANGE_SET_KIND)) == 1
    first_reader = _client(reader, repository, "repo-api", first.id)
    readiness = first_reader.get(f"/api/reviews/{first.id}/readiness").json()
    assert [row for row in readiness["conditions"] if row["category"] == "change_set"] == []

    # Unlink: no rows on either side, and a cached read of the web Change misses.
    before = reader._read_stamp(web_store)
    unlinked = client.post(
        f"/api/reviews/{web_change.id}/change-set/unlink", json={"repository": "repo-api", "review": second.id}
    )
    assert unlinked.status_code == 200, unlinked.text
    assert unlinked.json()["linked"] is False
    assert web_store.list_relationships(web_change.id, kind=CHANGE_SET_KIND) == ()
    assert api_store.list_relationships(second.id, kind=CHANGE_SET_KIND) == ()
    assert reader._read_stamp(web_store) != before

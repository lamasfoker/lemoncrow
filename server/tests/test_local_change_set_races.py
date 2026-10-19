"""Concurrent change-set writes through the local Reader routes, one OS process per request.

Every actor is a separate process with its own Reader over the same on-disk
SQLite stores. The first request pauses inside its writes (after taking the
change-set write lock) until the others have started their own requests and
had time to reach the lock; then it finishes. Whatever the interleaving, a
refused request changed nothing, an accepted one is never undone by another,
and a set never holds two Changes of one repository unless a process died
mid-update -- which the set then shows as ``blocked``.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from lemoncrow_server_core.local_reviews import LocalReviewReader, LocalReviewRepository, _ResolvedReader

from lemoncrow.pro.capabilities.review.change_sets import CHANGE_SET_KIND
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.store import new_revision_id, new_session_id

_WAIT_S = 60.0


def _wait(path: Path) -> None:
    deadline = time.monotonic() + _WAIT_S
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(str(path))
        time.sleep(0.01)


def _world(root: Path) -> tuple[LocalReviewRepository, LocalReviewReader]:
    repository = LocalReviewRepository(root / "reviews")
    return repository, LocalReviewReader(repository, listen_port=7420)


def _client(reader: LocalReviewReader, repository: LocalReviewRepository, repo_id: str, review_id: str) -> TestClient:
    return TestClient(reader._app(_ResolvedReader(repo_id, repository.store(repo_id), review_id)))


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


_PATH = {"link": "", "unlink": "/unlink", "replace": "/replace"}


def _actor(root: str, name: str, operation: str, web_id: str, review: str, role: dict[str, Any]) -> None:
    """One request in its own process. ``role``: ``hold`` (pause mid-write) or ``follow`` (start while held)."""

    from lemoncrow.pro.capabilities.review import change_sets as cs
    from lemoncrow.pro.capabilities.review.store import ReviewStore

    base = Path(root)
    if role.get("follow"):
        _wait(base / "holding")
        target = cs._target

        def announced(*args: Any) -> Any:
            (base / f"{name}.started").write_text("1")  # before the lock: the holder has it
            return target(*args)

        cs._target = announced  # type: ignore[assignment]
    if role.get("hold"):
        upsert = ReviewStore.upsert_relationship
        writes = {"count": 0}

        def held_upsert(self: Any, relationship: Any) -> Any:
            if relationship.kind != CHANGE_SET_KIND:
                return upsert(self, relationship)
            if writes["count"] == 0:
                (base / "holding").write_text("1")
                for other in role.get("followers", ()):
                    _wait(base / f"{other}.started")
                time.sleep(0.5)  # the followers are now blocked on the write lock (or, unlocked, racing)
            out = upsert(self, relationship)
            writes["count"] += 1
            if writes["count"] == role.get("crash_after"):
                os._exit(3)  # dies mid-request: no undo, no response
            return out

        ReviewStore.upsert_relationship = held_upsert  # type: ignore[method-assign]
    repository, reader = _world(base)
    response = _client(reader, repository, "repo-web", web_id).post(
        f"/api/reviews/{web_id}/change-set{_PATH[operation]}", json={"repository": "repo-api", "review": review}
    )
    (base / f"{name}.result").write_text(json.dumps({"status": response.status_code, "body": response.text[:300]}))


def _run(root: Path, web_id: str, actors: list[tuple[str, str, str, dict[str, Any]]]) -> dict[str, int]:
    context = mp.get_context("spawn")
    processes = [
        context.Process(target=_actor, args=(str(root), name, operation, web_id, review, role), name=name)
        for name, operation, review, role in actors
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(_WAIT_S + 30)
        assert process.exitcode is not None, f"{process.name} hung"
    return {
        name: json.loads((root / f"{name}.result").read_text())["status"]
        for name, *_rest in actors
        if (root / f"{name}.result").exists()
    }


def _rows(repository: LocalReviewRepository, repo_id: str, review_id: str) -> list[str]:
    rows = repository.store(repo_id).list_relationships(review_id, kind=CHANGE_SET_KIND)
    return sorted(json.loads(row.metadata_json)["review_id"] for row in rows)


def _setup(tmp_path: Path, apis: int) -> tuple[LocalReviewRepository, LocalReviewReader, str, list[str]]:
    repository, reader = _world(tmp_path)
    web = _change(repository, "repo-web")
    return repository, reader, web.id, [_change(repository, "repo-api").id for _ in range(apis)]


def _link(
    reader: LocalReviewReader, repository: LocalReviewRepository, web_id: str, api_id: str, path: str = ""
) -> Any:
    return _client(reader, repository, "repo-web", web_id).post(
        f"/api/reviews/{web_id}/change-set{path}", json={"repository": "repo-api", "review": api_id}
    )


def test_two_concurrent_updates_both_apply_in_turn_and_leave_one_api_change(tmp_path: Path) -> None:
    repository, reader, web, (api1, api2, api3) = _setup(tmp_path, 3)
    assert _link(reader, repository, web, api1).status_code == 200
    statuses = _run(
        tmp_path,
        web,
        [
            ("first", "replace", api2, {"hold": True, "followers": ["second"]}),
            ("second", "replace", api3, {"follow": True}),
        ],
    )
    assert statuses == {"first": 200, "second": 200}
    # Applied one after the other: the second names the set's api Change, and the old one is not resurrected.
    assert _rows(repository, "repo-web", web) == [api3]
    assert _rows(repository, "repo-api", api3) == [web]
    assert _rows(repository, "repo-api", api1) == [] and _rows(repository, "repo-api", api2) == []


def test_a_duplicate_link_and_a_conflicting_link_racing_one_link(tmp_path: Path) -> None:
    repository, _reader, web, (api1, api2) = _setup(tmp_path, 2)
    statuses = _run(
        tmp_path,
        web,
        [
            ("first", "link", api1, {"hold": True, "followers": ["duplicate", "conflict"]}),
            ("duplicate", "link", api1, {"follow": True}),
            ("conflict", "link", api2, {"follow": True}),
        ],
    )
    # The duplicate finds the link made (nothing to write); the conflict is refused and wrote nothing.
    assert statuses == {"first": 200, "duplicate": 200, "conflict": 422}
    assert _rows(repository, "repo-web", web) == [api1]
    assert _rows(repository, "repo-api", api1) == [web] and _rows(repository, "repo-api", api2) == []


def test_two_concurrent_links_of_one_repository_admit_exactly_one(tmp_path: Path) -> None:
    repository, _reader, web, (api1, api2) = _setup(tmp_path, 2)
    statuses = _run(
        tmp_path,
        web,
        [
            ("first", "link", api1, {"hold": True, "followers": ["second"]}),
            ("second", "link", api2, {"follow": True}),
        ],
    )
    assert statuses == {"first": 200, "second": 422}
    assert _rows(repository, "repo-web", web) == [api1]
    assert _rows(repository, "repo-api", api1) == [web] and _rows(repository, "repo-api", api2) == []


def test_an_update_that_dies_half_way_leaves_a_blocked_set_and_the_lock_free(tmp_path: Path) -> None:
    repository, reader, web, (api1, api2) = _setup(tmp_path, 2)
    assert _link(reader, repository, web, api1).status_code == 200
    # Dies after writing the new pair, before removing the old one; a request waiting on the lock meanwhile.
    statuses = _run(
        tmp_path,
        web,
        [
            ("crashed", "replace", api2, {"hold": True, "followers": ["waiting"], "crash_after": 2}),
            ("waiting", "link", api1, {"follow": True}),
        ],
    )
    # The waiting request got the lock once the holder died (its link already held: nothing to write).
    assert statuses == {"waiting": 200}
    assert _rows(repository, "repo-web", web) == sorted([api1, api2])

    client = _client(reader, repository, "repo-web", web)
    view = client.get(f"/api/reviews/{web}/change-set").json()
    assert (view["state"], view["duplicate_repositories"]) == ("blocked", ["repo-api"])
    for review_id, repo_id in ((web, "repo-web"), (api2, "repo-api")):
        readiness = _client(reader, repository, repo_id, review_id).get(f"/api/reviews/{review_id}/readiness").json()
        rows = {row["id"]: row for row in readiness["conditions"] if row["category"] == "change_set"}
        assert rows["change-set:members"]["state"] == "fail"
        assert rows["change-set:members"]["reason"] == "set_duplicate_repository"
        assert "Unlink" in rows["change-set:members"]["detail"]
        assert readiness["ready"] is False

    # Unlinking the old Change repairs it.
    assert _link(reader, repository, web, api1, "/unlink").status_code == 200
    assert _rows(repository, "repo-web", web) == [api2] and _rows(repository, "repo-api", api1) == []
    assert client.get(f"/api/reviews/{web}/change-set").json()["duplicate_repositories"] == []
    readiness = client.get(f"/api/reviews/{web}/readiness").json()
    reasons = {row["reason"] for row in readiness["conditions"] if row["category"] == "change_set"}
    assert "set_duplicate_repository" not in reasons

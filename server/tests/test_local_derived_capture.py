"""The local engine captures with its own Review client: the packet is derived from the client's Views.

``lcr`` freezes the base and head of a change, syncs each as an immutable View and
asks the server to derive the Review packet (``derive_packet``). The loopback
server runs the same derivation the hosted server does
(:mod:`lemoncrow_server_core.review_derivation`), over Views its one principal
owns, and refuses everything else typed, before anything is captured.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_client.config import ClientConfig
from lemoncrow_client.errors import AgentAction, ClientError
from lemoncrow_client.errors import ErrorCode as ClientErrorCode
from lemoncrow_client.review import _capture, _freeze_range, _gitlinks_for_range, resolve_review_range
from lemoncrow_client.review_session import ReviewSession
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig
from lemoncrow_server_core.context import Principal
from lemoncrow_server_core.errors import ErrorCode, ServerError
from lemoncrow_server_core.local_reviews import LocalReviewRepository

from lemoncrow.pro.capabilities.review.hosted import server_review_request

_OLD_ANSWER = "def answer():\n    return 41\n"
_NEW_ANSWER = "def answer():\n    return 42\n"
_GONE = "retired = True\n"
_ADDED = "fresh = True\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    """A committed base and a working-tree change that modifies, deletes and adds a file."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text(_OLD_ANSWER, encoding="utf-8")
    (repo / "gone.py").write_text(_GONE, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    (repo / "app.py").write_text(_NEW_ANSWER, encoding="utf-8")
    (repo / "gone.py").unlink()
    (repo / "new.py").write_text(_ADDED, encoding="utf-8")
    return repo


def _config(url: str, repo: Path, tmp_path: Path) -> ClientConfig:
    return ClientConfig(
        url=url,
        token="",
        repo_root=repo,
        state_dir=tmp_path / "client",
        install_mode="local",
        startup_budget_s=10.0,
        request_timeout_s=10.0,
        tool_timeout_s=60.0,
        resolution_cache_bytes=0,
        offer_local_fs=False,
        token_source="test",
    )


def _serve(tmp_path: Path, flow: Callable[[str, Any, Path], Any]) -> Any:
    async def run() -> Any:
        server, state = build_local_server(
            LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=False)
        )
        url = await server.start()
        try:
            return await asyncio.to_thread(flow, url, state, _repo(tmp_path))
        finally:
            await server.stop()

    return asyncio.run(run())


@contextmanager
def _synced_views(config: ClientConfig, tmp_path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """Sync the working-tree change's base and head Views as ``lcr`` does; yield its repo and capture body."""

    rng = resolve_review_range(config.repo_root, None, working_tree=True)
    frozen = tmp_path / "frozen"
    _freeze_range(config.repo_root, rng, frozen / "base", frozen / "head")
    remote = ReviewSession(config)
    try:
        remote.handshake(timeout_s=10.0)
        remote.open_session(timeout_s=10.0)
        base = remote.open_snapshot(frozen / "base", source_revision=rng.base_sha, timeout_s=60.0)
        head = remote.open_snapshot(frozen / "head", source_revision=rng.base_sha, timeout_s=60.0)
        yield str(head["repo_id"]), _capture_body(config, rng, base, head)
    finally:
        remote.close()


def _capture_body(config: ClientConfig, rng: Any, base: Mapping[str, Any], head: Mapping[str, Any]) -> dict[str, Any]:
    """The body ``ReviewSession.capture_from_views`` posts for these Views."""

    base_gitlinks, head_gitlinks = _gitlinks_for_range(config.repo_root, rng)
    body = {
        "view_id": head["view_id"],
        "view_revision": int(head["view_revision"]),
        "source_ref": rng.source_ref(config.repo_root),
        "title": rng.title,
        "restore_archived": False,
        "derive_packet": True,
        "with_impact": False,
        "range": rng.to_wire(),
        "gitlinks": {"base": base_gitlinks, "head": head_gitlinks},
        "base_view_id": base["view_id"],
        "base_view_revision": int(base["view_revision"]),
    }
    return body


def _post(url: str, path: str, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, dict(json.loads(response.read()))
    except urllib.error.HTTPError as exc:
        return exc.code, dict(json.loads(exc.read()))


def _reviews(url: str) -> list[Any]:
    with urllib.request.urlopen(url + "/v1/reviews", timeout=60) as response:
        return list(json.loads(response.read())["reviews"])


def test_lcr_captures_by_deriving_the_packet_from_its_own_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: dict[str, Any] = {}
    capture = LocalReviewRepository.capture

    def observed(self: LocalReviewRepository, **kwargs: Any) -> Any:
        recorded.update(kwargs)
        return capture(self, **kwargs)

    monkeypatch.setattr(LocalReviewRepository, "capture", observed)

    def flow(url: str, state: Any, repo: Path) -> None:
        config = _config(url, repo, tmp_path)
        response, link = _capture(
            config, resolve_review_range(repo, None, working_tree=True), with_impact=False, restore_archived=False
        )

        review = response["review"]
        assert link == f"{url}{review['review_path']}"
        repo_id = str(review["repo_id"])
        # The packet was derived on the server from the two Views' bytes.
        packet = recorded["packet"]
        assert packet.repo_root == f"local:{repo_id}"
        assert packet.range_mode == "working_tree"
        described = resolve_review_range(repo, None, working_tree=True)
        assert packet.base_sha == described.base_sha
        # The client's descriptive range title reaches the derived packet.
        assert packet.title == described.title != ""
        assert {item.path: item.status for item in packet.files} == {
            "app.py": "modified",
            "gone.py": "deleted",
            "new.py": "added",
        }
        [modified] = [item for item in packet.files if item.path == "app.py"]
        patch = "".join(hunk.patch for hunk in modified.hunks)
        assert "-    return 41" in patch and "+    return 42" in patch
        # Both sides of the change are recorded, read from the Views.
        assert recorded["old_blobs"] == {"app.py": _OLD_ANSWER, "gone.py": _GONE}
        assert recorded["new_blobs"] == {"app.py": _NEW_ANSWER, "new.py": _ADDED}
        assert set(recorded["old_source_tree"]) == {"app.py", "gone.py"}
        assert set(recorded["source_tree"]) == {"app.py", "new.py"}
        # The client's options reach the derivation: lcr ran with --no-impact.
        assert "impact_disabled" in packet.degraded

        # The Review opens with what was derived.
        overview = server_review_request(config, "GET", f"/api/reviews/{review['id']}")
        assert overview["stats"]["files"] == 3
        assert (overview["stats"]["additions"], overview["stats"]["deletions"]) == (2, 2)
        assert [str(item["id"]) for item in _reviews(url)] == [str(review["id"])]
        # Each derivation removes its scratch repository.
        assert list((tmp_path / "state" / "review-derive").iterdir()) == []

    _serve(tmp_path, flow)


#: (case, change to lcr's capture body, expected code, expected ``details.field``)
_REFUSED: tuple[tuple[str, Callable[[dict[str, Any]], None], ErrorCode, str | None], ...] = (
    (
        "derive flag not a boolean",
        lambda body: body.update(derive_packet="true"),
        ErrorCode.PAYLOAD_INVALID,
        "derive_packet",
    ),
    (
        "client packet alongside",
        lambda body: body.update(packet={"files": []}),
        ErrorCode.PAYLOAD_INVALID,
        "derive_packet",
    ),
    (
        "client blobs alongside",
        lambda body: body.update(new_blobs={"app.py": "x"}),
        ErrorCode.PAYLOAD_INVALID,
        "derive_packet",
    ),
    ("range missing", lambda body: body.pop("range"), ErrorCode.PAYLOAD_INVALID, "range.mode"),
    (
        "range mode unknown",
        lambda body: body["range"].update(mode="elsewhere"),
        ErrorCode.PAYLOAD_INVALID,
        "range.mode",
    ),
    (
        "range title too long",
        lambda body: body["range"].update(title="t" * 2000),
        ErrorCode.PAYLOAD_INVALID,
        "range.title",
    ),
    (
        "gitlink not an object id",
        lambda body: body.update(gitlinks={"base": {"deps/sub": "not-an-oid"}, "head": {}}),
        ErrorCode.PAYLOAD_INVALID,
        "gitlinks.base",
    ),
    (
        "impact flag not a boolean",
        lambda body: body.update(with_impact="yes"),
        ErrorCode.PAYLOAD_INVALID,
        "with_impact",
    ),
    (
        "base revision without a base view",
        lambda body: body.update(base_view_id="", base_view_revision=0),
        ErrorCode.PAYLOAD_INVALID,
        "base_view_id",
    ),
    ("unknown head view", lambda body: body.update(view_id="view_never_opened"), ErrorCode.VIEW_UNKNOWN, None),
    ("unknown base view", lambda body: body.update(base_view_id="view_never_opened"), ErrorCode.VIEW_UNKNOWN, None),
    (
        "stale head view",
        lambda body: body.update(view_revision=body["view_revision"] + 1),
        ErrorCode.VIEW_REVISION_STALE,
        None,
    ),
    (
        "stale base view",
        lambda body: body.update(base_view_revision=body["base_view_revision"] + 1),
        ErrorCode.VIEW_REVISION_STALE,
        None,
    ),
)


def test_a_malformed_or_unknown_view_derivation_is_refused_typed_and_captures_nothing(tmp_path: Path) -> None:
    def flow(url: str, state: Any, repo: Path) -> list[str]:
        with (
            _synced_views(_config(url, repo, tmp_path), tmp_path) as (repo_id, lcr_body),
            _another_repository_view(url, tmp_path) as foreign_view,
        ):
            return _refusals(url, repo_id, lcr_body, foreign_view)

    assert _serve(tmp_path, flow) == []


@contextmanager
def _another_repository_view(url: str, tmp_path: Path) -> Iterator[Mapping[str, Any]]:
    """A View this principal synced of a different repository."""

    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test")
    (other / "elsewhere.py").write_text("elsewhere = True\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "other")
    snapshot = tmp_path / "other-snapshot"
    snapshot.mkdir()
    (snapshot / "elsewhere.py").write_text("elsewhere = True\n", encoding="utf-8")
    remote = ReviewSession(_config(url, other, tmp_path / "other-client"))
    try:
        remote.handshake(timeout_s=10.0)
        remote.open_session(timeout_s=10.0)
        yield remote.open_snapshot(snapshot, source_revision="other", timeout_s=60.0)
    finally:
        remote.close()


def _refusals(url: str, repo_id: str, lcr_body: dict[str, Any], foreign_view: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    for case, change, code, field in _REFUSED:
        body = json.loads(json.dumps(lcr_body))
        change(body)
        status, answer = _post(url, f"/v1/repos/{repo_id}/reviews", body)
        error = answer.get("error", {})
        seen = (status >= 400, error.get("code"), error.get("details", {}).get("field") if field else None)
        if seen != (True, code.value, field):
            failures.append(f"{case}: {status} {answer}")
    # A View of this repository named under another repository is unknown there,
    # and so is another repository's View named as this one's head or base.
    assert foreign_view["repo_id"] != repo_id
    for case, path, body in (
        ("head view under another repository", "/v1/repos/repo_another_fixture/reviews", lcr_body),
        (
            "another repository's head view",
            f"/v1/repos/{repo_id}/reviews",
            {**lcr_body, "view_id": foreign_view["view_id"], "view_revision": int(foreign_view["view_revision"])},
        ),
        (
            "another repository's base view",
            f"/v1/repos/{repo_id}/reviews",
            {
                **lcr_body,
                "base_view_id": foreign_view["view_id"],
                "base_view_revision": int(foreign_view["view_revision"]),
            },
        ),
    ):
        status, answer = _post(url, path, body)
        if (status, answer.get("error", {}).get("code")) != (404, ErrorCode.VIEW_UNKNOWN.value):
            failures.append(f"{case}: {status} {answer}")
    if _reviews(url):
        failures.append("a refused derivation captured a Review")
    # The same body captures: each refusal was about its one change. Its gitlinks
    # (a submodule bump Views do not carry) reach the derived packet.
    bumped = {**lcr_body, "gitlinks": {"base": {"deps/sub": "1" * 40}, "head": {"deps/sub": "2" * 40}}}
    status, answer = _post(url, f"/v1/repos/{repo_id}/reviews", bumped)
    if status != 201:
        failures.append(f"unaltered body: {status} {answer}")
        return failures
    with urllib.request.urlopen(f"{url}/api/reviews/{answer['review']['id']}", timeout=60) as response:
        overview = json.loads(response.read())
    if overview["stats"]["files"] != 4:
        failures.append(f"the gitlink bump is not in the derived packet: {overview['stats']}")
    return failures


def test_the_local_principal_derives_only_from_views_it_owns(tmp_path: Path) -> None:
    def flow(url: str, state: Any, repo: Path) -> None:
        with _synced_views(_config(url, repo, tmp_path), tmp_path) as (repo_id, body):
            _owned_views_only(url, state, repo_id, body)

    _serve(tmp_path, flow)


def _owned_views_only(url: str, state: Any, repo_id: str, body: dict[str, Any]) -> None:
    path = f"/v1/repos/{repo_id}/reviews"
    org = state.local_principal.org_id
    mine = state.local_principal.subject
    for view_key in ("view_id", "base_view_id"):
        state.view_owners.record(org_id=org, view_id=body[view_key], subject="user_someone_else")
        status, answer = _post(url, path, body)
        assert (status, answer["error"]["code"]) == (404, ErrorCode.VIEW_UNKNOWN.value), view_key
        state.view_owners.record(org_id=org, view_id=body[view_key], subject=mine)
    # Views are owned per organization: the same subject elsewhere owns none of them.
    elsewhere = Principal(subject=mine, org_id="org_elsewhere", token_id="tok_elsewhere", auth_method="static_token")
    with pytest.raises(ServerError) as caught:
        state._review_capture_sync(elsewhere, repo_id, dict(body))
    assert caught.value.code is ErrorCode.VIEW_UNKNOWN
    assert _reviews(url) == []
    status, _answer = _post(url, path, body)
    assert status == 201


def _decoy(tmp_path: Path) -> Path:
    """A separate repository the server's inherited git environment names."""

    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _git(decoy, "init", "-q")
    _git(decoy, "config", "user.email", "decoy@example.com")
    _git(decoy, "config", "user.name", "Decoy")
    (decoy / "decoy.py").write_text("decoy = True\n", encoding="utf-8")
    _git(decoy, "add", "-A")
    _git(decoy, "commit", "-qm", "decoy")
    (decoy / "untracked.py").write_text("untracked = True\n", encoding="utf-8")
    return decoy


def _bytes_of(root: Path) -> dict[str, str]:
    """Every file below ``root`` (``.git`` included) by its content digest."""

    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_a_derived_capture_ignores_the_git_environment_the_server_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server started from a hook, an IDE task or ``git rebase --exec`` keeps that git environment.

    Derivation must neither read the repository it names nor write into it.
    """

    decoy = _decoy(tmp_path)
    before = _bytes_of(decoy)
    inherited = {
        "GIT_DIR": str(decoy / ".git"),
        "GIT_WORK_TREE": str(decoy),
        "GIT_INDEX_FILE": str(decoy / ".git" / "index"),
        "GIT_OBJECT_DIRECTORY": str(decoy / ".git" / "objects"),
        "GIT_CONFIG_PARAMETERS": "'core.autocrlf=true'",
    }
    recorded: dict[str, Any] = {}
    capture = LocalReviewRepository.capture

    def observed(self: LocalReviewRepository, **kwargs: Any) -> Any:
        recorded.update(kwargs)
        return capture(self, **kwargs)

    monkeypatch.setattr(LocalReviewRepository, "capture", observed)

    def flow(url: str, state: Any, repo: Path) -> tuple[int, dict[str, Any]]:
        # An unchanged CRLF file: core.autocrlf would rewrite its committed base.
        (repo / "notes.txt").write_bytes(b"first\r\nsecond\r\n")
        _git(repo, "add", "notes.txt")
        _git(repo, "commit", "-qm", "notes")
        with _synced_views(_config(url, repo, tmp_path), tmp_path) as (repo_id, body):
            with monkeypatch.context() as server_environment:
                for name, value in inherited.items():
                    server_environment.setenv(name, value)
                return _post(url, f"/v1/repos/{repo_id}/reviews", body)

    status, answer = _serve(tmp_path, flow)
    # The decoy is byte-identical: no object, index, HEAD or ref was written.
    assert _bytes_of(decoy) == before
    assert status == 201, answer
    packet = recorded["packet"]
    assert {item.path: item.status for item in packet.files} == {
        "app.py": "modified",
        "gone.py": "deleted",
        "new.py": "added",
    }
    [modified] = [item for item in packet.files if item.path == "app.py"]
    patch = "".join(hunk.patch for hunk in modified.hunks)
    assert "-    return 41" in patch and "+    return 42" in patch
    assert recorded["old_blobs"] == {"app.py": _OLD_ANSWER, "gone.py": _GONE}
    assert recorded["new_blobs"] == {"app.py": _NEW_ANSWER, "new.py": _ADDED}


# -- the branch Review: plan 2026-10-10 (Review base on every capture), P2 -------


def _rev_parse(repo: Path, spec: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", spec], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit_under_the_review(repo: Path) -> None:
    """Commit a new file: the working tree's bytes change, so the next capture is a new Snapshot."""

    (repo / "committed.py").write_text("committed = True\n", encoding="utf-8")
    _git(repo, "add", "committed.py")
    _git(repo, "commit", "-qm", "commit under the Review")


def _branch(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _lcr(config: ClientConfig) -> tuple[str, str]:
    """``lcr --working-tree``: the Review it lands in and the base its Snapshot diffs against."""

    response, _link = _capture(
        config,
        resolve_review_range(config.repo_root, None, working_tree=True),
        with_impact=False,
        restore_archived=False,
    )
    review_id = str(response["review"]["id"])  # type: ignore[index]
    number = int(response["revision"]["revision_number"])  # type: ignore[index]
    server_review_request(config, "POST", f"/api/reviews/{review_id}/revisions/{number}/publish", body={})
    overview = server_review_request(config, "GET", f"/api/reviews/{review_id}")
    assert overview["revision"]["revision_number"] == number
    return review_id, str(overview["revision"]["base_sha"])


def _row(url: str, repo_id: str, review_id: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{url}/v1/repos/{repo_id}/reviews", timeout=60) as response:
        rows = json.loads(response.read())["reviews"]
    [row] = [row for row in rows if row["id"] == review_id]
    return dict(row)


def _observe_capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    capture = LocalReviewRepository.capture

    def observed(self: LocalReviewRepository, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return capture(self, **kwargs)

    monkeypatch.setattr(LocalReviewRepository, "capture", observed)
    return seen


def test_lcr_routes_a_working_tree_review_by_its_branch_and_keeps_the_base_it_started_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _observe_capture(monkeypatch)

    def flow(url: str, state: Any, repo: Path) -> None:
        config = _config(url, repo, tmp_path)
        started_on = _rev_parse(repo)
        branch = _branch(repo)

        review_id, base = _lcr(config)
        assert seen[-1]["workspace_branch"] == branch
        assert base == started_on
        row = _row(url, str(seen[-1]["repo_id"]), review_id)
        assert (row["branch"], row["branch_active"], row["review_base_sha"]) == (branch, True, started_on)

        # A commit under the Review stays in its Change: the base is the one it started on.
        _commit_under_the_review(repo)
        assert _lcr(config) == (review_id, started_on)
        overview = server_review_request(config, "GET", f"/api/reviews/{review_id}")
        assert overview["stats"]["files"] == 4

        # Another branch is another Review, based where that branch stands.
        _git(repo, "switch", "-q", "-c", "topic")
        topic_id, topic_base = _lcr(config)
        assert seen[-1]["workspace_branch"] == "topic"
        assert (topic_id != review_id, topic_base) == (True, _rev_parse(repo))
        _git(repo, "switch", "-q", branch)
        assert _lcr(config) == (review_id, started_on)

        # A detached HEAD names no branch: it is not the branch's Review and HEAD is its base.
        _git(repo, "checkout", "-q", "--detach")
        detached_id, detached_base = _lcr(config)
        assert seen[-1]["workspace_branch"] == ""
        assert detached_id not in {review_id, topic_id}
        assert detached_base == _rev_parse(repo)

    _serve(tmp_path, flow)


def test_two_worktrees_on_one_branch_share_its_review_and_its_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan P2 open point: ``lcr`` opens Views on a copy, so its captures carry no worktree key.

    A branch Review is keyed by repository and branch alone, for ``lcr`` and the
    engine alike, so a second worktree checked out on the same branch continues
    the branch's Review and diffs against its recorded base. A worktree on
    another branch, or detached, never joins it.
    """

    seen = _observe_capture(monkeypatch)

    def flow(url: str, state: Any, repo: Path) -> None:
        started_on = _rev_parse(repo)
        branch = _branch(repo)
        second = tmp_path / "second-worktree"
        _git(repo, "worktree", "add", "-q", "--force", str(second), branch)
        (second / "second.py").write_text("second = True\n", encoding="utf-8")
        other = tmp_path / "other-worktree"
        _git(repo, "worktree", "add", "-q", "-b", "other", str(other))
        (other / "other.py").write_text("other = True\n", encoding="utf-8")

        review_id, base = _lcr(_config(url, repo, tmp_path / "first-client"))
        assert base == started_on
        assert seen[-1]["worktree_key"] == ""

        # The first worktree commits on the shared branch: the commit stays in the Change.
        _commit_under_the_review(repo)
        assert _lcr(_config(url, repo, tmp_path / "first-client")) == (review_id, started_on)

        # The second worktree's HEAD moved with the branch; its capture continues
        # the same Review and diffs against the same recorded base, not its HEAD.
        assert _rev_parse(second) != started_on
        assert _lcr(_config(url, second, tmp_path / "second-client")) == (review_id, started_on)
        assert (seen[-1]["workspace_branch"], seen[-1]["worktree_key"]) == (branch, "")
        assert _row(url, str(seen[-1]["repo_id"]), review_id)["review_base_sha"] == started_on

        other_id, other_base = _lcr(_config(url, other, tmp_path / "other-client"))
        assert other_id != review_id
        assert other_base == _rev_parse(other)

    _serve(tmp_path, flow)


def _older_server_routes_no_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    import lemoncrow_server_core.local_app as local_app

    monkeypatch.setattr(local_app, "CAPTURE_OPTIONS", local_app.CAPTURE_OPTIONS - {"workspace_branch"})


def _older_server_lacks_the_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that routes by branch but predates ``GET /api/branch-review``: the path falls to the Reader."""

    from lemoncrow_server_core.local_review_service import LocalReviewService
    from lemoncrow_server_core.protocol import BRANCH_REVIEW_PATH

    routes = LocalReviewService.route_table

    def without_lookup(self: LocalReviewService) -> tuple[Any, ...]:
        return tuple(route for route in routes(self) if route[1] != BRANCH_REVIEW_PATH)

    monkeypatch.setattr(LocalReviewService, "route_table", without_lookup)


def _lookup_status(url: str) -> int:
    query = "range_mode=working_tree&branch=main&repo_id=r"
    try:
        with urllib.request.urlopen(f"{url}/api/branch-review?{query}", timeout=60) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


@pytest.mark.parametrize(
    ("older", "routes_by_branch"),
    [
        pytest.param(_older_server_routes_no_branch, False, id="server does not route by branch"),
        pytest.param(_older_server_lacks_the_lookup, True, id="server does not serve the branch Review lookup"),
    ],
)
def test_a_newer_lcr_against_an_older_server_preserves_legacy_or_refuses_unsafe_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    older: Callable[[pytest.MonkeyPatch], None],
    routes_by_branch: bool,
) -> None:
    seen = _observe_capture(monkeypatch)
    older(monkeypatch)

    def flow(url: str, state: Any, repo: Path) -> None:
        config = _config(url, repo, tmp_path)
        if routes_by_branch:
            # The old server routes a branch Review but cannot answer its base
            # lookup. Falling back to HEAD would silently hide Review commits.
            assert _lookup_status(url) == 404
            before = len(seen)
            with pytest.raises(ClientError) as failed:
                _lcr(config)
            assert failed.value.code is ClientErrorCode.CAPABILITY_UNSUPPORTED
            assert failed.value.action is AgentAction.UPGRADE_SERVER
            assert "nothing was captured" in failed.value.message
            assert len(seen) == before
            _commit_under_the_review(repo)
            with pytest.raises(ClientError):
                _lcr(config)
            assert len(seen) == before  # no Snapshot against a guessed base
            return

        # A genuinely old server does not advertise branch identity: retain
        # legacy captures and compare with current HEAD on each recapture.
        review_id, base = _lcr(config)
        assert base == _rev_parse(repo)
        assert not seen[-1]["workspace_branch"]
        _commit_under_the_review(repo)
        assert _lcr(config) == (review_id, _rev_parse(repo))

    _serve(tmp_path, flow)


def test_a_failed_branch_review_lookup_fails_the_capture_retryably_and_captures_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that serves the lookup but cannot answer it: no Snapshot against a guessed base."""

    import lemoncrow_server_core.local_review_service as service
    from lemoncrow_client.errors import AgentAction, ClientError

    seen = _observe_capture(monkeypatch)

    def flow(url: str, state: Any, repo: Path) -> None:
        config = _config(url, repo, tmp_path)
        review_id, started_on = _lcr(config)
        _commit_under_the_review(repo)
        captures = len(seen)
        wire = service.branch_review_wire

        def broken(session: Any, repo_id: str) -> dict[str, Any]:
            raise ServerError(ErrorCode.INTERNAL, "the Review store is unavailable")

        # The route is bound when the server starts; its answer is read per request.
        monkeypatch.setattr(service, "branch_review_wire", broken)
        with pytest.raises(ClientError) as failed:
            _lcr(config)
        assert failed.value.retryable is True
        assert failed.value.action is AgentAction.RETRY_LATER
        assert "nothing was captured" in failed.value.message
        assert len(seen) == captures

        monkeypatch.setattr(service, "branch_review_wire", wire)
        assert _lcr(config) == (review_id, started_on)

    _serve(tmp_path, flow)

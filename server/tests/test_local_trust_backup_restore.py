"""Trust plan §39.8 item 8 locally: a restored state directory gives the same projection.

The local backup is a copy of the LemonCrow state directory
(``docs/operations/production-readiness.md``). A Review is captured from a real
repository, two of its targets are judged, and a second Snapshot changes one of
them; a second Review is fully reviewed. Review Status with the trust section
on, and the run plan, are read from the live server. The server stops, the
state directory is copied elsewhere, and the original, the client's own state
and the repository checkout are removed. A fresh process whose home,
configuration and cache directories are new and empty composes the local server
over the copy. It must give the same trust section (its digest included),
readiness conditions hash and run plan digest. Anything trust-relevant that
lived only in the first process, or outside the state directory, shows up as a
difference.
``enterprise/server/tests/ops/test_trust_backup_restore.py`` holds the same
line for the hosted backup paths.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_client.config import ClientConfig
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig
from test_local_review_inbox_views import _review, _review_everything
from test_local_trust_replica_determinism import _projections, _replica

from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.hosted import capture_server_review, server_review_request


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repository(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "restore@example.com")
    _git(repo, "config", "user.name", "Restore")
    for name in ("alpha", "beta", "gamma"):
        (repo / f"{name}.py").write_text(f"def {name}():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    for name in ("alpha", "beta", "gamma"):
        (repo / f"{name}.py").write_text(f"def {name}():\n    return 2\n", encoding="utf-8")
    return repo


def _capture(config: ClientConfig, repo: Path, client_store: Path) -> Any:
    captured = capture_server_review(
        config,
        repo,
        resolve_rev_range(repo, working_tree=True),
        store_root=client_store,
        session_id=None,
        limit=200,
        with_impact=False,
        with_provenance=False,
    )
    number = int(captured.response["revision"]["revision_number"])
    server_review_request(config, "POST", f"/api/reviews/{captured.review_id}/revisions/{number}/publish", body={})
    return captured


def _read(config: ClientConfig, review_id: str) -> dict[str, Any]:
    status = server_review_request(config, "GET", f"/api/reviews/{review_id}/readiness")
    plan = server_review_request(config, "GET", f"/api/reviews/{review_id}/run-plan")
    return _projections(status, plan)


def test_a_restored_state_directory_gives_the_same_trust_and_plan_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_REVIEW_TRUST", "on")
    state_root, repo = tmp_path / "state", _repository(tmp_path)
    live: dict[str, dict[str, Any]] = {}

    async def serve() -> None:
        server, state = build_local_server(
            LocalServerConfig(listen_port=0, state_root=state_root, local_no_auth=True, allow_local_fs=True)
        )
        config = ClientConfig(
            url=await server.start(),
            token="",
            repo_root=repo,
            state_dir=tmp_path / "client",
            install_mode="local",
            startup_budget_s=10.0,
            request_timeout_s=10.0,
            tool_timeout_s=30.0,
            resolution_cache_bytes=0,
            offer_local_fs=True,
            token_source="test",
        )

        def flow() -> None:
            captured = _capture(config, repo, tmp_path / "client-store")
            review_id = captured.review_id
            targets = server_review_request(config, "GET", f"/api/reviews/{review_id}/targets?order=recommended")
            judged = sorted(str(row["unit_key"]) for row in targets["targets"])[:2]
            assert len(judged) == 2, targets
            for unit_key in judged:
                revision_id = str(server_review_request(config, "GET", f"/api/reviews/{review_id}")["revision"]["id"])
                server_review_request(
                    config,
                    "POST",
                    f"/api/reviews/{review_id}/marks",
                    body={"unit_key": unit_key, "state": "reviewed", "expected_revision_id": revision_id},
                )
            # The second Snapshot changes one judged file: that judgment is no longer current.
            (repo / "alpha.py").write_text("def alpha():\n    return 3\n", encoding="utf-8")
            again = _capture(config, repo, tmp_path / "client-store")
            assert again.review_id == review_id
            assert int(again.response["revision"]["revision_number"]) > int(
                captured.response["revision"]["revision_number"]
            )

            store = state.review_repository.store(captured.repo_id)
            done, done_revision = _review(store, "done", units=3, repo_root=f"local:{captured.repo_id}")
            _review_everything(store, done, done_revision)
            for each in (review_id, done):
                live[each] = _read(config, each)
                # The second read may be answered from what this process kept; it must say the same.
                assert _read(config, each) == live[each]

        try:
            await asyncio.to_thread(flow)
        finally:
            await server.stop()

    asyncio.run(serve())
    assert all(len(answer["trust"]["items"]) > 1 for answer in live.values())
    assert len({answer["trust"]["summary"]["state"] for answer in live.values()}) == 2, live

    restored = tmp_path / "elsewhere" / "restored-state"
    restored.parent.mkdir()
    shutil.copytree(state_root, restored)
    for gone in (state_root, repo, tmp_path / "client", tmp_path / "client-store"):
        shutil.rmtree(gone, ignore_errors=True)
        assert not gone.exists()
    empty = tmp_path / "empty-home"
    empty.mkdir()
    fresh_machine = {
        "HOME": str(empty),
        "XDG_CONFIG_HOME": str(empty / "config"),
        "XDG_CACHE_HOME": str(empty / "cache"),
        "XDG_DATA_HOME": str(empty / "data"),
        "XDG_STATE_HOME": str(empty / "xdg-state"),
    }
    for review_id, expected in live.items():
        answer = _replica(
            restored, review_id, hash_seed="7", zone="Asia/Kolkata", extra_env=fresh_machine, local_fs=True
        )
        assert answer == expected, review_id

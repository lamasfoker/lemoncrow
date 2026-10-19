from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest
from aiohttp import ClientSession
from lemoncrow_client.config import ClientConfig
from lemoncrow_client.session import RemoteSession
from lemoncrow_server_core.app import _requested_lease_s
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig
from lemoncrow_server_core.errors import ServerError

ORG = "org_local"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_a_requested_lease_is_capped_and_a_malformed_one_refused() -> None:
    assert _requested_lease_s({}, 3600.0, 86_400.0) == 3600.0
    assert _requested_lease_s({"lease_s": 600}, 3600.0, 86_400.0) == 600.0
    assert _requested_lease_s({"lease_s": 10**9}, 3600.0, 86_400.0) == 86_400.0
    for bad in (0, -5, True, "60"):
        with pytest.raises(ServerError):
            _requested_lease_s({"lease_s": bad}, 3600.0, 86_400.0)


def test_session_activity_keeps_its_view_leased_without_shortening_a_snapshot(tmp_path: Path) -> None:
    asyncio.run(_exercise_leases(tmp_path))


async def _exercise_leases(tmp_path: Path) -> None:
    # Views were leased for a day and never renewed: 118 abandoned ones kept
    # 18 generations' graphs and ANN indexes alive. A View now lives while its
    # session is active, and a Review snapshot keeps the longer lease it asked for.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "lease@example.com")
    _git(repo, "config", "user.name", "Lease")
    (repo / "app.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "initial")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "app.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")

    server, state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            local_no_auth=True,
            allow_local_fs=False,
            view_lease_s=20.0,
            view_lease_max_s=1000.0,
        )
    )
    url = await server.start()
    config = ClientConfig(
        url=url,
        token="",
        repo_root=repo,
        state_dir=tmp_path / "client",
        install_mode="local",
        startup_budget_s=10.0,
        request_timeout_s=10.0,
        tool_timeout_s=30.0,
        resolution_cache_bytes=0,
        offer_local_fs=False,
        token_source="test",
    )
    try:
        remote = RemoteSession(config, publish_live_view=False)

        def open_view() -> str:
            remote.handshake(timeout_s=10.0)
            remote.open_session(timeout_s=10.0)
            return str(remote.open_view(timeout_s=30.0)["view_id"])

        view_id = await asyncio.to_thread(open_view)
        session_id = remote._state.session_id
        views = state.backend.views
        opened_until = views.get(ORG, view_id).lease_expires_at
        assert opened_until - time.time() <= 20.0

        async def session_activity() -> None:
            # Pretend half the lease has passed since the last renewal.
            state._view_lease_renewed_at[view_id] = time.monotonic() - 11.0
            async with ClientSession() as client:
                response = await client.get(f"{url}/v1/sessions/{session_id}")
                assert response.status == 200

        await asyncio.sleep(0.05)
        await session_activity()
        assert views.get(ORG, view_id).lease_expires_at > opened_until

        snapshot_opened = await asyncio.to_thread(
            lambda: remote.open_review_snapshot(snapshot, source_revision="lease-snapshot", timeout_s=30.0)
        )
        snapshot_id = str(snapshot_opened["view_id"])
        assert views.get(ORG, snapshot_id).lease_expires_at - time.time() > 900.0
        view_id = snapshot_id
        await session_activity()
        assert views.get(ORG, snapshot_id).lease_expires_at - time.time() > 900.0
    finally:
        await server.stop()

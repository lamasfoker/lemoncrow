"""Client/server version-skew matrix (G6-SKEW).

Two cells inside the support window -- client N-1 against server N, and
client N against server N-1 -- each covering the handshake, a capture, Ask, an
evidence read and one write that must fail closed; then the edges outside the
window, which must refuse loudly and name the side to upgrade.

Versions are simulated by parameterizing this build's constants and manifest,
not by checking out old code:

* client N-1 is this client with the window ``(N-1, N-2)`` and the previous
  packet schema;
* server N-1 is this server with the window ``(N-1, N-2)``, publishing only
  what a 2026-09-14 server published (the Review booleans, no manifest, no
  ``/v1/reviews/manifest``).
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession, web
from lemoncrow_client import protocol as client_protocol
from lemoncrow_client.config import ClientConfig
from lemoncrow_client.errors import AgentAction, ClientError, ErrorCode
from lemoncrow_client.session import RemoteSession
from lemoncrow_server_core import local_app
from lemoncrow_server_core import protocol as server_protocol
from lemoncrow_server_core.app import CoreServerRoutes
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig
from lemoncrow_server_core.local_reviews import LocalReviewReader

from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.hosted import capture_server_review, server_review_request, server_review_rows
from lemoncrow.pro.capabilities.review.models import SCHEMA_VERSION as PACKET_SCHEMA_VERSION

N = server_protocol.PROTOCOL_VERSION
N1 = server_protocol.PREVIOUS_PROTOCOL_VERSION


def _shift(version: str, days: int) -> str:
    return (date.fromisoformat(version) + timedelta(days=days)).isoformat()


N2 = _shift(N1, -21)
N3 = _shift(N2, -21)
N_PLUS_1 = _shift(N, 21)
N_PLUS_2 = _shift(N_PLUS_1, 21)

#: What a 2026-09-14 local server published under ``product_capabilities.review``.
_PREVIOUS_LOCAL_REVIEW_KEYS = frozenset(
    {
        "local_workspace",
        "workspace_refresh",
        "cross_device_history",
        "collaboration",
        "participants",
        "requests",
        "provider_sync",
        "organization_policy",
        "sharing",
        "guest_review",
        "private_drafts",
        "audit_history",
        "brief",
        "ask",
    }
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "initial")
    (repo / "app.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
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
        tool_timeout_s=30.0,
        resolution_cache_bytes=0,
        offer_local_fs=True,
        token_source="test",
    )


def _as_client(monkeypatch: pytest.MonkeyPatch, current: str, previous: str) -> None:
    monkeypatch.setattr(client_protocol, "PROTOCOL_VERSION", current)
    monkeypatch.setattr(client_protocol, "PREVIOUS_PROTOCOL_VERSION", previous)


def _as_previous_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """This server, one protocol behind: its window, and no manifest anywhere."""

    monkeypatch.setattr(server_protocol, "PROTOCOL_WINDOW", server_protocol.ProtocolWindow(current=N1, previous=N2))

    def published_before_manifests(features: Any, **_: Any) -> dict[str, Any]:
        return {name: value for name, value in dict(features).items() if name in _PREVIOUS_LOCAL_REVIEW_KEYS}

    monkeypatch.setattr(local_app, "review_capabilities", published_before_manifests)
    # Nor did it have the typed skew guards: its packet decoder was the only check.
    monkeypatch.setattr(local_app, "require_packet_schema", lambda packet: None)
    monkeypatch.setattr(local_app, "require_capture_mode", lambda body, **_: None)

    async def no_such_route(self: Any, request: web.Request) -> web.StreamResponse:
        raise web.HTTPNotFound()

    monkeypatch.setattr(CoreServerRoutes, "review_manifest", no_such_route)


def _run(tmp_path: Path, flow: Callable[[str, Path], Any]) -> Any:
    async def run() -> Any:
        server, _state = build_local_server(
            LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=True)
        )
        url = await server.start()
        try:
            return await asyncio.to_thread(flow, url, _repo(tmp_path))
        finally:
            await server.stop()

    return asyncio.run(run())


def _capture(config: ClientConfig, repo: Path, tmp_path: Path) -> Any:
    return capture_server_review(
        config,
        repo,
        resolve_rev_range(repo, working_tree=True),
        store_root=tmp_path / "client-store",
        session_id=None,
        limit=200,
        with_impact=False,
        with_provenance=False,
    )


def _ask_and_read_evidence(config: ClientConfig, review_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    ask = server_review_request(config, "GET", f"/api/reviews/{review_id}/ask")
    attached = server_review_request(
        config,
        "POST",
        f"/api/reviews/{review_id}/evidence/upload?kind=document&title=skew-notes&mime_type=application/json",
        body={"note": "read across versions"},
    )
    evidence_id = attached["evidence"]["id"]
    content = server_review_request(config, "GET", f"/api/evidence/{evidence_id}/content")
    return ask, content


@pytest.fixture
def no_host_session(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "OPENCODE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    yield


# --------------------------------------------------------------------------- #
# Inside the window                                                           #
# --------------------------------------------------------------------------- #


def test_client_n_minus_1_against_server_n(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_host_session: None
) -> None:
    _as_client(monkeypatch, N1, N2)

    def flow(url: str, repo: Path) -> None:
        config = _config(url, repo, tmp_path)

        # Handshake: the server serves the older client at the older version.
        remote = RemoteSession(config, publish_live_view=False)
        try:
            remote.handshake(timeout_s=10.0)
            assert remote.state.protocol_version == N1
            assert remote.state.review_manifest is not None and remote.state.review_manifest.declared
        finally:
            remote.close()

        # Capture.
        captured = _capture(config, repo, tmp_path)
        assert captured.review_id

        # Ask and an evidence read.
        ask, content = _ask_and_read_evidence(config, captured.review_id)
        assert ask["review_id"] == captured.review_id
        assert isinstance(ask["messages"], list)
        assert content == {"note": "read across versions"}

        # A write the server cannot apply as sent fails closed on the server:
        # an N-1 client posts its own (previous) packet schema and checks nothing.
        stale_packet = {**captured.build.packet.to_dict(), "schema_version": PACKET_SCHEMA_VERSION - 1}

        async def post_stale_packet() -> tuple[int, dict[str, Any]]:
            async with ClientSession() as http:
                response = await http.post(
                    f"{url}/v1/repos/{captured.repo_id}/reviews",
                    json={
                        "view_id": "view_skew",
                        "view_revision": 1,
                        "source_ref": "skew:stale-client",
                        "packet": stale_packet,
                        "new_blobs": {},
                    },
                )
                return response.status, dict(await response.json())

        before = len(server_review_rows(config))
        status, body = asyncio.run(post_stale_packet())
        assert status == 400
        assert body["error"]["code"] == ErrorCode.CAPABILITY_UNSUPPORTED.value
        assert body["error"]["action"] == AgentAction.UPGRADE_CLIENT.value
        assert body["error"]["details"] == {
            "schema": "packet",
            "version": PACKET_SCHEMA_VERSION - 1,
            "supported": [PACKET_SCHEMA_VERSION],
        }
        assert len(server_review_rows(config)) == before

    _run(tmp_path, flow)


def test_client_n_against_server_n_minus_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_host_session: None
) -> None:
    _as_previous_server(monkeypatch)

    def flow(url: str, repo: Path) -> None:
        config = _config(url, repo, tmp_path)

        # Handshake: N is refused as too new, the client steps down to N-1 once.
        remote = RemoteSession(config, publish_live_view=False)
        try:
            remote.handshake(timeout_s=10.0)
            assert remote.state.protocol_version == N1
            manifest = remote.state.review_manifest
            assert manifest is not None and not manifest.declared, "an N-1 server publishes no manifest"
        finally:
            remote.close()

        # Capture, through the N-1 baseline for a local server.
        captured = _capture(config, repo, tmp_path)
        assert captured.review_id

        # Ask and an evidence read: no manifest route, so the client asks the
        # handshake and serves both from the baseline.
        ask, content = _ask_and_read_evidence(config, captured.review_id)
        assert ask["review_id"] == captured.review_id
        assert content == {"note": "read across versions"}

        # A write the older server would not understand never leaves the client.
        newer_packet = {**captured.build.packet.to_dict(), "schema_version": PACKET_SCHEMA_VERSION + 1}
        before = len(server_review_rows(config))
        remote = RemoteSession(config, publish_live_view=False)
        try:
            remote.handshake(timeout_s=10.0)
            remote.open_session(timeout_s=10.0)
            remote.open_view(timeout_s=30.0)
            with pytest.raises(ClientError) as caught:
                remote.capture_review(
                    packet=newer_packet,
                    new_blobs={},
                    source_ref="skew:newer-client",
                    repo_id=captured.repo_id,
                    source_fingerprint="skew",
                )
        finally:
            remote.close()
        error = caught.value
        assert error.code is ErrorCode.CAPABILITY_UNSUPPORTED
        assert error.action is AgentAction.UPGRADE_SERVER
        assert error.details == {
            "schema": "packet",
            "version": PACKET_SCHEMA_VERSION + 1,
            "supported": [PACKET_SCHEMA_VERSION],
        }
        assert len(server_review_rows(config)) == before

    _run(tmp_path, flow)


def test_a_feature_the_server_does_not_serve_degrades_visibly_and_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_host_session: None
) -> None:
    """A Review-only engine without Ask: the client says so instead of a bare 404."""

    monkeypatch.setattr(LocalReviewReader, "ask_enabled", property(lambda self: False))
    seen: list[str] = []
    original = local_app.LocalServerApp.review_reader_api

    async def observed(self: Any, request: web.Request) -> web.StreamResponse:
        seen.append(request.path)
        return await original(self, request)

    monkeypatch.setattr(local_app.LocalServerApp, "review_reader_api", observed)

    def flow(url: str, repo: Path) -> None:
        config = _config(url, repo, tmp_path)
        captured = _capture(config, repo, tmp_path)
        with pytest.raises(ClientError) as caught:
            server_review_request(config, "POST", f"/api/reviews/{captured.review_id}/ask", body={"body": "why?"})
        assert caught.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
        assert caught.value.message.startswith("this server does not support Ask")
        assert not any(path.endswith("/ask") for path in seen)
        # The terminal shows it as is, not as a generic server failure to repair.
        from lemoncrow.gateway.cli.commands.review import _review_server_click_error

        shown = str(_review_server_click_error(caught.value, hosted=False))
        assert shown == "Review server failed: this server does not support Ask"

    _run(tmp_path, flow)


# --------------------------------------------------------------------------- #
# Outside the window                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("client_window", "negotiated", "action", "says"),
    [
        pytest.param((N_PLUS_1, N), N, None, "", id="client-n+1-steps-down-to-n"),
        pytest.param((N2, N3), None, AgentAction.UPGRADE_CLIENT, "upgrade the client", id="client-n-2"),
        pytest.param(
            (N_PLUS_2, N_PLUS_1),
            None,
            AgentAction.UPGRADE_SERVER,
            "the server is older; upgrade the server",
            id="client-n+2",
        ),
    ],
)
def test_the_window_edges_against_server_n(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client_window: tuple[str, str],
    negotiated: str | None,
    action: AgentAction | None,
    says: str,
) -> None:
    _as_client(monkeypatch, *client_window)

    def flow(url: str, repo: Path) -> None:
        remote = RemoteSession(_config(url, repo, tmp_path), publish_live_view=False)
        try:
            if negotiated is not None:
                remote.handshake(timeout_s=10.0)
                assert remote.state.protocol_version == negotiated
                return
            with pytest.raises(ClientError) as caught:
                remote.handshake(timeout_s=10.0)
            assert caught.value.code is ErrorCode.PROTOCOL_VERSION_MISMATCH
            assert caught.value.action is action
            assert says in caught.value.message
        finally:
            remote.close()

    _run(tmp_path, flow)

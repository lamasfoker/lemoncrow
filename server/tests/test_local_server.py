from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession
from lemoncrow_server_core.bootstrap import build_local_server, ensure_local_token, read_local_token
from lemoncrow_server_core.config import LocalServerConfig
from lemoncrow_server_core.dispatch import UnavailableDispatcher
from lemoncrow_server_core.index.memory import InMemoryAnalysisStore
from lemoncrow_server_core.local_app import LocalServerApp
from lemoncrow_server_core.local_reviews import LocalReviewRepository
from lemoncrow_server_core.protocol import PROTOCOL_VERSION, REQUIRED_CLIENT_CAPABILITIES


def test_public_loopback_server_runs_shared_engine(tmp_path: Path) -> None:
    asyncio.run(_exercise(tmp_path))


def test_local_no_auth_browser_claim_is_a_noop_capability(tmp_path: Path) -> None:
    asyncio.run(_exercise_local_no_auth_browser_claim(tmp_path))


async def _exercise_local_no_auth_browser_claim(tmp_path: Path) -> None:
    server, _state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            local_no_auth=True,
            allow_local_fs=False,
        )
    )
    url = await server.start()
    try:
        async with ClientSession() as client:
            claim = await client.post(
                f"{url}/v1/auth/local-browser/claim",
                json={"path": "/r/rev_fixture"},
                headers={"Origin": url, "Sec-Fetch-Site": "same-origin"},
            )
            assert claim.status == 200
            payload = await claim.json()
            assert payload["token"] == "local-no-auth"
            assert isinstance(payload["expires_at"], int)
            assert payload["expires_at"] > int(time.time())

            inbox = await client.get(f"{url}/v1/reviews")
            assert inbox.status == 200
    finally:
        await server.stop()


def test_every_route_refuses_a_rebound_host_whatever_the_auth_mode(tmp_path: Path) -> None:
    asyncio.run(_exercise_rebound_host(tmp_path))


async def _exercise_rebound_host(tmp_path: Path) -> None:
    server, _state = build_local_server(
        LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=True)
    )
    url = await server.start()
    port = url.rsplit(":", 1)[1]
    try:
        async with ClientSession() as client:
            assert (await client.get(f"{url}/v1/reviews")).status == 200
            for path in ("/v1/reviews", "/healthz", "/v1/views/open"):
                rebound = await client.get(f"{url}{path}", headers={"Host": f"attacker.example:{port}"})
                assert rebound.status == 403, path
                assert "loopback Host" in (await rebound.json())["error"]["message"]
    finally:
        await server.stop()


async def _exercise(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    assert not token_file.exists()
    server, _state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            token_file=token_file,
            allow_local_fs=False,
        )
    )
    token = read_local_token(token_file)
    # Runtime tools run in the agent's MCP process (Runtime Lite), never here.
    assert isinstance(_state.dispatcher, UnavailableDispatcher)
    assert not _state.dispatcher.available
    assert token.startswith("lc_local_")
    assert token_file.stat().st_mode & 0o077 == 0
    url = await server.start()
    try:
        async with ClientSession() as client:
            discovery = await client.get(f"{url}/.well-known/lemoncrow-server.json")
            assert discovery.status == 200
            discovery_body = await discovery.json()
            assert discovery_body["deployment"] == {"mode": "local"}
            assert discovery_body["products"] == {
                "core": {"installed": True, "configured": True, "available": True},
                "review": {"installed": True, "configured": True, "available": True},
                "dashboard": {"installed": True, "configured": True, "available": True},
                "runtime": {"installed": True, "configured": True, "available": True},
            }
            assert discovery_body["product_capabilities"]["review"]["local_workspace"] is True
            assert discovery_body["product_capabilities"]["review"]["collaboration"] is False
            assert discovery_body["product_capabilities"]["review"]["audit_history"] is False
            assert discovery_body["product_capabilities"]["review"]["brief"] is True
            assert discovery_body["product_capabilities"]["review"]["ask"] is True

            handshake = {
                "protocol_version": PROTOCOL_VERSION,
                "capabilities": sorted(REQUIRED_CLIENT_CAPABILITIES),
                "client_name": "test",
                "client_version": "1",
            }
            response = await client.post(f"{url}/v1/handshake", json=handshake)
            assert response.status == 200

            unauthenticated = await client.post(f"{url}/v1/sessions", json=handshake)
            assert unauthenticated.status == 401

            headers = {"Authorization": f"Bearer {token}"}
            opened = await client.post(f"{url}/v1/sessions", json=handshake, headers=headers)
            assert opened.status == 200
            session = await opened.json()
            headers["X-LemonCrow-Session"] = session["session_id"]

            tools = await client.get(f"{url}/v1/tools", headers=headers)
            assert tools.status == 404
            call = await client.post(f"{url}/v1/tools/read", json={"arguments": {}}, headers=headers)
            assert call.status == 404
            assert opened.status == 200
    finally:
        await server.stop()


def test_loopback_runtime_identity_accepts_only_safe_host_metadata() -> None:
    app = object.__new__(LocalServerApp)
    session = SimpleNamespace(session_id="server-session", client_name="lemoncrow-client")
    request = SimpleNamespace(
        headers={
            "X-LemonCrow-Host-Session": "01a0b52f-5187-7e11-a16b-000a4960428f",
            "X-LemonCrow-Host": "codex",
            "X-LemonCrow-Model": "gpt-5.6-sol",
        }
    )
    assert app._tool_runtime_identity(request, session) == (
        "01a0b52f-5187-7e11-a16b-000a4960428f",
        "codex",
        "gpt-5.6-sol",
    )

    malformed = SimpleNamespace(
        headers={
            "X-LemonCrow-Host-Session": "../../other-session",
            "X-LemonCrow-Host": "codex",
            "X-LemonCrow-Model": "gpt-5.6-sol",
        }
    )
    assert app._tool_runtime_identity(malformed, session) == (
        "server-session",
        "lemoncrow-client",
        "",
    )


def test_a_view_operation_waiting_on_a_repository_lock_does_not_stall_other_requests(tmp_path: Path) -> None:
    asyncio.run(_exercise_view_operation_off_the_loop(tmp_path))


async def _exercise_view_operation_off_the_loop(tmp_path: Path) -> None:
    # Maintenance and the deferred index worker hold a repository's write lock
    # for seconds on a large index. A view operation waiting for it inline on
    # the event loop stalled every request, health checks included.
    server, state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            token_file=tmp_path / "token",
            allow_local_fs=False,
        )
    )
    assert state.backend.authority_io_bound
    url = await server.start()
    release = threading.Event()
    try:
        waiting = asyncio.ensure_future(state._authority_call(release.wait, 10))
        await asyncio.sleep(0.05)
        async with ClientSession() as client:
            started = time.monotonic()
            health = await client.get(f"{url}/healthz")
            assert health.status == 200
            assert time.monotonic() - started < 2
        release.set()
        assert await waiting is True
    finally:
        release.set()
        await server.stop()


_CALLER: contextvars.ContextVar[str] = contextvars.ContextVar("test_caller", default="")


def test_off_loop_work_runs_in_the_caller_s_context(tmp_path: Path) -> None:
    # A hosted Review authority identifies a provider-subject lease holder by its
    # context; a worker thread that dropped it would let a stale holder write.
    _server, state = build_local_server(
        LocalServerConfig(
            listen_port=0, state_root=tmp_path / "state", token_file=tmp_path / "token", allow_local_fs=False
        )
    )

    async def call() -> str:
        _CALLER.set("holder")
        return await state._in_worker(_CALLER.get)

    assert asyncio.run(call()) == "holder"


@pytest.mark.parametrize("path", ["/v1/reviews", "/api/reviews/0123456789abcdef/source-state"])
def test_a_slow_review_lookup_does_not_stall_other_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    asyncio.run(_exercise_review_lookup_off_the_loop(tmp_path, monkeypatch, path))


async def _exercise_review_lookup_off_the_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    # Home and Reviews list every repository's Reviews, and every Reader
    # request resolves its Review across them, opening Review stores. Inline
    # on the event loop that stalled every request for seconds.
    token_file = tmp_path / "token"
    server, _state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            token_file=token_file,
            allow_local_fs=False,
        )
    )
    entered = threading.Event()
    release = threading.Event()

    def slow_repository_ids(self: LocalReviewRepository) -> tuple[str, ...]:
        entered.set()
        release.wait(5)
        return ()

    monkeypatch.setattr(LocalReviewRepository, "list_repository_ids", slow_repository_ids)
    headers = {"Authorization": f"Bearer {read_local_token(token_file)}"}
    url = await server.start()
    try:
        async with ClientSession() as client:
            started = time.monotonic()
            listing = asyncio.ensure_future(client.get(f"{url}{path}", headers=headers))
            while not entered.is_set():
                await asyncio.sleep(0.01)
            health = await client.get(f"{url}/healthz")
            assert health.status == 200
            assert time.monotonic() - started < 2
            release.set()
            assert (await listing).status in {200, 404}
    finally:
        release.set()
        await server.stop()


def test_ephemeral_local_server_uses_memory_index(tmp_path: Path) -> None:
    state_root = tmp_path / "ephemeral-state"
    server, state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=state_root,
            local_no_auth=True,
            allow_local_fs=False,
            ephemeral=True,
        )
    )

    assert isinstance(state.backend.analysis, InMemoryAnalysisStore)
    assert not (state_root / "index.sqlite3").exists()

    close_dispatcher = getattr(state.dispatcher, "close", None)
    if callable(close_dispatcher):
        close_dispatcher()
    if state.workspace is not None:
        state.workspace.close()
    del server


def test_machine_token_is_stable_across_restarts(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    first = ensure_local_token(token_file)
    second = ensure_local_token(token_file)
    assert second == first
    assert read_local_token(token_file) == first
    assert token_file.stat().st_mode & 0o077 == 0


def test_machine_token_refuses_unsafe_permissions(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("lc_local_test_token_0123456789abcdef\n", encoding="utf-8")
    token_file.chmod(0o644)
    with pytest.raises(RuntimeError, match="owner-only"):
        read_local_token(token_file)


def test_machine_token_refuses_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real-token"
    real.write_text("lc_local_test_token_0123456789abcdef\n", encoding="utf-8")
    real.chmod(0o600)
    linked = tmp_path / "token"
    linked.symlink_to(real)
    with pytest.raises(RuntimeError, match="symlink"):
        read_local_token(linked)


async def _raw_status(base_url: str, raw_path: str) -> int:
    """GET ``raw_path`` verbatim, with no client-side path normalisation."""

    authority = base_url.split("//", 1)[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", int(authority.rsplit(":", 1)[1]))
    try:
        writer.write(f"GET {raw_path} HTTP/1.1\r\nHost: {authority}\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        status_line = await reader.readline()
        return int(status_line.split(b" ")[1])
    finally:
        writer.close()


def test_review_browser_pairing_uses_review_only_origin_bound_capability(tmp_path: Path) -> None:
    asyncio.run(_exercise_review_browser_pairing(tmp_path))


def test_host_logo_assets_serve_but_the_bundle_root_stays_allowlisted(tmp_path: Path) -> None:
    asyncio.run(_exercise_host_logo_assets(tmp_path))


async def _exercise_host_logo_assets(tmp_path: Path) -> None:
    """``/system/hosts`` renders one ``<img>`` per host row.

    Those are bundle-root files, so a route that only knows ``/assets/`` turns
    every host mark into a broken image -- with the console itself loading fine.
    """
    token_file = tmp_path / "token"
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("<html>review</html>\n", encoding="utf-8")
    (frontend / "logos" / "hosts").mkdir(parents=True)
    (frontend / "logos" / "hosts" / "claude.svg").write_text("<svg></svg>\n", encoding="utf-8")
    (frontend / "logos" / "hosts" / "hermes.png").write_bytes(b"png-host-mark")
    (frontend / "secrets.txt").write_text("do not serve me\n", encoding="utf-8")
    server, _state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            token_file=token_file,
            frontend_dir=frontend,
            allow_local_fs=False,
        )
    )
    url = await server.start()
    try:
        async with ClientSession() as client:
            # The console page itself carries no credential; its icons must not
            # need one either, so both are public same-origin reads.
            assert (await client.get(f"{url}/system/hosts")).status == 200
            for logo_path, expected_type in (
                ("/logos/hosts/claude.svg", "image/svg+xml"),
                ("/logos/hosts/hermes.png", "image/png"),
            ):
                logo = await client.get(f"{url}{logo_path}")
                assert logo.status == 200, logo_path
                assert logo.headers["Content-Type"] == expected_type
            assert await (await client.get(f"{url}/logos/hosts/hermes.png")).read() == b"png-host-mark"
            # Only the allowlist is served from the bundle root: a logo prefix is
            # not a licence to publish whatever else a build dropped next to it.
            assert (await client.get(f"{url}/secrets.txt")).status in (401, 404)
            assert (await client.get(f"{url}/logos/hosts/")).status == 404
            # Sent raw: an HTTP client normalises %2e%2e away before it leaves,
            # which is exactly the spelling a traversal attempt would use.
            assert (await _raw_status(url, "/logos/%2e%2e/%2e%2e/etc/passwd")) == 404
    finally:
        await server.stop()


async def _exercise_review_browser_pairing(tmp_path: Path) -> None:
    from lemoncrow_server_core.local_browser_auth import LOCAL_BROWSER_SESSION_HEADER

    token_file = tmp_path / "token"
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<html>review</html>\n", encoding="utf-8")
    (frontend / "favicon.svg").write_text("<svg></svg>\n", encoding="utf-8")
    (frontend / "site.webmanifest").write_text('{"name":"LemonCrow"}\n', encoding="utf-8")
    (frontend / "lemoncrow-card.png").write_bytes(b"png-card")
    (frontend / "assets").mkdir()
    (frontend / "assets" / "app-1a2b.js").write_text("console.log('review')", encoding="utf-8")
    (frontend / "assets" / "app-1a2b.js.br").write_bytes(b"brotli-bytes")
    server, _state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            token_file=token_file,
            frontend_dir=frontend,
            allow_local_fs=False,
        )
    )
    machine_token = read_local_token(token_file)
    url = await server.start()
    review_path = "/r/00000000"
    machine_headers = {"Authorization": f"Bearer {machine_token}"}
    try:
        async with ClientSession() as client:
            # A clean Review URL contains no credential. Its same-origin UI
            # can claim a Review-only browser capability without a CLI launch.
            opened = await client.get(f"{url}{review_path}")
            assert opened.status == 200
            favicon = await client.get(f"{url}/favicon.svg")
            assert favicon.status == 200
            assert "<svg" in await favicon.text()
            manifest = await client.get(f"{url}/site.webmanifest")
            assert manifest.status == 200
            assert "LemonCrow" in await manifest.text()
            social_card = await client.get(f"{url}/lemoncrow-card.png")
            assert social_card.status == 200
            assert await social_card.read() == b"png-card"
            # Hashed bundles are immutable and served pre-compressed when offered.
            plain = await client.get(f"{url}/assets/app-1a2b.js", headers={"Accept-Encoding": "identity"})
            assert plain.headers["Cache-Control"] == "public, max-age=31536000, immutable"
            assert await plain.text() == "console.log('review')"
            brotli = await client.get(
                f"{url}/assets/app-1a2b.js", headers={"Accept-Encoding": "br"}, auto_decompress=False
            )
            assert brotli.headers["Content-Encoding"] == "br"
            assert brotli.headers["Vary"] == "Accept-Encoding"
            assert await brotli.read() == b"brotli-bytes"
            historical = await client.get(f"{url}/rr/00000000")
            assert historical.status == 200
            assert "Set-Cookie" not in opened.headers
            assert machine_token not in str(opened.url)
            assert (await client.get(f"{url}/v1/reviews")).status == 401

            # The persistent local console lands on Home; Reviews remains a
            # directly reachable trusted same-origin surface once paired.
            home = await client.get(f"{url}/")
            assert home.status == 200
            assert str(home.url).endswith("/home")
            claim_headers = {"Origin": url, "Sec-Fetch-Site": "same-origin"}

            async def arm(path: str) -> None:
                armed = await client.post(
                    f"{url}/v1/auth/local-browser/pair", json={"path": path}, headers=machine_headers
                )
                assert armed.status == 200, path

            # SL-1: same-origin headers are forgeable by any local process, so
            # an unarmed claim is refused with an actionable reconnect message.
            unarmed = await client.post(
                f"{url}/v1/auth/local-browser/claim", json={"path": "/reviews"}, headers=claim_headers
            )
            assert unarmed.status == 401
            assert "lc review --open" in await unarmed.text()

            # `lc dashboard open` arms ``/``; the browser claims where it lands.
            await arm("/")
            landing_claim = await client.post(
                f"{url}/v1/auth/local-browser/claim", json={"path": "/home"}, headers=claim_headers
            )
            assert landing_claim.status == 200

            await arm("/reviews")
            directory_claim = await client.post(
                f"{url}/v1/auth/local-browser/claim",
                json={"path": "/reviews"},
                headers=claim_headers,
            )
            assert directory_claim.status == 200
            directory_token = str((await directory_claim.json())["token"])
            directory_inbox = await client.get(
                f"{url}/v1/reviews",
                headers={LOCAL_BROWSER_SESSION_HEADER: directory_token},
            )
            assert directory_inbox.status == 200
            # The pairing window is single-use.
            spent = await client.post(
                f"{url}/v1/auth/local-browser/claim", json={"path": "/reviews"}, headers=claim_headers
            )
            assert spent.status == 401

            # The other persistent control-center pages bootstrap the same
            # review/control-scoped browser capability once paired.
            for console_path, api_path, expected_key in (
                ("/settings/integrations", "/api/local/integrations", "hosts"),
                ("/settings/diagnostics", "/api/local/system", "protocol_version"),
            ):
                console_page = await client.get(f"{url}{console_path}")
                assert console_page.status == 200
                await arm(console_path)
                console_claim = await client.post(
                    f"{url}/v1/auth/local-browser/claim",
                    json={"path": console_path},
                    headers=claim_headers,
                )
                assert console_claim.status == 200
                console_token = str((await console_claim.json())["token"])
                console_response = await client.get(
                    f"{url}{api_path}",
                    headers={LOCAL_BROWSER_SESSION_HEADER: console_token},
                )
                assert console_response.status == 200
                console_payload = await console_response.json()
                assert expected_key in console_payload
                if console_path == "/settings/integrations":
                    assert [tool["name"] for tool in console_payload["tools"]] == [
                        "bash",
                        "code_search",
                        "edit",
                        "read",
                        "web_fetch",
                    ]
                    assert console_payload["internal_dispatchable_count"] >= len(console_payload["tools"])
                if console_path == "/settings/diagnostics":
                    assert console_payload["tool_count"] == 5
            await arm(review_path)
            claimed = await client.post(
                f"{url}/v1/auth/local-browser/claim",
                json={"path": review_path},
                headers=claim_headers,
            )
            assert claimed.status == 200
            browser_token = str((await claimed.json())["token"])
            assert browser_token.startswith("v1.")
            assert machine_token not in browser_token

            # Reload and expiry renew by presenting the still-valid capability.
            replay = await client.post(
                f"{url}/v1/auth/local-browser/claim",
                json={"path": review_path},
                headers={**claim_headers, LOCAL_BROWSER_SESSION_HEADER: browser_token},
            )
            assert replay.status == 200
            assert (await replay.json())["token"] != browser_token
            forged_renewal = await client.post(
                f"{url}/v1/auth/local-browser/claim",
                json={"path": review_path},
                headers={**claim_headers, LOCAL_BROWSER_SESSION_HEADER: "v1.9999999999.nonce.forged"},
            )
            assert forged_renewal.status == 401
            unarmed_replay = await client.post(
                f"{url}/v1/auth/local-browser/claim", json={"path": review_path}, headers=claim_headers
            )
            assert unarmed_replay.status == 401

            cross_origin_claim = await client.post(
                f"{url}/v1/auth/local-browser/claim",
                json={"path": review_path},
                headers={"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"},
            )
            assert cross_origin_claim.status == 403

            browser_headers = {LOCAL_BROWSER_SESSION_HEADER: browser_token}
            inbox = await client.get(f"{url}/v1/reviews", headers=browser_headers)
            assert inbox.status == 200
            inbox_payload = await inbox.json()
            assert set(inbox_payload) == {"reviews", "status", "query", "next_cursor"}
            assert "counts" not in inbox_payload
            assert "bucket" not in inbox_payload

            # The derived capability is Review-only, never a replacement for the
            # machine credential on session/tool APIs.
            handshake = {
                "protocol_version": PROTOCOL_VERSION,
                "capabilities": sorted(REQUIRED_CLIENT_CAPABILITIES),
                "client_name": "browser-test",
                "client_version": "1",
            }
            sessions = await client.post(f"{url}/v1/sessions", json=handshake, headers=browser_headers)
            assert sessions.status == 401

            # Mutations additionally require explicit same-origin browser context.
            no_origin = await client.post(
                f"{url}/api/reviews/r-00000000/finish",
                json={"status": "finished"},
                headers=browser_headers,
            )
            assert no_origin.status == 401
            same_origin = await client.post(
                f"{url}/api/reviews/r-00000000/finish",
                json={"status": "finished"},
                headers={**browser_headers, "Origin": url, "Sec-Fetch-Site": "same-origin"},
            )
            assert same_origin.status == 404

            # A reverse proxy can connect through loopback; any standard proxy
            # forwarding context therefore disqualifies both pairing and browser
            # capability use. Cover Cloudflare and generic reverse-proxy spellings
            # so adding a tunnel in front of 7420 never turns it into local trust.
            forwarding_headers = {
                "Forwarded": "for=198.51.100.25;proto=https;host=review.example.com",
                "X-Forwarded-For": "198.51.100.25",
                "X-Forwarded-Host": "review.example.com",
                "X-Forwarded-Proto": "https",
                "X-Real-IP": "198.51.100.25",
                "CF-Connecting-IP": "198.51.100.25",
                "True-Client-IP": "198.51.100.25",
            }
            for header_name, header_value in forwarding_headers.items():
                tunneled_pair = await client.post(
                    f"{url}/v1/auth/local-browser/pair",
                    json={"path": review_path},
                    headers={**machine_headers, header_name: header_value},
                )
                assert tunneled_pair.status == 403, header_name
                tunneled_inbox = await client.get(
                    f"{url}/v1/reviews",
                    headers={**browser_headers, header_name: header_value},
                )
                assert tunneled_inbox.status == 401, header_name
    finally:
        await server.stop()


def test_review_browser_capability_is_bound_to_host_and_port() -> None:
    from lemoncrow_server_core.local_browser_auth import LocalBrowserSessions

    now = [1000.0]
    sessions = LocalBrowserSessions("lc_local_test_token_0123456789abcdef", clock=lambda: now[0])
    sessions.arm("/reviews/r-12345678")
    token, _expires = sessions.claim("/reviews/r-12345678", "127.0.0.1:7420")
    assert sessions.verify(token, "127.0.0.1:7420")
    assert not sessions.verify(token, "127.0.0.1:9999")
    assert not sessions.verify(token, "localhost:7420")
    now[0] += 31 * 24 * 60 * 60
    assert not sessions.verify(token, "127.0.0.1:7420")

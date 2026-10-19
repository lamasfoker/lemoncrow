"""SL-4: the `lc service start` listener is not trusted merely for being on loopback."""

from __future__ import annotations

import stat
import sys
import types
from pathlib import Path
from typing import Any

import pytest

FastAPITestClient = pytest.importorskip("fastapi.testclient", reason="service tests require the api extra").TestClient


@pytest.fixture()
def service_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".lemoncrow"
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("LEMONCROW_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("LEMONCROW_SERVICE_PORT", "8787")
    monkeypatch.delenv("LEMONCROW_REQUIRE_AUTH", raising=False)
    monkeypatch.delenv("LEMONCROW_API_KEY", raising=False)
    return root


def test_cors_trusts_only_the_served_origin(service_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.core.service.api import create_app

    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    client = FastAPITestClient(create_app(store_root=service_env))
    preflight = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"}

    other_port = client.options("/v1/swarm/runs", headers={**preflight, "Origin": "http://127.0.0.1:5173"})
    assert "access-control-allow-origin" not in other_port.headers
    served = client.options("/v1/swarm/runs", headers={**preflight, "Origin": "http://127.0.0.1:8787"})
    assert served.headers.get("access-control-allow-origin") == "http://127.0.0.1:8787"


def test_listener_refuses_a_rebound_host_header(service_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.core.service import api

    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    client = FastAPITestClient(api.create_service_app())

    rebound = client.get("/health", headers={"Host": "attacker.example:8787"})
    assert rebound.status_code == 421
    assert client.get("/health", headers={"Host": "127.0.0.1:8787"}).status_code == 200
    assert client.get("/health", headers={"Host": "localhost:8787"}).status_code == 200


def test_service_start_requires_a_minted_owner_only_key_on_loopback(
    service_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.core.service import api
    from lemoncrow.core.service.config import cfg, service_token_path

    launched: list[dict[str, Any]] = []
    fake_uvicorn = types.SimpleNamespace(run=lambda target, **kwargs: launched.append({"target": target, **kwargs}))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    api.main(host="127.0.0.1", port=8787)

    assert launched[0]["target"] == "lemoncrow.core.service.api:create_service_app"
    assert cfg.require_auth is True
    token_file = service_token_path()
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert cfg.api_key == token_file.read_text(encoding="utf-8").strip()
    assert len(cfg.api_key) >= 32

    client = FastAPITestClient(api.create_service_app(), headers={"Host": "127.0.0.1:8787"})
    assert client.get("/config").status_code == 401
    assert client.get("/config", headers={"Authorization": f"Bearer {cfg.api_key}"}).status_code == 200


def test_transport_sends_the_minted_key_only_to_loopback(service_env: Path) -> None:
    from lemoncrow.core.service.api import ensure_service_api_key
    from lemoncrow.gateway.transports.service import ServiceTransport

    key = ensure_service_api_key()

    assert ServiceTransport(base_url="http://127.0.0.1:8787")._api_key == key
    assert ServiceTransport(base_url="https://service.example.com")._api_key is None


def test_transport_never_forwards_authorization_across_a_redirect() -> None:
    """SL-17: urllib's default redirect handler copies Authorization to any host."""

    import http.server
    import threading

    from lemoncrow.gateway.transports.service import ServiceTransport

    seen: list[str] = []

    class Sink(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen.append(self.headers.get("Authorization", ""))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        do_GET = do_POST  # a 302 turns the POST into a GET

        def log_message(self, *_args: Any) -> None:
            pass

    sink = http.server.HTTPServer(("127.0.0.1", 0), Sink)

    class Redirect(Sink):
        def do_POST(self) -> None:
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{sink.server_port}/steal")
            self.end_headers()

    redirect = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    for server in (sink, redirect):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        transport = ServiceTransport(base_url=f"http://127.0.0.1:{redirect.server_port}", api_key="k" * 40, timeout=5)
        result = transport.get_context({"task": "t"})
    finally:
        sink.shutdown()
        redirect.shutdown()

    assert seen == []
    assert result["ok"] is False and result["error"] == "HTTP 302"

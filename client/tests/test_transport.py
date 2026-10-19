"""The one module that opens a connection, tested for what it refuses.

Everything about egress in this package reduces to this file's behaviour, so
the tests are about the boundary rather than about HTTP: one origin, no
redirect, no inherited proxy, no global state, and a transport failure that
arrives as a typed answer rather than as an exception the caller has to guess
at.
"""

from __future__ import annotations

import json
import select
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_client.errors import ClientError, ErrorCode
from lemoncrow_client.transport import HttpTransport


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:
        return

    def _reply(self, status: int, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/peer":
            self._reply(200, {"peer": list(self.client_address)})
            return
        if self.path == "/stale":
            self._reply(200, {"peer": list(self.client_address)})
            self.close_connection = True  # Simulate an idle timeout without a close header.
            return
        if self.path == "/close":
            self._reply(200, {"peer": list(self.client_address)}, {"Connection": "close"})
            return
        if self.path == "/slow":
            time.sleep(0.2)
            self._reply(200, {"ok": True})
            return
        if self.path == "/parallel":
            self.server.barrier.wait(timeout=5)  # type: ignore[attr-defined]
            self._reply(200, {"peer": list(self.client_address)})
            return
        if self.path == "/redirect":
            self._reply(302, {}, {"Location": "http://elsewhere.invalid/x"})
            return
        if self.path == "/refuse":
            self._reply(
                403,
                {
                    "error": {
                        "code": "forbidden",
                        "message": "nope",
                        "retryable": False,
                        "action": "request_access",
                        "details": {"scope": "tools"},
                    }
                },
            )
            return
        if self.path == "/garbage":
            body = b"<html>not json</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/echo-headers":
            self._reply(200, {"headers": {key.lower(): value for key, value in self.headers.items()}})
            return
        self._reply(200, {"ok": True, "path": self.path})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length).decode("ascii") if length else ""
        if self.path == "/drop-post":
            self.server.writes += 1  # type: ignore[attr-defined]
            self.close_connection = True
            return
        self._reply(
            200,
            {
                "path": self.path,
                "body": body,
                "content_type": self.headers.get("Content-Type", ""),
            },
        )


@pytest.fixture
def endpoint():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_successful_call_returns_the_decoded_payload(endpoint: str) -> None:
    transport = HttpTransport(endpoint, timeout_s=5.0)
    assert transport.get("/anything").require()["path"] == "/anything"


def test_oauth_form_post_is_urlencoded_and_origin_bound(endpoint: str) -> None:
    transport = HttpTransport(endpoint, timeout_s=5.0)
    payload = transport.post_form(
        "/oauth/token",
        form={"grant_type": "refresh_token", "scope": "openid email"},
    ).require()
    assert payload["path"] == "/oauth/token"
    assert payload["content_type"] == "application/x-www-form-urlencoded"
    assert payload["body"] == "grant_type=refresh_token&scope=openid+email"

    with pytest.raises(ClientError):
        transport.post_form("http://elsewhere.invalid/token", form={"token": "secret"})


def test_a_typed_refusal_is_raised_with_its_code_action_and_details(endpoint: str) -> None:
    transport = HttpTransport(endpoint, timeout_s=5.0)
    with pytest.raises(ClientError) as caught:
        transport.get("/refuse").require()
    assert caught.value.code is ErrorCode.FORBIDDEN
    assert caught.value.action.value == "request_access"
    assert caught.value.details == {"scope": "tools"}
    assert caught.value.retryable is False


def test_an_unknown_server_code_is_surfaced_rather_than_remapped(endpoint: str) -> None:
    """An unfamiliar refusal must stay visibly unfamiliar."""
    from lemoncrow_client.transport import Response

    error = ClientError.from_wire(
        {"error": {"code": "some_future_code", "message": "m", "retryable": True}}, status=400
    )
    assert error.code is ErrorCode.UNKNOWN
    assert error.server_code == "some_future_code"
    assert isinstance(Response(status=400, payload={}), Response)


def test_a_redirect_is_refused_rather_than_followed(endpoint: str) -> None:
    """A 302 is how a request to the endpoint becomes a request elsewhere."""
    transport = HttpTransport(endpoint, timeout_s=5.0)
    with pytest.raises(ClientError) as caught:
        transport.get("/redirect")
    assert caught.value.code is ErrorCode.NOT_CONFIGURED
    assert caught.value.details["status"] == 302
    assert caught.value.action.value == "abandon"


def test_a_request_to_another_origin_is_refused_before_it_is_sent(endpoint: str) -> None:
    transport = HttpTransport(endpoint, timeout_s=5.0)
    with pytest.raises(ClientError) as caught:
        transport.get("http://elsewhere.invalid/x")
    assert caught.value.code is ErrorCode.NOT_CONFIGURED
    assert caught.value.action.value == "abandon"


def test_a_closed_port_becomes_a_retryable_server_unreachable() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    with pytest.raises(ClientError) as caught:
        HttpTransport(f"http://127.0.0.1:{port}", timeout_s=2.0).get("/healthz")
    assert caught.value.code is ErrorCode.SERVER_UNREACHABLE
    assert caught.value.retryable is True


def test_a_server_that_never_answers_times_out_as_unreachable() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    try:
        with pytest.raises(ClientError) as caught:
            HttpTransport(f"http://127.0.0.1:{listener.getsockname()[1]}", timeout_s=0.4).get("/healthz")
    finally:
        listener.close()
    assert caught.value.code is ErrorCode.SERVER_UNREACHABLE


def test_a_non_json_answer_is_a_typed_error_not_a_crash(endpoint: str) -> None:
    with pytest.raises(ClientError) as caught:
        HttpTransport(endpoint, timeout_s=5.0).get("/garbage").require()
    assert caught.value.code is ErrorCode.INTERNAL


def test_the_transport_installs_no_global_opener_and_inherits_no_proxy(
    endpoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An environment proxy the client never audited is not a destination."""
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    before = urllib.request._opener  # type: ignore[attr-defined]
    transport = HttpTransport(endpoint, timeout_s=5.0)
    assert transport.get("/anything").require()["ok"] is True
    assert urllib.request._opener is before  # type: ignore[attr-defined]


def test_only_the_headers_the_caller_asked_for_are_sent(endpoint: str) -> None:
    transport = HttpTransport(endpoint, timeout_s=5.0)
    payload = transport.get("/echo-headers", headers={"X-LemonCrow-Session": "s1"}).require()
    headers = payload["headers"]
    assert headers["x-lemoncrow-session"] == "s1"
    assert headers["user-agent"] == "lemoncrow-client"
    assert "cookie" not in headers
    assert "authorization" not in headers


def test_sequential_requests_reuse_the_connection_without_leaking_headers(endpoint: str) -> None:
    transport = HttpTransport(endpoint, timeout_s=5)
    try:
        first = transport.get("/peer").require()
        transport.get("/echo-headers", headers={"Authorization": "Bearer secret"}).require()
        assert transport.get("/peer").require() == first
        assert "authorization" not in transport.get("/echo-headers").require()["headers"]
    finally:
        transport.close()


@pytest.mark.parametrize("path", ["/close", "/stale"])
def test_server_closed_connections_reconnect(endpoint: str, path: str) -> None:
    transport = HttpTransport(endpoint, timeout_s=5)
    try:
        first = transport.get(path).require()
        assert transport.get("/peer").require()["peer"] != first["peer"]
    finally:
        transport.close()


def test_post_discards_a_peer_closed_idle_connection_before_sending(endpoint: str) -> None:
    transport = HttpTransport(endpoint, timeout_s=5)
    try:
        transport.get("/stale").require()
        idle = transport._idle[0]
        assert idle.sock is not None
        # Wait for the peer's FIN, without a timing-dependent sleep.
        assert select.select([idle.sock], [], [], 5)[0]
        assert transport.post("/anything", body={"change": True}).require()["path"] == "/anything"
        assert idle not in transport._connections
    finally:
        transport.close()


def test_concurrent_requests_have_exclusive_connections_and_bounded_idle_pool() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.barrier = threading.Barrier(6)  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    transport = HttpTransport(f"http://127.0.0.1:{server.server_port}", timeout_s=5)
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            peers = list(pool.map(lambda _: transport.get("/parallel").require()["peer"], range(6)))
        assert len({tuple(peer) for peer in peers}) == 6
        assert len(transport._idle) == len(transport._connections) == 4
    finally:
        transport.close()
        assert not transport._connections
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_write_with_a_lost_response_is_not_replayed() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.writes = 0  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    transport = HttpTransport(f"http://127.0.0.1:{server.server_port}", timeout_s=5)
    try:
        transport.get("/peer").require()
        with pytest.raises(ClientError) as caught:
            transport.post("/drop-post", body={"change": True})
        assert caught.value.code is ErrorCode.SERVER_UNREACHABLE
        assert server.writes == 1  # type: ignore[attr-defined]
        assert transport.get("/peer").require()
    finally:
        transport.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_reused_connection_obeys_the_per_request_timeout(endpoint: str) -> None:
    transport = HttpTransport(endpoint, timeout_s=5)
    try:
        transport.get("/peer").require()
        with pytest.raises(ClientError) as caught:
            transport.get("/slow", timeout_s=0.05)
        assert caught.value.details["timed_out"] is True
        assert not transport._connections
    finally:
        transport.close()


def test_oversized_response_discards_the_connection(endpoint: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lemoncrow_client.transport._MAX_RESPONSE_BYTES", 4)
    transport = HttpTransport(endpoint, timeout_s=5)
    try:
        with pytest.raises(ClientError) as caught:
            transport.get("/peer")
        assert caught.value.code is ErrorCode.INTERNAL
        assert not transport._connections
    finally:
        transport.close()


def test_response_at_the_byte_limit_is_accepted(endpoint: str, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {"ok": True, "path": "/anything"}
    monkeypatch.setattr("lemoncrow_client.transport._MAX_RESPONSE_BYTES", len(json.dumps(expected).encode()))
    transport = HttpTransport(endpoint, timeout_s=5)
    try:
        assert transport.get("/anything").require() == expected
    finally:
        transport.close()


def test_close_is_idempotent_and_prevents_reopening(endpoint: str) -> None:
    transport = HttpTransport(endpoint, timeout_s=5)
    transport.get("/peer").require()
    transport.close()
    transport.close()
    assert not transport._connections
    with pytest.raises(ClientError) as caught:
        transport.get("/peer")
    assert caught.value.code is ErrorCode.NOT_CONFIGURED


def test_close_drains_an_active_request(endpoint: str) -> None:
    transport = HttpTransport(endpoint, timeout_s=5)
    started = threading.Event()
    original = transport._acquire

    def acquire(timeout_s: float) -> tuple[Any, bool]:
        result = original(timeout_s)
        started.set()
        return result

    # Coordinate close after checkout, before the response arrives.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(HttpTransport, "_acquire", lambda self, timeout_s: acquire(timeout_s))
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(transport.get, "/slow")
            assert started.wait(timeout=5)
            transport.close()
            assert pending.result().require()["ok"]
    assert not transport._connections


def test_https_reuses_connections_and_validates_certificates(tmp_path: Path) -> None:
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("openssl is needed for the loopback TLS certificate")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"https://localhost:{server.server_port}"
    transport = HttpTransport(url, timeout_s=5, ssl_context=ssl.create_default_context(cafile=str(cert)))
    untrusted = HttpTransport(url, timeout_s=5)
    try:
        assert transport.get("/peer").require() == transport.get("/peer").require()
        with pytest.raises(ClientError) as caught:
            untrusted.get("/peer")
        assert caught.value.code is ErrorCode.SERVER_UNREACHABLE
    finally:
        transport.close()
        untrusted.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("attack", ["redirect", "invalid_accept", "oversized_frame", "masked_frame", "server_command"])
def test_outbound_review_websocket_refuses_untrusted_handshake_and_frames(
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    """No redirects, no foreign upgrade and bounded, plain server text only."""
    import base64
    import hashlib

    class FakeSocket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.inbound = bytearray()
            self.closed = False

        def settimeout(self, timeout: float) -> None:
            assert timeout == 2

        def sendall(self, data: bytes) -> None:
            self.sent.append(data)
            if b"GET /v1/review-workers/wakeup?" not in data:
                return
            assert b"Authorization: Bearer machine-token" in data
            assert b"Upgrade: websocket" in data
            assert b"Sec-WebSocket-Extensions:" not in data
            client_key = data.split(b"Sec-WebSocket-Key: ")[1].split(b"\r\n")[0]
            accepted = base64.b64encode(hashlib.sha1(client_key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
            if attack == "invalid_accept":
                accepted = b"not-the-proper-key"
            if attack == "redirect":
                self.inbound.extend(b"HTTP/1.1 302 Found\r\nLocation: http://evil.invalid\r\n\r\n")
            else:
                self.inbound.extend(
                    b"HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n"
                    b"Upgrade: websocket\r\nSec-WebSocket-Accept: " + accepted + b"\r\n\r\n"
                )
            if attack == "oversized_frame":
                self.inbound.extend(b"\x81\x7e\x10\x00")
            if attack == "masked_frame":
                self.inbound.extend(b"\x81\x80")
            if attack == "server_command":
                # Generic transport parses an object; the Review cue validator
                # refuses the extra command / arbitrary payload.
                content = b'{"command":"run shell"}'
                self.inbound.extend(b"\x81" + bytes([len(content)]) + content)

        def recv(self, size: int) -> bytes:
            if not self.inbound:
                return b""
            part = bytes(self.inbound[:size])
            del self.inbound[:size]
            return part

        def close(self) -> None:
            self.closed = True

    sock = FakeSocket()
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: sock)
    transport = HttpTransport("http://127.0.0.1:9020", timeout_s=2)
    try:
        stream = transport.websocket_hints(
            "/v1/review-workers/wakeup?execution_class=runtime_validation",
            bearer="machine-token",
            timeout_s=2,
            max_messages=1,
        )
        if attack == "server_command":
            from lemoncrow_client.review_companion_cli import validate_pending_hint

            with pytest.raises(ValueError, match="class differs"):
                validate_pending_hint(next(stream), execution_class="runtime_validation")
            stream.close()
        else:
            with pytest.raises(ValueError):
                list(stream)
    finally:
        transport.close()
    assert sock.closed


def test_review_wakeup_refuses_foreign_origin_and_cleartext_remote_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_kw: pytest.fail("unexpected egress"))
    transport = HttpTransport("http://example.invalid", timeout_s=2)
    try:
        with pytest.raises(ValueError, match="requires TLS"):
            list(
                transport.websocket_hints(
                    "/v1/review-workers/wakeup?execution_class=runtime_validation",
                    bearer="t",
                    timeout_s=2,
                )
            )
        with pytest.raises(ClientError, match="origin"):
            list(
                transport.websocket_hints(
                    "https://another.invalid/v1/review-workers/wakeup?execution_class=runtime_validation",
                    bearer="t",
                    timeout_s=2,
                )
            )
        secure = HttpTransport("https://example.invalid", timeout_s=2)
        try:
            with pytest.raises(ValueError, match="fixed pending-work hints"):
                list(secure.websocket_hints("/v1/admin", bearer="t", timeout_s=2))
        finally:
            secure.close()
    finally:
        transport.close()

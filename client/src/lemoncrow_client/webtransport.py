"""Client-only public-page HTTP transport. Fresh connections; no product credentials."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

HTTPException = http.client.HTTPException

MAX_BODY_BYTES = 2_000_000
MAX_PDF_BODY_BYTES = 50_000_000
RETRY_STATUSES = {403, 408, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class Page:
    url: str
    status: int
    content_type: str
    body: bytes


def validate_url(url: str) -> None:
    if not url or any(ord(c) < 32 or ord(c) == 127 for c in url):
        raise ValueError("web_fetch URL is empty or contains control characters")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("web_fetch requires an HTTP/HTTPS URL with a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("web_fetch does not allow embedded credentials")
    _ = parsed.port


def _public_addresses(host: str, port: int, *, allow_loopback: bool) -> list[str]:
    addresses = list(dict.fromkeys(str(info[4][0]) for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    if not addresses:
        raise ValueError(f"web_fetch could not resolve host: {host}")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_loopback and allow_loopback:
            continue
        if not ip.is_global or ip.is_multicast:
            raise ValueError(f"web_fetch blocked private/local network IP: {address}")
    return addresses


def _once(url: str, *, accept: str, deadline: float, allow_loopback: bool) -> Page:
    for _ in range(6):
        validate_url(url)
        parsed = urlsplit(url)
        host = (parsed.hostname or "").encode("idna").decode("ascii")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = _public_addresses(host, port, allow_loopback=allow_loopback)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("web_fetch timed out")
        # Pin the validated IP. Preserve the hostname for Host, TLS SNI and certificate verification.
        connection = http.client.HTTPConnection(host, port, timeout=remaining)
        try:
            connection.sock = socket.create_connection((addresses[0], port), timeout=remaining)
            if parsed.scheme == "https":
                connection.sock = ssl.create_default_context().wrap_socket(connection.sock, server_hostname=host)
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            connection.request(
                "GET",
                target,
                headers={
                    "Accept": accept,
                    "Accept-Encoding": "identity",
                    "User-Agent": "Mozilla/5.0 LemonCrow/web_fetch",
                    "Connection": "close",
                },
            )
            stream_socket = connection.sock
            response = connection.getresponse()
            try:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location:
                        raise ValueError(f"web_fetch failed: HTTP {response.status} redirect without Location")
                    url = urljoin(url, location)
                    continue
                content_type = response.getheader("Content-Type", "")
                media = content_type.split(";", 1)[0].lower().strip()
                if not (
                    media.startswith("text/")
                    or media
                    in {
                        "application/json",
                        "application/xml",
                        "application/xhtml+xml",
                        "application/pdf",
                    }
                ):
                    raise ValueError(f"web_fetch unsupported content type: {media or 'unknown'}")
                limit = MAX_PDF_BODY_BYTES if media == "application/pdf" else MAX_BODY_BYTES
                chunks: list[bytes] = []
                size = 0
                while size <= limit:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("web_fetch timed out")
                    # getresponse may detach a Connection: close socket into
                    # the response file, so retain the socket for deadline updates.
                    if stream_socket is not None:
                        stream_socket.settimeout(remaining)
                    chunk = response.read1(min(65_536, limit + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                if size > limit:
                    raise ValueError(f"web_fetch body exceeds {limit} bytes")
                return Page(url, response.status, content_type, b"".join(chunks))
            finally:
                response.close()
        finally:
            connection.close()
    raise ValueError("web_fetch failed: too many redirects")


def fetch_page(url: str, *, accept: str, timeout: float = 20.0, allow_loopback: bool = False) -> Page:
    deadline = time.monotonic() + timeout
    for attempt in range(3):
        try:
            page = _once(url, accept=accept, deadline=deadline, allow_loopback=allow_loopback)
        except (OSError, http.client.HTTPException):
            if attempt == 2 or time.monotonic() + 0.25 * (attempt + 1) >= deadline:
                raise
        else:
            if page.status not in RETRY_STATUSES or attempt == 2 or time.monotonic() + 0.25 * (attempt + 1) >= deadline:
                return page
        delay = 0.25 * (attempt + 1)
        if time.monotonic() + delay >= deadline:
            raise TimeoutError("web_fetch timed out")
        time.sleep(delay)
    raise RuntimeError("web_fetch failed")

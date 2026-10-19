"""A service's client for the core's ``/internal/v1`` back-channel.

Review and Dashboard never open the code index: the core owns it, and a
service asks for what it needs -- current-index code intelligence and
content-addressed bytes -- over the same token that authenticates the core's
forwarded requests. Calls come from request worker threads, so this is a
synchronous stdlib client with one keep-alive connection per thread.
"""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, urlsplit

from lemoncrow.pro.capabilities.review.code_intelligence import CodeIntelligenceReadiness

from .service_protocol import HEADER_SERVICE_TOKEN, INTERNAL_PREFIX

__all__ = ["CoreClient", "CoreUnavailable", "RemoteCodeIntelligence"]

_TIMEOUT_S: Final[float] = 60.0


class CoreUnavailable(RuntimeError):
    """The core refused or could not be reached."""


class CoreClient:
    __slots__ = ("_host", "_local", "_port", "_token")

    def __init__(self, base_url: str, token: str) -> None:
        parts = urlsplit(base_url)
        if parts.scheme != "http" or not parts.hostname or not parts.port:
            raise ValueError(f"core URL must be http://host:port, got {base_url!r}")
        self._host = parts.hostname
        self._port = parts.port
        self._token = token
        self._local = threading.local()

    def _connection(self) -> http.client.HTTPConnection:
        connection: http.client.HTTPConnection | None = getattr(self._local, "connection", None)
        if connection is None:
            connection = http.client.HTTPConnection(self._host, self._port, timeout=_TIMEOUT_S)
            self._local.connection = connection
        return connection

    def _request(self, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
        headers = {HEADER_SERVICE_TOKEN: self._token}
        if body is not None:
            headers["Content-Type"] = "application/json"
        # A kept-alive connection the core closed while idle fails on first use;
        # one fresh connection settles it. Both calls are reads, so resending is safe.
        for attempt in range(2):
            connection = self._connection()
            try:
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                return response.status, response.read()
            except (OSError, http.client.HTTPException) as exc:
                connection.close()
                self._local.connection = None
                if attempt:
                    raise CoreUnavailable(f"the LemonCrow core is unreachable: {exc}") from exc
        raise AssertionError("unreachable")

    def content(self, digest: str) -> bytes | None:
        status, payload = self._request("GET", f"{INTERNAL_PREFIX}content/{quote(digest, safe='')}")
        if status == 404:
            return None
        if status != 200:
            raise CoreUnavailable(f"content read refused with HTTP {status}")
        return payload

    def code_intelligence(self, scope: Mapping[str, Any], method: str, *args: Any, **kwargs: Any) -> Any:
        body = json.dumps({"scope": dict(scope), "method": method, "args": list(args), "kwargs": kwargs}).encode()
        status, payload = self._request("POST", f"{INTERNAL_PREFIX}code-intelligence", body)
        if status != 200:
            raise CoreUnavailable(f"code intelligence {method} refused with HTTP {status}: {payload[:200]!r}")
        return json.loads(payload).get("result")

    def review_code_intelligence(self, repo_id: str, revision: Any) -> RemoteCodeIntelligence:
        """The provider for one Review revision; the core re-reads the revision per call."""
        return RemoteCodeIntelligence(self, {"repo_id": repo_id, "revision_id": str(getattr(revision, "id", ""))})

    def root_code_intelligence(self, project_root: Path) -> RemoteCodeIntelligence | None:
        """The provider for a proven worktree, or ``None`` when the core has no View for it."""
        scope = {"project_root": str(project_root)}
        return RemoteCodeIntelligence(self, scope) if self.code_intelligence(scope, "probe") else None


class RemoteCodeIntelligence:
    """``CodeIntelligenceProvider`` answered by the core that owns the index.

    Arguments and results are JSON: a tuple comes back as a list.
    """

    __slots__ = ("_client", "_scope")

    def __init__(self, client: CoreClient, scope: Mapping[str, Any]) -> None:
        self._client = client
        self._scope = dict(scope)

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        return self._client.code_intelligence(self._scope, method, *args, **kwargs)

    def readiness(self) -> CodeIntelligenceReadiness:
        return CodeIntelligenceReadiness.from_wire(self._call("readiness") or {})

    def find_symbol(self, symbol_name: str, **kwargs: Any) -> Mapping[str, Any] | None:
        result: Mapping[str, Any] | None = self._call("find_symbol", symbol_name, **kwargs)
        return result

    def neighborhood(self, symbol: Mapping[str, Any], **kwargs: Any) -> Mapping[str, Any]:
        return dict(self._call("neighborhood", dict(symbol), **kwargs) or {})

    def file_relationships(self, paths: tuple[str, ...], **kwargs: Any) -> Mapping[str, Any]:
        return dict(self._call("file_relationships", list(paths), **kwargs) or {})

    def references(self, symbol: Mapping[str, Any], **kwargs: Any) -> Mapping[str, Any]:
        return dict(self._call("references", dict(symbol), **kwargs) or {})

    def semantic_search(self, query: str, **kwargs: Any) -> Mapping[str, Any]:
        return dict(self._call("semantic_search", query, **kwargs) or {})

    def ranking_metadata(self, symbol: Mapping[str, Any]) -> Mapping[str, Any]:
        return dict(self._call("ranking_metadata", dict(symbol)) or {})

    def repository_file_links(self) -> Mapping[str, Any]:
        return dict(self._call("repository_file_links") or {})

    def path_digest(self, path: str) -> str:
        return str(self._call("path_digest", path) or "")

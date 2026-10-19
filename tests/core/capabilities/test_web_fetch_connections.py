"""Unread responses must be closed before their connection returns to the pool."""

from __future__ import annotations

from typing import Any

import pytest

from lemoncrow.core.capabilities import web_fetch


def test_pending_response_is_closed_before_pool_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    for _case_index, case in enumerate(["redirect", "unsupported", "capped", "read_failure"]):
        with monkeypatch.context() as case_monkeypatch:
            events: list[str] = []

            class Response:
                status = 302 if case == "redirect" else 200
                headers = (
                    {"location": "/next", "content-type": "text/plain"}
                    if case == "redirect"
                    else {"content-type": "application/octet-stream" if case == "unsupported" else "text/plain"}
                )

                def stream(self, _case: str = case, **_kwargs: Any) -> Any:
                    if _case == "read_failure":
                        raise RuntimeError("read failed")
                    yield (b"x" * (web_fetch.MAX_BODY_BYTES + 1 if _case == "capped" else 4))

                def close(self, _events: list[str] = events) -> None:
                    _events.append("close")

                def release_conn(self, _events: list[str] = events) -> None:
                    assert _events[-1] == "close"
                    _events.append("release")

            class Pool:

                def request(self, *_args: Any, _events: list[str] = events, **_kwargs: Any) -> Response:
                    if _events:
                        assert _events == ["close", "release"]
                        raise RuntimeError("next hop reached safely")
                    return Response()

            case_monkeypatch.setattr(web_fetch, "_HTTP", Pool())
            if case == "capped":
                result = web_fetch._fetch_uncached_once("https://example.com/start", accept="text/plain", timeout_s=5)
                assert result.truncated_body
            else:
                with pytest.raises((RuntimeError, ValueError)):
                    web_fetch._fetch_uncached_once("https://example.com/start", accept="text/plain", timeout_s=5)
            assert events == ["close", "release"]

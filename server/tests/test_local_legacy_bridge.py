"""The loopback server's in-process bridge to the legacy dashboard API."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import StreamingResponse
from lemoncrow_server_core.bootstrap import build_local_server, read_local_token
from lemoncrow_server_core.config import LocalServerConfig
from lemoncrow_server_core.local_app import LocalServerApp


def _legacy_app(gate: asyncio.Event) -> FastAPI:
    app = FastAPI()
    app.add_middleware(GZipMiddleware, minimum_size=1_000)

    @app.get("/big")
    def big() -> dict[str, Any]:
        return {"rows": ["x" * 64] * 200}

    @app.post("/stream")
    async def stream() -> StreamingResponse:
        async def chunks() -> Any:
            yield b"first\n"
            await gate.wait()
            yield b"second\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    return app


def test_the_bridge_skips_loopback_gzip_and_streams_chunk_by_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_exercise(tmp_path, monkeypatch))


async def _exercise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_file = tmp_path / "token"
    server, _state = build_local_server(
        LocalServerConfig(listen_port=0, state_root=tmp_path / "state", token_file=token_file, allow_local_fs=False)
    )
    gate = asyncio.Event()
    legacy = _legacy_app(gate)
    monkeypatch.setattr(LocalServerApp, "_legacy_dashboard", lambda self: legacy)
    headers = {"Authorization": f"Bearer {read_local_token(token_file)}", "Accept-Encoding": "gzip"}
    url = await server.start()
    try:
        async with ClientSession(auto_decompress=False) as client:
            big = await client.get(f"{url}/api/big", headers=headers)
            assert big.status == 200
            # Loopback only: compressing here costs more CPU than the bytes it saves.
            assert "Content-Encoding" not in big.headers
            assert len((await big.json())["rows"]) == 200

            # The first chunk reaches the client while the generator is still
            # waiting, and the stream is not cut off once the body was read.
            streamed = await asyncio.wait_for(client.post(f"{url}/api/stream", headers=headers, json={}), 5)
            assert streamed.status == 200
            assert streamed.headers["Content-Type"].startswith("text/event-stream")
            assert await asyncio.wait_for(streamed.content.readline(), 5) == b"first\n"
            gate.set()
            assert await asyncio.wait_for(streamed.content.read(), 5) == b"second\n"
    finally:
        gate.set()
        await server.stop()

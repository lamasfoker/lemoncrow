"""UX0a over the local server: Settings reads this machine's AI route and background envelope."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig

from lemoncrow.core import settings

PATH = "/api/review-intelligence"


@pytest.fixture(autouse=True)
def settings_root(tmp_path: Path) -> Iterator[Path]:
    from lemoncrow.pro.capabilities.model_setup.transport import set_active_store_root

    root = tmp_path / "lemoncrow-root"
    root.mkdir()
    set_active_store_root(root)
    try:
        yield root
    finally:
        set_active_store_root(None)


def _get(tmp_path: Path, *, no_auth: bool = True) -> tuple[int, Any]:
    async def exercise() -> tuple[int, Any]:
        server, _state = build_local_server(
            LocalServerConfig(
                listen_port=0,
                state_root=tmp_path / "state",
                local_no_auth=no_auth,
                token_file=None if no_auth else tmp_path / "token",
                allow_local_fs=False,
            )
        )
        url = await server.start()
        try:
            async with ClientSession() as client:
                response = await client.get(f"{url}{PATH}")
                body = await response.json(content_type=None)
                return response.status, body
        finally:
            await server.stop()

    return asyncio.run(exercise())


def test_settings_endpoint_serves_the_local_projection(tmp_path: Path, settings_root: Path) -> None:
    settings.write_setting(settings_root, "intelligence.background.monthly_model_calls", 40)
    status, body = _get(tmp_path)
    assert status == 200, body
    assert body["scope"] == "local"
    assert body["intelligence"]["editable"] is True
    assert "provider credentials" in body["intelligence"]["edit_source"]
    assert body["intelligence"]["run_on"]["effective_value"] == "local"
    assert body["preferences"]["intelligence.background.monthly_model_calls"] == 40
    assert body["background"]["available"] is True
    assert body["background"]["policy"]["monthly_model_calls"] == 40


def test_settings_endpoint_requires_the_local_credential(tmp_path: Path) -> None:
    status, _body = _get(tmp_path, no_auth=False)
    assert status in {401, 403}

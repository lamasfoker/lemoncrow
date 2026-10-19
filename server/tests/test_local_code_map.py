"""The local Dashboard serves useful Code features over Runtime Lite.

Deterministic repository views use the open-source Runtime Lite index and label
relationships import-level. Local-machine actions (Ask, editor handoff and
Review capture) stay available; precise references, generated module purpose and
agent handoff remain richer/full-product capabilities.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession
from lemoncrow_server_core.bootstrap import build_local_server, read_local_token
from lemoncrow_server_core.config import LocalServerConfig

pytest.importorskip("lemoncrow.core.service.api", reason="the dashboard data still comes from the full package")

_MOUNTED = (
    "projects",
    "modules",
    "relationships",
    "focus",
    "files",
    "file-symbols",
    "file-content",
    "search",
    "symbol",
    "changes",
    "file-changes",
    "hotspots",
    "activity",
)
_HOSTED = (
    ("GET", "references"),
    ("POST", "module-purpose"),
    ("POST", "agent-handoff"),
)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "core.py").write_text("def charge():\n    return 1\n", encoding="utf-8")
    (root / "pkg" / "api.py").write_text("from pkg.core import charge\n\n\ndef run():\n    return charge()\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    (tmp_path / "outside.py").write_text("secret = True\n", encoding="utf-8")
    return root


@pytest.fixture()
def private_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.core.capabilities import code_intelligence_provider as seam
    from lemoncrow.core.service import code_map, code_map_lite

    def absent(project_root: str) -> Any:
        raise seam.ProEngineUnavailable(seam.PRO_ENGINE_UNAVAILABLE_MESSAGE)

    monkeypatch.setattr(code_map, "get_engine", absent)
    monkeypatch.setattr(seam, "private_code_intelligence_available", lambda: False)
    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    code_map_lite.lite_engine.cache_clear()


@pytest.mark.usefixtures("private_absent")
def test_local_dashboard_serves_the_deterministic_code_map_only(tmp_path: Path) -> None:
    asyncio.run(_exercise(tmp_path, _repo(tmp_path)))


async def _exercise(tmp_path: Path, repo: Path) -> None:
    token_file = tmp_path / "token"
    server, _state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            token_file=token_file,
            allow_local_fs=False,
            isolate_services=False,
        )
    )
    url = await server.start()
    auth = {"Authorization": f"Bearer {read_local_token(token_file)}"}
    base = f"{url}/api/v1/code-map"
    root = {"project_root": str(repo)}
    try:
        async with ClientSession() as client:
            identity = await (await client.get(f"{url}/.well-known/lemoncrow-server.json")).json()
            assert identity["product_capabilities"]["dashboard"] == {
                "code_map": True,
                "code_ask": True,
                "local_editor": True,
                "review_capture": True,
                "project_knowledge": True,
            }
            # The same auth as every other local Dashboard route.
            assert (await client.get(f"{base}/files", params=root)).status == 401

            symbols = await client.get(f"{base}/file-symbols", params={**root, "path": "pkg/core.py"}, headers=auth)
            assert symbols.status == 200
            symbol_id = (await symbols.json())["symbols"][0]["id"]
            module = (await (await client.get(f"{base}/modules", params=root, headers=auth)).json())["modules"][0]["id"]
            queries: dict[str, dict[str, str]] = {
                "relationships": {"path": "pkg/core.py"},
                "focus": {"level": "module", "id": module},
                "file-symbols": {"path": "pkg/core.py"},
                "file-content": {"path": "pkg/core.py"},
                "search": {"q": "charge"},
                "symbol": {"symbol_id": symbol_id},
                "file-changes": {"path": "pkg/core.py"},
            }
            for route in _MOUNTED:
                response = await client.get(f"{base}/{route}", params={**root, **queries.get(route, {})}, headers=auth)
                assert response.status == 200, (route, await response.text())

            for params in ({"path": "pkg/core.py"}, {"symbol_id": symbol_id}):
                related = await (
                    await client.get(f"{base}/relationships", params={**root, **params}, headers=auth)
                ).json()
                assert related["context"]["relationship_basis"] == "imports"
                assert related["context"]["degraded"] is True
                assert [row["kind"] for row in related["used_by"]] == ["imports"]
            modules = await (await client.get(f"{base}/modules", params=root, headers=auth)).json()
            assert modules["context"]["relationship_basis"] == "imports"

            for escape in ("../outside.py", str(tmp_path / "outside.py")):
                refused = await client.get(f"{base}/file-content", params={**root, "path": escape}, headers=auth)
                assert refused.status == 400, escape
                assert "secret" not in await refused.text()

            for method, route in _HOSTED:
                response = await client.request(method, f"{base}/{route}", params=root, json={}, headers=auth)
                assert response.status == 404, (method, route)
                assert "hosted" in (await response.json())["detail"]
    finally:
        await server.stop()

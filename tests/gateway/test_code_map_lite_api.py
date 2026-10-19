"""`/v1/code-map` over Runtime Lite when the private engine is absent, and unchanged when present."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from lemoncrow.core.capabilities import code_intelligence_provider as seam
from lemoncrow.core.service import code_map, code_map_lite, code_surface
from lemoncrow.core.service.api import create_app
from lemoncrow.infra.storage.bundle import StoreBundle, build_sqlite_store_bundle

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

FastAPITestClient = pytest.importorskip(
    "fastapi.testclient", reason="FastAPI API tests require the api extra"
).TestClient

_FILES = {
    "src/pkg/__init__.py": "",
    "src/pkg/core.py": "def charge():\n    return 1\n",
    "src/pkg/api.py": "from pkg.core import charge\n\n\ndef handler():\n    return charge()\n",
}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for relative, text in _FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    (tmp_path / "outside.py").write_text("secret = True\n", encoding="utf-8")
    code_surface._MODULES_CACHE.clear()
    code_map_lite.lite_engine.cache_clear()
    return root


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    store: StoreBundle = build_sqlite_store_bundle(tmp_path / ".lemoncrow")
    store.init()
    return cast("TestClient", FastAPITestClient(create_app(store_root=store.knowledge.root)))


@pytest.fixture()
def private_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    def absent(project_root: str) -> Any:
        raise seam.ProEngineUnavailable(seam.PRO_ENGINE_UNAVAILABLE_MESSAGE)

    monkeypatch.setattr(code_map, "get_engine", absent)
    monkeypatch.setattr(seam, "private_code_intelligence_available", lambda: False)


def _import_level(payload: dict[str, Any]) -> None:
    context = payload["context"]
    assert context["relationship_basis"] == "imports"
    assert context["degraded"] is True
    assert context["degraded_reason"] == "lite_import_level"


@pytest.mark.usefixtures("private_absent")
def test_deterministic_routes_read_runtime_lite_and_say_import_level(
    client: TestClient, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        code_map, "list_projects", lambda: [{"project_id": "proj_x", "root": str(repo), "indexed": False}]
    )
    params = {"project_root": str(repo)}

    files = client.get("/v1/code-map/files", params=params)
    assert files.status_code == 200
    assert {row["path"] for row in files.json()["files"]} == set(_FILES)
    projects = client.get("/v1/code-map/projects").json()["projects"]
    assert [(row["index"], row["indexed"]) for row in projects] == [("runtime_lite", True)]

    symbols = client.get("/v1/code-map/file-symbols", params={**params, "path": "src/pkg/core.py"}).json()
    assert [row["label"] for row in symbols["symbols"]] == ["charge"]
    symbol_id = symbols["symbols"][0]["id"]

    search = client.get("/v1/code-map/search", params={**params, "q": "charge"}).json()
    assert [row["id"] for row in search["results"]] == [symbol_id]
    assert {row["path"] for row in search["text_results"]} == {"src/pkg/core.py", "src/pkg/api.py"}
    detail = client.get("/v1/code-map/symbol", params={**params, "symbol_id": symbol_id}).json()
    assert (detail["provenance"], detail["source"]) == ("runtime_lite", "def charge():")

    by_file = client.get("/v1/code-map/relationships", params={**params, "path": "src/pkg/core.py"}).json()
    _import_level(by_file)
    assert [(row["path"], row["kind"]) for row in by_file["used_by"]] == [("src/pkg/api.py", "imports")]
    by_symbol = client.get("/v1/code-map/relationships", params={**params, "symbol_id": symbol_id}).json()
    _import_level(by_symbol)

    modules = client.get("/v1/code-map/modules", params=params).json()
    _import_level(modules)
    assert modules["links_truncated"] is False
    for level, ident in (("module", modules["modules"][0]["id"]), ("file", "src/pkg/core.py"), ("symbol", symbol_id)):
        focus = client.get("/v1/code-map/focus", params={**params, "level": level, "id": ident})
        assert focus.status_code == 200, (level, focus.text)
        _import_level(focus.json())
        assert {edge["kind"] for edge in focus.json()["graph"]["edges"]} <= {"imports"}

    for route, extra in (
        ("changes", {}),
        ("file-changes", {"path": "src/pkg/core.py"}),
        ("hotspots", {}),
        ("activity", {}),
    ):
        response = client.get(f"/v1/code-map/{route}", params={**params, **extra})
        assert response.status_code == 200, (route, response.text)


# One small repository per language, side by side, in styles the line-only parser missed.
_MIXED = {
    "py/pyproject.toml": "[project]\nname = 'shop'\n",
    "py/src/shop/__init__.py": "",
    "py/src/shop/pricing.py": "def total():\n    return 1\n",
    "py/src/shop/cart.py": "from .pricing import (\n    total,\n)\nimport requests\n",
    "package.json": '{"name": "mono", "private": true, "workspaces": ["packages/*", "apps/*"]}',
    "tsconfig.json": '{\n  // aliases\n  "compilerOptions": {"paths": {"@app/*": ["./apps/web/src/*"]},},\n}\n',
    "packages/ui/package.json": '{"name": "@mono/ui", "main": "dist/index.js"}',
    "packages/ui/src/index.ts": "export const Button = 1;\n",
    "apps/web/src/util.ts": "export const util = 1;\n",
    "apps/web/src/main.tsx": (
        'import {\n  Button,\n} from "@mono/ui";\nimport { util } from "@app/util";\nimport React from "react";\n'
    ),
    "svc/go.mod": "module example.com/svc\n",
    "svc/store/store.go": "package store\n",
    "svc/cmd/main.go": 'package main\n\nimport (\n\t"fmt"\n\t"example.com/svc/store"\n)\n\nfunc main() {}\n',
    "rs/Cargo.toml": '[package]\nname = "rs"\n',
    "rs/src/model.rs": "pub struct Thing;\n",
    "rs/src/lib.rs": "mod model;\nuse crate::model::{\n    Thing,\n};\nuse std::fmt;\n",
}


@pytest.mark.usefixtures("private_absent")
def test_relationships_link_a_mixed_language_repository(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "mixed"
    for relative, text in _MIXED.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    code_surface._MODULES_CACHE.clear()
    code_map_lite.lite_engine.cache_clear()
    params = {"project_root": str(root)}

    for target, importer in (
        ("py/src/shop/pricing.py", "py/src/shop/cart.py"),
        ("packages/ui/src/index.ts", "apps/web/src/main.tsx"),
        ("apps/web/src/util.ts", "apps/web/src/main.tsx"),
        ("svc/store/store.go", "svc/cmd/main.go"),
        ("rs/src/model.rs", "rs/src/lib.rs"),
    ):
        payload = client.get("/v1/code-map/relationships", params={**params, "path": target}).json()
        _import_level(payload)
        assert [(row["path"], row["kind"]) for row in payload["used_by"]] == [(importer, "imports")], target
    uses = client.get("/v1/code-map/relationships", params={**params, "path": "apps/web/src/main.tsx"}).json()
    # ``react`` is an installed package: no link, and nothing reported missing.
    assert {row["path"] for row in uses["uses"]} == {"packages/ui/src/index.ts", "apps/web/src/util.ts"}


@pytest.mark.usefixtures("private_absent")
def test_file_content_stays_inside_the_selected_worktree(client: TestClient, repo: Path) -> None:
    params = {"project_root": str(repo)}
    inside = client.get("/v1/code-map/file-content", params={**params, "path": "src/pkg/core.py"})
    assert (inside.status_code, inside.text) == (200, _FILES["src/pkg/core.py"])
    (repo / "link.py").symlink_to(repo.parent / "outside.py")
    for escape in ("../outside.py", str(repo.parent / "outside.py"), "src/../../outside.py", "link.py"):
        refused = client.get("/v1/code-map/file-content", params={**params, "path": escape})
        assert refused.status_code == 400, escape
        assert "secret" not in refused.text


@pytest.mark.usefixtures("private_absent")
def test_references_and_model_routes_never_answer_from_runtime_lite(
    client: TestClient, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    params = {"project_root": str(repo)}
    symbol_id = client.get("/v1/code-map/search", params={**params, "q": "charge"}).json()["results"][0]["id"]
    references = client.get("/v1/code-map/references", params={**params, "symbol_id": symbol_id})
    assert references.status_code == 501
    assert "LemonCrow Hosted" in references.json()["detail"]

    def no_lite(project_root: str) -> Any:
        raise AssertionError("model-backed routes must not read Runtime Lite")

    monkeypatch.setattr(code_map_lite, "lite_engine", no_lite)
    with pytest.raises(seam.ProEngineUnavailable):
        client.post("/v1/code-map/module-purpose", json={**params, "module": "src"})
    with pytest.raises(seam.ProEngineUnavailable):
        client.post("/v1/code-map/agent-handoff", json={**params, "path": "src/pkg/core.py", "message": "Look here."})


def test_private_engine_present_keeps_every_route_on_it(
    client: TestClient, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = object()
    monkeypatch.setattr(code_map, "get_engine", lambda project_root: private)
    monkeypatch.setattr(seam, "private_code_intelligence_available", lambda: True)

    def no_lite(*args: object, **kwargs: object) -> Any:
        raise AssertionError("Runtime Lite must not be read while the private engine is present")

    monkeypatch.setattr(code_map_lite, "lite_engine", no_lite)
    monkeypatch.setattr(code_map_lite, "lite_index_present", no_lite)
    listed = [{"project_id": "proj_x", "root": str(repo), "indexed": True}]
    monkeypatch.setattr(code_map, "list_projects", lambda: [dict(row) for row in listed])
    engines: list[object] = []
    providers: list[str] = []

    def files(engine: object, root: Path, *, include_ignored: bool = False) -> dict[str, Any]:
        engines.append(engine)
        return {"files": [{"path": "src/pkg/core.py"}]}

    def relationships(provider: object, path: str) -> dict[str, Any]:
        providers.append(type(provider).__name__)
        return {"used_by": [], "uses": [], "context": {"backend": "legacy"}}

    def references(engine: object, symbol_id: str, *, limit: int) -> dict[str, Any]:
        engines.append(engine)
        return {"symbol_id": symbol_id, "references": []}

    monkeypatch.setattr(code_map, "list_file_nodes", files)
    monkeypatch.setattr(code_surface, "file_relationships", relationships)
    monkeypatch.setattr(code_map, "find_references", references)
    params = {"project_root": str(repo)}

    assert client.get("/v1/code-map/projects").json() == {"projects": listed}
    assert client.get("/v1/code-map/files", params=params).status_code == 200
    relationship = client.get("/v1/code-map/relationships", params={**params, "path": "src/pkg/core.py"}).json()
    assert "relationship_basis" not in relationship["context"]
    assert client.get("/v1/code-map/references", params={**params, "symbol_id": "s1"}).status_code == 200
    assert engines == [private, private]
    assert providers == ["LegacyReviewCodeIntelligence"]

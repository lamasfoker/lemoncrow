"""Every call site of a module the Review-only engine leaves out degrades, and only for that.

server/review-engine-optional.txt lists what shipped code can import but the engine does
not ship. For each ``module_absent`` / ``skip_if_absent`` site there are two cases, run
in-process and fast:

* absent -- ``sys.modules[name] = None`` makes importing the module raise
  ``ModuleNotFoundError`` with ``.name == name``, exactly as in the engine; the caller
  must show its visible fallback (None, a skipped step, an ``unavailable`` reason, 501);
* broken -- the module is importable but needs a dependency that is missing; that is a
  real fault and must propagate, not read as "this build leaves it out".

The end-to-end proof is tests/gateway/test_review_engine_smoke.py.
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import sys
from collections.abc import Iterator
from dataclasses import replace
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.core.foundation.optional_modules import module_absent, skip_if_absent
from lemoncrow.infra.tree_sitter import tags
from lemoncrow.pro.capabilities.review import development, packet
from lemoncrow.pro.capabilities.review.api import REMOTE_IMAGES, make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.architecture_delta import REFERENCE_GRAPH
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.models import ProvenanceRecord
from lemoncrow.pro.capabilities.review.packet import REVIEW_JOURNAL, PacketBuild, build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.sources import local
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, record_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.tool_supervision import edit_impact

_TREE_SITTER = "lemoncrow.pro.capabilities.semantic_file_memory.treesitter_ast"
_RATIONALE = "lemoncrow.pro.capabilities.review.rationale"
_EVIDENCE_CAPTURE = "lemoncrow.pro.capabilities.review.evidence_capture"
_PRECOMPUTE = "lemoncrow.pro.capabilities.review.precompute"
#: What a "broken" optional module fails to import: never installed anywhere.
_DEPENDENCY = "zz_lemoncrow_missing_dependency"
_TOKEN = "engine-optional-modules-token"
_PORT = 45998
_REMOTE_IMAGE = "https://img.example.invalid/diagram.svg"
_EXACT = ProvenanceRecord(
    status="matched",
    host="claude",
    session_id="engine-optional-session",
    certainty="exact",
    match_confidence=1.0,
    match_reason="test exact author session",
)


def _missing(name: str) -> ModuleNotFoundError:
    return ModuleNotFoundError(f"No module named {name!r}", name=name)


class _NeedsMissingDependency(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Finds one module, whose execution then fails on a dependency that is not installed."""

    def __init__(self, module: str) -> None:
        self.module = module

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> ModuleSpec | None:
        return importlib.util.spec_from_loader(fullname, self) if fullname == self.module else None

    def create_module(self, spec: ModuleSpec) -> None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        raise _missing(_DEPENDENCY)


def _absent(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    monkeypatch.setitem(sys.modules, module, None)


def _broken(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    monkeypatch.delitem(sys.modules, module, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_NeedsMissingDependency(module), *sys.meta_path])


def _fault() -> Any:
    return pytest.raises(ModuleNotFoundError, match=_DEPENDENCY)


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


# --------------------------------------------------------------------------- #
# The helpers                                                                 #
# --------------------------------------------------------------------------- #


def test_module_absent_names_only_the_module_or_its_parents() -> None:
    for module, missing, expected in [
        (_RATIONALE, _RATIONALE, True),
        (_RATIONALE, "lemoncrow.pro.capabilities.review", True),
        (_RATIONALE, "lemoncrow", True),
        (_RATIONALE, "lemoncrow.pro.capabilities.review.rationale_store", False),
        (_RATIONALE, "lemoncrow.pro.capabilities.review.ration", False),
        ("a.b.journal", "a.b.jour", False),
        ("a.b.journal_x", "a.b.journal", False),
        (_RATIONALE, f"{_RATIONALE}.sidecars", False),
        (_RATIONALE, "yaml", False),
        (_RATIONALE, "", False),
    ]:
        assert module_absent(_missing(missing), module) is expected


def test_skip_if_absent_skips_the_module_or_its_parents_and_reraises_anything_else() -> None:
    skipped: list[str] = []
    for missing in (_RATIONALE, "lemoncrow.pro.capabilities"):
        with skip_if_absent(_RATIONALE):
            raise _missing(missing)
        skipped.append(missing)
    assert skipped == [_RATIONALE, "lemoncrow.pro.capabilities"]
    for missing in ("yaml", "lemoncrow.pro.capabilities.review.ration", f"{_RATIONALE}.sidecars"):
        with pytest.raises(ModuleNotFoundError), skip_if_absent(_RATIONALE):
            raise _missing(missing)


# --------------------------------------------------------------------------- #
# tags.py: tree-sitter configuration                                          #
# --------------------------------------------------------------------------- #


def test_tags_without_tree_sitter_use_the_python_and_legacy_readers(monkeypatch: pytest.MonkeyPatch) -> None:
    _absent(monkeypatch, _TREE_SITTER)

    assert tags.tree_sitter_languages() is None
    python = tags.extract_tags_from_text("def add(a, b):\n    return a + b\n", "pkg/maths.py")
    assert ("add", "definition") in {(tag.name, tag.kind) for tag in python}
    typescript = tags.extract_tags_from_text("export function greet(name: string) {}\n", "web/app.ts")
    assert ("greet", "definition") in {(tag.name, tag.kind) for tag in typescript}
    # A tree-sitter-only language reads as one whose parser is unavailable: no guess.
    assert tags.extract_tags_from_text("class A { void run() {} }\n", "src/A.java") == []
    # The literal scan asks the same question: Python only.
    assert edit_impact._blob_language("pkg/maths.py") == "python"
    assert edit_impact._blob_language("web/app.ts") is None


def test_tags_propagate_a_broken_tree_sitter_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    _broken(monkeypatch, _TREE_SITTER)

    with _fault():
        tags.tree_sitter_languages()
    with _fault():
        tags.extract_tags_from_text("export function greet() {}\n", "web/app.ts")


# --------------------------------------------------------------------------- #
# development.py: agent-ledger ingestion                                      #
# --------------------------------------------------------------------------- #


def test_agent_ledgers_without_their_ingestor_report_live_ingestion_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _absent(monkeypatch, development.DEVELOPMENT_INGEST)
    store: Any = object()  # never touched: there is nothing to ingest with
    session: Any = object()

    assert development.ledger_ingestor(store, source_store_root=tmp_path, repo_root=tmp_path) is None
    assert (
        development.sync_ledgers_before_capture(store, session, repo_root=tmp_path, source_store_root=tmp_path) is None
    )


def test_agent_ledgers_propagate_a_broken_ingestor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _broken(monkeypatch, development.DEVELOPMENT_INGEST)
    store: Any = object()
    session: Any = object()

    with _fault():
        development.ledger_ingestor(store, source_store_root=tmp_path, repo_root=tmp_path)
    with _fault():
        development.sync_ledgers_before_capture(store, session, repo_root=tmp_path, source_store_root=tmp_path)


# --------------------------------------------------------------------------- #
# sources/local.py: hook-prepared builds, agent sidecars                      #
# --------------------------------------------------------------------------- #


def test_capture_without_hook_prepared_builds_builds_afresh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _absent(monkeypatch, _PRECOMPUTE)
    working_tree: Any = SimpleNamespace(mode="working_tree", paths=())

    assert local._prepared_build(tmp_path, tmp_path, working_tree) is None


def test_capture_propagates_a_broken_build_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _broken(monkeypatch, _PRECOMPUTE)
    working_tree: Any = SimpleNamespace(mode="working_tree", paths=())

    with _fault():
        local._prepared_build(tmp_path, tmp_path, working_tree)


def _repository(tmp_path: Path) -> tuple[Path, Any]:
    """A committed base plus a working-tree change to Python and Markdown."""

    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Optional Modules"
    repo.config["user.email"] = "optional@example.test"
    (root / "src" / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (root / "README.md").write_text("# App\n", encoding="utf-8")
    repo.index.add_all()
    repo.index.write()
    signature = pygit2.Signature("Optional Modules", "optional@example.test", 1700000000, 0)
    repo.create_commit("HEAD", signature, signature, "base", repo.index.write_tree(), [])
    (root / "src" / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    (root / "README.md").write_text(f"# App\n\n![diagram]({_REMOTE_IMAGE})\n", encoding="utf-8")
    return root, resolve_rev_range(root, None, working_tree=True)


def _build(root: Path, rng: Any, store_root: Path) -> PacketBuild:
    return build_review_packet_with_blobs(
        root, rng, store_root=store_root, with_impact=False, with_provenance=False, with_patch_text=True
    )


@pytest.mark.parametrize("reader", [_RATIONALE, _EVIDENCE_CAPTURE])
def test_capture_without_a_sidecar_reader_records_the_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reader: str
) -> None:
    root, rng = _repository(tmp_path)
    store = ReviewStore(tmp_path / "store")
    session = open_or_create_session(store, root, rng)
    build = _build(root, rng, store.root)
    _absent(monkeypatch, reader)

    recorded = record_revision(store, session, root, rng, store_root=store.root, build=build)

    assert store.latest_revision(session.id) == recorded.revision


@pytest.mark.parametrize("reader", [_RATIONALE, _EVIDENCE_CAPTURE])
def test_capture_propagates_a_broken_sidecar_reader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reader: str
) -> None:
    root, rng = _repository(tmp_path)
    store = ReviewStore(tmp_path / "store")
    session = open_or_create_session(store, root, rng)
    build = _build(root, rng, store.root)
    _broken(monkeypatch, reader)

    with _fault():
        record_revision(store, session, root, rng, store_root=store.root, build=build)


# --------------------------------------------------------------------------- #
# packet.py: the author-session journal (both build paths)                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def exact_author(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provenance finds the exact authoring session, so packets reach for its journal."""

    monkeypatch.setattr(packet, "_collect_provenance", lambda *_args, **_kwargs: (_EXACT, (), ()))


def _packet_journal(tmp_path: Path, path: str) -> Any:
    root, rng = _repository(tmp_path)
    store_root = tmp_path / "store"
    if path == "build":
        built = build_review_packet_with_blobs(
            root, rng, store_root=store_root, with_impact=False, with_provenance=True
        )
    else:
        built = packet.attach_provenance(store_root, root, rng, _build(root, rng, store_root))
    assert built.packet.provenance == _EXACT
    return built.packet.journal


@pytest.mark.parametrize("path", ["build", "attach_provenance"])
@pytest.mark.usefixtures("exact_author")
def test_packets_without_the_journal_reader_carry_no_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str
) -> None:
    _absent(monkeypatch, REVIEW_JOURNAL)

    assert _packet_journal(tmp_path, path) == ()


@pytest.mark.parametrize("path", ["build", "attach_provenance"])
@pytest.mark.usefixtures("exact_author")
def test_packets_propagate_a_broken_journal_reader(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str) -> None:
    _broken(monkeypatch, REVIEW_JOURNAL)

    with _fault():
        _packet_journal(tmp_path, path)


# --------------------------------------------------------------------------- #
# api.py and architecture_delta.py, through the Reader's routes               #
# --------------------------------------------------------------------------- #


@pytest.fixture
def reader(tmp_path: Path) -> Iterator[tuple[TestClient, str]]:
    """A Reader over one Review whose exact author session is known (no journal captured)."""

    root, rng = _repository(tmp_path)
    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    built = _build(root, rng, store_root)
    built = PacketBuild(packet=replace(built.packet, provenance=_EXACT), blobs=built.blobs)
    session = open_or_create_session(store, root, rng, title=built.packet.title)
    record_revision(store, session, root, rng, store_root=store_root, build=built)
    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(_TOKEN),
        repo_root=root,
        port=_PORT,
        source_repo_root=root,
        source_store_root=store_root,
    )
    with TestClient(app, base_url=f"http://127.0.0.1:{_PORT}") as client:
        yield client, session.id


def _get(reader: tuple[TestClient, str], suffix: str, **params: str) -> Any:
    client, review_id = reader
    return client.get(f"/api/reviews/{review_id}{suffix}", params=params, headers={"Authorization": f"Bearer {_TOKEN}"})


def test_reader_journal_without_its_reader_says_it_is_not_in_this_build(
    monkeypatch: pytest.MonkeyPatch, reader: tuple[TestClient, str]
) -> None:
    _absent(monkeypatch, REVIEW_JOURNAL)

    response = _get(reader, "/journal")

    assert response.status_code == 200, response.text
    assert response.json()["reason"] == "live author-session journal is not in this build"


def test_reader_journal_propagates_a_broken_reader(
    monkeypatch: pytest.MonkeyPatch, reader: tuple[TestClient, str]
) -> None:
    _broken(monkeypatch, REVIEW_JOURNAL)

    with _fault():
        _get(reader, "/journal")


def test_remote_markdown_images_without_a_fetcher_answer_501(
    monkeypatch: pytest.MonkeyPatch, reader: tuple[TestClient, str]
) -> None:
    _absent(monkeypatch, REMOTE_IMAGES)

    response = _get(reader, "/markdown-image", document_path="README.md", side="new", src=_REMOTE_IMAGE)

    assert response.status_code == 501, response.text
    assert response.json()["detail"] == "remote Markdown images are not available in this build"


def test_remote_markdown_images_propagate_a_broken_fetcher(
    monkeypatch: pytest.MonkeyPatch, reader: tuple[TestClient, str]
) -> None:
    _broken(monkeypatch, REMOTE_IMAGES)

    with _fault():
        _get(reader, "/markdown-image", document_path="README.md", side="new", src=_REMOTE_IMAGE)


def test_architecture_delta_without_the_repository_map_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, reader: tuple[TestClient, str]
) -> None:
    _absent(monkeypatch, REFERENCE_GRAPH)

    response = _get(reader, "/map", projection="architecture_delta")

    assert response.status_code == 200, response.text
    delta = response.json()["architecture_delta"]
    assert delta["status"] == "unavailable"
    assert "needs the repository map, which this build does not include" in repr(delta)


def test_architecture_delta_propagates_a_broken_repository_map(
    monkeypatch: pytest.MonkeyPatch, reader: tuple[TestClient, str]
) -> None:
    _broken(monkeypatch, REFERENCE_GRAPH)

    with _fault():
        _get(reader, "/map", projection="architecture_delta")

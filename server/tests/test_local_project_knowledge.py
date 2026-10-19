"""UX0b over the local server: ``/api/project-knowledge`` reads, hides, regenerates and proposes.

One LemonCrow root: the PK0 store ``lc knowledge project sync`` writes there.
A read never creates the store; hide and regenerate touch only AI narratives;
a correction is answered with a patch and nothing is written.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from aiohttp import ClientSession
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig

from lemoncrow.core.service.jobs import JOB_BACKGROUND_INTELLIGENCE
from lemoncrow.infra.storage.bundle import build_sqlite_store_bundle
from lemoncrow.pro.capabilities.review.intelligence_policy import (
    IntelligencePolicyLayer,
    PolicyOrigin,
    resolve_intelligence_policy,
)
from lemoncrow.pro.capabilities.review.intelligence_runtime import (
    ModelCallUsage,
    StructuredExecution,
    resolve_driver_model,
)
from lemoncrow.pro.capabilities.review.project_knowledge import (
    KnowledgeQuery,
    ProjectKnowledgeStore,
    RepositoryIdentity,
)
from lemoncrow.pro.capabilities.review.project_knowledge_narration import (
    REPOSITORY_REFRESH,
    KnowledgeNarrationExecutor,
)
from lemoncrow.pro.capabilities.review.project_knowledge_surface import LocalKnowledgeSources
from lemoncrow.pro.capabilities.review.store import DB_NAME, ReviewStore

PATH = "/api/project-knowledge"
REPO = RepositoryIdentity("local", "ux0b")
BASE = f"{PATH}/{quote(REPO.id)}"


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


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _grounded(**kwargs: Any) -> StructuredExecution:
    prompt = json.loads(kwargs["prompt"])
    answer = {
        "subjects": [
            {
                "subject_id": row["subject_id"],
                "statements": [
                    {"text": f"{row['key']} is current.", "source_refs": [row["facts"][0]["source_refs"][0]]}
                ],
            }
            for row in prompt["subjects"]
        ]
    }
    resolved = resolve_driver_model(None, None, policy=kwargs["policy"])
    return StructuredExecution(answer, resolved.driver, resolved.model, ModelCallUsage(calls=1))


def _synced(tmp_path: Path, root: Path) -> ProjectKnowledgeStore:
    """A real checkout synced into *root*, its subjects narrated once, and its source recorded."""

    from lemoncrow.pro.capabilities.review.project_knowledge_source import CanonicalGitKnowledge

    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "UX0b Test")
    _git(repo, "config", "user.email", "ux0b@example.invalid")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (repo / "docs" / "design.md").write_text("# Design\n\nCharges go through the gateway.\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    store = ProjectKnowledgeStore(ReviewStore(root))
    CanonicalGitKnowledge(repo, REPO, "refs/heads/main").sync(store)
    LocalKnowledgeSources(root).record(REPO, canonical_ref="refs/heads/main", repo_root=repo)
    policy = resolve_intelligence_policy(
        [IntelligencePolicyLayer(origin=PolicyOrigin("organization", "test"), driver="openai", model="gpt-test")]
    )
    executor = KnowledgeNarrationExecutor(
        REPOSITORY_REFRESH, store=lambda: store, content_access=lambda s: (), policy=lambda: policy, call=_grounded
    )
    subject = {
        "repository": {"provider": REPO.provider, "repository_key": REPO.repository_key},
        "generation": store.current_generation(REPO),
    }
    assert executor.execute(subject, max_calls=4).result["status"] == "completed"
    return store


def _exchange(
    tmp_path: Path, requests: list[tuple[str, str, Any]], *, no_auth: bool = True
) -> list[tuple[int, Any, str]]:
    async def exercise() -> list[tuple[int, Any, str]]:
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
        out: list[tuple[int, Any, str]] = []
        try:
            async with ClientSession() as client:
                for method, path, body in requests:
                    response = await client.request(method, f"{url}{path}", json=body)
                    payload = await response.json(content_type=None)
                    out.append((response.status, payload, response.headers.get("Cache-Control", "")))
        finally:
            await server.stop()
        return out

    return asyncio.run(exercise())


def test_the_listing_never_creates_the_store(tmp_path: Path, settings_root: Path) -> None:
    ((status, body, cache),) = _exchange(tmp_path, [("GET", PATH, None)])
    assert status == 200 and body["repositories"] == [] and body["scope"] == "local"
    assert "lc knowledge project sync" in body["sync_command"] and cache == "no-store"
    assert not (settings_root / DB_NAME).exists()
    ((missing, _body, _cache),) = _exchange(tmp_path, [("GET", BASE, None)])
    assert missing == 404 and not (settings_root / DB_NAME).exists()


def test_the_surface_reads_hides_regenerates_and_proposes(tmp_path: Path, settings_root: Path) -> None:
    store = _synced(tmp_path, settings_root)
    narratives = store.query(REPO, query=KnowledgeQuery(supports=("ai_narrated",))).entries
    narrative = next(row for row in narratives if row.subject.key == "docs/design.md")
    deterministic = store.query(REPO, query=KnowledgeQuery(supports=("deterministic",))).entries[0]
    entry = f"{BASE}/narratives/{quote(narrative.entry_id)}"
    results = _exchange(
        tmp_path,
        [
            ("GET", PATH, None),
            ("GET", f"{BASE}?q=gateway", None),
            ("GET", f"{BASE}?offset=-1", None),
            ("GET", f"{BASE}?offset=%C2%B2", None),
            ("POST", f"{entry}/hide", None),
            ("POST", f"{BASE}/narratives/{quote(deterministic.entry_id)}/hide", None),
            ("POST", f"{BASE}/narratives/{quote('knowledge:' + '0' * 64)}/hide", None),
            ("POST", f"{PATH}/not-a-repository/narratives/{quote(narrative.entry_id)}/hide", None),
            ("POST", f"{entry}/regenerate", None),
            (
                "POST",
                f"{BASE}/corrections",
                {"text": "The ledger replaced the gateway.", "entry_id": narrative.entry_id},
            ),
            ("POST", f"{BASE}/corrections", {"text": 7, "subject": {"kind": "path", "key": "app.py"}}),
            ("POST", f"{BASE}/corrections", {"text": "ok\x00", "subject": {"kind": "path", "key": "app.py"}}),
        ],
    )
    (
        listing,
        overview,
        bad_offset,
        unicode_offset,
        hidden,
        not_narrative,
        unknown,
        bad_repo,
        queued,
        proposal,
        bad_body,
        bad_text,
    ) = results

    assert listing[0] == 200 and listing[1]["repositories"][0]["source"]["repository_key"] == "ux0b"
    assert overview[0] == 200 and overview[2] == "no-store"
    assert [row["subject"]["key"] for row in overview[1]["context"]["entries"]] == ["docs/design.md"]
    assert overview[1]["regenerate"]["available"] is True and len(overview[1]["narratives"]) >= 1
    assert bad_offset[0] == 400
    assert unicode_offset[0] == 400  # a superscript digit passes isdigit() but not int()
    assert hidden[0] == 200 and hidden[1] == {"status": "hidden", "entry_id": narrative.entry_id}
    assert store.entry(REPO, narrative.entry_id).state == "superseded"  # type: ignore[union-attr]
    assert not_narrative[0] == 409 and store.entry(REPO, deterministic.entry_id) == deterministic
    assert unknown[0] == 404 and bad_repo[0] == 400
    assert queued[0] == 200 and queued[1]["status"] == "queued" and queued[1]["enqueued"] is True
    bundle = build_sqlite_store_bundle(settings_root)
    bundle.init()
    (job,) = bundle.jobs.list_jobs(job_type=JOB_BACKGROUND_INTELLIGENCE, limit=5)
    assert str(job["id"]) == queued[1]["job_id"]
    assert job["payload"]["subject"]["regenerate"]["entry_id"] == narrative.entry_id
    assert proposal[0] == 200 and proposal[1]["patch"].startswith("diff --git a/docs/project-knowledge/corrections/")
    assert not (tmp_path / "repo" / "docs" / "project-knowledge").exists(), "a proposal writes nothing"
    assert bad_body[0] == 400 and bad_text[0] == 400


def test_the_surface_requires_the_local_credential(tmp_path: Path, settings_root: Path) -> None:
    store = _synced(tmp_path, settings_root)
    narrative = store.query(REPO, query=KnowledgeQuery(supports=("ai_narrated",))).entries[0]
    results = _exchange(
        tmp_path,
        [("GET", PATH, None), ("POST", f"{BASE}/narratives/{quote(narrative.entry_id)}/hide", None)],
        no_auth=False,
    )
    assert [status for status, _body, _cache in results] == [401, 401]
    assert store.entry(REPO, narrative.entry_id).state == "current"  # type: ignore[union-attr]

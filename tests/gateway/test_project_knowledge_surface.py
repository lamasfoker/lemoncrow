"""UX0b: the Project Knowledge surface (view, hide, regenerate, correction, overlay migration).

Reads are plain views with no model call. Hide moves only an AI narrative and
every reader stops seeing it; deterministic history never moves. Regenerate
queues one BI2 job whose result replaces the old (or hidden) narrative. A
correction and the legacy overlay come back as new-document patches that
change nothing until a reviewed Change lands them.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.core.service.jobs import JOB_BACKGROUND_INTELLIGENCE
from lemoncrow.infra.storage.bundle import build_sqlite_store_bundle
from lemoncrow.pro.capabilities.review import project_knowledge_surface as surface
from lemoncrow.pro.capabilities.review.background_local import LocalBackgroundScheduler
from lemoncrow.pro.capabilities.review.background_scheduler import NARRATION_REQUESTED
from lemoncrow.pro.capabilities.review.intelligence_policy import (
    EffectiveIntelligencePolicy,
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
    CanonicalAdvanceProof,
    CanonicalRefIdentity,
    KnowledgeFact,
    KnowledgeKey,
    KnowledgeQuery,
    KnowledgeSubject,
    ProjectKnowledgeStore,
    RepositoryGeneration,
    RepositoryIdentity,
    StoredRepository,
    build_baseline_projection,
    build_merged_change_projection,
    build_source_advance_projection,
)
from lemoncrow.pro.capabilities.review.project_knowledge_narration import (
    CHANGE_NARRATION,
    CHANGE_NARRATIVE_KIND,
    REPOSITORY_REFRESH,
    SUBJECT_NARRATIVE_KIND,
    KnowledgeNarrationExecutor,
)
from lemoncrow.pro.capabilities.review.project_knowledge_surface import (
    CORRECTIONS_DIR,
    LEGACY_OVERLAY_DOCUMENT,
    MAX_CORRECTION_CHARS,
    KnowledgeSurfaceError,
    LocalKnowledgeSources,
    correction_patch,
    hide_narrative,
    knowledge_listing,
    knowledge_overview,
    legacy_overlay_patch,
    propose_correction,
    regenerate_narrative,
)

REPO = RepositoryIdentity("github", "acme/widgets")
REF = CanonicalRefIdentity(REPO, "refs/heads/main")
NOW = "2026-10-07T12:00:00+00:00"
LANDED = CanonicalAdvanceProof("provider_observed", "refs/heads/main", descendant=True)
OPENAI = {"driver": "openai", "model": "gpt-test"}
NONCE = "0123456789abcdef"


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


def _policy() -> EffectiveIntelligencePolicy:
    return resolve_intelligence_policy([IntelligencePolicyLayer(origin=PolicyOrigin("organization", "test"), **OPENAI)])


def _source(path: str, char: str) -> str:
    return f"source:new:{path}@sha256:{char * 64}"


def _fact(path: str, char: str, *, kind: str = "inventory", body: str = "") -> KnowledgeFact:
    return KnowledgeFact(
        subject=KnowledgeSubject("path", path),
        knowledge_kind=kind,
        body=body or f"Committed path {path} ({char}).",
        source_refs=(_source(path, char),),
        support="source_document" if kind == "document" else "deterministic",
    )


def _baseline(store: ProjectKnowledgeStore) -> RepositoryGeneration:
    g1 = RepositoryGeneration(REF, "1" * 40)
    facts = (
        _fact("src/a.py", "a"),
        _fact("src/b.py", "a"),
        _fact("docs/adr/0001.md", "a", kind="document", body="Charges go through the gateway."),
    )
    store.apply(build_baseline_projection(g1, facts=facts, observed_at="2026-10-01T10:00:00Z"))
    return g1


def _land(store: ProjectKnowledgeStore, previous: RepositoryGeneration) -> RepositoryGeneration:
    g2 = RepositoryGeneration(REF, "2" * 40)
    store.apply(
        build_merged_change_projection(
            previous,
            g2,
            proof=LANDED,
            merge_ref="merge:github:acme/widgets#42",
            review_ref="review:r-42",
            revision_ref="revision:rv-42",
            facts=(_fact("src/a.py", "b", body="A now applies discounts."),),
            retire=(KnowledgeKey(KnowledgeSubject("path", "src/a.py"), "inventory"),),
            observed_at="2026-10-05T09:00:00Z",
        )
    )
    return g2


class _Model:
    """Grounded structured-call seam: one statement per shown subject, citing its first ref."""

    def __init__(self) -> None:
        self.prompts: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> StructuredExecution:
        prompt = json.loads(kwargs["prompt"])
        self.prompts.append(prompt)
        resolved = resolve_driver_model(None, None, policy=kwargs["policy"])
        if "landed change" in prompt["task"]:
            first = prompt["subjects"][0]
            answer: dict[str, Any] = {
                "statements": [
                    {
                        "text": f"Change {len(self.prompts)} touched {first['key']}.",
                        "source_refs": [first["facts"][0]["source_refs"][0]],
                        "subject_ids": [first["subject_id"]],
                    }
                ]
            }
        else:
            answer = {
                "subjects": [
                    {
                        "subject_id": row["subject_id"],
                        "statements": [
                            {
                                "text": f"{row['key']} narrated ({len(self.prompts)}).",
                                "source_refs": [row["facts"][0]["source_refs"][0]],
                            }
                        ],
                    }
                    for row in prompt["subjects"]
                ]
            }
        usage = ModelCallUsage(calls=1, provider_cost=0.001, currency="USD")
        return StructuredExecution(answer, resolved.driver, resolved.model, usage)


def _executor(capability_id: str, store: ProjectKnowledgeStore, model: _Model) -> KnowledgeNarrationExecutor:
    return KnowledgeNarrationExecutor(
        capability_id,
        store=lambda: store,
        content_access=lambda subject: (),
        policy=_policy,
        call=model,
        clock=lambda: NOW,
    )


def _subject(generation: RepositoryGeneration) -> dict[str, Any]:
    return {
        "repository": {"provider": REPO.provider, "repository_key": REPO.repository_key},
        "generation": generation.id,
    }


def _live(store: ProjectKnowledgeStore, kind: str = SUBJECT_NARRATIVE_KIND) -> list[Any]:
    """What PK2 readers (Brief, Ask, AI Review) see: the live states only."""

    query = KnowledgeQuery(knowledge_kinds=(kind,), states=("current", "uncertain", "historical"))
    return list(store.query(REPO, query=query).entries)


def _all(store: ProjectKnowledgeStore, **filters: Any) -> list[Any]:
    states = ("current", "uncertain", "historical", "superseded")
    return list(store.query(REPO, query=KnowledgeQuery(states=states, **filters)).entries)


def _narrated(tmp_path: Path) -> tuple[ProjectKnowledgeStore, RepositoryGeneration, _Model]:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    g1 = _baseline(store)
    model = _Model()
    outcome = _executor(REPOSITORY_REFRESH, store, model).execute(_subject(g1), max_calls=4)
    assert outcome.result["status"] == "completed", outcome.result
    return store, g1, model


def _scheduler(tmp_path: Path) -> tuple[LocalBackgroundScheduler, Any]:
    root = tmp_path / "jobs"
    bundle = build_sqlite_store_bundle(root)
    bundle.init()
    return LocalBackgroundScheduler.for_root(bundle.jobs, root, policy=_policy), bundle


def _payload(bundle: Any, job_id: str) -> dict[str, Any]:
    rows = bundle.jobs.list_jobs(job_type=JOB_BACKGROUND_INTELLIGENCE, limit=20)
    return next(row["payload"] for row in rows if str(row["id"]) == job_id)


def _git(root: Path, *args: str, stdin: str | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], input=stdin, text=True, check=True, capture_output=True
    ).stdout.strip()


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "UX0b Test")
    _git(repo, "config", "user.email", "ux0b@example.invalid")
    (repo / "README.md").write_text("Canonical contract.\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    return repo


def _applies(repo: Path, patch: str, path: str) -> str:
    """Apply *patch* in *repo* exactly as a person would, and return the new file."""

    _git(repo, "apply", "--check", "-", stdin=patch)
    _git(repo, "apply", "-", stdin=patch)
    return (repo / path).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# store reads and hide
# --------------------------------------------------------------------------- #


def test_hide_moves_only_the_narrative_and_every_reader_stops_seeing_it(tmp_path: Path) -> None:
    store, _g1, _model = _narrated(tmp_path)
    narrative = next(row for row in _live(store) if row.subject.key == "src/a.py")
    other = next(row for row in _live(store) if row.subject.key == "src/b.py")
    source_backed = [row.to_wire() for row in _all(store, supports=("deterministic", "source_document"))]
    events = store.recent_evolution_events(StoredRepository(REPO.id))

    assert hide_narrative(store, REPO.id, narrative.entry_id, clock=lambda: NOW) == {
        "status": "hidden",
        "entry_id": narrative.entry_id,
    }
    live = {row.entry_id for row in _live(store)}
    assert narrative.entry_id not in live and other.entry_id in live and len(live) == 2
    hidden = store.entry(StoredRepository(REPO.id), narrative.entry_id)
    assert hidden is not None and hidden.state == "superseded" and hidden.updated_at == NOW
    # Deterministic and document history, and the evolution log, never move.
    assert [row.to_wire() for row in _all(store, supports=("deterministic", "source_document"))] == source_backed
    assert store.recent_evolution_events(StoredRepository(REPO.id)) == events
    assert hide_narrative(store, REPO.id, narrative.entry_id)["status"] == "already_hidden"

    deterministic = _all(store, supports=("deterministic",))[0]
    with pytest.raises(KnowledgeSurfaceError) as refused:
        hide_narrative(store, REPO.id, deterministic.entry_id)
    assert refused.value.status == 409 and refused.value.reason == "not_a_narrative"
    assert store.hide_narrative(StoredRepository(REPO.id), deterministic.entry_id, at=NOW) is False
    assert store.entry(StoredRepository(REPO.id), deterministic.entry_id) == deterministic
    with pytest.raises(KnowledgeSurfaceError) as missing:
        hide_narrative(store, REPO.id, "knowledge:" + "0" * 64)
    assert missing.value.status == 404
    with pytest.raises(KnowledgeSurfaceError) as invalid:
        hide_narrative(store, "github:acme/widgets", narrative.entry_id)
    assert invalid.value.status == 400
    # Another repository's id never reaches this repository's narrative.
    elsewhere = RepositoryIdentity("github", "acme/other").id
    assert store.hide_narrative(StoredRepository(elsewhere), other.entry_id, at=NOW) is False


def test_query_filters_by_support_and_leaves_out_kinds(tmp_path: Path) -> None:
    store, _g1, _model = _narrated(tmp_path)
    documents = store.query(REPO, query=KnowledgeQuery(supports=("source_document",))).entries
    assert [row.subject.key for row in documents] == ["docs/adr/0001.md"]
    context = store.query(
        REPO,
        query=KnowledgeQuery(supports=("deterministic", "source_document"), exclude_knowledge_kinds=("inventory",)),
    ).entries
    assert {row.knowledge_kind for row in context} == {"document"}
    assert {row.support for row in store.query(REPO, query=KnowledgeQuery(supports=("ai_narrated",))).entries} == {
        "ai_narrated"
    }
    with pytest.raises(ValueError):
        KnowledgeQuery(supports=("invented",))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        KnowledgeQuery(exclude_knowledge_kinds=tuple(f"kind{i}" for i in range(101)))
    with pytest.raises(ValueError):
        StoredRepository("repository:not-a-digest")


def test_listing_and_overview_are_bounded_plain_reads(tmp_path: Path, settings_root: Path) -> None:
    store, g1, model = _narrated(tmp_path)
    calls = len(model.prompts)
    sources = LocalKnowledgeSources(settings_root)
    listing = knowledge_listing(store, sources)
    (row,) = listing["repositories"]
    assert row["repository_id"] == REPO.id and row["current_generation"] == g1.id and row["source"] is None
    assert row["entries"] == {"deterministic": 2, "source_document": 1, "ai_narrated": 3}
    assert row["canonical_ref"] == "refs/heads/main" and listing["scope"] == "local"

    view = knowledge_overview(store, sources, REPO.id)
    assert {entry["subject"]["key"] for entry in view["narratives"]} == {"src/a.py", "src/b.py", "docs/adr/0001.md"}
    assert all(
        entry["support_label"] == "AI narrative" and entry["narrative"] == "subject" for entry in view["narratives"]
    )
    # The context list is documents and source-backed facts, never the path inventory.
    assert [entry["subject"]["key"] for entry in view["context"]["entries"]] == ["docs/adr/0001.md"]
    assert view["evolution"][0]["label"] == "Baseline observed"
    assert view["narratives"][0]["event"]["generation_id"] == g1.id
    assert view["regenerate"]["available"] is False and "sync" in view["regenerate"]["reason"]

    sources.record(REPO, canonical_ref="refs/heads/main", repo_root=tmp_path)
    labelled = knowledge_overview(store, sources, REPO.id, term="gateway")
    assert labelled["regenerate"] == {"available": True, "reason": ""}
    assert labelled["repository"]["source"]["repository_key"] == "acme/widgets"
    assert [entry["subject"]["key"] for entry in labelled["context"]["entries"]] == ["docs/adr/0001.md"]
    assert knowledge_overview(store, sources, REPO.id, term="nothing-matches")["context"]["entries"] == []
    assert knowledge_listing(store, sources)["repositories"][0]["source"]["repo_root"] == str(tmp_path.resolve())
    assert len(model.prompts) == calls, "reading never calls a model"

    with pytest.raises(KnowledgeSurfaceError) as unknown:
        knowledge_overview(store, sources, RepositoryIdentity("github", "acme/other").id)
    assert unknown.value.status == 404
    with pytest.raises(KnowledgeSurfaceError) as negative:
        knowledge_overview(store, sources, REPO.id, offset=-1)
    assert negative.value.status == 400


def test_a_long_body_and_many_refs_are_bounded_on_the_wire(tmp_path: Path, settings_root: Path) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    g1 = RepositoryGeneration(REF, "1" * 40)
    fact = KnowledgeFact(
        subject=KnowledgeSubject("path", "docs/long.md"),
        knowledge_kind="document",
        body="x" * 2500,
        source_refs=tuple(_source(f"docs/long{i}.md", "a") for i in range(10)),
        support="source_document",
    )
    store.apply(build_baseline_projection(g1, facts=(fact,), observed_at="2026-10-01T10:00:00Z"))
    (entry,) = knowledge_overview(store, LocalKnowledgeSources(settings_root), REPO.id)["context"]["entries"]
    assert len(entry["body"]) == 2000 and entry["body_truncated"] is True
    # Ten document refs plus the generation ref: eight shown, the rest counted.
    assert len(entry["source_refs"]) == 8 and entry["source_refs_omitted"] == 3


# --------------------------------------------------------------------------- #
# regenerate: one BI2 job; its result replaces the old or hidden narrative
# --------------------------------------------------------------------------- #


def test_regenerate_queues_one_job_whose_result_replaces_the_hidden_narrative(
    tmp_path: Path, settings_root: Path
) -> None:
    store, g1, model = _narrated(tmp_path)
    old = next(row for row in _live(store) if row.subject.key == "src/a.py")
    hide_narrative(store, REPO.id, old.entry_id)
    sources = LocalKnowledgeSources(settings_root)
    sources.record(REPO, canonical_ref="refs/heads/main", repo_root=tmp_path)
    scheduler, bundle = _scheduler(tmp_path)
    calls = len(model.prompts)

    queued = regenerate_narrative(
        store, sources, lambda: scheduler, REPO.id, old.entry_id, access_digest=lambda source: "rules-x", nonce=NONCE
    )
    assert queued["status"] == "queued" and queued["enqueued"] is True and queued["entry_id"] == old.entry_id
    assert queued["capability"] == REPOSITORY_REFRESH and queued["locus"] == "local"
    assert len(model.prompts) == calls, "queueing never calls a model"
    payload = _payload(bundle, queued["job_id"])
    assert payload["event"]["kind"] == NARRATION_REQUESTED
    assert payload["event"]["content_access_digest"] == "rules-x"
    assert payload["subject"]["generation"] == g1.id and payload["subject"]["repo_root"] == str(tmp_path.resolve())
    assert payload["subject"]["regenerate"] == {
        "entry_id": old.entry_id,
        "nonce": NONCE,
        "subject": {"kind": "path", "key": "src/a.py"},
    }
    # The same request converges; a new request is a new job.
    again = regenerate_narrative(
        store, sources, lambda: scheduler, REPO.id, old.entry_id, access_digest=lambda source: "rules-x", nonce=NONCE
    )
    assert again["job_id"] == queued["job_id"] and again["enqueued"] is False
    fresh = regenerate_narrative(
        store, sources, lambda: scheduler, REPO.id, old.entry_id, access_digest=lambda source: "rules-x"
    )
    assert fresh["job_id"] != queued["job_id"] and fresh["enqueued"] is True

    executor = _executor(REPOSITORY_REFRESH, store, model)
    assert executor.is_current(payload["subject"])
    outcome = executor.execute(payload["subject"], max_calls=4)
    assert outcome.result["status"] == "completed" and outcome.result["applied"] == 1 and outcome.calls_used == 1
    # Only the named subject was shown to the model.
    assert [row["key"] for row in model.prompts[-1]["subjects"]] == ["src/a.py"]
    replacement = next(row for row in _live(store) if row.subject.key == "src/a.py")
    assert replacement.entry_id != old.entry_id and replacement.synthesis_identity != old.synthesis_identity
    assert store.entry(StoredRepository(REPO.id), old.entry_id).state == "superseded"  # type: ignore[union-attr]
    # The other subjects keep their narratives.
    assert len(_live(store)) == 3
    # A repeat of the finished job reuses its narrative instead of calling again.
    assert executor.execute(payload["subject"], max_calls=4).result["status"] == "reused"


def test_regenerating_a_change_narrative_narrates_its_own_landed_generation(
    tmp_path: Path, settings_root: Path
) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    g2 = _land(store, _baseline(store))
    model = _Model()
    executor = _executor(CHANGE_NARRATION, store, model)
    assert executor.execute(_subject(g2), max_calls=2).result["applied"] == 1
    (old,) = _live(store, CHANGE_NARRATIVE_KIND)
    # The branch moved on since: the change is still narrated at its own generation.
    g3 = RepositoryGeneration(REF, "3" * 40)
    store.apply(
        build_source_advance_projection(
            g2,
            g3,
            proof=CanonicalAdvanceProof("local_canonical_ref", "refs/heads/main", descendant=True),
            facts=(_fact("src/b.py", "c"),),
            retire=(KnowledgeKey(KnowledgeSubject("path", "src/b.py"), "inventory"),),
            observed_at="2026-10-06T09:00:00Z",
        )
    )
    assert store.current_generation(REPO) == g3.id
    sources = LocalKnowledgeSources(settings_root)
    sources.record(REPO, canonical_ref="refs/heads/main", repo_root=tmp_path)
    scheduler, bundle = _scheduler(tmp_path)

    queued = regenerate_narrative(
        store, sources, lambda: scheduler, REPO.id, old.entry_id, access_digest=lambda source: "rules-x", nonce=NONCE
    )
    assert queued["capability"] == CHANGE_NARRATION and queued["status"] == "queued"
    subject = _payload(bundle, queued["job_id"])["subject"]
    assert subject["generation"] == g2.id
    assert executor.execute(subject, max_calls=2).result["applied"] == 1
    (replacement,) = _live(store, CHANGE_NARRATIVE_KIND)
    assert replacement.entry_id != old.entry_id and replacement.state == "historical"
    assert replacement.review_refs == ("review:r-42",) and replacement.valid_from_generation == g2.id


def test_regenerate_queues_nothing_without_the_checkout_or_its_content_policy(
    tmp_path: Path, settings_root: Path
) -> None:
    store, _g1, _model = _narrated(tmp_path)
    narrative = _live(store)[0]
    sources = LocalKnowledgeSources(settings_root)
    scheduler, bundle = _scheduler(tmp_path)
    never: Callable[[], Any] = lambda: pytest.fail("no scheduler without a source")  # noqa: E731

    with pytest.raises(KnowledgeSurfaceError) as unknown:
        regenerate_narrative(store, sources, never, REPO.id, narrative.entry_id, access_digest=lambda source: "x")
    assert unknown.value.status == 409 and unknown.value.reason == "source_unknown"

    sources.record(REPO, canonical_ref="refs/heads/main", repo_root=tmp_path)

    def unresolvable(source: Any) -> str:
        raise ValueError("review.yaml does not parse")

    refused = regenerate_narrative(store, sources, never, REPO.id, narrative.entry_id, access_digest=unresolvable)
    assert refused["status"] == "refused" and refused["reason"].startswith("content_access_unavailable")
    deterministic = _all(store, supports=("deterministic",))[0]
    with pytest.raises(KnowledgeSurfaceError) as not_narrative:
        regenerate_narrative(store, sources, never, REPO.id, deterministic.entry_id, access_digest=lambda source: "x")
    assert not_narrative.value.status == 409
    assert bundle.jobs.list_jobs(job_type=JOB_BACKGROUND_INTELLIGENCE, limit=5) == []
    # A real checkout without a canonical policy file still resolves (no rules), so it queues.
    repo = _repo(tmp_path)
    sources.record(REPO, canonical_ref="refs/heads/main", repo_root=repo)
    real = regenerate_narrative(store, sources, lambda: scheduler, REPO.id, narrative.entry_id)
    assert real["status"] == "queued"


def test_a_malformed_or_orphaned_regenerate_job_runs_nothing(tmp_path: Path) -> None:
    store, g1, model = _narrated(tmp_path)
    narrative = _live(store)[0]
    executor = _executor(REPOSITORY_REFRESH, store, model)
    calls = len(model.prompts)
    good = {"entry_id": narrative.entry_id, "nonce": NONCE, "subject": {"kind": "path", "key": narrative.subject.key}}
    for regenerate in (
        {**good, "nonce": "not-hex"},
        {**good, "entry_id": "knowledge:short"},
        {**good, "subject": "src/a.py"},
        {**good, "subject": {"kind": "invented", "key": "x"}},
        "regenerate everything",
    ):
        subject = {**_subject(g1), "regenerate": regenerate}
        assert executor.is_current(subject) is False
        assert executor.execute(subject, max_calls=4).result["status"] == "invalid_subject"
    # The narrative it would replace is gone: superseded, nothing narrated.
    orphan = {**_subject(g1), "regenerate": {**good, "entry_id": "knowledge:" + "f" * 64}}
    assert executor.execute(orphan, max_calls=4).result["status"] == "superseded"
    assert len(model.prompts) == calls


# --------------------------------------------------------------------------- #
# where each repository was synced from
# --------------------------------------------------------------------------- #


def test_the_source_registry_records_removes_bounds_and_rejects_forged_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = LocalKnowledgeSources(tmp_path)
    assert sources.all() == {} and not sources.path.exists()
    recorded = sources.record(REPO, canonical_ref="refs/heads/main", repo_root=tmp_path)
    assert sources.get(REPO.id) == recorded and recorded.repo_root == str(tmp_path.resolve())
    document = json.loads(sources.path.read_text(encoding="utf-8"))
    assert document["version"] == 1 and set(document["sources"]) == {REPO.id}

    # A row filed under an id its identity does not hash to is ignored.
    document["sources"][RepositoryIdentity("github", "acme/other").id] = document["sources"][REPO.id]
    sources.path.write_text(json.dumps(document), encoding="utf-8")
    assert set(sources.all()) == {REPO.id}
    sources.path.write_text("{not json", encoding="utf-8")
    assert sources.all() == {}

    stamps = iter(f"2026-10-07T10:00:0{i}+00:00" for i in range(10))
    monkeypatch.setattr(surface, "_now", lambda: next(stamps))
    monkeypatch.setattr(surface, "MAX_SOURCES", 3)
    repos = [RepositoryIdentity("local", f"repo-{i}") for i in range(4)]
    for repo in repos:
        sources.record(repo, canonical_ref="refs/heads/main", repo_root=tmp_path)
    assert set(sources.all()) == {repo.id for repo in repos[1:]}, "the least recently synced is dropped"
    assert sources.remove(repos[2].id) is True and sources.remove(repos[2].id) is False
    assert set(sources.all()) == {repos[1].id, repos[3].id}


# --------------------------------------------------------------------------- #
# corrections and the legacy overlay: patches a person lands
# --------------------------------------------------------------------------- #


def test_a_correction_is_a_new_document_patch_that_git_applies(tmp_path: Path) -> None:
    store, _g1, _model = _narrated(tmp_path)
    narrative = next(row for row in _live(store) if row.subject.key == "src/a.py")
    text = "\r\n  A applies no discounts; see docs/pricing.md.\r\n\r\n- Refunds use `ledger`.\n\n"
    proposal = propose_correction(store, REPO.id, text=text, entry_id=narrative.entry_id, proposed_on=NOW)
    assert proposal["path"].startswith(f"{CORRECTIONS_DIR}/src-a-py-") and proposal["path"].endswith(".md")
    content = proposal["content"]
    assert content.startswith("# Project Knowledge correction: src/a.py\n\n  A applies no discounts")
    assert "\r" not in content and "- Refunds use `ledger`.\n\n---" in content
    assert f"Corrects AI narration `{narrative.entry_id}`" in content and "Proposed: 2026-10-07" in content
    assert _applies(_repo(tmp_path), proposal["patch"], proposal["path"]) == content
    # Deterministic: the same correction is the same document; it writes nothing itself.
    assert propose_correction(store, REPO.id, text=text, entry_id=narrative.entry_id, proposed_on=NOW) == proposal
    assert not (tmp_path / CORRECTIONS_DIR).exists()

    by_path = propose_correction(store, REPO.id, text="Fact.", subject={"kind": "path", "key": "a`b.py"})
    assert "- Subject: `` path:a`b.py ``" in by_path["content"] and "Corrects AI narration" not in by_path["content"]
    assert _applies(_repo(tmp_path, "repo2"), by_path["patch"], by_path["path"]) == by_path["content"]
    document = _all(store, supports=("source_document",))[0]
    assert (
        "Corrects AI narration"
        not in propose_correction(store, REPO.id, text="Fact.", entry_id=document.entry_id)["content"]
    )


@pytest.mark.parametrize(
    ("kwargs", "status"),
    [
        ({"text": "ok\x00", "subject": {"kind": "path", "key": "a.py"}}, 400),
        ({"text": "ok\x1b[31m", "subject": {"kind": "path", "key": "a.py"}}, 400),
        ({"text": " \n\t\n ", "subject": {"kind": "path", "key": "a.py"}}, 400),
        ({"text": "x" * (MAX_CORRECTION_CHARS + 1), "subject": {"kind": "path", "key": "a.py"}}, 400),
        ({"text": "ok", "subject": {"kind": "invented", "key": "a.py"}}, 400),
        ({"text": "ok"}, 400),
        ({"text": "ok", "entry_id": "knowledge:" + "0" * 64}, 404),
    ],
)
def test_a_malformed_correction_is_refused(tmp_path: Path, kwargs: dict[str, Any], status: int) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    _baseline(store)
    with pytest.raises(KnowledgeSurfaceError) as refused:
        propose_correction(store, REPO.id, **kwargs)
    assert refused.value.status == status


def test_the_correction_bound_is_inclusive() -> None:
    proposal = correction_patch(KnowledgeSubject("path", "a.py"), "x" * MAX_CORRECTION_CHARS, proposed_on=NOW)
    assert ("x" * MAX_CORRECTION_CHARS) in proposal["content"]


def test_a_long_legacy_overlay_entry_is_marked_as_cut() -> None:
    whole, cut = "a" * 500, "b" * 501
    content = legacy_overlay_patch({"notes": [whole, cut]}, converted_on=NOW)["content"]  # type: ignore[index]
    assert f"- {whole}\n" in content
    assert f"- {'b' * 499}…\n" in content and "b" * 500 not in content


def test_the_legacy_overlay_becomes_an_advisory_document_never_a_rule(tmp_path: Path) -> None:
    team = {"notes": ["Prefer small PRs.", "  "], "boost": ["security\x1b[0m"], "suppress": ["style nits"]}
    personal = {"notes": ["My private note."], "boost": [], "suppress": []}
    assert legacy_overlay_patch({"notes": [], "boost": [], "suppress": []}) is None
    assert legacy_overlay_patch({}, {"notes": [" "]}) is None

    shared = legacy_overlay_patch(team, converted_on=NOW)
    assert shared is not None and shared["path"] == LEGACY_OVERLAY_DOCUMENT
    content = shared["content"]
    assert "It is not Review policy" in content and "- Prefer small PRs." in content
    assert "### Topics the overlay emphasized\n\n- security [0m" in content and "\x1b" not in content
    assert "### Topics the overlay de-emphasized\n\n- style nits" in content
    assert "My private note." not in content, "personal content needs an explicit opt-in"
    assert _applies(_repo(tmp_path), shared["patch"], shared["path"]) == content

    both = legacy_overlay_patch(team, personal, converted_on=NOW)
    assert both is not None and "## Personal overlay\n\n### Notes\n\n- My private note." in both["content"]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _cli(state: Path, *args: str, stdin: str | None = None, env: Mapping[str, str] | None = None) -> Any:
    from lemoncrow.gateway.cli.commands.knowledge import knowledge_group

    return CliRunner().invoke(knowledge_group, list(args), obj={"root": str(state)}, input=stdin, env=dict(env or {}))


def _project_args(command: str, repo: Path, *extra: str) -> list[str]:
    return [
        "project",
        command,
        "--repo",
        str(repo),
        "--repository-key",
        "ux0b",
        "--canonical-ref",
        "refs/heads/main",
        *extra,
    ]


def test_the_cli_records_the_source_and_drives_hide_regenerate_and_correct(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "design.md").write_text("# Design\n\nCharges go through the gateway.\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "design")
    state = tmp_path / "state"
    identity = RepositoryIdentity("local", "ux0b")

    synced = _cli(state, *_project_args("sync", repo))
    assert synced.exit_code == 0, synced.output
    source = LocalKnowledgeSources(state).get(identity.id)
    assert source is not None and source.repo_root == str(repo.resolve())
    listed = _cli(state, *_project_args("narratives", repo))
    assert listed.exit_code == 0 and json.loads(listed.output) == {"narratives": [], "truncated": False}

    from lemoncrow.pro.capabilities.review.store import ReviewStore

    store = ProjectKnowledgeStore(ReviewStore(state))
    model = _Model()
    generation = store.current_generation(identity)
    subject = {"repository": {"provider": "local", "repository_key": "ux0b"}, "generation": generation}
    executor = KnowledgeNarrationExecutor(
        REPOSITORY_REFRESH,
        store=lambda: store,
        content_access=lambda s: (),
        policy=_policy,
        call=model,
        clock=lambda: NOW,
    )
    assert executor.execute(subject, max_calls=4).result["status"] == "completed"
    narratives = json.loads(_cli(state, *_project_args("narratives", repo)).output)["narratives"]
    entry_id = narratives[0]["entry_id"]

    hidden = _cli(state, *_project_args("hide", repo, entry_id))
    assert hidden.exit_code == 0 and json.loads(hidden.output)["status"] == "hidden"
    assert entry_id not in _cli(state, *_project_args("narratives", repo)).output
    refused = _cli(state, *_project_args("hide", repo, "knowledge:" + "0" * 64))
    assert refused.exit_code != 0 and "no such Project Knowledge entry" in refused.output

    regenerated = _cli(state, *_project_args("regenerate", repo, entry_id))
    assert regenerated.exit_code == 0, regenerated.output
    queued = json.loads(regenerated.output)
    assert queued["status"] == "queued" and queued["enqueued"] is True
    bundle = build_sqlite_store_bundle(state)
    bundle.init()
    assert _payload(bundle, queued["job_id"])["subject"]["regenerate"]["entry_id"] == entry_id

    patch = _cli(
        state,
        *_project_args("correct", repo, "--subject-path", "docs/design.md", "--text", "-", "--patch-only"),
        stdin="The gateway was replaced by the ledger.\n",
    )
    assert patch.exit_code == 0, patch.output
    _git(repo, "apply", "--check", "-", stdin=patch.output)
    assert patch.output.startswith(f"diff --git a/{CORRECTIONS_DIR}/docs-design-md-")
    both = _cli(state, *_project_args("correct", repo, "--entry", entry_id, "--subject-path", "x", "--text", "t"))
    neither = _cli(state, *_project_args("correct", repo, "--text", "t"))
    assert both.exit_code == 2 and neither.exit_code == 2

    purged = _cli(state, *_project_args("purge", repo))
    assert purged.exit_code == 0 and LocalKnowledgeSources(state).get(identity.id) is None


def test_knowledge_show_puts_project_knowledge_first_and_overlay_patch_converts_it(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    repo = _repo(tmp_path)
    env = {"CLAUDE_WORKSPACE_ROOT": str(repo)}

    empty = _cli(state, "show", "--json", env=env)
    assert empty.exit_code == 0, empty.output
    shown = json.loads(empty.output)
    assert shown["project_knowledge"]["repositories"] == [] and "never Review policy" in shown["legacy_overlay_note"]
    assert set(shown) == {"project_knowledge", "legacy_overlay_note", "team", "personal"}
    assert not (state / "lemoncrow_reviews.db").exists(), "show never creates the store"
    nothing = _cli(state, "overlay-patch", env=env)
    assert nothing.exit_code == 0 and "No legacy review overlay to convert." in nothing.output

    (repo / ".lemoncrow").mkdir()
    (repo / ".lemoncrow" / "review.json").write_text(
        json.dumps({"notes": ["Prefer small PRs."], "boost": [], "suppress": ["style nits"]}), encoding="utf-8"
    )
    (state / "review_overlay.json").write_text(json.dumps({"notes": ["My private note."]}), encoding="utf-8")
    _cli(state, *_project_args("sync", repo))
    text = _cli(state, "show", env=env).output
    assert text.index("Project Knowledge") < text.index("Legacy review overlay")
    assert "ux0b refs/heads/main: " in text and "[legacy overlay: team (repo, shared)]" in text

    patch = _cli(state, "overlay-patch", env=env)
    assert patch.exit_code == 0 and "My private note." not in patch.output
    _git(repo, "apply", "--check", "-", stdin=patch.output)
    personal = json.loads(_cli(state, "overlay-patch", "--include-personal", "--json", env=env).output)
    assert personal["path"] == LEGACY_OVERLAY_DOCUMENT and "My private note." in personal["content"]
    # review.json itself is left alone.
    assert json.loads((repo / ".lemoncrow" / "review.json").read_text(encoding="utf-8"))["notes"] == [
        "Prefer small PRs."
    ]

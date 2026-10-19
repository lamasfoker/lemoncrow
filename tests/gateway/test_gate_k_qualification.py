"""Gate K: Project Knowledge properties no single package test proved on its own.

Each test names the Gate K property it closes; the Gate K matrix maps every
property to all of its proving tests.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

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
    ProjectKnowledgeEntry,
    ProjectKnowledgeStore,
    RepositoryGeneration,
    RepositoryIdentity,
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
    KnowledgeSurfaceError,
    LocalKnowledgeSources,
    hide_narrative,
    knowledge_overview,
)

WIDGETS = RepositoryIdentity("github", "acme/widgets")
GADGETS = RepositoryIdentity("github", "acme/gadgets")
NOW = "2026-10-07T12:00:00+00:00"
LANDED = CanonicalAdvanceProof("provider_observed", "refs/heads/main", descendant=True)
ADVANCED = CanonicalAdvanceProof("local_canonical_ref", "refs/heads/main", descendant=True)
PATHS = ("src/a.py", "src/b.py")
LIVE = ("current", "uncertain", "historical")


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


def _policy(model: str = "gpt-test") -> Callable[[], EffectiveIntelligencePolicy]:
    layer = IntelligencePolicyLayer(origin=PolicyOrigin("organization", "test"), driver="openai", model=model)
    return lambda: resolve_intelligence_policy([layer])


def _generation(repo: RepositoryIdentity, char: str) -> RepositoryGeneration:
    return RepositoryGeneration(CanonicalRefIdentity(repo, "refs/heads/main"), char * 40)


def _fact(path: str, char: str, body: str = "") -> KnowledgeFact:
    return KnowledgeFact(
        subject=KnowledgeSubject("path", path),
        knowledge_kind="inventory",
        body=body or f"Committed path {path} ({char}).",
        source_refs=(f"source:new:{path}@sha256:{char * 64}",),
    )


def _retire(path: str) -> tuple[KnowledgeKey, ...]:
    return (KnowledgeKey(KnowledgeSubject("path", path), "inventory"),)


def _baseline(store: ProjectKnowledgeStore, repo: RepositoryIdentity) -> RepositoryGeneration:
    g1 = _generation(repo, "1")
    facts = tuple(_fact(path, "a") for path in PATHS)
    store.apply(build_baseline_projection(g1, facts=facts, observed_at="2026-10-01T10:00:00Z"))
    return g1


def _land(
    store: ProjectKnowledgeStore, repo: RepositoryIdentity, previous: RepositoryGeneration, char: str, at: str
) -> RepositoryGeneration:
    generation = _generation(repo, char)
    store.apply(
        build_merged_change_projection(
            previous,
            generation,
            proof=LANDED,
            merge_ref=f"merge:github:{repo.repository_key}#{char}",
            review_ref=f"review:r-{char}",
            revision_ref=f"revision:rv-{char}",
            facts=(_fact("src/a.py", char, f"A changed in landing {char}."),),
            retire=_retire("src/a.py"),
            observed_at=at,
        )
    )
    return generation


def _advance(
    store: ProjectKnowledgeStore, repo: RepositoryIdentity, previous: RepositoryGeneration, char: str, at: str
) -> RepositoryGeneration:
    generation = _generation(repo, char)
    store.apply(
        build_source_advance_projection(
            previous,
            generation,
            proof=ADVANCED,
            facts=(_fact("src/b.py", char),),
            retire=_retire("src/b.py"),
            observed_at=at,
        )
    )
    return generation


class _Model:
    """A grounded structured-call seam: one statement per shown subject, citing that subject's first ref."""

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
                        "text": f"The change touched {first['key']}.",
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
                            {"text": f"{row['key']} is current.", "source_refs": [row["facts"][0]["source_refs"][0]]}
                        ],
                    }
                    for row in prompt["subjects"]
                ]
            }
        return StructuredExecution(
            answer, resolved.driver, resolved.model, ModelCallUsage(calls=1, provider_cost=0.001, currency="USD")
        )


def _narrate(
    store: ProjectKnowledgeStore,
    repo: RepositoryIdentity,
    generation: RepositoryGeneration,
    model: _Model,
    *,
    capability: str,
    route: str = "gpt-test",
) -> dict[str, Any]:
    executor = KnowledgeNarrationExecutor(
        capability,
        store=lambda: store,
        content_access=lambda subject: (),
        policy=_policy(route),
        call=model,
        clock=lambda: NOW,
    )
    subject = {
        "repository": {"provider": repo.provider, "repository_key": repo.repository_key},
        "generation": generation.id,
    }
    outcome = executor.execute(subject, max_calls=8)
    return {**outcome.result, "calls": outcome.calls_used}


def _narratives(store: ProjectKnowledgeStore, repo: RepositoryIdentity) -> list[ProjectKnowledgeEntry]:
    query = KnowledgeQuery(supports=("ai_narrated",), states=LIVE, limit=100)
    return list(store.query(repo, query=query).entries)


def _assert_every_narrative_is_backed(store: ProjectKnowledgeStore, repo: RepositoryIdentity) -> None:
    """Gate K: what a reader is shown about history still has that history behind it.

    A historical narrative tells one landed generation: that generation's
    evolution event is retained. A current narrative describes its subject:
    every ref it cites is carried by one of that subject's live source-backed
    entries.
    """

    events = {str(event["generation_id"]) for event in store.recent_evolution_events(repo, limit=100)}
    for narrative in _narratives(store, repo):
        if narrative.state == "historical":
            assert narrative.valid_from_generation in events, (narrative.body, "its landed history is gone")
            continue
        backing = store.query(
            repo,
            query=KnowledgeQuery(
                subjects=(narrative.subject,),
                supports=("deterministic", "source_document"),
                states=("current", "uncertain"),
            ),
        ).entries
        carried = {ref for entry in backing for ref in entry.source_refs}
        assert backing and set(narrative.source_refs) <= carried, (narrative.body, "its subject's sources are gone")


def _rendered_narratives(store: ProjectKnowledgeStore, root: Path, repo: RepositoryIdentity) -> list[dict[str, Any]]:
    narratives: list[dict[str, Any]] = knowledge_overview(store, LocalKnowledgeSources(root), repo.id)["narratives"]
    return narratives


# --------------------------------------------------------------------------- #
# every rendered knowledge narrative has surviving source refs
# --------------------------------------------------------------------------- #


def test_every_rendered_narrative_keeps_its_landed_history_through_retention_and_purge(
    tmp_path: Path, settings_root: Path
) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    model = _Model()
    g1 = _baseline(store, WIDGETS)
    assert _narrate(store, WIDGETS, g1, model, capability=REPOSITORY_REFRESH)["applied"] == 2
    g2 = _land(store, WIDGETS, g1, "2", "2026-10-05T09:00:00Z")
    assert _narrate(store, WIDGETS, g2, model, capability=CHANGE_NARRATION)["applied"] == 1
    assert _narrate(store, WIDGETS, g2, model, capability=REPOSITORY_REFRESH)["applied"] == 1
    g3 = _advance(store, WIDGETS, g2, "3", "2026-10-06T09:00:00Z")
    _assert_every_narrative_is_backed(store, WIDGETS)
    kinds = sorted((row.knowledge_kind, row.subject.key) for row in _narratives(store, WIDGETS))
    assert kinds == [(CHANGE_NARRATIVE_KIND, "acme/widgets"), (SUBJECT_NARRATIVE_KIND, "src/a.py")]

    # Retention between the landing and the next advance drops the landing's event. The
    # narrative of that landing was written later (it is newer than the cutoff) and must go too.
    pruned = store.prune_before(WIDGETS, cutoff="2026-10-05T12:00:00Z")
    assert pruned["events"] == 2
    _assert_every_narrative_is_backed(store, WIDGETS)
    assert [row.knowledge_kind for row in _narratives(store, WIDGETS)] == [SUBJECT_NARRATIVE_KIND]
    rendered = _rendered_narratives(store, settings_root, WIDGETS)
    assert [row["narrative"] for row in rendered] == ["subject"] and rendered[0]["state"] == "current"

    # A later landing narrated after the cutoff keeps its narrative while its event is retained.
    g4 = _land(store, WIDGETS, g3, "4", "2026-10-07T09:00:00Z")
    assert _narrate(store, WIDGETS, g4, model, capability=CHANGE_NARRATION)["applied"] == 1
    store.prune_before(WIDGETS, cutoff="2026-10-07T00:00:00Z")
    _assert_every_narrative_is_backed(store, WIDGETS)
    historical = [row for row in _rendered_narratives(store, settings_root, WIDGETS) if row["state"] == "historical"]
    assert [row["generation"] for row in historical] == [g4.id]
    assert all(row["event"] is not None for row in historical), "a rendered landing names its event"

    # A hard source purge takes every narrative that may carry the purged path with it.
    assert store.purge_paths(WIDGETS, ("src/a.py",)) > 0
    _assert_every_narrative_is_backed(store, WIDGETS)
    assert all("src/a.py" not in row.source_paths for row in _narratives(store, WIDGETS))


# --------------------------------------------------------------------------- #
# tenant/repository boundaries hold (one local store, two repositories)
# --------------------------------------------------------------------------- #


def test_repository_boundaries_hold_for_every_knowledge_reader_and_writer(tmp_path: Path, settings_root: Path) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    model = _Model()
    generations: dict[str, RepositoryGeneration] = {}
    for repo in (WIDGETS, GADGETS):
        g1 = _baseline(store, repo)
        generations[repo.id] = _land(store, repo, g1, "2", "2026-10-05T09:00:00Z")
        _narrate(store, repo, generations[repo.id], model, capability=CHANGE_NARRATION)
        _narrate(store, repo, generations[repo.id], model, capability=REPOSITORY_REFRESH)
    gadgets_digest = store.deterministic_digest(GADGETS)
    gadgets_narratives = {row.entry_id for row in _narratives(store, GADGETS)}
    widgets_narratives = {row.entry_id for row in _narratives(store, WIDGETS)}
    assert gadgets_narratives and widgets_narratives and not gadgets_narratives & widgets_narratives

    # Same paths, same subjects: every read is scoped to its repository.
    for repo in (WIDGETS, GADGETS):
        rows = store.query(repo, query=KnowledgeQuery(states=(*LIVE, "superseded"), limit=100)).entries
        assert rows and {row.repository_id for row in rows} == {repo.id}
        assert {str(event["generation_id"]) for event in store.recent_evolution_events(repo)} <= {
            _generation(repo, "1").id,
            generations[repo.id].id,
        }
    foreign = next(iter(gadgets_narratives))
    assert store.entry(WIDGETS, foreign) is None
    assert store.narration_identities(WIDGETS, generations[GADGETS.id].id) == {}
    assert store.generation_delta(WIDGETS, generations[GADGETS.id].id).introduced == ()
    counts = {row["repository_id"]: row["entries"] for row in store.repositories()["repositories"]}
    assert counts[WIDGETS.id] == counts[GADGETS.id]

    # Writes through one repository never reach the other.
    assert store.hide_narrative(WIDGETS, foreign, at=NOW) is False
    with pytest.raises(KnowledgeSurfaceError) as refused:
        hide_narrative(store, WIDGETS.id, foreign)
    assert refused.value.status == 404
    assert _narrate(store, WIDGETS, generations[WIDGETS.id], model, capability=REPOSITORY_REFRESH, route="gpt-2")[
        "calls"
    ]
    store.prune_before(WIDGETS, cutoff="2026-10-09T00:00:00Z")
    store.purge_paths(WIDGETS, ("src/a.py",))
    assert store.purge_repository(WIDGETS) > 0
    assert store.query(WIDGETS).entries == ()
    assert store.deterministic_digest(GADGETS) == gadgets_digest
    assert {row.entry_id for row in _narratives(store, GADGETS)} == gadgets_narratives


# --------------------------------------------------------------------------- #
# route/model changes invalidate only dependent AI artifacts
# --------------------------------------------------------------------------- #


def test_a_route_change_renarrates_only_ai_artifacts_and_leaves_deterministic_history(tmp_path: Path) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.sqlite3")
    model = _Model()
    landed: dict[str, RepositoryGeneration] = {}
    for repo in (WIDGETS, GADGETS):
        landed[repo.id] = _land(store, repo, _baseline(store, repo), "2", "2026-10-05T09:00:00Z")
        for capability in (CHANGE_NARRATION, REPOSITORY_REFRESH):
            _narrate(store, repo, landed[repo.id], model, capability=capability)
    before = {repo.id: store.deterministic_digest(repo) for repo in (WIDGETS, GADGETS)}
    events = store.recent_evolution_events(WIDGETS)
    old = {(row.knowledge_kind, row.subject.key): row for row in _narratives(store, WIDGETS)}
    untouched = {row.entry_id for row in _narratives(store, GADGETS)}

    # The same route reuses everything: no call.
    for capability in (CHANGE_NARRATION, REPOSITORY_REFRESH):
        same = _narrate(store, WIDGETS, landed[WIDGETS.id], model, capability=capability)
        assert same["status"] == "reused" and same["calls"] == 0

    # A new model narrates WIDGETS again; its deterministic history and GADGETS stay exactly as they were.
    for capability in (CHANGE_NARRATION, REPOSITORY_REFRESH):
        moved = _narrate(store, WIDGETS, landed[WIDGETS.id], model, capability=capability, route="gpt-2")
        assert moved["calls"] == 1 and moved["applied"] >= 1
    new = {(row.knowledge_kind, row.subject.key): row for row in _narratives(store, WIDGETS)}
    assert set(new) == set(old)
    assert all(new[key].synthesis_identity != old[key].synthesis_identity for key in old)
    superseded = store.query(
        WIDGETS, query=KnowledgeQuery(supports=("ai_narrated",), states=("superseded",), limit=100)
    ).entries
    assert {row.entry_id for row in superseded} == {row.entry_id for row in old.values()}
    assert {repo.id: store.deterministic_digest(repo) for repo in (WIDGETS, GADGETS)} == before
    assert store.recent_evolution_events(WIDGETS) == events
    assert {row.entry_id for row in _narratives(store, GADGETS)} == untouched

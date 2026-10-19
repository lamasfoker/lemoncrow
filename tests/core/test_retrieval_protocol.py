"""Domain-neutral Retriever protocol: conformance + injection seam.

The code vertical (``CodeContextEngine``) is private; its conformance tests
live in ``tests/private/core/test_retrieval_protocol.py``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.capabilities import code_intelligence_provider as seam
from lemoncrow.core.capabilities.code_intelligence_provider import (
    PRO_ENGINE_UNAVAILABLE_MESSAGE,
    ProEngineUnavailable,
)
from lemoncrow.core.capabilities.retrieval import Retriever, default_retriever_factory
from lemoncrow.core.runtime.engine import LemonCrowRuntimeCore


class FakeRetriever:
    """Minimal non-code retriever conforming to the protocol."""

    def __init__(self, corpus_id: str = "docs-corpus") -> None:
        self._corpus_id = corpus_id
        self.calls: list[dict[str, Any]] = []

    @property
    def source_id(self) -> str:
        return self._corpus_id

    def retrieve(
        self,
        query: str,
        *,
        budget_tokens: int = 2000,
        max_items: int = 8,
        seeds: list[str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"query": query, "budget_tokens": budget_tokens})
        return {"matches": [], "query": query}


def test_fake_retriever_conforms() -> None:
    assert isinstance(FakeRetriever(), Retriever)


def test_default_retriever_factory_requires_the_private_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _missing() -> Any:
        raise ProEngineUnavailable(PRO_ENGINE_UNAVAILABLE_MESSAGE)

    monkeypatch.setattr(seam, "_private_provider", _missing)
    with pytest.raises(ProEngineUnavailable, match=re.escape("Private code intelligence is not installed/available.")):
        default_retriever_factory(tmp_path)


def test_runtime_core_accepts_injected_retriever_factory(tmp_path: Path) -> None:
    fake = FakeRetriever()
    core = LemonCrowRuntimeCore(tmp_path / "store", retriever_factory=lambda root: fake)
    assert core.retriever_factory(tmp_path) is fake


def test_runtime_core_defaults_to_code_retriever(tmp_path: Path) -> None:
    core = LemonCrowRuntimeCore(tmp_path / "store")
    assert core.retriever_factory is default_retriever_factory


def test_get_context_keys_default_bootstrap_by_workspace_id_without_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.core.capabilities.code_context_contract import workspace_repo_id

    def _missing() -> Any:
        raise ProEngineUnavailable(PRO_ENGINE_UNAVAILABLE_MESSAGE)

    monkeypatch.setattr(seam, "_private_provider", _missing)
    monkeypatch.setattr("lemoncrow.core.runtime.engine.resolve_workspace_root", lambda root: tmp_path)
    core = LemonCrowRuntimeCore(tmp_path / "store")
    payload = core.get_context(task="summarize the auth module", agent_id="tester", recall=False)
    assert isinstance(payload, dict)
    assert payload["bootstrap"]["repo_id"] == workspace_repo_id(tmp_path)


def test_get_context_uses_injected_retriever_for_bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRetriever(corpus_id="docs-corpus")
    core = LemonCrowRuntimeCore(tmp_path / "store", retriever_factory=lambda root: fake)
    monkeypatch.setattr(
        "lemoncrow.core.runtime.engine.resolve_workspace_root",
        lambda root: tmp_path,
    )
    payload = core.get_context(task="summarize onboarding docs", agent_id="tester", recall=False)
    assert isinstance(payload, dict)
    assert payload["bootstrap"]["repo_id"] == "docs-corpus"

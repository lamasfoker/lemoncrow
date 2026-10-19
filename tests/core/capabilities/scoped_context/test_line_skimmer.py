"""Tests for the T12 goal-conditioned per-line skimmer.

Headless: no real embedding backend is required. The lexical path is exercised
directly; the embedding path is driven through an injected fake ranker so the
blend and the lexical-fallback degrade both run deterministically offline.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

import pytest

from lemoncrow.pro.capabilities.repo_map.budget import count_tokens
from lemoncrow.pro.capabilities.scoped_context import ScopedContextCapability, Subtask
from lemoncrow.pro.capabilities.scoped_context.line_skimmer import (
    LineSkimmer,
    build_goal_text,
    is_line_skim_enabled,
    is_structural_anchor,
    skim_chunks,
)

_CHUNK_BODY = 'import os\nimport logging\n\n\nclass PaymentGateway:\n    def __init__(self, client):\n        self.client = client\n        self.audit_log = []\n\n    def charge_card(self, amount, currency):\n        # validate the payment amount before charging the card\n        if amount <= 0:\n            raise ValueError("amount must be positive")\n        receipt = self.client.charge(amount, currency)\n        self.audit_log.append(receipt)\n        return receipt\n\n    def render_dashboard_widget(self):\n        colors = ["red", "green", "blue"]\n        layout = compute_grid_layout(colors)\n        return layout\n'
_GOAL = "fix the charge card payment amount validation"


@dataclass
class _FakeRecord:
    file_path: str
    symbol_name: str
    kind: str = "function"
    language: str = "python"
    qualified_name: str = ""
    signature: str = ""
    snippet: str = ""
    score: float | None = None
    provenance: str = "local"
    commit_sha: str = ""


class _FakeEngine:
    """Minimal engine exposing the search_symbols subset pull() calls."""

    def __init__(self, records: list[_FakeRecord]) -> None:
        self._records = records
        self.index_version = 0

    def search_symbols(
        self,
        query: str,
        *,
        limit: int = 20,
        mode: str = "auto",
        snippet: str = "none",
        snippet_lines: int = 8,
        file_glob: str | None = None,
        provenance_filter: str | None = None,
        **_: object,
    ) -> list[_FakeRecord]:
        recs = self._records
        if file_glob is not None:
            recs = [r for r in recs if r.file_path == file_glob or fnmatch.fnmatch(r.file_path, file_glob)]
        if provenance_filter is not None:
            recs = [r for r in recs if r.provenance == provenance_filter]
        return recs[:limit]

    def _current_index_version(self) -> int:
        return self.index_version


@dataclass
class _FakeChunk:
    snippet: str
    path: str = "src/pay.py"
    symbol: str = "charge_card"


@dataclass
class _FakeRanker:
    """Deterministic offline ranker: bag-of-words vectors over a fixed vocab."""

    available: bool = True
    vocab: tuple[str, ...] = ("charge", "card", "payment", "amount", "validate", "validation")
    calls: list[str] = field(default_factory=list)

    def _vec(self, text: str) -> list[float]:
        low = text.lower()
        return [1.0 if term in low else 0.0 for term in self.vocab]

    def embed_query(self, text: str) -> list[float]:
        self.calls.append(text)
        return self._vec(text)

    def embed_text(self, text: str) -> list[float]:
        return self._vec(text)


def _relevant_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def test_line_skimmer_helpers_contract() -> None:
    """case: test_is_line_skim_enabled_default_off"""
    assert is_line_skim_enabled(env={}) is False
    assert is_line_skim_enabled(env={"LEMONCROW_LINE_SKIM": "1"}) is True
    assert is_line_skim_enabled(env={"LEMONCROW_LINE_SKIM": "0"}) is False
    "case: test_build_goal_text_combines_fields"
    sub = Subtask(description="fix validation", keywords=["charge", "card"], affected_paths=["src/pay.py"])
    goal = build_goal_text(sub)
    assert "fix validation" in goal
    assert "charge" in goal and "card" in goal
    assert "src/pay.py" in goal
    "case: test_structural_anchor_detection"
    assert is_structural_anchor("def charge_card(self):")
    assert is_structural_anchor("    class Foo:")
    assert is_structural_anchor("@decorator")
    assert is_structural_anchor("import os")
    assert not is_structural_anchor("        colors = ['red']")
    assert not is_structural_anchor("")


def test_lexical_skimming_contract() -> None:
    """case: test_lexical_skim_keeps_relevant_and_anchors_drops_noise"""
    skimmer = LineSkimmer(ranker=None)
    result = skimmer.skim(_CHUNK_BODY, _GOAL)
    assert result.used_embedding is False
    assert result.changed
    assert result.dropped_lines > 0
    assert result.kept_lines == result.total_lines - result.dropped_lines
    kept = _relevant_lines(result.text)
    assert "class PaymentGateway:" in kept
    assert "def charge_card(self, amount, currency):" in kept
    assert "def render_dashboard_widget(self):" in kept
    assert any("amount must be positive" in line for line in kept)
    assert any("validate the payment amount" in line for line in kept)
    assert not any("compute_grid_layout" in line for line in kept)
    assert not any(line == "return layout" for line in kept)
    "case: test_lexical_skim_reduces_tokens"
    skimmer = LineSkimmer(ranker=None)
    result = skimmer.skim(_CHUNK_BODY, _GOAL)
    assert count_tokens(result.text) < count_tokens(_CHUNK_BODY)
    "case: test_skim_output_stays_parseable"
    import ast

    skimmer = LineSkimmer(ranker=None)
    result = skimmer.skim(_CHUNK_BODY, _GOAL)
    ast.parse(result.text)


def test_skimming_noop_contract() -> None:
    """case: test_empty_goal_is_noop"""
    skimmer = LineSkimmer(ranker=None)
    result = skimmer.skim(_CHUNK_BODY, "   ")
    assert result.dropped_lines == 0
    assert result.text == _CHUNK_BODY
    "case: test_no_signal_keeps_chunk_whole"
    skimmer = LineSkimmer(ranker=None)
    result = skimmer.skim(_CHUNK_BODY, "quantum chromodynamics lattice")
    assert result.dropped_lines == 0
    assert result.text == _CHUNK_BODY
    "case: test_tiny_body_is_noop"
    skimmer = LineSkimmer(ranker=None)
    body = "a = 1\nb = 2"
    result = skimmer.skim(body, _GOAL)
    assert result.dropped_lines == 0
    assert result.text == body


def test_embedding_fallback_contract() -> None:
    """case: test_embedding_path_used_when_ranker_available"""
    ranker = _FakeRanker(available=True)
    skimmer = LineSkimmer(ranker=ranker)
    result = skimmer.skim(_CHUNK_BODY, _GOAL)
    assert result.used_embedding is True
    assert result.changed
    assert ranker.calls
    kept = _relevant_lines(result.text)
    assert any("amount must be positive" in line for line in kept)
    "case: test_degrades_to_lexical_when_embedding_unavailable"
    ranker = _FakeRanker(available=False)
    skimmer = LineSkimmer(ranker=ranker)
    result = skimmer.skim(_CHUNK_BODY, _GOAL)
    assert result.used_embedding is False
    assert not ranker.calls
    assert result.changed


def test_skim_chunks_contract() -> None:
    """case: test_skim_chunks_mutates_snippet_in_place"""
    chunk = _FakeChunk(snippet=_CHUNK_BODY)
    before = count_tokens(chunk.snippet)
    out = skim_chunks([chunk], goal=_GOAL, ranker=None)
    assert out[0] is chunk
    assert count_tokens(chunk.snippet) < before
    "case: test_skim_chunks_empty_goal_noop"
    chunk = _FakeChunk(snippet=_CHUNK_BODY)
    skim_chunks([chunk], goal="  ", ranker=None)
    assert chunk.snippet == _CHUNK_BODY


def _records() -> list[_FakeRecord]:
    return [
        _FakeRecord(
            "src/pay.py",
            "charge_card",
            score=0.9,
            signature="def charge_card(self, amount, currency): ...",
            snippet=_CHUNK_BODY,
        )
    ]


def test_pull_unchanged_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_LINE_SKIM", raising=False)
    cap = ScopedContextCapability(_FakeEngine(_records()))
    result = cap.pull(
        Subtask(
            description="fix the charge card payment amount validation",
            keywords=["charge", "card", "amount"],
            affected_paths=["src/pay.py"],
            budget_tokens=4000,
        )
    )
    assert result.chunks
    assert result.chunks[0].snippet == _CHUNK_BODY
    assert "line-skim" not in result.rationale


def test_pull_skims_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LINE_SKIM", "1")
    cap = ScopedContextCapability(_FakeEngine(_records()))
    result = cap.pull(
        Subtask(
            description="fix the charge card payment amount validation",
            keywords=["charge", "card", "amount"],
            affected_paths=["src/pay.py"],
            budget_tokens=4000,
        )
    )
    assert result.chunks
    skimmed = result.chunks[0].snippet
    assert skimmed != _CHUNK_BODY
    assert count_tokens(skimmed) < count_tokens(_CHUNK_BODY)
    assert "def charge_card(self, amount, currency):" in skimmed
    assert "amount must be positive" in skimmed
    assert "compute_grid_layout" not in skimmed
    assert "line-skim" in result.rationale

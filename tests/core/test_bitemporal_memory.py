"""N13: bi-temporal memory tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lemoncrow.core.foundation.memory_models import ArchivalPassage
from lemoncrow.pro.capabilities.archival_recall.ranking import rank_archival_passages

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _passage(
    pid: str,
    text: str,
    *,
    valid_at: datetime | None = None,
    invalid_at: datetime | None = None,
) -> ArchivalPassage:
    return ArchivalPassage(
        id=pid,
        agent_id="lemoncrow:code",
        text=text,
        embedding_provenance="none",
        source="user",
        dedup_hash=pid,
        created_at=_T0,
        valid_at=valid_at,
        invalid_at=invalid_at,
    )


# --------------------------------------------------------------------------
# Additive schema: open-ended defaults; old records still load
# --------------------------------------------------------------------------
def test_bitemporal_fields_default_open_ended() -> None:
    passage = _passage("p", "hello")
    assert passage.valid_at is None
    assert passage.invalid_at is None
    # Open-ended window covers any moment.
    assert passage.is_valid_at(_T0) is True
    assert passage.is_valid_at() is True


def test_old_record_without_bitemporal_fields_still_loads() -> None:
    # Simulate a record persisted before N13 (the store reconstructs passages
    # without passing valid_at/invalid_at). The model must accept it unchanged.
    legacy = ArchivalPassage(
        id="legacy",
        agent_id="shared",
        text="legacy passage",
        embedding_provenance="legacy_stub",
        source="trace",
        dedup_hash="legacy",
    )
    assert legacy.valid_at is None
    assert legacy.invalid_at is None
    assert legacy.is_valid_at() is True
    # Round-trips through model_dump/model_validate (store/bridge path). The
    # injection_flagged computed field is excluded as it is derived, not stored.
    restored = ArchivalPassage.model_validate(legacy.model_dump(exclude={"injection_flagged"}))
    assert restored.valid_at is None
    assert restored.invalid_at is None


def test_is_valid_at_window_boundaries() -> None:
    start = _T0
    end = _T0 + timedelta(days=10)
    passage = _passage("p", "x", valid_at=start, invalid_at=end)
    assert passage.is_valid_at(start - timedelta(seconds=1)) is False  # before window
    assert passage.is_valid_at(start) is True  # inclusive start
    assert passage.is_valid_at(end - timedelta(seconds=1)) is True  # inside
    assert passage.is_valid_at(end) is False  # exclusive end


# --------------------------------------------------------------------------
# Recall-time filter excludes invalidated memory (opt-in)
# --------------------------------------------------------------------------
def test_recall_filter_default_off_keeps_invalidated() -> None:
    invalidated = _passage("stale", "shopify identity", invalid_at=_T0 + timedelta(days=1))
    fresh = _passage("fresh", "shopify identity")
    # No valid_as_of -> byte-identical to today: invalidated passage NOT filtered.
    ranked = rank_archival_passages(
        query="shopify identity",
        passages=[invalidated, fresh],
        top_k=5,
    )
    ids = {item.passage.id for item in ranked}
    assert ids == {"stale", "fresh"}


def test_recall_filter_excludes_invalidated_when_opted_in() -> None:
    as_of = _T0 + timedelta(days=2)
    invalidated = _passage("stale", "shopify identity", invalid_at=_T0 + timedelta(days=1))
    fresh = _passage("fresh", "shopify identity")
    ranked = rank_archival_passages(
        query="shopify identity",
        passages=[invalidated, fresh],
        top_k=5,
        valid_as_of=as_of,
    )
    ids = {item.passage.id for item in ranked}
    assert ids == {"fresh"}  # invalidated-before-as_of memory excluded


def test_recall_filter_excludes_not_yet_valid_memory() -> None:
    as_of = _T0
    future = _passage("future", "shopify identity", valid_at=_T0 + timedelta(days=5))
    fresh = _passage("fresh", "shopify identity")
    ranked = rank_archival_passages(
        query="shopify identity",
        passages=[future, fresh],
        top_k=5,
        valid_as_of=as_of,
    )
    ids = {item.passage.id for item in ranked}
    assert ids == {"fresh"}

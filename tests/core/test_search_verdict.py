"""Contract tests for the pure search-verdict state machine."""

from __future__ import annotations

import pytest

from lemoncrow.pro.capabilities.code_context.search_verdict import (
    ChannelHealth,
    SearchHistory,
    breaker_threshold,
    compute_verdict,
    normalize_query,
    reformulation_threshold,
)


def test_verdict_contract() -> None:
    cases = [
        (dict(hit_count=3, query="timezone conversion"), "found"),
        (dict(hit_count=1, query="foo", channels=ChannelHealth(semantic=False, zoekt=False)), "found"),
        (dict(hit_count=0, query="foo", channels=ChannelHealth(semantic=None, zoekt=None)), "missed"),
        (dict(hit_count=0, query="render template tag"), "missed"),
        (
            dict(
                hit_count=0,
                query="timezone offset handling",
                prior_empties=(normalize_query("timezone convert backend"),),
            ),
            "absent",
        ),
        (
            dict(
                hit_count=0,
                query="timezone offset handling",
                prior_empties=(normalize_query("database migration squash"),),
            ),
            "missed",
        ),
        (dict(hit_count=0, query="timezone convert", prior_empties=(normalize_query("timezone convert"),)), "missed"),
        (
            dict(
                hit_count=0,
                query="timezone convert backend",
                prior_empties=(normalize_query("timezone convert backend utc"),),
            ),
            "missed",
        ),
        (
            dict(hit_count=0, query="alpha gamma delta", prior_empties=(normalize_query("alpha beta"),), threshold=3),
            "missed",
        ),
    ]
    for kwargs, expected in cases:
        result = compute_verdict(**kwargs)
        assert result.verdict == expected, kwargs
        if expected in {"found", "missed", "absent"}:
            assert result.next == ""
    assert compute_verdict(hit_count=3, query="timezone conversion").as_payload() == {"verdict": "found"}


def test_dark_channel_contract() -> None:
    for channels, label in [
        (ChannelHealth(semantic=False), "semantic"),
        (ChannelHealth(zoekt=False), "zoekt"),
        (ChannelHealth(semantic=False, zoekt=False), "semantic + zoekt"),
    ]:
        result = compute_verdict(hit_count=0, query="foo bar", channels=channels)
        assert result.verdict == "dark" and label in result.next and result.as_payload()["next"]


def test_threshold_and_normalization_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    assert normalize_query("Foo, bar FOO!") == frozenset({"foo", "bar"})
    assert normalize_query("") == frozenset()
    for value, expected in [("4", 4), ("garbage", 2), ("0", 2)]:
        monkeypatch.setenv("LEMONCROW_SEARCH_REFORMULATION_THRESHOLD", value)
        assert reformulation_threshold() == expected
    for value, expected in [("3", 3), ("junk", 6)]:
        monkeypatch.setenv("LEMONCROW_SEARCH_BREAKER_THRESHOLD", value)
        assert breaker_threshold() == expected


def test_search_history_contract() -> None:
    history = SearchHistory()
    history.record("alpha beta", found=False)
    history.record("gamma delta", found=True)
    history.record("   ", found=False)
    assert history.prior_empties() == (normalize_query("alpha beta"),)

    history = SearchHistory()
    history.record("timezone convert backend", found=False)
    history.record("database migration", found=False)
    history.record("timezone offset utc", found=True)
    assert history.prior_empties() == (normalize_query("database migration"),)

    bounded = SearchHistory()
    for i in range(50):
        bounded.record(f"unique-term-{i}", found=False)
    assert len(bounded.prior_empties()) <= 24


def test_search_breaker_contract() -> None:
    history = SearchHistory()
    for i in range(6):
        history.record(f"miss-term-{i}", found=False)
    assert history.unproductive_streak == 6
    assert history.breaker_tripped(threshold=6) is True
    assert history.breaker_tripped(threshold=10) is False
    assert history.breaker_tripped(threshold=0) is False
    history.record("hit here", found=True)
    assert history.unproductive_streak == 0 and not history.breaker_tripped(threshold=2)


def test_history_drives_absent_across_calls() -> None:
    history = SearchHistory()
    first = compute_verdict(hit_count=0, query="timezone convert backend", prior_empties=history.prior_empties())
    history.record("timezone convert backend", found=False)
    second = compute_verdict(hit_count=0, query="timezone offset handling", prior_empties=history.prior_empties())
    assert (first.verdict, second.verdict) == ("missed", "absent")

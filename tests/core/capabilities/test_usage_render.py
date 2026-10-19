"""The rendering contract: what a bucket with no real price is allowed to print.

``lc usage`` is the first surface that reads ``UsageRow.cost_usd is None``, and
the whole point of that field dies at the renderer: ``total_usd`` is ``0.0`` for
an unpriced bucket, so the naive cell prints ``$0.00`` and a busy local session
reads as free. Every test here pins one half of the rule -- what gets printed
instead, and that a dollar sign only ever appears next to a real measurement.

The rest covers shape: totals, truncation, billions, and the two degraded cases
(empty report, unpriced run) that must render a value rather than raise.
"""

from __future__ import annotations

from typing import Any

from lemoncrow.pro.capabilities.usage.models import UsageAggregate
from lemoncrow.pro.capabilities.usage.render import (
    UNPRICED_LABELS,
    basis_cell,
    cost_cell,
    render_explain,
    render_usage,
)


def _bucket(
    key: str,
    *,
    rows: int = 1,
    sessions: int = 1,
    tokens: int = 1000,
    billed: float = 0.0,
    estimated: float = 0.0,
    unpriced: int = 0,
    mix: tuple[tuple[str, int], ...] = (("api_estimated", 1),),
) -> UsageAggregate:
    return UsageAggregate(
        key=key,
        rows=rows,
        sessions=sessions,
        total_tokens=tokens,
        billed_usd=billed,
        estimated_usd=estimated,
        unpriced_rows=unpriced,
        provenance_mix=mix,
    )


# --------------------------------------------------------------------------- #
# cost_cell -- the rule                                                       #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# basis_cell                                                                  #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# render_usage                                                                #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# render_explain                                                              #
# --------------------------------------------------------------------------- #


def _explain_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "session_id": "abc123",
        "host": "claude",
        "model": "claude-sonnet-5",
        "models": ["claude-sonnet-5"],
        "project": "alpha",
        "total_cost_usd": 2.5,
        "cost_provenance": "provider_billed",
        "breakdown_basis": "ledger_per_turn",
        "breakdown_basis_approximate": False,
        "total_tokens": 1_000_000,
        "duration_seconds": 543.0,
        "breakdown": [
            {"bucket": "cache_read", "tokens": 900_000, "cost_usd": 2.0, "share": 0.9, "basis": "ledger_per_turn"},
            {"bucket": "output", "tokens": 100_000, "cost_usd": 0.5, "share": 0.1, "basis": "ledger_per_turn"},
            {"bucket": "subagents", "tokens": 0, "cost_usd": None, "share": 0.0, "basis": "unseparable"},
        ],
        "top_tools": [{"name": "Read", "calls": 12, "cost_usd": 0.31}],
        "notes": ["subagent usage is folded into this session's totals"],
    }
    payload.update(overrides)
    return payload


def test_cost_cell_contract() -> None:
    cases = [
        (_bucket("k", rows=3, unpriced=3, mix=(("self_hosted_unpriced", 3),)), "local", False),
        (_bucket("k", rows=3, unpriced=3, mix=(("enterprise_allocated", 3),)), "seat", False),
        (_bucket("k", rows=3, unpriced=3, mix=(("unknown", 3),)), UNPRICED_LABELS["unknown"], False),
        (_bucket("ollama", rows=2, estimated=0.0, mix=(("self_hosted_estimated", 2),)), "local", False),
        (_bucket("claude", rows=2, billed=4.1, mix=(("provider_billed", 2),)), "$4.10", True),
        (_bucket("openai", rows=1, estimated=0.0, tokens=0, mix=(("api_estimated", 1),)), "$0.00", True),
        (
            _bucket("claude", rows=4, billed=2.5, unpriced=1, mix=(("provider_billed", 3), ("unknown", 1))),
            "$2.50",
            True,
        ),
        (_bucket("k", rows=0, mix=()), "$0.00", True),
    ]
    for bucket, expected, money in cases:
        actual = cost_cell(bucket)
        assert actual == expected
        assert ("$" in actual) is money


def test_basis_cell_contract() -> None:
    cases = [
        (_bucket("k", mix=(("provider_billed", 3),)), "billed"),
        (_bucket("k", mix=(("api_estimated", 1), ("provider_billed", 9))), "billed+estimated"),
        (_bucket("k", mix=(("api_estimated", 1), ("provider_billed", 2), ("unknown", 3))), "mixed"),
        (_bucket("k", mix=()), UNPRICED_LABELS["unknown"]),
    ]
    for bucket, expected in cases:
        assert basis_cell(bucket) == expected


def test_usage_rendering_contract() -> None:
    empty = _bucket("__total__", rows=0, sessions=0, tokens=0, mix=())
    text = render_usage([], empty, by="host", window="7d", no_color=True)
    assert "No usage recorded in the last 7d" in text and "\x1b[" not in text

    priced = _bucket("claude", rows=2, billed=3.0, mix=(("provider_billed", 2),))
    text = render_usage(
        [priced],
        _bucket("__total__", rows=2, billed=3.0, mix=(("provider_billed", 2),)),
        by="host",
        window="7d",
        no_color=True,
    )
    assert "HOST" in text and "TOTAL" in text and "$3.00" in text and "raise `--limit`" not in text
    for claim in ("potential optimization", "would have saved", "could have saved"):
        assert claim not in text.lower()

    local = _bucket("opencode", rows=5, unpriced=5, mix=(("self_hosted_unpriced", 5),))
    text = render_usage(
        [local],
        _bucket("__total__", rows=5, unpriced=5, mix=(("self_hosted_unpriced", 5),)),
        by="host",
        window="30d",
        no_color=True,
    )
    assert "$0.00" not in text and "local" in text and "5 of 5 rows carry no price" in text

    mixed = _bucket(
        "__total__",
        rows=10,
        billed=1.0,
        unpriced=4,
        mix=(("provider_billed", 6), ("enterprise_allocated", 3), ("unknown", 1)),
    )
    text = render_usage([], mixed, by="host", window="7d", no_color=True)
    assert "subscription seat" in text and "model id unresolved" in text

    billions = _bucket("__total__", rows=1, tokens=53_949_350_000, billed=1.0, mix=(("provider_billed", 1),))
    text = render_usage([billions], billions, by="host", window="90d", no_color=True)
    assert "53.95B" in text and "53949.35M" not in text

    shown = [_bucket("claude", rows=2, billed=1.0, mix=(("provider_billed", 2),))]
    truncated = _bucket("__total__", rows=9, billed=1.0, mix=(("provider_billed", 9),))
    assert "raise `--limit`" in render_usage(shown, truncated, by="host", window="7d", no_color=True)

    rich = render_usage([priced], priced, by="host", window="7d")
    assert "claude" in rich and "TOTAL" in rich and "$3.00" in rich


def test_explain_rendering_contract() -> None:
    text = render_explain(_explain_payload(), no_color=True)
    assert (
        "RUN  abc123" in text
        and "$2.50 billed" in text
        and "9m 3s" in text
        and "cache_read" in text
        and "Read" in text
        and "folded" in text
    )
    assert "split: derived, not billed" in text
    cache_line = next(line for line in text.splitlines() if "cache_read" in line)
    assert "derived" in cache_line
    subagent_line = next(line for line in text.splitlines() if "subagents" in line)
    assert UNPRICED_LABELS["unknown"] in subagent_line and "$" not in subagent_line and "unseparable" in subagent_line

    unpriced = render_explain(
        _explain_payload(
            total_cost_usd=None,
            cost_provenance="self_hosted_unpriced",
            breakdown=[{"bucket": "output", "tokens": 10, "cost_usd": None, "share": 1.0}],
            top_tools=[],
            notes=["cost is unpriced: local or self-hosted model with no rate card"],
        ),
        no_color=True,
    )
    assert "local" in unpriced and "$0.00" not in unpriced

    approximate = render_explain(_explain_payload(breakdown_basis_approximate=True), no_color=True)
    assert "at approximate rates" in approximate

    unknown = render_explain(
        _explain_payload(
            breakdown_basis="unattributable",
            breakdown=[
                {"bucket": "cache_read", "tokens": 900_000, "cost_usd": None, "share": 1.0, "basis": "unattributable"}
            ],
        ),
        no_color=True,
    )
    assert "split: unknown" in unknown and "nothing records which turn used which" in unknown

    minimal = render_explain({"session_id": "x"}, no_color=True)
    assert "RUN  x" in minimal and "WHERE IT WENT" in minimal
    rich = render_explain(_explain_payload())
    assert "abc123" in rich and "cache_read" in rich

"""The usage contract itself: provider resolution, token disjointness, defaults.

These are the pure-data guards. ``provider_for_model`` must never guess -- a
bare ``llama-3.3`` is served by half a dozen vendors and attributing it to one
of them silently moves spend onto the wrong provider's line. And the token
components must stay disjoint, because two of the seven fields the row carries
are verified subsets of another field and summing all seven bills them twice.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from lemoncrow.core.capabilities.pricing import _SUBSCRIPTION_VENDOR_PREFIXES
from lemoncrow.pro.capabilities.usage.models import (
    COST_PROVENANCE_VALUES,
    SUBSCRIPTION_VENDOR_PREFIXES,
    UNKNOWN_STARTED_AT,
    UNPRICED_PROVENANCE_VALUES,
    USAGE_SCHEMA_VERSION,
    UsageAggregate,
    UsageRow,
    provider_for_model,
    sum_token_components,
)


def test_provider_resolution_contract() -> None:
    """case: test_provider_for_model_reads_the_namespace"""
    for model, expected in [
        ("anthropic/claude-opus-5", "anthropic"),
        ("openai/gpt-5", "openai"),
        ("ollama/qwen2.5-coder", "ollama"),
        ("custom/my-endpoint", "custom"),
        ("vllm/llama-3.3-70b", "vllm"),
        ("copilot/gpt-5", "copilot"),
        ("cursor/composer-2", "cursor"),
        ("zen/whatever", "zen"),
        ("claude-opus-5", "anthropic"),
        ("gpt-5", "openai"),
        ("o3-mini", "openai"),
        ("gemini-3-pro", "google"),
    ]:
        assert provider_for_model(model) == expected
    "case: test_provider_for_model_returns_unknown_rather_than_guessing"
    for model in [
        "",
        "   ",
        "<synthetic>",
        "_default",
        "qwen3-coder",
        "llama-3.3-70b",
        "mistral-large",
        "deepseek-v3",
        "nope/x",
    ]:
        assert provider_for_model(model) == "unknown"
    "case: test_subscription_prefixes_match_pricing"
    "Drift guard: this tuple decides who gets ``enterprise_allocated``."
    assert set(SUBSCRIPTION_VENDOR_PREFIXES) == set(_SUBSCRIPTION_VENDOR_PREFIXES)


def test_usage_row_contract() -> None:
    """case: test_default_row_is_valid_and_unpriced"""
    row = UsageRow()
    assert row.schema_version == USAGE_SCHEMA_VERSION
    assert row.cost_usd is None
    assert row.cost_provenance == "unknown"
    assert row.host == "unknown"
    assert row.project == "unknown"
    assert row.parent_session_id is None
    assert row.started_at == UNKNOWN_STARTED_AT
    assert row.started_at.tzinfo is not None
    assert row.is_priced is False
    "case: test_row_is_frozen_and_hashable"
    row = UsageRow(session_id="a")
    with pytest.raises(FrozenInstanceError):
        row.session_id = "b"
    assert isinstance(hash(row), int)
    "case: test_unpriced_provenance_values_are_a_subset_of_the_literal"
    assert set(UNPRICED_PROVENANCE_VALUES) <= set(COST_PROVENANCE_VALUES)
    assert "self_hosted_unpriced" in UNPRICED_PROVENANCE_VALUES
    assert "enterprise_allocated" in UNPRICED_PROVENANCE_VALUES


def test_usage_aggregation_contract() -> None:
    """case: test_sum_token_components_excludes_documented_subsets"""
    assert (
        sum_token_components(
            input_tokens=100, output_tokens=50, cache_read_tokens=10, cache_write_tokens=5, thinking_tokens=1
        )
        == 166
    )
    assert sum_token_components(input_tokens=-5, output_tokens=10) == 10
    "case: test_aggregate_total_usd_ignores_unpriced_rows"
    bucket = UsageAggregate(key="k", rows=3, billed_usd=1.0, estimated_usd=2.0, unpriced_rows=1)
    assert bucket.total_usd == 3.0

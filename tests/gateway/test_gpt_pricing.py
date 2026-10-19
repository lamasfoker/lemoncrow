"""Official GPT rates and fallback isolation across named model families."""

from pathlib import Path

import pytest

from lemoncrow.core.capabilities import pricing
from lemoncrow.core.capabilities.plugin_runtime import _codex_estimated_cost_usd


@pytest.fixture(autouse=True)
def builtin_rates(monkeypatch):
    rates = pricing._load_overrides_from_file(Path(pricing.__file__).with_name("pricing.yaml"))
    monkeypatch.setattr(pricing, "_load_litellm_model_cost", lambda: {})
    monkeypatch.setattr(pricing, "_load_overrides_from_disk", lambda: rates)
    monkeypatch.setattr(pricing, "_OVERRIDE_PRICING", {})
    clear_table = pricing._load_pricing_table.cache_clear
    clear_table()
    pricing._nearest_table_match.cache_clear()
    yield
    clear_table()
    pricing._nearest_table_match.cache_clear()


def test_official_rates_and_context_boundary():
    for model, short, long in [
        ("gpt-6.1-sol", (2, 0.1, 2.5, 10), (4, 0.2, 5, 15)),
        ("gpt-6-sol", (2, 0.2, 2.5, 10), (4, 0.4, 5, 15)),
        ("gpt-6-astra", (10, 1, 12.5, 50), (20, 2, 25, 75)),
        ("gpt-6-luna", (0.1, 0.01, 0.125, 0.5), (0.2, 0.02, 0.25, 0.75)),
        ("gpt-5.6-sol", (4, 0.4, 5, 20), (8, 0.8, 10, 30)),
        ("gpt-5.6-terra", (2, 0.2, 2.5, 12), (4, 0.4, 5, 18)),
        ("gpt-5.6-luna", (0.2, 0.02, 0.25, 1.2), (0.4, 0.04, 0.5, 1.8)),
    ]:
        card = pricing.get_model_pricing(model)
        assert card.known and (not card.approximate)
        assert (card.input, card.cache_read, card.cache_write, card.output) == short
        assert card.long_context_threshold() == 272000
        assert (
            tuple(
                tiers[0].rate
                for tiers in (card.input_tiers, card.cache_read_tiers, card.cache_write_tiers, card.output_tiers)
            )
            == long
        )
        assert card.request_cost_usd(input_tokens=272000, output_tokens=1000) == pytest.approx(
            (272000 * short[0] + 1000 * short[3]) / 1000000.0
        )
        assert card.request_cost_usd(input_tokens=272001, output_tokens=1000, long_context=True) == pytest.approx(
            (272001 * long[0] + 1000 * long[3]) / 1000000.0
        )


def test_future_models_use_same_named_family():
    for family, expected in [
        ("sol", "gpt-6.1-sol"),
        ("astra", "gpt-6-astra"),
        ("luna", "gpt-6-luna"),
        ("terra", "gpt-5.6-terra"),
    ]:
        for prefix in ["", "openai/"]:
            card = pricing.get_model_pricing(f"{prefix}gpt-7.2-{family}")
            assert card.model_id == expected
            assert card.approximate and card.known
            assert pricing.get_model_pricing(f"copilot/gpt-7.2-{family}").input == 0


def test_missing_named_family_does_not_cross_to_other_gpt(monkeypatch):
    table = dict(pricing._load_pricing_table())
    table = {key: value for key, value in table.items() if not key.endswith("-sol")}
    monkeypatch.setattr(pricing, "_load_pricing_table", lambda: table)
    pricing._nearest_table_match.cache_clear()
    assert not pricing.get_model_pricing("gpt-7-sol").known


def test_codex_cost_no_longer_zero_for_real_usage():
    assert _codex_estimated_cost_usd(
        "gpt-6.1-sol", {"input_tokens": 1000, "cache_read_tokens": 10000, "output_tokens": 100}
    ) == pytest.approx(0.004)

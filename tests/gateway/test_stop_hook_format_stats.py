"""Stop hook `_format_stats`: normal formatting + resilience to a stale
installed `lc` package.

This file is copied verbatim to ``~/.lemoncrow/claude-plugin/hooks/stop.py`` by
``scripts/install_claude.sh`` and is kept in sync with the dev repo
independently of the *installed* ``lc`` package (``uv tool install``).
A prebuilt/older install can lag behind and miss recently-added private
helpers (e.g. ``_fmt_tok``/``_fmt_usd`` in ``savings_summary.py``) -- that
mismatch must degrade to an equivalent inline formatter, never crash the hook.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from lemoncrow.core.capabilities import savings_summary

_STOP = Path("integrations/claude/plugin/hooks/stop.py")


def _load_stop() -> ModuleType:
    spec = importlib.util.spec_from_file_location("lemoncrow_stop_hook_format_stats", _STOP)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stats(**overrides: object) -> dict:
    base = {
        "input_tokens": 1_000,
        "output_tokens": 500,
        "cache_read_tokens": 2_000,
        "cache_write_tokens": 300,
        "tool_calls": 4,
        "turns": 2,
        "est_cost_usd": 0.05,
        "tools_used": {"read": 3, "edit": 1},
    }
    base.update(overrides)
    return base


def _savings(**overrides: object) -> dict:
    base = {
        "saved_usd": 1.5,
        "tokens_saved": 12_000,
        "calls_avoided": 3,
        "routing_usd": 0.0,
        "carry_usd": 0.0,
        "carry_tokens": 0,
        "output_usd": 0.0,
        "output_tokens": 0,
    }
    base.update(overrides)
    return base


def test_format_stats_normal_path_uses_canonical_formatters() -> None:
    stop = _load_stop()
    out = stop._format_stats(_stats(), _savings())
    assert "est. cost: $0.0500(I:1.3k C:2.0k O:500)" in out
    assert "↓ $1.50(I:12.0k) · 3 calls avoided" in out
    assert "top tools: read×3 · edit×1" in out  # noqa: RUF001


def test_format_stats_falls_back_when_installed_package_lacks_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulates the reported bug: an installed `lc` predating
    `_fmt_tok`/`_fmt_usd` must not crash `_format_stats` with an ImportError --
    it should fall back to an equivalent inline formatter and render the same
    output.
    """
    monkeypatch.delattr(savings_summary, "_fmt_tok", raising=True)
    monkeypatch.delattr(savings_summary, "_fmt_usd", raising=True)

    stop = _load_stop()
    out = stop._format_stats(_stats(), _savings())

    assert "est. cost: $0.0500(I:1.3k C:2.0k O:500)" in out
    assert "↓ $1.50(I:12.0k) · 3 calls avoided" in out


def test_format_stats_fallback_matches_canonical_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback formatter must render byte-identical output to the
    canonical one -- a stale install should be invisible to the user, not
    just non-crashing."""
    stats, savings = _stats(), _savings(output_usd=0.4, output_tokens=800, carry_usd=0.1, carry_tokens=200)

    canonical_stop = _load_stop()
    canonical_out = canonical_stop._format_stats(stats, savings)

    monkeypatch.delattr(savings_summary, "_fmt_tok", raising=True)
    monkeypatch.delattr(savings_summary, "_fmt_usd", raising=True)
    fallback_stop = _load_stop()
    fallback_out = fallback_stop._format_stats(stats, savings)

    assert fallback_out == canonical_out


def test_codex_and_claude_compact_breakdowns_match(tmp_path, monkeypatch):
    from lemoncrow.core.capabilities import plugin_runtime

    savings = _savings(
        carry_usd=0.2,
        carry_tokens=200,
        output_usd=0.4,
        output_tokens=800,
        input_usd=0.1,
        input_tokens=3000,
        routing_usd=0.05,
    )
    claude = _load_stop()._format_stats(_stats(), savings, real_cost=True)
    monkeypatch.setattr(
        plugin_runtime,
        "build_savings_report",
        lambda *args, **kwargs: {
            "session": {"turns": 2, "usage": _stats()},
            "cost": {
                "saved_usd": 1.5,
                "total_saved_usd": 1.7,
                "carry_usd": 0.2,
                "carry_tokens": 200,
                "output_saved_usd": 0.4,
                "output_saved_tokens": 800,
                "input_saved_usd": 0.1,
                "input_saved_tokens": 3000,
                "routing_saved_usd": 0.05,
            },
            "tokens_saved": 12000,
            "calls_avoided": 3,
        },
    )
    codex = plugin_runtime.build_codex_stop_output(
        tmp_path,
        {
            "hook_event_name": "Stop",
            "session_id": "parity",
            "cost": {"total_cost_usd": 0.05},
        },
    )["systemMessage"]
    claude_expected = "cost: $0.0500(I:1.3k C:2.0k O:500) ↓ $1.70(I:12.0k K:200 O:800 C:3.0k R:$0.05) · 3 calls avoided"
    assert claude_expected in claude.splitlines()
    assert "cost: $0.0500(I:1.3k C:2.0k O:500)" in codex.splitlines()
    assert any(
        line.startswith("est. LC savings: $1.70(I:12.0k K:200 O:800 C:3.0k R:$0.05)") and "3 calls avoided" in line
        for line in codex.splitlines()
    )


def test_format_stats_measured_subagents_separates_actual_cost_from_estimated_savings() -> None:
    stop = _load_stop()
    stats = _stats(
        measurement_source="claude_mod",
        measured_main_turns=2,
        measured_subagent_count=3,
        measured_subagent_turns=20,
        subagent_attributed_cost_usd=9.0,
        est_cost_usd=10.0,
    )
    savings = _savings(total_saved_usd=2.5)

    out = stop._format_stats(stats, savings, real_cost=True)

    assert "2 main turns · 4 main tool calls" in out
    assert "3 subagents · 20 child turns · 90.0% of spend" in out
    assert "Claude cost: $10.0000" in out
    assert "est. LC savings: $2.50" in out
    assert "~20.0% lower" in out
    assert "baseline" not in out
    assert "top main tools:" in out


def test_format_verified_local_savings_is_separate_from_modelled_total(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.verified_work import calibration

    monkeypatch.setattr(
        calibration,
        "session_calibrated_savings",
        lambda session_id, root=None: {
            "promoted_repairs": 3,
            "qualified_repairs": 3,
            "unqualified_repairs": 0,
            "local_cost_usd": 0.0,
            "calibrated_net_savings_usd": 1.42,
            "provenance": "paired_shadow_host_usage",
        },
    )
    stop = _load_stop()

    line = stop._format_verified_local_savings("session-1")

    assert line == "\nverified local: 3 promoted · 3 calibrated · net $1.42 paired (separate from est. LC savings)"

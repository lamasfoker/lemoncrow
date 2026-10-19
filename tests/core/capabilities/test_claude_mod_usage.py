from __future__ import annotations

import json
from pathlib import Path

import pytest

from lemoncrow.core.capabilities.claude_mod_usage import (
    bucket_cost_usd,
    load_claude_mod_usage,
    measurement_path,
)


def _payload(session_id: str = "sess-1") -> dict:
    return {
        "schema_version": 1,
        "source": "claude_mod",
        "session_id": session_id,
        "started_at_ms": 1000,
        "updated_at_ms": 2000,
        "session_usage": {"cost": {"total_cost_usd": 12.34}},
        "totals": {
            "steps": 3,
            "turns": 3,
            "duration_ms": 900,
            "input_tokens": 30,
            "output_tokens": 300,
            "cache_read_input_tokens": 30_000,
            "cache_creation_input_tokens": 3_000,
        },
        "main": {
            "kind": "main",
            "agent_id": None,
            "steps": 1,
            "turns": 1,
            "duration_ms": 300,
            "input_tokens": 10,
            "output_tokens": 100,
            "cache_read_input_tokens": 10_000,
            "cache_creation_input_tokens": 1_000,
            "models": {"claude-opus-5-5": 1},
            "model_usage": {
                "claude-opus-5-5": {
                    "steps": 1,
                    "input_tokens": 10,
                    "output_tokens": 100,
                    "cache_read_input_tokens": 10_000,
                    "cache_creation_input_tokens": 1_000,
                }
            },
        },
        "subagents": {
            "agent-a": {
                "kind": "subagent",
                "agent_id": "agent-a",
                "steps": 2,
                "turns": 2,
                "duration_ms": 600,
                "input_tokens": 20,
                "output_tokens": 200,
                "cache_read_input_tokens": 20_000,
                "cache_creation_input_tokens": 2_000,
                "models": {"claude-opus-5-5": 2},
                "model_usage": {
                    "claude-opus-5-5": {
                        "steps": 2,
                        "input_tokens": 20,
                        "output_tokens": 200,
                        "cache_read_input_tokens": 20_000,
                        "cache_creation_input_tokens": 2_000,
                    }
                },
            }
        },
        "models": {"claude-opus-5-5": 3},
    }


def test_load_claude_mod_usage_separates_main_and_subagents(tmp_path: Path) -> None:
    path = measurement_path(tmp_path, "sess-1")
    path.write_text(json.dumps(_payload()), encoding="utf-8")

    usage = load_claude_mod_usage(tmp_path, "sess-1")

    assert usage is not None
    assert usage.measured_cost_usd == pytest.approx(12.34)
    assert usage.total_steps == 3
    assert usage.main.turns == 1
    assert usage.subagent_turns == 2
    assert usage.subagent_tokens == 22_220
    assert usage.cache_read_tokens == 30_000
    assert bucket_cost_usd(usage.subagents["agent-a"]) > bucket_cost_usd(usage.main)


def test_load_claude_mod_usage_rejects_wrong_session(tmp_path: Path) -> None:
    path = measurement_path(tmp_path, "sess-1")
    payload = _payload("different")
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_claude_mod_usage(tmp_path, "sess-1") is None

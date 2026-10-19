from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from lemoncrow.gateway.cli import cli
from lemoncrow.pro.capabilities.cross_vendor_routing.configuration import RouteConfig, save_route_config
from lemoncrow.pro.capabilities.lesson_promotion.models import TypedLesson
from lemoncrow.pro.capabilities.lesson_promotion.store import TypedLessonStore


def _invoke(root: Path, *args: str) -> Result:
    runner = CliRunner()
    return runner.invoke(cli, ["--root", str(root), *args])


@pytest.mark.parametrize("command", ["configure", "plan"])
def test_retired_route_commands_are_unavailable(tmp_path: Path, command: str) -> None:
    root = tmp_path / ".lemoncrow"
    result = _invoke(root, "route", command, "--help")
    assert result.exit_code == 2
    assert f"No such command '{command}'" in result.output
    assert not (root / "route.yaml").exists()


def test_route_status_reports_recommendation_count_and_savings(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    save_route_config(root, RouteConfig(enabled_vendors=["anthropic"]))
    TypedLessonStore(root).upsert_lesson(
        TypedLesson(
            kind="route-preference",
            match={"tool": "read", "phase": "explore"},
            prefer={"vendor": "anthropic", "model": "claude-haiku-4-5"},
            confidence=0.9,
        )
    )
    (root / "live_savings_events.jsonl").write_text(
        json.dumps(
            {
                "kind": "model_recommendation",
                "configured": True,
                "cost_saved_usd": 0.125,
                "applied_lessons": ["tl-1"],
                "cost_cap_triggered": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    res = _invoke(root, "route", "status", "--json")

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["recommendation_count"] == 1
    assert payload["estimated_savings_usd"] == 0.125
    assert payload["active_lesson_count"] == 1
    assert payload["lesson_application_count"] == 1
    assert payload["cost_cap_trigger_count"] == 1

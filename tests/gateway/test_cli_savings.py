from __future__ import annotations

import json
from pathlib import Path

import click
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.pro.capabilities.verified_work.product import IsolationStatus, KindQualification, SavingsProductStatus


def test_savings_has_one_runtime_registration() -> None:
    root_ctx = click.Context(cli, info_name="lc")
    root_savings = cli.get_command(root_ctx, "savings")
    runtime = cli.get_command(root_ctx, "runtime")
    assert root_savings is None
    assert isinstance(runtime, click.Group)
    assert "savings" in runtime.commands
    assert runtime.commands["savings"].hidden is False
    assert "detail" not in runtime.commands["savings"].commands
    assert "reset" not in runtime.commands["savings"].commands
    result = CliRunner().invoke(cli, ["runtime", "savings", "--help"])
    assert result.exit_code == 0, result.output
    assert "enable" in result.output
    assert "status" in result.output
    assert "auto" in result.output


def test_enable_starts_learning_when_auto_is_not_yet_qualified(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.gateway.cli.commands import local_savings as savings_cmd
    from lemoncrow.pro.capabilities.verified_work import product

    model = "custom/ollama/test-coder"
    monkeypatch.setattr(product, "ensure_local_worker", lambda root, requested="auto": (model, "probed_local_model"))
    monkeypatch.setattr(
        product,
        "product_status",
        lambda root, settings, repo=None: SavingsProductStatus(
            "learning",
            settings.mode,
            model,
            "collecting_shadow_evidence",
            IsolationStatus(True, True, True, "qualified"),
            (KindQualification("lint", 0, 0.0, 0.0, False),),
            False,
            0,
            None,
            "collecting_shadow_evidence",
        ),
    )
    monkeypatch.setattr(
        savings_cmd,
        "_status_payload",
        lambda root, repo=None: {
            "status": {
                "state": "learning",
                "model": model,
                "isolation": {"qualified": True, "detail": "qualified"},
                "kinds": [],
            },
            "outcomes": {"totals": {}},
        },
    )

    result = CliRunner().invoke(
        cli,
        ["--root", str(tmp_path), "runtime", "savings", "enable", "--auto"],
    )

    assert result.exit_code == 0, result.output
    assert "staying in Learning mode" in result.output
    stored = json.loads((tmp_path / "plugin_settings.json").read_text())
    assert stored["verified_work.mode"] == "shadow"
    assert stored["verified_work.model"] == "auto"
    assert stored["verified_work.require_isolation"] is True


def test_auto_refuses_until_ready(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.verified_work import product

    monkeypatch.setattr(
        product,
        "product_status",
        lambda root, settings, repo=None: SavingsProductStatus(
            "learning",
            settings.mode,
            "custom/ollama/test",
            "collecting_shadow_evidence",
            IsolationStatus(True, True, True, "qualified"),
            (KindQualification("lint", 3, 0.66, 100.0, False),),
            False,
            0,
            None,
            "collecting_shadow_evidence",
        ),
    )

    result = CliRunner().invoke(cli, ["--root", str(tmp_path), "runtime", "savings", "auto"])

    assert result.exit_code != 0
    assert "not Ready yet" in result.output


def test_disable_is_one_command(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["--root", str(tmp_path), "runtime", "savings", "disable"])
    assert result.exit_code == 0, result.output
    stored = json.loads((tmp_path / "plugin_settings.json").read_text())
    assert stored["verified_work.mode"] == "off"


def test_status_renders_qualified_calibrated_savings(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.gateway.cli.commands import local_savings as savings_cmd

    monkeypatch.setattr(
        savings_cmd,
        "_status_payload",
        lambda root, repo=None: {
            "status": {
                "state": "auto",
                "model": "custom/ollama/test-coder",
                "isolation": {"qualified": True, "detail": "qualified"},
                "kinds": [],
            },
            "outcomes": {"totals": {"attempts": 10, "verified": 9, "promoted": 4, "provider_cost_usd": 0.0}},
            "calibration": {
                "status": "qualified",
                "pairs": 20,
                "min_pairs": 20,
                "calibrated_net_savings_usd": 3.25,
            },
            "jobs": {"pending": 0, "running": 0, "done": 10},
        },
    )

    result = CliRunner().invoke(cli, ["--root", str(tmp_path), "runtime", "savings", "status"])

    assert result.exit_code == 0, result.output
    assert "Calibrated net saved:" in result.output
    assert "3.25" in result.output
    assert "20 clean Learning pairs" in result.output
    assert "paired measured host usage" in result.output

"""Runtime aggregate-savings and statusline rendering contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from lemoncrow.gateway.cli import cli
from tests.helpers import init_store_at

# The full statusline contract, spec §5.1. Named here so a future rename has to
# delete a line from this list and explain itself.
_STATUSLINE_ENV = (
    "LEMONCROW_STATUS_SESSION_ID",
    "LEMONCROW_STATUS_HOST",
    "LEMONCROW_STATUSLINE_COST_USD",
    "LEMONCROW_STATUSLINE_LIVE_IN_TOK",
    "LEMONCROW_STATUSLINE_LIVE_CACHE_TOK",
    "LEMONCROW_STATUSLINE_LIVE_OUT_TOK",
    "LEMONCROW_STATUSLINE_CTX_PCT",
    "LEMONCROW_STATUSLINE_CTX_TOK",
    "LEMONCROW_STATUSLINE_NO_COLOR",
    "LEMONCROW_STATUS_WORKSPACE_ROOT",
    "LEMONCROW_STATUS_MODEL",
)


def _invoke(root: Path, *args: str) -> Result:
    return CliRunner().invoke(cli, ["--root", str(root), *args])


def test_savings_is_namespaced_in_help() -> None:
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    listed = {line.strip().split(" ", 1)[0] for line in result.output.splitlines() if line.startswith("  ")}
    assert "savings" not in listed
    assert "optimize" not in listed
    # The surface that replaced it in --help must actually be there.
    assert "usage" in listed


def test_runtime_savings_help() -> None:
    result = CliRunner().invoke(cli, ["runtime", "savings", "--help"])

    assert result.exit_code == 0, result.output
    assert "--json" in result.output


def test_savings_segment_flag_still_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / ".lemoncrow"
    init_store_at(str(root))
    monkeypatch.setenv("LEMONCROW_STATUS_SESSION_ID", "statusline-session")
    monkeypatch.setenv("LEMONCROW_STATUS_HOST", "claude")
    monkeypatch.setenv("LEMONCROW_STATUSLINE_COST_USD", "1.25")
    monkeypatch.setenv("LEMONCROW_STATUSLINE_LIVE_IN_TOK", "1000")
    monkeypatch.setenv("LEMONCROW_STATUSLINE_LIVE_CACHE_TOK", "200")
    monkeypatch.setenv("LEMONCROW_STATUSLINE_LIVE_OUT_TOK", "300")
    monkeypatch.setenv("LEMONCROW_STATUSLINE_CTX_PCT", "42")
    monkeypatch.setenv("LEMONCROW_STATUSLINE_CTX_TOK", "84000")
    monkeypatch.setenv("LEMONCROW_STATUSLINE_NO_COLOR", "1")

    result = _invoke(root, "runtime", "savings", "--segment")

    assert result.exit_code == 0, result.output
    assert result.output != ""


def test_savings_segment_tolerates_malformed_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A statusline must never be the thing that breaks a shell prompt."""

    root = tmp_path / ".lemoncrow"
    init_store_at(str(root))
    monkeypatch.setenv("LEMONCROW_STATUS_SESSION_ID", "statusline-session")
    monkeypatch.setenv("LEMONCROW_STATUSLINE_COST_USD", "not-a-number")
    monkeypatch.setenv("LEMONCROW_STATUSLINE_CTX_PCT", "")

    result = _invoke(root, "runtime", "savings", "--segment")

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output


def test_savings_segment_env_contract_is_unchanged() -> None:
    """Every variable in §5.1 must still be read by the segment branch."""

    source = Path("src/lemoncrow/gateway/cli/commands/savings.py").read_text(encoding="utf-8")
    for name in _STATUSLINE_ENV:
        assert name in source, f"statusline env var dropped: {name}"


def test_savings_segment_writes_raw_stdout_not_click_echo() -> None:
    """``click.echo`` strips ANSI off a non-TTY; the segment is colour-bearing."""

    source = Path("src/lemoncrow/gateway/cli/commands/savings.py").read_text(encoding="utf-8")
    assert "sys.stdout.write(" in source
    assert "sys.stdout.flush()" in source


def test_savings_json_still_works(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    init_store_at(str(root))

    result = _invoke(root, "runtime", "savings", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "optimization" in payload
    assert "calls_avoided" in payload


def test_build_savings_payload_matches_the_cli_json(tmp_path: Path) -> None:
    """The §3 PR-7 extraction is behaviour-preserving, asserted rather than claimed."""

    from lemoncrow.gateway.cli.commands.savings import build_savings_payload

    root = tmp_path / ".lemoncrow"
    init_store_at(str(root))

    result = _invoke(root, "runtime", "savings", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == json.loads(json.dumps(build_savings_payload(root), default=str))


def test_savings_json_exposes_local_calibration_without_folding_into_modeled_total(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    init_store_at(str(root))

    result = _invoke(root, "runtime", "savings", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    local = payload["verified_local_work"]
    assert local["provenance"] == "paired_shadow_host_usage"
    assert local["included_in_modeled_total_saved"] is False
    assert local["calibration"]["status"] == "calibrating"


def test_savings_text_labels_local_calibration_separately(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    init_store_at(str(root))

    result = _invoke(root, "runtime", "savings")

    assert result.exit_code == 0, result.output
    assert "Local verified work" in result.output
    assert "Calibrated net saved:   learning" in result.output
    assert "paired baselines" in result.output

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_full_distribution_exposes_real_lcr_console_script() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert data["project"]["scripts"]["lcr"] == "lemoncrow.gateway.cli:main"


def test_thin_review_distribution_exposes_real_lcr_console_script() -> None:
    data = tomllib.loads((ROOT / "client" / "pyproject.toml").read_text())
    assert data["project"]["scripts"]["lcr"] == "lemoncrow_client.cli:review_entrypoint"


def test_full_cli_maps_lcr_argv0_to_review() -> None:
    from lemoncrow.gateway.cli.app import _argv_for_program

    assert _argv_for_program("lcr", ["--help"]) == ["review", "--help"]


def test_installers_no_longer_write_lcr_shell_aliases() -> None:
    common = (ROOT / "scripts" / "lib" / "common.sh").read_text()
    install = (ROOT / "scripts" / "install.sh").read_text()
    engine = (ROOT / "scripts" / "engine_install.sh").read_text()
    assert "alias lcr" not in common
    assert "_alias_line" not in install + engine
    assert "for short_bin in lemoncrow lc lcr" in engine


def test_full_lcr_dispatches_review_product_commands_before_click(monkeypatch) -> None:
    """The actual installed full-dist `lcr` must not swallow companion options."""
    import io
    import sys

    from lemoncrow_client import review

    from lemoncrow.gateway.cli import app

    forwarded: list[list[str]] = []
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(review, "review_main", lambda arguments, *, sink: forwarded.append(arguments) or 23)
    for arguments in (
        ["companion", "run", "job-1", "--allow-external-source"],
        ["companion", "--help"],
        ["watch", "--once"],
        ["pending", "--watch", "--class", "review_intelligence"],
        ["device", "check"],
        ["task", "run", "job-1", "--max-cost", "1000000"],
        ["connect"],
    ):
        monkeypatch.setattr(sys, "argv", ["/example/venv/bin/lcr", *arguments])
        with __import__("pytest").raises(SystemExit) as exited:
            app.main()
        assert exited.value.code == 23
    assert forwarded == [
        ["companion", "run", "job-1", "--allow-external-source"],
        ["companion", "--help"],
        ["watch", "--once"],
        ["pending", "--watch", "--class", "review_intelligence"],
        ["device", "check"],
        ["task", "run", "job-1", "--max-cost", "1000000"],
        ["connect"],
    ]

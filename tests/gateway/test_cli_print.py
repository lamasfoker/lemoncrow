from __future__ import annotations

from click.testing import CliRunner

from lemoncrow.gateway.cli import cli


def test_hosted_code_owns_one_shot_prompt_surface() -> None:
    result = CliRunner().invoke(cli, ["code", "-p", "explain this"])
    assert result.exit_code == 1
    assert "Hosted LemonCrow coding agent" in result.output


def test_top_level_print_aliases_are_removed() -> None:
    for flag, prompt in [("-p", "explain this"), ("--print", "debug this")]:
        result = CliRunner().invoke(cli, [flag, prompt])
        assert result.exit_code == 2
        assert f"No such option '{flag}'" in result.output

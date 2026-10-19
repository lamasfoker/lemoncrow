"""Retired CLI commands have no public or hidden compatibility routes."""

from __future__ import annotations

from click.testing import CliRunner

from lemoncrow.gateway.cli import cli

_RETIRED_ROOT = (
    "letta",
    "reembed",
    "share",
    "proof",
    "optimize",
    "savings",
    "add-playbook",
    "list-playbooks",
    "project",
    "report",
    "insights",
    "resume-context",
    "domain",
    "playbook",
    "lesson",
    "import-style-guide",
    "stale-nudge",
    "import",
    "perf",
    "eval",
    "defaults",
)
_RETIRED_PATHS = [[command] for command in _RETIRED_ROOT] + [
    [prefix, command] for prefix in ("runtime", "rt") for command in _RETIRED_ROOT if command != "savings"
]


def test_retired_v3_command_surface_is_absent() -> None:
    runner = CliRunner()

    for args in _RETIRED_PATHS:
        result = runner.invoke(cli, [*args, "--help"])
        assert result.exit_code == 2, (args, result.output)
        assert f"No such command '{args[-1]}'" in result.output

    root_help = runner.invoke(cli, ["--help"], env={"LEMONCROW_SHOW_ALL": "1"})
    assert root_help.exit_code == 0, root_help.output
    listed = {
        line.strip().split()[0] for line in root_help.output.splitlines() if line.startswith("  ") and line.strip()
    }
    assert not (set(_RETIRED_ROOT) & listed)

    for shell in ("bash", "zsh", "fish"):
        completion = runner.invoke(cli, ["completions", shell])
        assert completion.exit_code == 0, completion.output
        for command in _RETIRED_ROOT:
            if shell == "zsh":
                assert f"'{command}:" not in completion.output
            elif shell == "bash":
                declaration = next(line for line in completion.output.splitlines() if "local commands=" in line)
                assert command not in declaration.split('"')[1].split()
            else:
                assert f" -a {command} -d " not in completion.output

    for args in (
        ["tools", "mode"],
        ["lesson", "list"],
        ["knowledge", "extract"],
        ["runtime", "savings", "detail"],
        ["runtime", "savings", "reset"],
    ):
        result = runner.invoke(cli, [*args, "--help"])
        assert result.exit_code == 2, (args, result.output)
        assert "No such command" in result.output

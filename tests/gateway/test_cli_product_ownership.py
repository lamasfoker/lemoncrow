from __future__ import annotations

import click

from lemoncrow.gateway.cli.app import cli
from lemoncrow.gateway.cli.product_ownership import (
    COMMAND_OWNERSHIP,
    PRODUCT_ALIASES,
    RUNTIME_COMMAND_NAMES,
    RUNTIME_ENGINE_SHORTCUTS,
    RUNTIME_NAMESPACED_ONLY_COMMANDS,
    RUNTIME_PROMOTED_COMMANDS,
    VISIBLE_COMMAND_OWNERSHIP,
    ProductOwner,
)


def test_every_visible_top_level_command_has_one_product_owner() -> None:
    ctx = click.Context(cli, info_name="lc")
    visible = {
        name
        for name in cli.list_commands(ctx)
        if (command := cli.get_command(ctx, name)) is not None and not command.hidden
    }
    assert visible == set(VISIBLE_COMMAND_OWNERSHIP)


def test_review_and_runtime_surfaces_do_not_share_ownership() -> None:
    assert VISIBLE_COMMAND_OWNERSHIP["review"] is ProductOwner.REVIEW
    for command in ("code", "mcp", "agent", "skill"):
        if command in COMMAND_OWNERSHIP:
            assert COMMAND_OWNERSHIP[command] is ProductOwner.RUNTIME


def test_core_owns_brand_level_lifecycle_surfaces() -> None:
    for command in ("auth", "help", "settings", "update", "usage"):
        assert VISIBLE_COMMAND_OWNERSHIP[command] is ProductOwner.CORE


def test_root_help_promotes_common_work_without_exposing_runtime_internals() -> None:
    from click.testing import CliRunner

    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Work:" in result.output
    assert "Try with your agent:" in result.output
    assert "Products:" in result.output
    assert "Setup & system:" in result.output
    assert "Runtime tools:" not in result.output

    work = result.output.split("Work:", 1)[1].split("Products:", 1)[0]
    for command in ("review", "code", "run", "session", "context", "usage"):
        assert f"  {command} " in work
    try_agents = result.output.split("Try with your agent:", 1)[1].split("Products:", 1)[0]
    for command in RUNTIME_ENGINE_SHORTCUTS:
        assert f"  {command} " in try_agents
    products = result.output.split("Products:", 1)[1].split("Setup & system:", 1)[0]
    assert "runtime" in products
    for command in RUNTIME_NAMESPACED_ONLY_COMMANDS:
        assert f"  {command} " not in result.output
        assert cli.get_command(click.Context(cli, info_name="lc"), command) is None


def test_runtime_namespace_mounts_all_runtime_commands_and_promoted_commands_share_identity() -> None:
    ctx = click.Context(cli, info_name="lc")
    runtime = cli.get_command(ctx, "runtime")
    assert isinstance(runtime, click.Group)

    expected = {
        name
        for name, owner in COMMAND_OWNERSHIP.items()
        if owner is ProductOwner.RUNTIME
        and name not in {"runtime", *PRODUCT_ALIASES}
        and name not in RUNTIME_ENGINE_SHORTCUTS
    }
    assert expected <= set(runtime.commands)
    for name in RUNTIME_PROMOTED_COMMANDS:
        assert name in cli.commands
        assert runtime.commands[name] is cli.commands[name]
        assert runtime.commands[name].hidden is False
    for name in RUNTIME_NAMESPACED_ONLY_COMMANDS:
        assert name not in cli.commands
        assert runtime.commands[name].hidden is False


def test_promoted_runtime_commands_work_at_root_and_namespaced_only_commands_do_not() -> None:
    from click.testing import CliRunner

    for command in RUNTIME_PROMOTED_COMMANDS:
        assert command in RUNTIME_COMMAND_NAMES
        root = CliRunner().invoke(cli, [command, "--help"])
        nested = CliRunner().invoke(cli, ["runtime", command, "--help"])
        shorthand = CliRunner().invoke(cli, ["rt", command, "--help"])
        assert root.exit_code == 0, root.output
        assert nested.exit_code == 0, nested.output
        assert shorthand.exit_code == 0, shorthand.output

    for command in ("mcp", "model", "agent", "skill"):
        assert command in RUNTIME_NAMESPACED_ONLY_COMMANDS
        nested = CliRunner().invoke(cli, ["runtime", command, "--help"])
        assert nested.exit_code == 0, nested.output
        top_level = CliRunner().invoke(cli, [command, "--help"])
        assert top_level.exit_code == 2
        assert "No such command" in top_level.output


def test_engine_shortcuts_are_root_only_runtime_owned_entrypoints() -> None:
    from click.testing import CliRunner

    ctx = click.Context(cli, info_name="lc")
    runtime = cli.get_command(ctx, "runtime")
    assert isinstance(runtime, click.Group)
    for command in RUNTIME_ENGINE_SHORTCUTS:
        assert COMMAND_OWNERSHIP[command] is ProductOwner.RUNTIME
        assert command in cli.commands
        assert command not in runtime.commands
        result = CliRunner().invoke(cli, [command, "--help"], prog_name="lc")
        assert result.exit_code == 0, result.output
        assert f"Usage: lc {command}" in result.output

    result = CliRunner().invoke(cli, ["with", "--help"], prog_name="lc")
    assert result.exit_code == 0, result.output
    assert "Try LemonCrow with an existing coding agent" in result.output
    for host in ("claude", "codex", "opencode"):
        assert host in result.output
        host_help = CliRunner().invoke(cli, ["with", host, "--help"], prog_name="lc")
        assert host_help.exit_code == 0, host_help.output
        assert "for this process only" in host_help.output


def test_rt_is_hidden_alias_for_runtime_namespace() -> None:
    from click.testing import CliRunner

    assert COMMAND_OWNERSHIP["rt"] is ProductOwner.RUNTIME
    assert "rt" in PRODUCT_ALIASES
    root_help = CliRunner().invoke(cli, ["--help"])
    assert root_help.exit_code == 0
    assert "  rt " not in root_help.output

    canonical = CliRunner().invoke(cli, ["runtime", "code", "--help"])
    shorthand = CliRunner().invoke(cli, ["rt", "code", "--help"])
    assert canonical.exit_code == 0, canonical.output
    assert shorthand.exit_code == 0, shorthand.output
    assert shorthand.output.split("Options:", 1)[-1] == canonical.output.split("Options:", 1)[-1]

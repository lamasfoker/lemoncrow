"""Hidden developer entry point; private packages load only after invocation."""

from __future__ import annotations

import click


@click.command(
    "dev",
    add_help_option=False,
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("arguments", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def dev_command(ctx: click.Context, arguments: tuple[str, ...]) -> None:
    """Manage checkout profiles and run engineering tools."""
    from lemoncrow_client.development import dev_main

    if arguments[:1] and arguments[0] in {"eval", "perf", "defaults"}:
        arguments = ("--root", str(ctx.obj["root"]), *arguments)
    raise SystemExit(dev_main(arguments))

"""The once-a-day "update available" footer for interactive CLI commands."""

from __future__ import annotations

import sys

import click

# Machine-facing commands: their stdout/stderr feeds another program, never a person.
_NO_UPDATE_FOOTER = frozenset({"mcp", "update", "savings", "completions"})


def print_update_footer(command_name: str, argv: list[str]) -> None:
    """One "update available" line on stderr, at most once a day, interactive terminals only."""
    if command_name in _NO_UPDATE_FOOTER or {"--json", "-p"} & set(argv):
        return
    try:
        if not (sys.stdout.isatty() and sys.stderr.isatty()):
            return
        from lemoncrow.core.foundation.update_notice import notice_for

        if notice := notice_for("cli"):
            from lemoncrow.gateway.cli import style

            click.echo(f"\n{style.accent('◇')} {notice}", err=True)
    except Exception:
        return


__all__ = ["print_update_footer"]

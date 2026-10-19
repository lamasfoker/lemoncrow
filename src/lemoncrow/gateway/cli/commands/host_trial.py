"""`lc with <host>` — try Runtime in an existing coding-agent session."""

from __future__ import annotations

import click
from lemoncrow_client.host_trial import TRIAL_HOSTS, HostTrialError, run_trial_host


@click.group("with", short_help="Try LemonCrow with an existing coding agent, this session only.")
def with_group() -> None:
    """Try LemonCrow with an existing coding agent for this session only."""


def _host_command(host: str) -> click.Command:
    label = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode", "pi": "Pi"}[host]

    @click.command(
        host,
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
        help=(
            f"Launch your normal {label} with LemonCrow tools for this process only. "
            "Arguments after the host are passed through unchanged."
        ),
    )
    @click.argument("args", nargs=-1, type=click.UNPROCESSED)
    def command(args: tuple[str, ...]) -> None:
        try:
            return_code = run_trial_host(host, args)
        except HostTrialError as exc:
            raise click.ClickException(str(exc)) from exc
        if return_code:
            raise SystemExit(return_code)

    return command


for _host in TRIAL_HOSTS:
    with_group.add_command(_host_command(_host), name=_host)


__all__ = ["with_group"]

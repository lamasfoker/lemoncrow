"""``lc review observation-worker run|check``: the customer-side production observation worker.

Runs on the customer's infrastructure, next to the telemetry. ``run`` claims
leased production observation jobs from the hosted Review service, queries the
locally pinned Prometheus/Sentry connectors with credentials from this
machine's environment, and reports bounded aggregates. ``check`` is a dry run:
it proves the hosted service is reachable, this worker is designated and each
pinned connector accepts its credential, with requests that read no telemetry.

The worker token is read from an environment variable (never a flag), and no
output carries a token or credential value.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import click


def _client(server: str, token_env: str) -> Any:
    from lemoncrow.pro.capabilities.review.outcomes.observation_worker import HostedObservationClient

    token = os.environ.get(token_env, "")
    if not token:
        raise click.ClickException(f"set the worker token in ${token_env}")
    try:
        return HostedObservationClient(server, token)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


def _pins(path: Path) -> Any:
    from lemoncrow.pro.capabilities.review.outcomes.observation_worker import load_connector_pins

    try:
        return load_connector_pins(path)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


_SERVER = click.option("--server", required=True, help="Hosted LemonCrow URL (https).")
_TOKEN_ENV = click.option(
    "--token-env",
    default="LEMONCROW_WORKER_TOKEN",
    show_default=True,
    help="Environment variable holding this worker's token.",
)
_REPOS = click.option(
    "--repo", "repos", multiple=True, help="Hosted repository id (repeatable; default: all authorized)."
)
_PINS = click.option(
    "--pins",
    "pins_path",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    required=True,
    help='Connector pins: {"version": 1, "connectors": [{"kind", "base_url", "credential_env"}]}.',
)


@click.group("observation-worker")
def observation_worker_group() -> None:
    """Run production observation for hosted Reviews on this (customer) machine."""


@observation_worker_group.command("check")
@_SERVER
@_TOKEN_ENV
@_REPOS
@_PINS
@click.option("--json", "as_json", is_flag=True, help="Print the machine-readable result.")
def check_cmd(server: str, token_env: str, repos: tuple[str, ...], pins_path: Path, as_json: bool) -> None:
    """Dry run: reachability, designation and connector permission; ingests no telemetry."""

    from lemoncrow.pro.capabilities.review.outcomes.observation_worker import check_worker

    result = check_worker(_client(server, token_env), repository_ids=repos, pins=_pins(pins_path), environ=os.environ)
    if as_json:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    elif not result["reachable"]:
        click.echo(f"hosted service unreachable: {result['error']}")
    else:
        click.echo(f"worker {result['worker_id']}: designated={result['designated']} ready={result['ready']}")
        for repo in result["repositories"]:
            locus = repo.get("locus") or {}
            click.echo(f"  {repo['repo_id']}: {locus.get('locus', 'unknown')} ({locus.get('reason', '')})")
            for item in repo["connectors"]:
                click.echo(f"    {item['kind']} {item['base_url']} [{item['credential_env'] or '-'}]: {item['probe']}")
    if not result.get("ready"):
        raise SystemExit(1)


#: The longest pause after consecutive failed passes (the poll interval doubles up to it).
MAX_FAILURE_BACKOFF_SECONDS = 15 * 60.0


@observation_worker_group.command("run")
@_SERVER
@_TOKEN_ENV
@_REPOS
@_PINS
@click.option("--once", is_flag=True, help="Process leases until none is due, then exit.")
@click.option(
    "--interval", type=click.FloatRange(min=5.0), default=60.0, show_default=True, help="Seconds between polls."
)
def run_cmd(server: str, token_env: str, repos: tuple[str, ...], pins_path: Path, once: bool, interval: float) -> None:
    """Claim, execute and report production observation leases."""

    from lemoncrow.pro.capabilities.review.outcomes.observation_worker import ObservationWorkerError, run_until_idle

    client, pins = _client(server, token_env), _pins(pins_path)
    failures = 0
    while True:
        error = ""
        try:
            outcomes = run_until_idle(client, repository_ids=repos, pins=pins, environ=os.environ)
        except ObservationWorkerError as exc:
            error, outcomes = f"hosted call failed: {exc.code}", []
        except Exception as exc:
            # One failed pass never stops a long-running worker. The type only: an exception's text may
            # carry an endpoint or a response fragment.
            error, outcomes = f"worker pass failed: {type(exc).__name__}", []
        if error and once:
            raise click.ClickException(error)
        for outcome in outcomes:
            if outcome.state != "idle":
                click.echo(f"{outcome.job_id}: {outcome.state}")
        if once:
            return
        failures = failures + 1 if error else 0
        delay = min(interval * (2 ** (failures - 1)), MAX_FAILURE_BACKOFF_SECONDS) if failures else interval
        if error:
            click.echo(f"{error}; retrying in {delay:.0f}s", err=True)
        time.sleep(delay)

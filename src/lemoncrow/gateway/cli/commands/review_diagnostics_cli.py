"""``lc review diagnostics``: one redacted diagnostic bundle for Review support.

The local surface of the shared builder
(:mod:`lemoncrow.pro.capabilities.review.diagnostic_bundle`); the hosted surface
is ``GET /v1/admin/reviews/diagnostic-bundle`` (organization admins). It reads
the loopback server's Review stores read-only, this client's protocol window,
the configured server's discovery document and the data-flow origins of one
repository, and prints a JSON bundle with no source and no secrets. ``--include``
adds exactly the named extra item and nothing else.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import click

from lemoncrow.gateway.cli.commands.review_data_flow_cli import _git, read_trusted_base, store_origin

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.data_flow import FlowOrigin
    from lemoncrow.pro.capabilities.review.diagnostic_bundle import DataFlowFacts

#: Restated for ``click.Choice`` (``lc --help`` imports nothing heavy); pinned equal
#: to ``diagnostic_bundle.LOCAL_INCLUDES`` by the tests.
_INCLUDE_CHOICES: tuple[str, ...] = ("error-text", "model-names")


def _data_flow_facts(repo: Path | None, *, base_ref: str | None, store_root: Path, origin: FlowOrigin) -> DataFlowFacts:
    from lemoncrow.gateway.cli.commands.review_data_flow_cli import local_data_flow
    from lemoncrow.pro.capabilities.review.diagnostic_bundle import DataFlowFacts

    if repo is None:
        return DataFlowFacts(unavailable="not_a_repository")
    try:
        projection = local_data_flow(repo, base_ref=base_ref, store_root=store_root, store_origin=origin)
    except Exception:
        return DataFlowFacts(unavailable="data_flow_failed")
    return DataFlowFacts(projections=(("current_repository", projection),))


@click.command("diagnostics")
@click.option(
    "--repo",
    "repo_path",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Repository whose data-flow origins to include. Default: the current directory, if it is in a Git repository.",
)
@click.option(
    "--base", "base_ref", default=None, help="Trusted base for the data-flow origins (as `lc review data-flow`)."
)
@click.option(
    "--review-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="The loopback server's Review stores. Default: <root>/server/data/reviews (LEMONCROW_SERVER_REVIEW_ROOT).",
)
@click.option(
    "--repository-id",
    "repository_id",
    default="",
    help="Read only this repository's Review store (a partition name under the Review root). "
    "Default: the stores whose Reviews were updated most recently.",
)
@click.option(
    "--include",
    "includes",
    multiple=True,
    type=click.Choice(_INCLUDE_CHOICES),
    help="Add exactly one named extra item: error-text (bounded, scrubbed error messages) or model-names. Repeatable.",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Write the bundle to FILE (owner-only) instead of printing it.",
)
@click.pass_context
def diagnostics_cmd(
    ctx: click.Context,
    repo_path: Path | None,
    base_ref: str | None,
    review_root: Path | None,
    repository_id: str,
    includes: tuple[str, ...],
    out_path: Path | None,
) -> None:
    """Export one redacted diagnostic bundle: versions, protocol and capability manifest,
    data-flow origins, Snapshot ids, job states, provider operation ids, degradation
    reasons and recent errors.

    No source, no secrets, no environment values: fields come from an allowlist and
    error and degradation text keeps only diagnostic vocabulary. A section this
    machine cannot collect says why instead of being left out.
    """

    from lemoncrow.core.foundation.paths import default_store_root, local_server_review_root
    from lemoncrow.pro.capabilities.model_setup.endpoints import inline_api_keys
    from lemoncrow.pro.capabilities.review.diagnostic_bundle import LOCAL_INCLUDES, build_diagnostic_bundle
    from lemoncrow.pro.capabilities.review.diagnostic_local import local_diagnostic_facts, local_protocol_facts

    obj = ctx.find_root().obj or {}
    store_root = Path(obj.get("root") or default_store_root())
    where = (repo_path or Path.cwd()).expanduser()
    top = _git(where, "rev-parse", "--show-toplevel")
    repo = Path(top.stdout.decode("utf-8", errors="replace").strip()) if top.returncode == 0 else None
    if repo_path is not None and repo is None:
        raise click.ClickException(f"{where} is not inside a Git repository")
    if base_ref is not None and (repo is None or read_trusted_base(repo, base_ref)[0] is None):
        raise click.ClickException(f"--base {base_ref!r} does not resolve to a readable commit")
    reviews = (review_root or local_server_review_root(store_root)).expanduser()
    facts = local_diagnostic_facts(
        review_root=reviews,
        protocol=local_protocol_facts(repo or Path.cwd()),
        data_flow=_data_flow_facts(repo, base_ref=base_ref, store_root=store_root, origin=store_origin(ctx)),
        repository_id=repository_id,
    )
    if repository_id and facts.records.repositories_scanned == 0 and facts.records.snapshots.unavailable:
        raise click.ClickException("--repository-id does not name a Review store under the Review root")
    try:
        bundle = build_diagnostic_bundle(
            facts,
            include=includes,
            allowed_includes=LOCAL_INCLUDES,
            requested_by="local",
            # A key typed into `lc model add-endpoint --api-key` lives in providers.json, not the environment.
            extra_secrets=inline_api_keys(store_root),
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    text = json.dumps(bundle, indent=2, sort_keys=True) + "\n"
    if out_path is None:
        click.echo(text, nl=False)
        return
    target = out_path.expanduser()
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    click.echo(f"Wrote the diagnostic bundle to {target}")


__all__ = ["diagnostics_cmd"]

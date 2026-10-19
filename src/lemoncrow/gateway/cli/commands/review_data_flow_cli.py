"""``lc review data-flow``: where this repository's source, code and evidence may travel.

The local surface of the shared data-flow builder
(:mod:`lemoncrow.pro.capabilities.review.data_flow`); the hosted surface is
``GET /v1/repos/{repo_id}/data-flow``. Policy is read only from the trusted
base (``origin/HEAD`` when known, else ``HEAD``), never the working tree.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.data_flow import DataFlowProjection, FlowOrigin
    from lemoncrow.pro.capabilities.review.trusted_base import TrustedBaseDocument

_REVIEW_CONFIG = ".lemoncrow/review.yaml"
_MAX_CONFIG_BYTES = 1024 * 1024


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)


def read_trusted_base(repo: Path, ref: str) -> tuple[TrustedBaseDocument | None, str]:
    """The ``.lemoncrow/review.yaml`` committed at *ref*: present, absent, or unknown with the reason."""

    from lemoncrow.pro.capabilities.review.trusted_base import TrustedBaseDocument

    resolved = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}") if not ref.startswith("-") else None
    if resolved is None or resolved.returncode != 0:
        return None, f"trusted base {ref!r} does not resolve to a commit"
    sha = resolved.stdout.decode("ascii", errors="replace").strip()
    source = f"trusted base {ref} ({sha[:12]}) {_REVIEW_CONFIG}"
    if _git(repo, "cat-file", "-e", f"{sha}:{_REVIEW_CONFIG}").returncode != 0:
        return TrustedBaseDocument(path=_REVIEW_CONFIG, content="", state="absent", source=source), ""
    shown = _git(repo, "show", f"{sha}:{_REVIEW_CONFIG}")
    if shown.returncode != 0 or len(shown.stdout) > _MAX_CONFIG_BYTES:
        return None, f"{_REVIEW_CONFIG} at {ref} could not be read"
    content = shown.stdout.decode("utf-8", errors="replace")
    return TrustedBaseDocument(path=_REVIEW_CONFIG, content=content, state="present", source=source), ""


def local_data_flow(
    repo: Path, *, base_ref: str | None, store_root: Path, store_origin: FlowOrigin | None = None
) -> DataFlowProjection:
    """Compose the local inputs and run the shared builder."""

    from lemoncrow.pro.capabilities.model_setup.endpoints import list_endpoints
    from lemoncrow.pro.capabilities.review.data_flow import (
        CA_SETTINGS,
        LOCAL_ENVIRONMENT,
        PROXY_SETTINGS,
        STORE_ROOT_ENV,
        DataFlowInputs,
        EvidenceFeed,
        FlowOrigin,
        build_data_flow,
        environment_route_facts,
        set_environment_names,
    )
    from lemoncrow.pro.capabilities.review.data_governance import load_data_governance
    from lemoncrow.pro.capabilities.review.intelligence_policy import (
        process_intelligence_policy,
        resolve_intelligence_route,
    )
    from lemoncrow.pro.capabilities.review.intelligence_runtime import agent_cli_drivers_allowed
    from lemoncrow.pro.capabilities.review.outcomes.local_deployments import (
        LOCAL_SETTINGS_FILE,
        load_local_deployment_settings,
    )
    from lemoncrow.pro.capabilities.review.outcomes.local_observability import (
        LOCAL_OBSERVABILITY_FILE,
        LOCAL_SOURCE,
        load_local_observability_settings,
    )
    from lemoncrow.pro.capabilities.review.reproduce import default_trusted_base

    ref = base_ref or default_trusted_base(repo)
    trusted_base, reason = read_trusted_base(repo, ref)
    agent_cli = agent_cli_drivers_allowed()
    governance = load_data_governance()
    # The policy every route in this process resolves under (``--root`` included).
    policy = process_intelligence_policy()
    inputs = DataFlowInputs(
        surface="local",
        repository=str(repo),
        trusted_base=trusted_base,
        trusted_base_reason=reason,
        routes=environment_route_facts(
            layer=LOCAL_ENVIRONMENT, agent_cli_allowed=agent_cli, locus="local", governance=governance, policy=policy
        ),
        governance=governance,
        intelligence=resolve_intelligence_route(policy, allowed_run_on=("local",), governance=governance),
        agent_cli_allowed=agent_cli,
        endpoints=tuple(list_endpoints(store_root)),
        deployments=EvidenceFeed(
            load_local_deployment_settings(store_root), (FlowOrigin(LOCAL_SETTINGS_FILE, LOCAL_SOURCE),)
        ),
        observability=EvidenceFeed(
            load_local_observability_settings(store_root), (FlowOrigin(LOCAL_OBSERVABILITY_FILE, LOCAL_SOURCE),)
        ),
        environment_names=set_environment_names((*PROXY_SETTINGS, *CA_SETTINGS, STORE_ROOT_ENV)),
        store_origin=store_origin,
    )
    return build_data_flow(inputs)


def store_origin(ctx: click.Context) -> FlowOrigin:
    """Where the store root came from: ``lc --root``, ``LEMONCROW_ROOT`` or the built-in default."""

    from lemoncrow.pro.capabilities.review.data_flow import (
        LOCAL_ENVIRONMENT,
        STORE_ROOT_ENV,
        FlowOrigin,
        set_environment_names,
    )

    root_ctx = ctx.find_root()
    if root_ctx.get_parameter_source("root") not in (None, click.ParameterSource.DEFAULT):
        return FlowOrigin("lc --root", "command_line")
    if set_environment_names((STORE_ROOT_ENV,)):
        return FlowOrigin(STORE_ROOT_ENV, LOCAL_ENVIRONMENT)
    return FlowOrigin(STORE_ROOT_ENV, "built_in_default")


@click.command("data-flow")
@click.option(
    "--base",
    "base_ref",
    default=None,
    help="Trusted base whose .lemoncrow/review.yaml governs. Default: origin/HEAD when known, else HEAD.",
)
@click.option(
    "--repo",
    "repo_path",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Repository to describe. Default: the current directory.",
)
@click.option("--json", "as_json", is_flag=True, help="Output the projection as JSON.")
@click.pass_context
def data_flow_cmd(ctx: click.Context, base_ref: str | None, repo_path: Path | None, as_json: bool) -> None:
    """Show where this repository's source, code and evidence may travel under its effective policy.

    Every row names the setting and policy layer it came from. Unknowns are
    shown as unknown, never as allowed. Environment values are never printed;
    proxy and CA settings are reported by name.
    """

    from lemoncrow.core.foundation.paths import default_store_root
    from lemoncrow.pro.capabilities.review.data_flow import render_data_flow_text

    where = (repo_path or Path.cwd()).expanduser()
    top = _git(where, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise click.ClickException(f"{where} is not inside a Git repository")
    repo = Path(top.stdout.decode("utf-8", errors="replace").strip())
    if base_ref is not None and read_trusted_base(repo, base_ref)[0] is None:
        raise click.ClickException(f"--base {base_ref!r} does not resolve to a readable commit in {repo}")
    obj = ctx.find_root().obj or {}
    projection = local_data_flow(
        repo,
        base_ref=base_ref,
        store_root=Path(obj.get("root") or default_store_root()),
        store_origin=store_origin(ctx),
    )
    if as_json:
        click.echo(json.dumps(projection.to_wire(), indent=2, sort_keys=True))
    else:
        click.echo(render_data_flow_text(projection), nl=False)


__all__ = ["data_flow_cmd", "local_data_flow", "read_trusted_base", "store_origin"]

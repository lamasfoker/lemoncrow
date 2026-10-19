from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import click

from lemoncrow.core.capabilities.providers.config import LITELLM_PREFIX
from lemoncrow.gateway.cli.commands._shared import _emit


def _resolve_litellm_model(provider: str, model: str) -> str:
    """Auto-prefix *model* with the litellm provider prefix when needed.

    Examples::

        _resolve_litellm_model("openai", "gpt-4o")          # "openai/gpt-4o"
        _resolve_litellm_model("anthropic", "claude-opus-4-8")  # "anthropic/claude-opus-4-8"
        _resolve_litellm_model("openai", "openai/gpt-4o")   # "openai/gpt-4o" (already prefixed)
        _resolve_litellm_model("", "openai/gpt-4o")         # "openai/gpt-4o" (already prefixed)
    """
    if not model:
        return model
    if "/" in model:
        return model  # already provider-prefixed
    prefix = LITELLM_PREFIX.get(provider.lower(), "")
    return f"{prefix}{model}" if prefix else model


def _root_from_obj(obj: dict[str, Any]) -> Path:
    if isinstance(obj, dict):
        root = obj.get("root")
        if isinstance(root, Path):
            return root
    return Path.home() / ".lemoncrow"


@click.group("run")
def run_group() -> None:
    """Explain past LemonCrow runs from recorded evidence."""


@run_group.command("explain")
@click.argument("run_id", required=False, default=None)
@click.option("--limit", default=20, show_default=True, type=int, help="Max signals to show per category.")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of text.")
@click.pass_context
def run_explain_cmd(ctx: click.Context, run_id: str | None, limit: int, as_json: bool) -> None:
    """Explain what happened in a run, pointing at evidence rather than guessing.

    RUN_ID is a session id or a unique prefix; omit it for the latest run.
    Every line cites the record it came from, and any evidence source that was
    missing is named rather than silently treated as "nothing happened there".
    """
    from lemoncrow.gateway.cli.commands._shared import _latest_ledger_path
    from lemoncrow.pro.capabilities.run_explain.attribution import explain_run_attribution, render_attribution

    root = _root_from_obj(ctx.obj or {})

    target = (run_id or "").strip()
    if not target:
        latest = _latest_ledger_path(root)
        if latest is None:
            raise click.ClickException(f"no run ledger found under {root}. Pass a run id or record a session first.")
        target = latest.parent.name

    try:
        report = explain_run_attribution(root, target, limit_per_category=limit)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        _emit(report.to_dict(), as_json=True)
        return

    _emit(render_attribution(report), as_json=False)


if os.environ.get("LEMONCROW_INTERNAL_PRIVATE_RUNTIME", "").strip() == "1":
    # Private monorepo/development only. Public mirrors and public release wheels
    # deliberately exclude this module. Importing it here attaches the hidden
    # owned-execution subcommands to the otherwise inspection-only run group.
    try:
        from . import _private_run as _private_run
    except ModuleNotFoundError:
        pass


__all__ = ["run_explain_cmd", "run_group"]

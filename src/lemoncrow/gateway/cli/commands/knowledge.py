"""``lc knowledge`` — Project Knowledge first, then the legacy review overlay."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import click

from lemoncrow.gateway.cli.commands._shared import _emit
from lemoncrow.gateway.cli.commands.project_knowledge import register_project_knowledge_commands


@click.group("knowledge")
def knowledge_group() -> None:
    """Project Knowledge (repository history) and the legacy review overlay."""


def _project_knowledge(root: Path) -> dict[str, Any]:
    """The stored repositories under *root*; never creates the store."""
    from lemoncrow.pro.capabilities.review.project_knowledge import ProjectKnowledgeStore
    from lemoncrow.pro.capabilities.review.project_knowledge_surface import (
        SYNC_COMMAND,
        LocalKnowledgeSources,
        knowledge_listing,
    )
    from lemoncrow.pro.capabilities.review.store import DB_NAME, ReviewStore

    if not (root / DB_NAME).is_file():
        return {"repositories": [], "truncated": False, "sync_command": SYNC_COMMAND}
    listing = knowledge_listing(ProjectKnowledgeStore(ReviewStore(root)), LocalKnowledgeSources(root))
    return {key: listing[key] for key in ("repositories", "truncated", "sync_command")}


@knowledge_group.command("show")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def knowledge_show_cmd(ctx: click.Context, as_json: bool) -> None:
    """Show Project Knowledge, then the legacy review overlays (team and personal)."""
    from lemoncrow.pro.capabilities.live_reviewer.knowledge import load_overlay, load_repo_overlay
    from lemoncrow.pro.capabilities.review.project_knowledge_surface import LEGACY_OVERLAY_NOTE

    repo_root = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    team = load_repo_overlay(repo_root)
    personal = load_overlay(ctx.obj["root"])
    knowledge = _project_knowledge(Path(ctx.obj["root"]))
    if as_json:
        _emit(
            {
                "project_knowledge": knowledge,
                "legacy_overlay_note": LEGACY_OVERLAY_NOTE,
                "team": team,
                "personal": personal,
            },
            as_json=True,
        )
        return
    click.echo("Project Knowledge (repository history; advisory context, never Review policy)")
    if not knowledge["repositories"]:
        click.echo(f"  nothing synced yet: {knowledge['sync_command']}")
    for row in knowledge["repositories"]:
        source = row.get("source") or {}
        name = source.get("repository_key") or row["repository_id"][:23]
        counts = row["entries"]
        click.echo(
            f"  {name} {row['canonical_ref']}: {counts['deterministic'] + counts['source_document']} source-backed "
            f"entries, {counts['ai_narrated']} AI narratives (updated {row['updated_at']})"
        )
    click.echo("")
    click.echo(f"Legacy review overlay. {LEGACY_OVERLAY_NOTE}")
    for label, overlay in (("team (repo, shared)", team), ("personal (you)", personal)):
        click.echo(f"[legacy overlay: {label}]")
        for key in ("notes", "boost", "suppress"):
            values = overlay.get(key) or []
            click.echo(f"  {key} ({len(values)}):")
            for value in values:
                click.echo(f"    - {value}")


@knowledge_group.command("overlay-patch")
@click.option("--include-personal", is_flag=True, help="Also convert your personal overlay into the shared document.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def knowledge_overlay_patch_cmd(ctx: click.Context, include_personal: bool, as_json: bool) -> None:
    """Convert the legacy review overlay into a reviewable document (a patch; nothing is written).

    The document is advisory context, never a Review rule; review.json itself is left alone.
    """
    from lemoncrow.pro.capabilities.live_reviewer.knowledge import load_overlay, load_repo_overlay
    from lemoncrow.pro.capabilities.review.project_knowledge_surface import legacy_overlay_patch

    repo_root = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    proposal = legacy_overlay_patch(
        load_repo_overlay(repo_root), load_overlay(ctx.obj["root"]) if include_personal else None
    )
    if proposal is None:
        if as_json:
            _emit({"patch": None}, as_json=True)
        else:
            click.echo("No legacy review overlay to convert.", err=True)
        return
    if as_json:
        _emit(proposal, as_json=True)
    else:
        click.echo(proposal["patch"], nl=False)


register_project_knowledge_commands(knowledge_group)

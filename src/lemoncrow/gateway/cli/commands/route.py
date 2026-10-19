"""Inspect recorded cross-vendor routing status."""

from __future__ import annotations

import click

from lemoncrow.gateway.cli.commands._shared import _emit


@click.group("route", hidden=True)
def route_public_group() -> None:
    """Inspect cross-vendor routing status."""


@route_public_group.command("status")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def route_status_cmd(ctx: click.Context, as_json: bool) -> None:
    from lemoncrow.pro.capabilities.cross_vendor_routing.advisor import CrossVendorRouteAdvisor
    from lemoncrow.pro.capabilities.cross_vendor_routing.configuration import RouteConfigError

    try:
        payload = CrossVendorRouteAdvisor(ctx.obj["root"]).status()
    except RouteConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo("Enabled vendors: " + ", ".join(payload["enabled_vendors"]))
    click.echo(f"Recommendations logged: {payload['recommendation_count']}")
    click.echo(f"Estimated savings: ${payload['estimated_savings_usd']:.6f}")
    click.echo(f"Active lessons: {payload['active_lesson_count']}")
    click.echo(f"Lesson-driven recommendations: {payload['lesson_application_count']}")
    click.echo(f"Cost-cap triggers: {payload['cost_cap_trigger_count']}")


__all__ = ["route_public_group"]

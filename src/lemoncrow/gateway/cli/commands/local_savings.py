"""Productized Verified Local Work controls under the existing savings surface."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import click

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.verified_work.consent import RepositoryIdentity
    from lemoncrow.pro.capabilities.verified_work.product import SavingsProductStatus


def _root(ctx: click.Context) -> Path:
    obj = ctx.find_root().obj if ctx.find_root() is not None else None
    if isinstance(obj, dict) and isinstance(obj.get("root"), Path):
        return obj["root"]
    return Path.home() / ".lemoncrow"


def _persist(root: Path, key: str, value: object) -> None:
    from lemoncrow.core.settings import find_by_key, write_setting

    write_setting(root, key, value)
    # The current CLI process imported settings before this command ran. Keep
    # product-status calculations in this same invocation consistent with the
    # persisted value; future processes read plugin_settings.json normally.
    spec = find_by_key(key)
    if spec is not None and spec.env_var:
        os.environ[spec.env_var] = "true" if value is True else "false" if value is False else str(value)


def _settings():  # type: ignore[no-untyped-def]
    from lemoncrow.core.capabilities.verified_work.settings import load_verified_work_settings

    return load_verified_work_settings()


def _repository(path: Path | None) -> RepositoryIdentity | None:
    """The git repository Auto consent applies to (default: current directory)."""
    from lemoncrow.pro.capabilities.verified_work.consent import repository_identity

    return repository_identity(path or Path.cwd())


_REPO_OPTION = click.option(
    "--repo",
    "repo_path",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Repository to act on (default: the current directory).",
)


def _status_payload(root: Path, repo: RepositoryIdentity | None = None) -> dict[str, object]:
    from lemoncrow.pro.capabilities.verified_work.calibration import summarize_calibration
    from lemoncrow.pro.capabilities.verified_work.consent import HOST_SUPPORT
    from lemoncrow.pro.capabilities.verified_work.jobs import queue_counts
    from lemoncrow.pro.capabilities.verified_work.outcomes import summarize_outcomes
    from lemoncrow.pro.capabilities.verified_work.product import product_status
    from lemoncrow.pro.capabilities.verified_work.repairs import (
        MAX_AGE_S,
        MAX_RECORD_BYTES,
        MAX_RECORDS,
        list_repairs,
    )

    settings = _settings()
    status = product_status(root, settings, repo=repo)
    repairs = list_repairs(root, repo_key=repo.key, limit=5) if repo is not None else []
    return {
        "status": asdict(status),
        "outcomes": summarize_outcomes(root=root),
        "calibration": summarize_calibration(root=root, min_pairs=settings.calibration_min_pairs),
        "jobs": queue_counts(root),
        "hosts": dict(HOST_SUPPORT),
        "undo_bounds": {
            "max_records": MAX_RECORDS,
            "max_age_days": MAX_AGE_S // 86_400,
            "max_record_bytes": MAX_RECORD_BYTES,
        },
        "repairs": [
            {
                "id": record.id,
                "status": record.status,
                "created_at": record.created_at,
                "kind": record.kind,
                "model": record.model,
                "files": [row.path for row in record.files],
            }
            for record in repairs
        ],
    }


_HOST_LABELS = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}


def _print_repository(status: dict[str, Any], payload: dict[str, object]) -> None:
    repo = status.get("repository")
    if isinstance(repo, dict):
        click.echo(f"  Repository:         {repo.get('root')}")
        grant = repo.get("grant")
        if isinstance(grant, dict):
            granted = [list(row) for row in grant.get("pairs") or [] if len(row) == 2]
            pairs = ", ".join(f"{row[1]} via {row[0]}" for row in granted)
            click.echo(f"  Auto opt-in:        by {grant.get('granted_by')} at {grant.get('granted_at')} ({pairs})")
            click.echo("                      revoke with `lc rt savings auto --revoke`")
            # Pairs that graduated after the opt-in stay Learning until opted in.
            pending = [list(row) for row in repo.get("grantable") or [] if list(row) not in granted]
            if pending:
                waiting = ", ".join(f"{row[1]} via {row[0]}" for row in pending if len(row) == 2)
                click.echo(f"  Ready, not opted in: {waiting}; rerun `lc rt savings auto` to add")
        elif status.get("mode") == "enforce" or repo.get("grantable"):
            click.echo("  Auto opt-in:        not granted here; run `lc rt savings auto` in this repository to opt in")
        else:
            click.echo("  Auto opt-in:        none; Learning until this repository's own window qualifies")
    hosts = payload.get("hosts")
    if isinstance(hosts, dict) and hosts:
        supported = [_HOST_LABELS.get(str(name), str(name)) for name, value in hosts.items() if value == "supported"]
        unsupported = [_HOST_LABELS.get(str(name), str(name)) for name, value in hosts.items() if value != "supported"]
        line = ", ".join(supported) + " supported" if supported else "none supported"
        if unsupported:
            line += f" · {', '.join(unsupported)} unsupported (Learning only)"
        click.echo(f"  Auto hosts:         {line}")
    repairs = payload.get("repairs")
    if isinstance(repairs, list) and repairs:
        click.echo("  Recent repairs:")
        for row in repairs:
            if isinstance(row, dict):
                files = ", ".join(str(path) for path in row.get("files") or [])
                click.echo(f"    {row.get('id')}  {row.get('status')!s:<8} {row.get('kind')!s:<10} {files}")
    bounds = payload.get("undo_bounds")
    if isinstance(bounds, dict):
        click.echo(
            f"  Undo:               `lc rt savings undo <id>` or `--last`; newest {bounds.get('max_records')} "
            f"repairs kept for {bounds.get('max_age_days')} days"
        )


def _print_status(payload: dict[str, object]) -> None:
    status = cast(dict[str, Any], payload["status"])
    outcomes = cast(dict[str, Any], payload["outcomes"])
    jobs = cast(dict[str, Any], payload.get("jobs") or {})
    calibration = cast(dict[str, Any], payload.get("calibration") or {})
    raw_totals = outcomes.get("totals")
    totals: dict[str, Any] = raw_totals if isinstance(raw_totals, dict) else {}
    state = str(status.get("state") or "unknown").upper()
    click.echo(f"Local Savings: {state}")
    model = str(status.get("model") or "")
    click.echo(f"  Model:              {model or 'none'}")
    raw_isolation = status.get("isolation")
    isolation: dict[str, Any] = raw_isolation if isinstance(raw_isolation, dict) else {}
    click.echo(f"  Isolation:          {'qualified' if isolation.get('qualified') else 'not qualified'}")
    if isolation.get("detail"):
        click.echo(f"                      {isolation['detail']}")
    raw_kinds = status.get("kinds")
    kinds: list[Any] = raw_kinds if isinstance(raw_kinds, list) else []
    if kinds:
        click.echo("  Qualification:")
        for row in kinds:
            if not isinstance(row, dict):
                continue
            mark = "ready" if row.get("qualified") and row.get("economics_qualified") else "learning"
            click.echo(
                f"    {row.get('kind')!s:<10} {mark:<8} "
                f"{int(row.get('attempts') or 0)} samples · {float(row.get('verification_rate') or 0) * 100:.1f}% verified · "
                f"{float(row.get('average_latency_ms') or 0) / 1000:.1f}s avg"
            )
    _print_repository(status, payload)
    click.echo("  Observed local work:")
    click.echo(f"    attempts          {int(totals.get('attempts') or 0)}")
    click.echo(f"    verified          {int(totals.get('verified') or 0)}")
    click.echo(f"    promoted          {int(totals.get('promoted') or 0)}")
    click.echo(f"    local model cost  ${float(totals.get('provider_cost_usd') or 0):.4f}")
    if any(int(jobs.get(key) or 0) for key in ("pending", "running")):
        click.echo(
            f"  Shadow queue:       {int(jobs.get('pending') or 0)} pending · {int(jobs.get('running') or 0)} running"
        )
    calibration_status = str(calibration.get("status") or "calibrating")
    pairs = int(calibration.get("pairs") or 0)
    min_pairs = int(calibration.get("min_pairs") or 0)
    calibrated = calibration.get("calibrated_net_savings_usd")
    if calibration_status == "qualified" and calibrated is not None:
        click.echo(f"  Calibrated net saved:${float(calibrated):>10.2f}")
        click.echo(f"  Paired baselines:   {pairs:>6} clean Learning pairs")
        click.echo("  Savings evidence:   paired measured host usage - local model cost")
    else:
        click.echo(f"  Net local savings:  calibrating ({pairs}/{min_pairs} clean paired baselines)")
        click.echo("  Savings evidence:   no counterfactual claimed until paired baseline qualifies")


def _grant_repository(root: Path, repo: RepositoryIdentity, status: SavingsProductStatus) -> dict[str, object]:
    """Record the explicit, repository-scoped Auto opt-in and switch Auto on."""
    from lemoncrow.pro.capabilities.verified_work.consent import grant_auto

    pairs = status.repository.grantable if status.repository is not None else ()
    grant = grant_auto(repo, pairs, via="cli", root=root)
    _persist(root, "verified_work.mode", "enforce")
    return asdict(grant)


@click.command("enable")
@click.option(
    "--auto",
    "enable_auto",
    is_flag=True,
    help="Also opt this repository in to Auto if it is already Ready (same as `lc rt savings auto`).",
)
@click.option("--model", default="auto", help="Specific custom/<endpoint>/<model>, or auto-discover local runtimes.")
@_REPO_OPTION
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
@click.pass_context
def local_savings_enable(
    ctx: click.Context, enable_auto: bool, model: str, repo_path: Path | None, as_json: bool
) -> None:
    """Discover a local model and start safe local-savings qualification."""
    from lemoncrow.pro.capabilities.verified_work.product import ensure_local_worker, product_status

    root = _root(ctx)
    chosen, reason = ensure_local_worker(root, requested=model)
    if chosen is None:
        raise click.ClickException(
            "No reachable local OpenAI-compatible model found. Start Ollama, LM Studio, llama.cpp, or vLLM, then rerun `lc rt savings enable`."
        )
    configured_model = chosen if model != "auto" else "auto"
    _persist(root, "verified_work.model", configured_model)
    # A named model selects the worker but does not bypass qualification. Auto
    # deliberately stays `auto` after proving at least one local worker exists,
    # so Learning can explore alternatives instead of pinning the first model.
    # The advanced `model_policy=explicit` setting remains the manual escape hatch.
    _persist(root, "verified_work.model_policy", "local_only")
    _persist(root, "verified_work.mode", "shadow")
    _persist(root, "verified_work.require_isolation", True)

    repo = _repository(repo_path)
    status = product_status(root, _settings(), repo=repo)
    grant: dict[str, object] | None = None
    if enable_auto and repo is not None and status.repository is not None and status.repository.grantable:
        grant = _grant_repository(root, repo, status)
        status = product_status(root, _settings(), repo=repo)

    payload = {"selected_model": chosen, "selection_reason": reason, "grant": grant, **_status_payload(root, repo)}
    if as_json:
        click.echo(json.dumps(payload, sort_keys=True))
        return
    if model == "auto":
        click.echo(f"Local model pool: auto (reachable: {chosen})")
    else:
        click.echo(f"Local model: {chosen}")
    if enable_auto and grant is None:
        click.echo(
            "Auto is not qualified in this repository yet; staying in Learning mode until real verifier "
            "evidence is sufficient. Then run `lc rt savings auto` here."
        )
    else:
        click.echo(
            "Local Savings enabled in Learning mode."
            if status.state != "auto"
            else "Local Savings enabled in Auto mode for this repository."
        )
    _print_status(payload)


@click.command("status")
@_REPO_OPTION
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
@click.pass_context
def local_savings_status(ctx: click.Context, repo_path: Path | None, as_json: bool) -> None:
    """Show Off/Learning/Ready/Auto state, this repository's opt-in and recent repairs."""
    payload = _status_payload(_root(ctx), _repository(repo_path))
    if as_json:
        click.echo(json.dumps(payload, sort_keys=True))
    else:
        _print_status(payload)


@click.command("doctor")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
@click.pass_context
def local_savings_doctor(ctx: click.Context, as_json: bool) -> None:
    """Discover local runtimes and check model/sandbox prerequisites."""
    from lemoncrow.pro.capabilities.verified_work.product import diagnose_local_worker, probe_isolation

    root = _root(ctx)
    diagnosis = diagnose_local_worker(root)
    model = diagnosis.model
    model_reason = diagnosis.model_reason
    isolation = probe_isolation()
    payload = {
        "detected_endpoints": list(diagnosis.detected_endpoints),
        "model": model,
        "model_reason": model_reason,
        "isolation": asdict(isolation),
        "ready_to_learn": bool(model),
        "ready_to_auto": bool(model) and isolation.qualified,
    }
    if as_json:
        click.echo(json.dumps(payload, sort_keys=True))
        return
    click.echo("Local Savings doctor")
    click.echo(f"  Local model:   {model or 'not found'}")
    click.echo(f"  Isolation:     {'qualified' if isolation.qualified else 'not qualified'} — {isolation.detail}")
    if diagnosis.detected_endpoints:
        click.echo(f"  Detected:      {', '.join(diagnosis.detected_endpoints)}")
    if not model and diagnosis.detected_endpoints:
        click.echo(f"  Model probe:   {model_reason}")
    click.echo("  Learning:      ready" if model else "  Learning:      blocked")
    click.echo(
        "  Auto:          ready"
        if model and isolation.qualified
        else "  Auto:          blocked until prerequisites pass"
    )


@click.command("auto")
@_REPO_OPTION
@click.option("--revoke", is_flag=True, help="Withdraw this repository's Auto opt-in.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
@click.pass_context
def local_savings_auto(ctx: click.Context, repo_path: Path | None, revoke: bool, as_json: bool) -> None:
    """Opt this repository in to Auto for its Ready (model, kind) pairs.

    The opt-in is recorded with who and when, covers only the pairs Ready now
    (rerun to add pairs that graduate later), and is revoked with --revoke,
    `lc rt savings learn` or `lc rt savings disable`.
    """
    from lemoncrow.pro.capabilities.verified_work.consent import revoke_auto
    from lemoncrow.pro.capabilities.verified_work.product import product_status

    root = _root(ctx)
    repo = _repository(repo_path)
    if repo is None:
        raise click.ClickException(
            "Auto is opted in per git repository: run this inside the repository or pass --repo PATH."
        )
    if revoke:
        revoked = revoke_auto(repo.key, via="cli", root=root)
        if as_json:
            click.echo(json.dumps({"repository": repo.root, "revoked": revoked}, sort_keys=True))
        elif revoked:
            click.echo(f"Auto opt-in revoked for {repo.root}; it is Learning only again.")
        else:
            click.echo(f"No Auto opt-in to revoke for {repo.root}.")
        return
    status = product_status(root, _settings(), repo=repo)
    if status.repository is None or not status.repository.grantable:
        raise click.ClickException(
            "Local Savings is not Ready yet in this repository. Run `lc rt savings status` to see missing "
            "evidence/prerequisites; Auto needs this repository's own verifier window."
        )
    grant = _grant_repository(root, repo, status)
    payload = {"grant": grant, **_status_payload(root, repo)}
    if as_json:
        click.echo(json.dumps(payload, sort_keys=True))
        return
    pairs = ", ".join(f"{kind} via {model}" for model, kind in status.repository.grantable)
    click.echo(f"Local Savings Auto enabled for {repo.root} ({pairs}).")
    click.echo(
        f"Opt-in recorded for {grant.get('granted_by')} at {grant.get('granted_at')}. Every repair prints "
        "its undo command; revoke with `lc rt savings auto --revoke`."
    )
    _print_status(payload)


@click.command("undo")
@click.argument("repair_id", required=False)
@click.option("--last", "last", is_flag=True, help="Undo the newest repair in this repository.")
@_REPO_OPTION
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
@click.pass_context
def local_savings_undo(
    ctx: click.Context, repair_id: str | None, last: bool, repo_path: Path | None, as_json: bool
) -> None:
    """Restore the exact pre-repair bytes of one automatic repair.

    Refuses, naming the files, when any of them changed after the repair.
    """
    from lemoncrow.pro.capabilities.verified_work.repairs import undo_repair

    if bool(repair_id) == last:
        raise click.UsageError("Pass exactly one of REPAIR_ID or --last.")
    repo_key: str | None = None
    if last:
        repo = _repository(repo_path)
        if repo is None:
            raise click.ClickException("--last is per repository: run it inside the repository or pass --repo PATH.")
        repo_key = repo.key
    result = undo_repair(repair_id, repo_key=repo_key, root=_root(ctx))
    if as_json:
        click.echo(json.dumps(asdict(result), sort_keys=True))
        if not result.ok:
            ctx.exit(1)
        return
    if result.ok:
        if result.reason == "already_restored":
            click.echo(f"Repair {result.record_id} was already reverted; marked as undone.")
        else:
            click.echo(f"Undid repair {result.record_id}: restored {', '.join(result.restored_paths)}")
        return
    label = result.record_id or repair_id or "the last repair"
    if result.reason == "drift":
        raise click.ClickException(
            f"Refusing to undo repair {label}: these files changed after the repair: "
            f"{', '.join(result.conflicts)}. Nothing was restored."
        )
    if result.reason == "restore_partial":
        raise click.ClickException(
            f"Undo of repair {label} stopped part-way: a file could not be written and "
            f"{', '.join(result.restored_paths)} could not be put back, so those files now hold their "
            "pre-repair bytes while the others still hold the repair. Check them by hand."
        )
    messages = {
        "not_found": "no such repair record (records are kept for the newest repairs within the retention window)",
        "already_undone": "it was already undone",
        "workspace_missing": "its checkout no longer exists",
        "path_escape": "its record names a path outside the checkout",
        "record_corrupt": "its saved pre-repair bytes are damaged",
        "restore_failed": "a file could not be written and every file was put back as it was",
        "path_locked": "another LemonCrow process is promoting or undoing these files; try again in a moment",
        "path_lock_failed": "the file locks under the LemonCrow store could not be opened",
    }
    raise click.ClickException(
        f"Cannot undo {label}: {messages.get(result.reason, result.reason)}. Nothing was restored."
    )


@click.command("learn")
@click.pass_context
def local_savings_learn(ctx: click.Context) -> None:
    """Return to non-mutating Learning mode and revoke every Auto opt-in."""
    from lemoncrow.pro.capabilities.verified_work.consent import revoke_all_auto

    root = _root(ctx)
    _persist(root, "verified_work.mode", "shadow")
    revoked = revoke_all_auto(via="cli", root=root)
    suffix = f" Revoked Auto in {revoked} repositor{'y' if revoked == 1 else 'ies'}." if revoked else ""
    click.echo("Local Savings is in Learning mode; candidates are verified but never promoted." + suffix)


@click.command("disable")
@click.pass_context
def local_savings_disable(ctx: click.Context) -> None:
    """Disable local repair work immediately and revoke every Auto opt-in."""
    from lemoncrow.pro.capabilities.verified_work.consent import revoke_all_auto

    root = _root(ctx)
    _persist(root, "verified_work.mode", "off")
    revoke_all_auto(via="cli", root=root)
    click.echo("Local Savings disabled.")


__all__ = [
    "local_savings_auto",
    "local_savings_disable",
    "local_savings_doctor",
    "local_savings_enable",
    "local_savings_learn",
    "local_savings_status",
    "local_savings_undo",
]

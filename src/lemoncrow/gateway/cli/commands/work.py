"""Verified Local Work CLI and host-adapter bridge."""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import click

from lemoncrow.core.capabilities.verified_work import (
    build_opportunity,
    evaluate_eligibility,
    load_verified_work_settings,
)


@click.group("work")
def work_group() -> None:
    """Run mechanically verifiable work with bounded local models."""


def _root(ctx: click.Context) -> Path:
    obj = ctx.find_root().obj if ctx.find_root() is not None else None
    if isinstance(obj, dict) and isinstance(obj.get("root"), Path):
        return obj["root"]
    return Path.home() / ".lemoncrow"


def _run_initial_verifier(command: str, cwd: Path, timeout_s: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["bash", "-c", command],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": 124,
            "stdout": exc.stdout if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr if isinstance(exc.stderr, str) else "") + "\nverifier timed out",
        }


def process_after_bash_payload(
    payload: dict[str, Any],
    *,
    root: Path,
    learning_only: bool = False,
    synchronous_learning: bool = False,
) -> dict[str, Any]:
    """Host-neutral bridge from an already-completed Bash result into Verified Work.

    ``learning_only`` (durable background jobs) never promotes, whatever the
    mode: a repair the host never sees must not touch the live checkout.
    In enforce mode Auto applies only where ``auto_gate`` allows it; anywhere
    else the failure is learned from without mutating -- queued in the
    background by default, or run in-process with ``synchronous_learning``.
    """
    settings = load_verified_work_settings()
    if learning_only and settings.mode == "enforce":
        settings = replace(settings, mode="shadow")
    mode = settings.mode
    base: dict[str, Any] = {"schema_version": 1, "mode": mode, "handled": False, "promoted": False}
    if mode == "off":
        return {**base, "reason": "mode_off"}

    command = str(payload.get("command") or "").strip()
    workspace = Path(str(payload.get("workspace") or payload.get("cwd") or ".")).expanduser().resolve()
    cwd = Path(str(payload.get("cwd") or workspace)).expanduser().resolve()
    session_id = str(payload.get("session_id") or f"work-{uuid.uuid4().hex}")
    try:
        exit_code = int(payload.get("exit_code") or 1)
    except (TypeError, ValueError):
        exit_code = 1
    if not command or exit_code == 0 or not workspace.is_dir() or not cwd.is_dir():
        return {**base, "reason": "not_failed_verifier"}

    opportunity = build_opportunity(
        session_id=session_id,
        workspace=workspace,
        command=command,
        cwd=cwd,
        exit_code=exit_code,
        stdout=str(payload.get("stdout") or ""),
        stderr=str(payload.get("stderr") or ""),
    )
    if opportunity is None:
        return {**base, "reason": "not_eligible_verifier_shape"}
    decision = evaluate_eligibility(opportunity, settings)
    if not decision.eligible:
        return {**base, "reason": decision.reason_codes[0], "kind": opportunity.kind}

    from lemoncrow.infra.runtime.run_ledger import RunLedger
    from lemoncrow.pro.capabilities.verified_work.consent import auto_gate, consent_notice_for
    from lemoncrow.pro.capabilities.verified_work.repairs import promotion_notice
    from lemoncrow.pro.capabilities.verified_work.runtime import (
        run_enforced_repair_bounded,
        run_shadow_repair_bounded,
    )

    host = str(payload.get("host") or "")
    allowed_models: tuple[str, ...] | None = None
    if mode == "enforce":
        gate = auto_gate(settings, workspace=workspace, kind=opportunity.kind, host=host, root=root)
        if not gate.allowed:
            refused: dict[str, Any] = {**base, "reason": gate.reason, "kind": opportunity.kind, "auto": False}
            notice = consent_notice_for(gate, root=root)
            if notice:
                refused["notice"] = notice
            settings = replace(settings, mode="shadow")
            if not synchronous_learning:
                # Learn without holding the host's tool call open.
                from lemoncrow.pro.capabilities.verified_work.jobs import enqueue_and_spawn

                job_id, _ = enqueue_and_spawn(root, payload)
                return {**refused, "queued": True, "job_id": job_id}
            mode = "shadow"
            base = {**refused, "mode": mode}
        else:
            allowed_models = gate.models

    ledger = RunLedger(
        session_id=session_id,
        agent=str(payload.get("host") or "runtime"),
        root=root,
        workspace_path=workspace,
        task="verified local work",
        domain="coding",
    )
    try:
        if mode == "shadow":
            repair = run_shadow_repair_bounded(opportunity, decision, settings=settings, sink=ledger)
        else:
            repair = run_enforced_repair_bounded(
                opportunity,
                decision,
                settings=settings,
                sink=ledger,
                host=host,
                allowed_models=allowed_models,
            )
    finally:
        ledger.persist(root)

    result = asdict(repair)
    return {
        **base,
        "handled": repair.attempted,
        "promoted": repair.promoted,
        "verified": repair.verified,
        "reason": base.get("reason") or repair.reason,
        "kind": opportunity.kind,
        "repair": result,
        "message": (
            promotion_notice(repair.changed_paths, attempts=repair.attempts, record_id=repair.repair_record_id)
            if repair.promoted
            else ""
        ),
    }


@work_group.command("record-baseline", hidden=True)
@click.option("--input", "input_path", type=click.Path(path_type=Path, exists=True, dir_okay=False), required=True)
@click.pass_context
def record_baseline_cmd(ctx: click.Context, input_path: Path) -> None:
    """Persist one Claude Learning-mode host repair interval."""
    from lemoncrow.pro.capabilities.verified_work.calibration import record_host_baseline

    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        result = record_host_baseline(payload, root=_root(ctx))
        click.echo(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        input_path.unlink(missing_ok=True)


@work_group.command("enqueue-bash", hidden=True)
@click.option("--input", "input_path", type=click.Path(path_type=Path, exists=True, dir_okay=False), required=True)
@click.pass_context
def enqueue_bash_cmd(ctx: click.Context, input_path: Path) -> None:
    """Persist a failed Bash payload and launch a short-lived shadow worker."""
    from lemoncrow.pro.capabilities.verified_work.jobs import enqueue_and_spawn

    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        job_id, spawned = enqueue_and_spawn(_root(ctx), payload)
        click.echo(json.dumps({"queued": True, "job_id": job_id, "worker_spawned": spawned}, sort_keys=True))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        input_path.unlink(missing_ok=True)


@work_group.command("drain", hidden=True)
@click.option("--one", is_flag=True, help="Process at most one queued shadow job.")
@click.pass_context
def drain_cmd(ctx: click.Context, one: bool) -> None:
    """Process durable shadow work queued by host integrations."""
    from lemoncrow.pro.capabilities.verified_work.jobs import claim_next, fail_job, finish_job

    root = _root(ctx)
    processed = 0
    while True:
        job = claim_next(root)
        if job is None:
            break
        try:
            # Background jobs are Learning evidence only; they never promote.
            result = process_after_bash_payload(job.payload, root=root, learning_only=True)
            finish_job(root, job, result)
        except Exception as exc:
            fail_job(root, job, f"worker_error:{type(exc).__name__}")
        processed += 1
        if one:
            break
    click.echo(json.dumps({"processed": processed}, sort_keys=True))


@work_group.command("after-bash", hidden=True)
@click.option("--input", "input_path", type=click.Path(path_type=Path, exists=True, dir_okay=False), required=True)
@click.pass_context
def after_bash_cmd(ctx: click.Context, input_path: Path) -> None:
    """Internal bridge used by host integrations after a failed Bash call."""
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        result = process_after_bash_payload(payload, root=_root(ctx))
        click.echo(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        input_path.unlink(missing_ok=True)


@work_group.command("report")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
@click.pass_context
def report_cmd(ctx: click.Context, as_json: bool) -> None:
    """Show observed Verified Local Work outcomes; never invent counterfactual savings."""
    from lemoncrow.pro.capabilities.verified_work.outcomes import summarize_outcomes

    report = summarize_outcomes(root=_root(ctx))
    if as_json:
        click.echo(json.dumps(report, sort_keys=True))
        return
    from lemoncrow.gateway.cli import style

    totals = report["totals"]
    click.echo(style.title("Verified Local Work"))
    click.echo("")
    if not totals["attempts"]:
        click.echo(style.row("off", "No local repair attempts yet."))
        click.echo(style.next_step("Try one against a failing check:", 'lc rt work repair --verify "<test command>"'))
        return

    def row(label: str, value: object, detail: str = "") -> None:
        line = f"    {label + ':':<25}{value}"
        click.echo(f"{line}   {style.dim(detail)}" if detail else line)

    row("Attempts", totals["attempts"], f"{totals['shadow_attempts']} shadow · {totals['enforce_attempts']} enforce")
    row("Candidate verifier pass", totals["verified"], f"{totals['verification_rate'] * 100:.1f}%")
    row("Promoted repairs", totals["promoted"])
    row("Frontier loops contained", totals["promoted"])
    row("Local-model tokens", f"{totals['input_tokens']:,} in · {totals['output_tokens']:,} out")
    row("Reported model cost", f"${totals['provider_cost_usd']:.4f}")
    row("Avg repair latency", f"{totals['average_latency_ms'] / 1000:.2f}s")
    row("Estimated savings", style.dim("not claimed (paired baseline not qualified)"))


@work_group.command("repair")
@click.option("--verify", "command", required=True, help="Verifier command to run and, if eligible, repair against.")
@click.option(
    "--cwd", type=click.Path(path_type=Path, file_okay=False), default=Path.cwd, show_default="current directory"
)
@click.option(
    "--mode", type=click.Choice(["shadow", "enforce"]), default=None, help="Override verified_work.mode for this run."
)
@click.option("--model", default=None, help="Use one registered custom/<endpoint>/<model> worker for this run.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
@click.pass_context
def repair_cmd(
    ctx: click.Context,
    command: str,
    cwd: Path,
    mode: str | None,
    model: str | None,
    as_json: bool,
) -> None:
    """Run a verifier and attempt a bounded local repair if it fails."""
    from lemoncrow.core.capabilities.verified_work.settings import load_verified_work_settings

    cfg = load_verified_work_settings()
    if mode:
        cfg = replace(cfg, mode=mode)  # type: ignore[arg-type]
    if model:
        cfg = replace(cfg, model=model, model_policy="explicit")
    if cfg.mode == "off":
        raise click.ClickException("verified_work.mode is off; pass --mode shadow|enforce or enable it in settings")

    cwd = cwd.expanduser().resolve()
    initial = _run_initial_verifier(command, cwd, cfg.timeout_s)
    if int(initial["exit_code"]) == 0:
        payload = {
            "schema_version": 1,
            "mode": cfg.mode,
            "handled": False,
            "promoted": False,
            "reason": "already_passed",
        }
    else:
        # Explicit CLI overrides are scoped to this process only.
        import os

        old_mode = os.environ.get("LEMONCROW_VERIFIED_WORK_MODE")
        old_model = os.environ.get("LEMONCROW_VERIFIED_WORK_MODEL")
        old_policy = os.environ.get("LEMONCROW_VERIFIED_WORK_MODEL_POLICY")
        os.environ["LEMONCROW_VERIFIED_WORK_MODE"] = cfg.mode
        os.environ["LEMONCROW_VERIFIED_WORK_MODEL"] = cfg.model
        os.environ["LEMONCROW_VERIFIED_WORK_MODEL_POLICY"] = cfg.model_policy
        try:
            payload = process_after_bash_payload(
                {
                    "host": "cli",
                    "command": command,
                    "workspace": str(cwd),
                    "cwd": str(cwd),
                    "session_id": f"cli-{uuid.uuid4().hex}",
                    **initial,
                },
                root=_root(ctx),
                synchronous_learning=True,
            )
        finally:
            for key, previous in (
                ("LEMONCROW_VERIFIED_WORK_MODE", old_mode),
                ("LEMONCROW_VERIFIED_WORK_MODEL", old_model),
                ("LEMONCROW_VERIFIED_WORK_MODEL_POLICY", old_policy),
            ):
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous

    if as_json:
        click.echo(json.dumps(payload, sort_keys=True))
        return
    click.echo(f"Verified Local Work: {payload.get('reason', 'unknown')}")
    if payload.get("message"):
        click.echo(str(payload["message"]))
    if payload.get("notice"):
        click.echo(str(payload["notice"]))


__all__ = ["process_after_bash_payload", "work_group"]

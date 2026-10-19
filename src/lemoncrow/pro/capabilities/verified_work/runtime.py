"""Bounded shadow execution loop for Verified Local Work."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.verified_work.models import (
    EligibilityDecision,
    RepairAttemptResult,
    RepairProposal,
    VerifiedWorkOpportunity,
)
from lemoncrow.core.capabilities.verified_work.settings import VerifiedWorkSettings, load_verified_work_settings
from lemoncrow.core.foundation.runtime_decisions import RuntimeDecisionEvent, RuntimeDecisionSink

from .consent import RepositoryIdentity, auto_gate, repository_identity
from .model import WorkerModelChoice, propose_patch, resolve_worker_model
from .promote import promote_candidate
from .repairs import RepairJournal, RepairMeta
from .workspace import CandidateWorkspace, apply_proposal, run_exact_verifier

ProposalFn = Callable[..., tuple[RepairProposal, dict[str, Any]]]


def _repository(opportunity: VerifiedWorkOpportunity) -> RepositoryIdentity | None:
    try:
        return repository_identity(opportunity.workspace)
    except Exception:
        return None


def _event_mode(mode: str) -> str:
    return "shadow" if mode == "shadow" else "enforce" if mode == "enforce" else "observe"


def _persist_sink(sink: RuntimeDecisionSink | None) -> None:
    if sink is None:
        return
    persist = getattr(sink, "persist", None)
    if not callable(persist):
        return
    try:
        persist()
    except Exception:
        pass


def _record(
    sink: RuntimeDecisionSink | None,
    opportunity: VerifiedWorkOpportunity,
    decision: EligibilityDecision,
    settings: VerifiedWorkSettings,
    *,
    actual: dict[str, Any],
    reasons: tuple[str, ...],
    metrics: dict[str, Any] | None = None,
) -> None:
    if sink is None:
        return
    sink.record_runtime_decision(
        RuntimeDecisionEvent(
            kind="verified_work.repair",
            phase="verification",
            policy="verified_work.v1",
            mode=_event_mode(settings.mode),  # type: ignore[arg-type]
            session_id=opportunity.session_id,
            reason_codes=reasons,
            proposed={"kind": opportunity.kind, "eligible": decision.eligible},
            actual=actual,
            budget={
                "max_attempts": settings.max_attempts,
                "max_files": settings.max_files,
                "max_diff_lines": settings.max_diff_lines,
                "timeout_s": settings.timeout_s,
            },
            metrics=metrics or {},
        )
    )


def _reserve(session_id: str, settings: VerifiedWorkSettings):  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.verified_work.budget import reserve_work

    return reserve_work(session_id, settings)


def _release(reservation) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.verified_work.budget import release_work

    if reservation is not None:
        release_work(reservation)


def _feedback(stdout: str, stderr: str) -> str:
    return "\n".join(part for part in (stdout, stderr) if part)[-6000:]


def _persist_model_outcome(
    opportunity: VerifiedWorkOpportunity,
    choice: WorkerModelChoice,
    *,
    started: float,
    verified: bool,
    promoted: bool,
    input_tokens: int,
    output_tokens: int,
    provider_cost_usd: float | None,
    mode: str,
    repo: str | None = None,
) -> None:
    """Best-effort aggregate evidence; never changes repair semantics."""
    try:
        from lemoncrow.pro.capabilities.verified_work.outcomes import record_model_outcome

        record_model_outcome(
            choice.model,
            opportunity.kind,
            verified=verified,
            promoted=promoted,
            latency_ms=int(max(0.0, time.monotonic() - started) * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            provider_cost_usd=provider_cost_usd,
            mode=mode,
            repo=repo,
        )
        from lemoncrow.pro.capabilities.verified_work.calibration import record_local_observation

        record_local_observation(
            opportunity,
            model=choice.model,
            verified=verified,
            promoted=promoted,
            latency_ms=int(max(0.0, time.monotonic() - started) * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            provider_cost_usd=provider_cost_usd,
            mode=mode,
        )
    except Exception:
        # Outcome recording is optimization evidence, never an execution gate.
        pass


def run_repair(
    opportunity: VerifiedWorkOpportunity,
    decision: EligibilityDecision,
    settings: VerifiedWorkSettings,
    *,
    sink: RuntimeDecisionSink | None = None,
    proposal_fn: ProposalFn = propose_patch,
    model_choice: WorkerModelChoice | None = None,
    host: str = "",
) -> RepairAttemptResult:
    """Evaluate one isolated candidate and optionally promote it in enforce mode.

    Promotion additionally requires the repository-scoped Auto gate (host
    qualified, opt-in covering this model and kind, repository window
    qualified) and a written undo record; see ``consent`` and ``repairs``.
    """

    if settings.mode not in {"shadow", "enforce"}:
        return RepairAttemptResult(attempted=False, verified=False, reason="verified_work_disabled")
    if not decision.eligible:
        _record(sink, opportunity, decision, settings, actual={"attempted": False}, reasons=decision.reason_codes)
        return RepairAttemptResult(attempted=False, verified=False, reason=decision.reason_codes[0])

    choice = model_choice
    model_reason = "injected_model"
    if choice is None:
        choice, model_reason = resolve_worker_model(settings, kind=opportunity.kind)
    if choice is None:
        _record(
            sink,
            opportunity,
            decision,
            settings,
            actual={"attempted": False, "model": ""},
            reasons=(model_reason,),
        )
        return RepairAttemptResult(attempted=False, verified=False, reason=model_reason)

    total_input = 0
    total_output = 0
    provider_cost = 0.0
    provider_cost_known = False
    changed_paths: tuple[str, ...] = ()
    diff_lines = 0
    verifier_exit: int | None = None
    verifier_ms = 0
    network_isolated = False
    resource_limited = False
    feedback = ""
    work_started = time.monotonic()
    model_attempted = False
    repo = _repository(opportunity)
    repo_key = repo.key if repo is not None else None

    try:
        with CandidateWorkspace(opportunity) as candidate:
            assert candidate.root is not None and candidate.workspace is not None and candidate.cwd is not None
            for attempt in range(1, settings.max_attempts + 1):
                model_attempted = True
                proposal, usage = proposal_fn(
                    opportunity,
                    candidate_root=candidate.workspace,
                    allowed_paths=decision.allowed_paths,
                    choice=choice,
                    timeout_s=settings.timeout_s,
                    feedback=feedback,
                )
                total_input += int(usage.get("input_tokens") or 0)
                total_output += int(usage.get("output_tokens") or 0)
                raw_cost = usage.get("provider_cost_usd")
                if isinstance(raw_cost, int | float) and not isinstance(raw_cost, bool):
                    provider_cost += max(0.0, float(raw_cost))
                    provider_cost_known = True
                changed_paths, diff_lines = apply_proposal(
                    candidate.workspace,
                    proposal,
                    allowed_paths=decision.allowed_paths,
                    max_diff_lines=settings.max_diff_lines,
                )
                verifier = run_exact_verifier(
                    opportunity.command,
                    cwd=candidate.cwd,
                    root=candidate.root,
                    timeout_s=settings.timeout_s,
                    memory_mb=settings.memory_mb,
                )
                verifier_exit = verifier.exit_code
                verifier_ms = verifier.duration_ms
                network_isolated = verifier.network_isolated
                resource_limited = verifier.resource_limited
                if verifier_exit == 0:
                    promoted = False
                    record_id = ""
                    reason = "shadow_candidate_verified"
                    isolation_qualified = network_isolated and resource_limited
                    gate = (
                        auto_gate(
                            settings,
                            workspace=opportunity.workspace,
                            kind=opportunity.kind,
                            host=host,
                            model=choice.model,
                            repo=repo,
                        )
                        if settings.mode == "enforce"
                        else None
                    )
                    if settings.mode == "enforce" and settings.require_isolation and not isolation_qualified:
                        reason = "isolation_not_qualified"
                    elif gate is not None and (not gate.allowed or gate.repo is None):
                        # Ready/enforce is a request; only an explicit opt-in
                        # for this repository, model and kind is consent.
                        reason = gate.reason
                    elif gate is not None and gate.repo is not None:
                        live_cwd = Path(opportunity.cwd).resolve()
                        live_root = Path(opportunity.workspace).resolve()

                        def _post_verify(cwd: Path = live_cwd, root: Path = live_root) -> bool:
                            post = run_exact_verifier(
                                opportunity.command,
                                cwd=cwd,
                                root=root,
                                timeout_s=settings.timeout_s,
                                memory_mb=settings.memory_mb,
                            )
                            return post.exit_code == 0

                        journal = RepairJournal(
                            RepairMeta(
                                workspace=str(live_root),
                                repo_key=gate.repo.key,
                                repo_root=gate.repo.root,
                                session_id=opportunity.session_id,
                                host=host,
                                model=choice.model,
                                kind=opportunity.kind,
                                verifier_command=opportunity.command,
                            )
                        )
                        promotion = promote_candidate(
                            opportunity,
                            candidate_workspace=candidate.workspace,
                            changed_paths=changed_paths,
                            post_verify=_post_verify,
                            journal=journal,
                        )
                        promoted = promotion.promoted
                        reason = promotion.reason
                        record_id = promotion.record_id
                    result = RepairAttemptResult(
                        attempted=True,
                        verified=True,
                        reason=reason,
                        model=choice.model,
                        attempts=attempt,
                        input_tokens=total_input,
                        output_tokens=total_output,
                        provider_cost_usd=provider_cost if provider_cost_known else None,
                        changed_paths=changed_paths,
                        diff_lines=diff_lines,
                        verifier_exit_code=0,
                        verifier_duration_ms=verifier_ms,
                        network_isolated=network_isolated,
                        resource_limited=resource_limited,
                        promoted=promoted,
                        repair_record_id=record_id,
                    )
                    _record(
                        sink,
                        opportunity,
                        decision,
                        settings,
                        actual={
                            "attempted": True,
                            "verified": True,
                            "model": choice.model,
                            "promoted": promoted,
                        },
                        reasons=(reason,),
                        metrics={
                            "attempts": attempt,
                            "input_tokens": total_input,
                            "output_tokens": total_output,
                            "diff_lines": diff_lines,
                            "changed_files": len(changed_paths),
                            "verifier_duration_ms": verifier_ms,
                            "network_isolated": network_isolated,
                            "resource_limited": resource_limited,
                        },
                    )
                    _persist_model_outcome(
                        opportunity,
                        choice,
                        started=work_started,
                        verified=True,
                        promoted=promoted,
                        input_tokens=total_input,
                        output_tokens=total_output,
                        provider_cost_usd=provider_cost if provider_cost_known else None,
                        mode=settings.mode,
                        repo=repo_key,
                    )
                    return result
                feedback = _feedback(verifier.stdout, verifier.stderr)
    except Exception as exc:
        _record(
            sink,
            opportunity,
            decision,
            settings,
            actual={"attempted": True, "verified": False, "model": choice.model, "promoted": False},
            reasons=("shadow_worker_error", type(exc).__name__),
        )
        if model_attempted:
            _persist_model_outcome(
                opportunity,
                choice,
                started=work_started,
                verified=False,
                promoted=False,
                input_tokens=total_input,
                output_tokens=total_output,
                provider_cost_usd=provider_cost if provider_cost_known else None,
                mode=settings.mode,
                repo=repo_key,
            )
        return RepairAttemptResult(
            attempted=True,
            verified=False,
            reason=f"shadow_worker_error:{type(exc).__name__}",
            model=choice.model,
            input_tokens=total_input,
            output_tokens=total_output,
            provider_cost_usd=provider_cost if provider_cost_known else None,
            changed_paths=changed_paths,
            diff_lines=diff_lines,
            verifier_exit_code=verifier_exit,
            verifier_duration_ms=verifier_ms,
            network_isolated=network_isolated,
            resource_limited=resource_limited,
        )

    _record(
        sink,
        opportunity,
        decision,
        settings,
        actual={"attempted": True, "verified": False, "model": choice.model, "promoted": False},
        reasons=("shadow_verifier_failed",),
        metrics={
            "attempts": settings.max_attempts,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "diff_lines": diff_lines,
            "changed_files": len(changed_paths),
            "verifier_exit_code": verifier_exit or 0,
            "verifier_duration_ms": verifier_ms,
            "network_isolated": network_isolated,
            "resource_limited": resource_limited,
        },
    )
    if model_attempted:
        _persist_model_outcome(
            opportunity,
            choice,
            started=work_started,
            verified=False,
            promoted=False,
            input_tokens=total_input,
            output_tokens=total_output,
            provider_cost_usd=provider_cost if provider_cost_known else None,
            mode=settings.mode,
            repo=repo_key,
        )
    return RepairAttemptResult(
        attempted=True,
        verified=False,
        reason="shadow_verifier_failed",
        model=choice.model,
        attempts=settings.max_attempts,
        input_tokens=total_input,
        output_tokens=total_output,
        provider_cost_usd=provider_cost if provider_cost_known else None,
        changed_paths=changed_paths,
        diff_lines=diff_lines,
        verifier_exit_code=verifier_exit,
        verifier_duration_ms=verifier_ms,
        network_isolated=network_isolated,
        resource_limited=resource_limited,
    )


def run_shadow_repair(
    opportunity: VerifiedWorkOpportunity,
    decision: EligibilityDecision,
    settings: VerifiedWorkSettings,
    *,
    sink: RuntimeDecisionSink | None = None,
    proposal_fn: ProposalFn = propose_patch,
    model_choice: WorkerModelChoice | None = None,
) -> RepairAttemptResult:
    if settings.mode != "shadow":
        return RepairAttemptResult(attempted=False, verified=False, reason="shadow_mode_required")
    return run_repair(
        opportunity,
        decision,
        settings,
        sink=sink,
        proposal_fn=proposal_fn,
        model_choice=model_choice,
    )


def run_enforced_repair(
    opportunity: VerifiedWorkOpportunity,
    decision: EligibilityDecision,
    settings: VerifiedWorkSettings,
    *,
    sink: RuntimeDecisionSink | None = None,
    proposal_fn: ProposalFn = propose_patch,
    model_choice: WorkerModelChoice | None = None,
    host: str = "",
) -> RepairAttemptResult:
    if settings.mode != "enforce":
        return RepairAttemptResult(attempted=False, verified=False, reason="enforce_mode_required")
    return run_repair(
        opportunity,
        decision,
        settings,
        sink=sink,
        proposal_fn=proposal_fn,
        model_choice=model_choice,
        host=host,
    )


def run_shadow_repair_bounded(
    opportunity: VerifiedWorkOpportunity,
    decision: EligibilityDecision,
    *,
    sink: RuntimeDecisionSink | None = None,
    settings: VerifiedWorkSettings | None = None,
) -> RepairAttemptResult:
    """Run one synchronous shadow attempt under durable cross-process budgets."""

    cfg = settings or load_verified_work_settings()
    if cfg.mode != "shadow":
        return RepairAttemptResult(attempted=False, verified=False, reason="shadow_mode_not_enabled")
    if not decision.eligible:
        _record(sink, opportunity, decision, cfg, actual={"attempted": False}, reasons=decision.reason_codes)
        return RepairAttemptResult(attempted=False, verified=False, reason=decision.reason_codes[0])
    choice, model_reason = resolve_worker_model(cfg, kind=opportunity.kind)
    if choice is None:
        _record(
            sink,
            opportunity,
            decision,
            cfg,
            actual={"attempted": False, "model": ""},
            reasons=(model_reason,),
        )
        return RepairAttemptResult(attempted=False, verified=False, reason=model_reason)
    reservation, reason = _reserve(opportunity.session_id, cfg)
    if reservation is None:
        _record(sink, opportunity, decision, cfg, actual={"attempted": False}, reasons=(reason,))
        return RepairAttemptResult(attempted=False, verified=False, reason=reason)
    try:
        return run_repair(opportunity, decision, cfg, sink=sink, model_choice=choice)
    finally:
        _release(reservation)


def run_enforced_repair_bounded(
    opportunity: VerifiedWorkOpportunity,
    decision: EligibilityDecision,
    *,
    sink: RuntimeDecisionSink | None = None,
    settings: VerifiedWorkSettings | None = None,
    host: str = "",
    allowed_models: tuple[str, ...] | None = None,
) -> RepairAttemptResult:
    """Run one synchronous enforce attempt under process/session budgets.

    ``allowed_models`` restricts the worker to models the repository opted in
    to (and whose repository window qualifies), so Auto never spends a run on
    a model it may not promote.
    """

    cfg = settings or load_verified_work_settings()
    if cfg.mode != "enforce":
        return RepairAttemptResult(attempted=False, verified=False, reason="enforce_mode_not_enabled")
    if not decision.eligible:
        _record(sink, opportunity, decision, cfg, actual={"attempted": False}, reasons=decision.reason_codes)
        return RepairAttemptResult(attempted=False, verified=False, reason=decision.reason_codes[0])
    choice, model_reason = resolve_worker_model(cfg, kind=opportunity.kind, allowed_models=allowed_models)
    if choice is None:
        _record(
            sink,
            opportunity,
            decision,
            cfg,
            actual={"attempted": False, "model": ""},
            reasons=(model_reason,),
        )
        return RepairAttemptResult(attempted=False, verified=False, reason=model_reason)
    reservation, reason = _reserve(opportunity.session_id, cfg)
    if reservation is None:
        _record(sink, opportunity, decision, cfg, actual={"attempted": False}, reasons=(reason,))
        return RepairAttemptResult(attempted=False, verified=False, reason=reason)
    try:
        return run_repair(opportunity, decision, cfg, sink=sink, model_choice=choice, host=host)
    finally:
        _release(reservation)


def submit_shadow_repair(
    opportunity: VerifiedWorkOpportunity,
    decision: EligibilityDecision,
    *,
    sink: RuntimeDecisionSink | None = None,
    settings: VerifiedWorkSettings | None = None,
) -> tuple[bool, str]:
    """Queue a daemon shadow attempt without delaying the host tool result."""

    cfg = settings or load_verified_work_settings()
    if cfg.mode != "shadow":
        return False, "shadow_mode_not_enabled"
    if not decision.eligible:
        _record(sink, opportunity, decision, cfg, actual={"attempted": False}, reasons=decision.reason_codes)
        return False, decision.reason_codes[0]
    choice, model_reason = resolve_worker_model(cfg, kind=opportunity.kind)
    if choice is None:
        _record(
            sink,
            opportunity,
            decision,
            cfg,
            actual={"attempted": False, "model": ""},
            reasons=(model_reason,),
        )
        return False, model_reason

    reservation, reason = _reserve(opportunity.session_id, cfg)
    if reservation is None:
        _record(sink, opportunity, decision, cfg, actual={"attempted": False}, reasons=(reason,))
        return False, reason

    def _worker() -> None:
        try:
            run_repair(opportunity, decision, cfg, sink=sink, model_choice=choice)
        finally:
            _persist_sink(sink)
            _release(reservation)

    thread = threading.Thread(target=_worker, name=f"lc-verified-work-{opportunity.id[:8]}", daemon=True)
    thread.start()
    return True, "shadow_queued"


def _reset_state_for_testing() -> None:
    """Compatibility no-op: worker budgets are now durable and cross-process."""
    return None


__all__ = [
    "run_enforced_repair",
    "run_enforced_repair_bounded",
    "run_repair",
    "run_shadow_repair",
    "run_shadow_repair_bounded",
    "submit_shadow_repair",
]

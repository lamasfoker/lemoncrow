"""``lc dev eval review``: replay the Review evaluation corpus and run Gate I.

``run``, ``gate`` and ``accept`` replay recorded fixtures offline (CI mode) and
never construct a model driver. Only ``record --live --max-calls N`` calls a
provider, and it refuses to run in CI or under pytest. See
``docs/reference/review-evaluation.md``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.evaluation.corpus import Corpus
    from lemoncrow.pro.capabilities.review.evaluation.gate import GateConfig, GateResult
    from lemoncrow.pro.capabilities.review.evaluation.harness import EvalReport, ReplaySpec


def _gate_dir_option(func: Any) -> Any:
    return click.option(
        "--gate-dir",
        type=click.Path(path_type=Path, file_okay=False, exists=True),
        default=None,
        help="Directory holding gate.yaml (default: the nearest evals/review above the current directory).",
    )(func)


def _corpus_options(func: Any) -> Any:
    func = click.option("--case", "case_ids", multiple=True, metavar="ID", help="Only these case ids.")(func)
    func = click.option(
        "--organization-opt-in",
        metavar="ORG",
        default=None,
        help="Explicit opt-in to load the named organization's corpus (it must live outside this checkout).",
    )(func)
    return click.option(
        "--corpus",
        "corpus_path",
        type=click.Path(path_type=Path, file_okay=False, exists=True),
        default=None,
        help="Corpus directory (default: the gate.yaml corpus, the built-in public set).",
    )(func)


def _gate_config(gate_dir: Path | None) -> GateConfig:
    from lemoncrow.pro.capabilities.review.evaluation.gate import GateError, find_gate_root, load_gate_config

    try:
        root = gate_dir.resolve() if gate_dir is not None else find_gate_root(Path.cwd())
        return load_gate_config(root)
    except GateError as exc:
        raise click.ClickException(str(exc)) from exc


def _repository_root(config: GateConfig) -> Path | None:
    from lemoncrow.pro.capabilities.review.gitdiff import detect_repo_root

    try:
        return detect_repo_root(config.root)
    except ValueError:
        return None


def _corpus(
    config: GateConfig,
    corpus_path: Path | None,
    organization_opt_in: str | None,
    case_ids: Sequence[str],
    *,
    require_public: bool,
) -> Corpus:
    from lemoncrow.pro.capabilities.review.evaluation.corpus import CorpusError, load_corpus

    try:
        return load_corpus(
            corpus_path.resolve() if corpus_path is not None else config.corpus_path,
            organization_opt_in=organization_opt_in,
            repository_root=_repository_root(config),
            require_visibility="public" if require_public else None,
            case_ids=case_ids,
        )
    except CorpusError as exc:
        raise click.ClickException(str(exc)) from exc


def _refuse_bundled_output(config: GateConfig, corpus: Corpus, *paths: Path | None) -> None:
    """Organization results never land inside this checkout or the installed package."""

    if corpus.visibility != "organization":
        return
    from lemoncrow.pro.capabilities.review.evaluation.corpus import bundled_roots

    for path in paths:
        if path is None:
            continue
        resolved = path.resolve()
        for root in bundled_roots(_repository_root(config)):
            if resolved == root or root in resolved.parents:
                raise click.ClickException(
                    f"organization corpus output must stay outside {root}; choose another location for {path}"
                )


def _spec(config: GateConfig, selections: Sequence[str]) -> ReplaySpec:
    from lemoncrow.pro.capabilities.review.evaluation.harness import ReplaySpec
    from lemoncrow.pro.capabilities.review.evaluation.pipeline import Selection

    if not selections:
        return config.spec
    known = {row.label: row for row in config.spec.selections}
    known.update({row.label.removeprefix("custom:"): row for row in config.spec.selections})
    known.update({row.label.removeprefix("policy:"): row for row in config.spec.selections})
    chosen: list[Selection] = []
    for name in selections:
        row = known.get(name)
        if row is None:
            try:
                row = Selection.preset(name)
            except ValueError as exc:
                raise click.BadParameter(
                    f"{exc}; or one of the gate.yaml selections {sorted(known)}", param_hint="--selection"
                ) from exc
        if row not in chosen:
            chosen.append(row)
    return ReplaySpec(reviewer=config.spec.reviewer, selections=tuple(chosen))


def _replay(corpus: Corpus, spec: ReplaySpec, keep_dir: Path | None = None) -> EvalReport:
    from lemoncrow.pro.capabilities.review.evaluation.fixtures import FixtureFormatError
    from lemoncrow.pro.capabilities.review.evaluation.harness import replay_corpus

    try:
        return replay_corpus(corpus, spec, keep_dir=keep_dir)
    except FixtureFormatError as exc:
        raise click.ClickException(f"unreadable fixture (re-record it): {exc}") from exc


def _rate(value: object) -> str:
    return "n/a" if not isinstance(value, (int, float)) else f"{value:.0%}"


def _number(value: object, suffix: str = "") -> str:
    if value is None:
        return "unknown"
    if isinstance(value, float):
        return f"{value:.2f}{suffix}"
    return f"{value}{suffix}"


def _selection_line(label: str, metrics: Mapping[str, Any]) -> str:
    catch = metrics["catch"]
    behavior = metrics["behavior"]
    noise = metrics["noise"]
    per_review = metrics["cost"]["per_review"]
    usd = per_review["provider_usd"]
    return (
        f"{label:<20} catch {_rate(catch['rate'])} ({len(catch['caught'])}/{catch['expected']})"
        f"  behavior {_rate(behavior['rate'])} ({len(behavior['caught'])}/{behavior['expected']})"
        f"  noise {_rate(noise['rate'])} ({len(noise['false'])}/{noise['surfaced_findings']})"
        f"  cost/review {_number(per_review['estimated_compute_minutes'], ' min')}"
        f" (customer {_number(per_review['estimated_customer_minutes'])},"
        f" managed {_number(per_review['estimated_managed_minutes'])}),"
        f" {_number(per_review['model_calls'])} model calls,"
        f" ${'unknown' if usd is None else f'{usd:.4f}'}"
        f"  time {metrics['time']['wall_seconds_per_review']}s"
    )


def _render_report(report: EvalReport) -> None:
    corpus = report.corpus
    click.echo(
        f"corpus {corpus.id} ({corpus.visibility}, {len(corpus.cases)} cases); reviewer "
        f"{report.spec.reviewer.driver}/{report.spec.reviewer.model}; replay from recorded fixtures"
    )
    for label, metrics in report.selections.items():
        click.echo(_selection_line(label, metrics))
        for entry_id, row in metrics["entries"].items():
            ran = len(row["run_cases"])
            how = (
                "executed"
                if len(row["executed_cases"]) == ran
                else f"{len(row['executed_cases'])} executed, {len(row['estimate_only_cases'])} estimate-only"
            )
            click.echo(
                f"    {entry_id:<30} {ran} reviews ({how}); catches {len(row['catches'])},"
                f" behavior {len(row['behavior_caught'])}, false {len(row['false_findings'])},"
                f" model calls {row['model_calls']}, {row['estimated_minutes']:.2f} est. min"
            )
    events = report.fixture_events()
    if events:
        click.echo(f"fixtures: {len(events)} stale or missing (never reused; re-record with lc dev eval review record)")
        for event in events[:20]:
            click.echo(
                f"    {event['case']} [{event['selection']}] {event['status']} {event['stage']}: {event['reason']}"
            )


def _render_gate(result: GateResult) -> None:
    wire = result.to_wire()
    effect = "BLOCKING" if result.blocking else ("report only" if result.mode == "report" else "ok")
    click.echo(f"Gate I: {result.status.upper()} (mode {result.mode}; {effect})")
    if wire["changed_components"]:
        click.echo(f"changed since baseline: {', '.join(wire['changed_components'])}")
    for label, row in wire["selections"].items():
        cost = row["cost_per_review"] or {}
        per_catch = wire["cost_per_catch"].get(label) or {}
        current, before = per_catch.get("current") or {}, per_catch.get("baseline") or {}
        baseline_text = (
            f" (baseline {_number(before.get('estimated_compute_minutes_per_catch'), ' min')},"
            f" {_number(before.get('model_calls_per_catch'))} calls)"
            if before
            else " (no baseline figure)"
        )
        click.echo(
            f"  {label:<20} catch {_rate(row['catch_rate'])}  behavior {_rate(row['behavior_catch_rate'])}"
            f"  noise {_rate(row['noise_rate'])}  cost/review {_number(cost.get('estimated_compute_minutes'), ' min')},"
            f" {_number(cost.get('model_calls'))} calls"
            f"  cost/catch {_number(current.get('estimated_compute_minutes_per_catch'), ' min')},"
            f" {_number(current.get('model_calls_per_catch'))} calls{baseline_text}"
        )
    for breach in wire["breaches"]:
        click.echo(
            f"  breach {breach['selection']} {breach['metric']}: {breach['baseline']} -> {breach['current']}"
            f" (threshold {breach['threshold']}) {breach['detail']}".rstrip()
        )
    for change in wire["cost_per_review_changes"]:
        click.echo(
            f"  cost/review {change['selection']} {change['metric']}: {change['baseline']} -> {change['current']}"
            f" {change['detail']}"
        )
    for check in wire["acceptances"]:
        state = "valid" if check["valid"] else "invalid: " + "; ".join(check["problems"])
        click.echo(f"  acceptance {check['path']}: {state}")
    for event in wire["fixture_events"][:20]:
        click.echo(
            f"  fixture {event['case']} [{event['selection']}] {event['status']} {event['stage']}: {event['reason']}"
        )
    for note in wire["notes"]:
        click.echo(f"  note: {note}")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _git_user(root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "config", "user.name"], cwd=root, capture_output=True, text=True, check=False, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


@click.group(name="review")
def review_eval() -> None:
    """Review evaluation corpus and Gate I (offline replay; never spends model money by default)."""


@review_eval.command("run")
@_gate_dir_option
@_corpus_options
@click.option("--selection", "selections", multiple=True, metavar="NAME", help="Depth or gate.yaml selection.")
@click.option(
    "--out",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Write the full report, with every per-case Review record, as JSON.",
)
@click.option(
    "--keep-stores",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Keep each case's repository and Review store here for inspection.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the full report as JSON.")
def review_eval_run(
    gate_dir: Path | None,
    corpus_path: Path | None,
    organization_opt_in: str | None,
    case_ids: tuple[str, ...],
    selections: tuple[str, ...],
    out: Path | None,
    keep_stores: Path | None,
    as_json: bool,
) -> None:
    """Replay the corpus from recorded fixtures and report catch, noise and cost per selection.

    Exit status 1 when a fixture is stale or missing (it is reported, never reused).
    """

    config = _gate_config(gate_dir)
    corpus = _corpus(config, corpus_path, organization_opt_in, case_ids, require_public=False)
    _refuse_bundled_output(config, corpus, out, keep_stores)
    report = _replay(corpus, _spec(config, selections), keep_stores)
    wire = report.to_wire()
    if out is not None:
        _write_json(out, wire)
    if as_json:
        click.echo(json.dumps(wire, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        _render_report(report)
        if out is not None:
            click.echo(f"report: {out}")
    if not report.complete:
        sys.exit(1)


@review_eval.command("record")
@_gate_dir_option
@_corpus_options
@click.option("--synthetic", is_flag=True, help="Author fixtures with the deterministic scripted reviewer (no model).")
@click.option(
    "--execute",
    is_flag=True,
    help="Record execution fixtures with the real local sandbox once per exact request (refused in CI/pytest).",
)
@click.option(
    "--live",
    is_flag=True,
    help="Call the gate.yaml reviewer's real provider. Spends money; needs --max-calls; refused in CI and pytest.",
)
@click.option("--max-calls", type=click.IntRange(min=1), default=None, help="Live budget cap: provider calls.")
@click.option(
    "--max-usd",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    help="Live budget cap: provider dollars (every call must report its cost).",
)
def review_eval_record(
    gate_dir: Path | None,
    corpus_path: Path | None,
    organization_opt_in: str | None,
    case_ids: tuple[str, ...],
    synthetic: bool,
    execute: bool,
    live: bool,
    max_calls: int | None,
    max_usd: float | None,
) -> None:
    """Record model outputs for every gate selection into the corpus fixtures.

    A case whose recording fails keeps its old fixtures, which then replay as stale.
    """

    from datetime import UTC, datetime

    from lemoncrow.pro.capabilities.review.evaluation.corpus import EvalCase
    from lemoncrow.pro.capabilities.review.evaluation.fixtures import (
        LiveBudget,
        LiveRecordingRefused,
        RecordedWith,
        Responder,
        SyntheticReviewer,
        ensure_live_recording_allowed,
        live_responder,
    )
    from lemoncrow.pro.capabilities.review.evaluation.harness import record_corpus

    if synthetic == live:
        raise click.UsageError("choose exactly one of --synthetic or --live")
    if synthetic and (max_calls is not None or max_usd is not None):
        raise click.UsageError("--max-calls and --max-usd apply to --live only")
    if live and max_calls is None:
        raise click.UsageError("--live needs a --max-calls budget cap")
    if execute:
        try:
            ensure_live_recording_allowed()
        except LiveRecordingRefused as exc:
            raise click.ClickException(f"local execution recording refused: {exc}") from exc
    config = _gate_config(gate_dir)
    reviewer = config.spec.reviewer
    budget: LiveBudget | None = None
    if live:
        try:
            ensure_live_recording_allowed()
        except LiveRecordingRefused as exc:
            raise click.ClickException(str(exc)) from exc
        if reviewer.model.startswith("synthetic"):
            raise click.ClickException(
                f"the gate reviewer is {reviewer.driver}/{reviewer.model}; set replay.reviewer in gate.yaml to the "
                "real driver and model before a live recording"
            )
    corpus = _corpus(config, corpus_path, organization_opt_in, case_ids, require_public=False)

    def synthetic_for(case: EvalCase) -> Responder:
        return SyntheticReviewer(case, driver=reviewer.driver, model=reviewer.model)

    if live:
        assert max_calls is not None
        budget = LiveBudget(max_calls=max_calls, max_usd=max_usd)
        responder = live_responder(budget)
        recorded_with = RecordedWith(
            mode="live",
            driver=reviewer.driver,
            model=reviewer.model,
            recorded_at=datetime.now(UTC).isoformat(timespec="seconds"),
            budget={"max_calls": max_calls, "max_usd": max_usd},
        )
        result = record_corpus(
            corpus,
            config.spec,
            responder_for=lambda _case: responder,
            recorded_with=recorded_with,
            stop_on_failure=True,
            execute=execute,
        )
    else:
        recorded_with = RecordedWith(mode="synthetic", driver=reviewer.driver, model=reviewer.model)
        result = record_corpus(
            corpus, config.spec, responder_for=synthetic_for, recorded_with=recorded_with, execute=execute
        )
    for path in result.written:
        click.echo(f"recorded {path}")
    for path in result.execution_written:
        click.echo(f"recorded execution {path}")
    for case_id, errors in sorted(result.failed.items()):
        click.echo(f"not recorded {case_id}: {'; '.join(errors)}", err=True)
    click.echo(f"{len(result.written)} cases recorded, {len(result.failed)} failed, {result.calls} distinct calls")
    if budget is not None:
        click.echo(f"live budget: {json.dumps(budget.to_wire(), sort_keys=True)}")
    if not result.ok:
        sys.exit(1)


@review_eval.command("gate")
@_gate_dir_option
@click.option(
    "--mode",
    type=click.Choice(["report", "enforce"]),
    default=None,
    help="Override the gate.yaml mode for this run.",
)
@click.option(
    "--update-baseline",
    is_flag=True,
    help="Record this result as the baseline (refused for a blocked or stale result).",
)
@click.option(
    "--out",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Write the gate result and full report as JSON.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the gate result as JSON.")
def review_eval_gate(
    gate_dir: Path | None,
    mode: str | None,
    update_baseline: bool,
    out: Path | None,
    as_json: bool,
) -> None:
    """Gate I: replay the built-in corpus and compare with the recorded baseline.

    Report mode always exits 0. Enforce mode exits 1 unless the result passes
    or an authorized owner's acceptance record covers it.
    """

    from lemoncrow.pro.capabilities.review.evaluation.gate import GateError, evaluate_gate
    from lemoncrow.pro.capabilities.review.evaluation.gate import update_baseline as record_baseline

    config = _gate_config(gate_dir)
    corpus = _corpus(config, None, None, (), require_public=True)
    report = _replay(corpus, config.spec)
    try:
        result = evaluate_gate(config, report, mode="enforce" if mode == "enforce" else ("report" if mode else None))
    except GateError as exc:
        raise click.ClickException(str(exc)) from exc
    if out is not None:
        _write_json(out, {"gate": result.to_wire(), "report": report.to_wire()})
    if as_json:
        click.echo(json.dumps(result.to_wire(), indent=2, sort_keys=True, ensure_ascii=False))
    else:
        _render_gate(result)
    if update_baseline:
        try:
            path = record_baseline(config, result)
        except GateError as exc:
            click.echo(f"baseline not updated: {exc}", err=True)
            sys.exit(2)
        click.echo(f"baseline recorded: {path}", err=as_json)
        return
    if result.blocking:
        sys.exit(1)


@review_eval.command("accept")
@_gate_dir_option
@click.option("--owner", required=True, help="Authorized owner accepting the trade-off (gate.yaml authorized_owners).")
@click.option("--reason", required=True, help="Why the measured regression is acceptable.")
@click.option("--recorded-by", default=None, help="Who recorded it (default: git config user.name).")
def review_eval_accept(gate_dir: Path | None, owner: str, reason: str, recorded_by: str | None) -> None:
    """Record an owner's explicit acceptance of a blocked gate result, with the measured numbers."""

    from lemoncrow.pro.capabilities.review.evaluation.gate import GateError, evaluate_gate, write_acceptance

    config = _gate_config(gate_dir)
    corpus = _corpus(config, None, None, (), require_public=True)
    report = _replay(corpus, config.spec)
    try:
        result = evaluate_gate(config, report)
        path = write_acceptance(
            config,
            result,
            owner=owner,
            reason=reason,
            recorded_by=_git_user(config.root) if recorded_by is None else recorded_by,
        )
    except GateError as exc:
        raise click.ClickException(str(exc)) from exc
    _render_gate(result)
    click.echo(f"acceptance recorded: {path}")
    click.echo("commit it, then run: lc dev eval review gate --update-baseline")


def _register_gate_h() -> None:
    from lemoncrow.gateway.cli.commands.review_eval_gate_h import gate_h

    review_eval.add_command(gate_h)


_register_gate_h()

__all__ = ["review_eval"]

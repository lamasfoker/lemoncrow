"""Replay (CI mode) and record a corpus across every configured selection."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from lemoncrow.pro.capabilities.review.evaluation.corpus import Corpus, EvalCase, digest
from lemoncrow.pro.capabilities.review.evaluation.digests import compute_digest_set
from lemoncrow.pro.capabilities.review.evaluation.execution_fixtures import (
    CaseExecutionFixtures,
    ExecutionFixtureReplayer,
    LocalExecutionRunner,
    RecordingExecutionRunner,
    SyntheticExecutionRunner,
    load_execution_fixtures,
    write_execution_fixtures,
)
from lemoncrow.pro.capabilities.review.evaluation.fixtures import (
    CallKey,
    CaseFixtures,
    FixtureReplayer,
    RecordedWith,
    RecordingExecutor,
    Responder,
    load_case_fixtures,
    write_case_fixtures,
)
from lemoncrow.pro.capabilities.review.evaluation.metrics import aggregate, evaluate_case, gate_view
from lemoncrow.pro.capabilities.review.evaluation.pipeline import CaseRun, Reviewer, Selection, replay_case
from lemoncrow.pro.capabilities.review.intelligence_runtime import StructuredExecution

REPORT_VERSION: Final[int] = 1


@dataclass(frozen=True, slots=True)
class ReplaySpec:
    reviewer: Reviewer
    selections: tuple[Selection, ...]

    def to_wire(self) -> dict[str, Any]:
        return {"reviewer": self.reviewer.to_wire(), "selections": [row.to_wire() for row in self.selections]}


class _ObservingReplayer(FixtureReplayer):
    """Fixture replay that also remembers every system prompt it was asked with."""

    def __init__(self, case: EvalCase, fixtures: CaseFixtures | None, systems: set[tuple[str, str]]) -> None:
        super().__init__(case, fixtures)
        self._systems = systems

    def __call__(self, **kwargs: Any) -> StructuredExecution:
        key = CallKey.of(kwargs)
        self._systems.add((key.stage, key.system_digest))
        return super().__call__(**kwargs)


@dataclass(frozen=True, slots=True)
class EvalReport:
    corpus: Corpus
    spec: ReplaySpec
    runs: tuple[CaseRun, ...]
    selections: Mapping[str, Mapping[str, Any]]
    outcomes: Mapping[str, Sequence[Mapping[str, Any]]]
    digest_set: Mapping[str, Any]
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    @property
    def complete(self) -> bool:
        return all(run.complete for run in self.runs)

    def fixture_events(self) -> list[dict[str, Any]]:
        return [
            {"case": run.case_id, "selection": run.selection, **event.to_wire()}
            for run in self.runs
            for event in run.fixture_events
        ]

    def snapshot(self) -> dict[str, Any]:
        """Deterministic gate input: the digest set and each selection's gated numbers."""

        body = {
            "digest_set": dict(self.digest_set),
            "corpus": {"id": self.corpus.id, "visibility": self.corpus.visibility},
            "selections": {label: gate_view(metrics) for label, metrics in self.selections.items()},
        }
        return {**body, "snapshot_digest": digest(body)}

    def to_wire(self) -> dict[str, Any]:
        return {
            "version": REPORT_VERSION,
            "mode": "replay",
            "generated_at": self.generated_at,
            "corpus": self.corpus.summary(),
            "spec": self.spec.to_wire(),
            "complete": self.complete,
            "fixture_events": self.fixture_events(),
            "digest_set": dict(self.digest_set),
            "snapshot": self.snapshot(),
            "selections": {label: dict(metrics) for label, metrics in self.selections.items()},
            "outcomes": {label: [dict(row) for row in rows] for label, rows in self.outcomes.items()},
            "records": [run.to_wire() for run in self.runs],
        }


def _load_fixtures(corpus: Corpus) -> dict[str, CaseFixtures | None]:
    return {case.id: load_case_fixtures(corpus.fixtures_dir, case.id) for case in corpus.cases}


def replay_corpus(corpus: Corpus, spec: ReplaySpec, *, keep_dir: Path | None = None) -> EvalReport:
    """CI mode: every model output comes from a recorded fixture; nothing live is constructed."""

    fixtures = _load_fixtures(corpus)
    execution_fixtures = {case.id: load_execution_fixtures(corpus.fixtures_dir, case.id) for case in corpus.cases}
    systems: set[tuple[str, str]] = set()
    runs: list[CaseRun] = []
    selections: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, list[dict[str, Any]]] = {}
    for selection in spec.selections:
        selection_runs: list[CaseRun] = []
        for case in corpus.cases:
            workdir = None if keep_dir is None else keep_dir / selection.label.replace(":", "-") / case.id
            replayer = _ObservingReplayer(case, fixtures[case.id], systems)
            execution_replayer = ExecutionFixtureReplayer(case, execution_fixtures[case.id])
            selection_runs.append(
                replay_case(
                    case,
                    selection,
                    executor=replayer,
                    reviewer=spec.reviewer,
                    execution_runner=execution_replayer,
                    workdir=workdir,
                )
            )
        evaluated = [
            evaluate_case(corpus.case(run.case_id), run, tolerance=corpus.line_tolerance) for run in selection_runs
        ]
        selections[selection.label] = aggregate(selection, evaluated)
        outcomes[selection.label] = [item.to_wire() for item in evaluated]
        runs.extend(selection_runs)
    digest_set = compute_digest_set(
        corpus=corpus,
        reviewer=spec.reviewer.to_wire(),
        selections=[row.to_wire() for row in spec.selections],
        fixtures=fixtures,
        observed_systems=systems,
    )
    return EvalReport(
        corpus=corpus,
        spec=spec,
        runs=tuple(runs),
        selections=selections,
        outcomes=outcomes,
        digest_set=digest_set,
    )


@dataclass(frozen=True, slots=True)
class RecordResult:
    written: tuple[Path, ...]
    failed: Mapping[str, tuple[str, ...]]
    calls: int
    execution_written: tuple[Path, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed


def record_corpus(
    corpus: Corpus,
    spec: ReplaySpec,
    *,
    responder_for: Callable[[EvalCase], Responder],
    recorded_with: RecordedWith,
    stop_on_failure: bool = False,
    execute: bool = False,
) -> RecordResult:
    """Run every selection with *responder_for* and write one fixture file per case.

    A case whose recording hit any responder error is not written (its old
    fixtures, if any, stay and will replay as stale). With *stop_on_failure*
    (live recording) the first failure stops the whole recording.
    """

    written: list[Path] = []
    execution_written: list[Path] = []
    failed: dict[str, tuple[str, ...]] = {}
    calls = 0
    for case in corpus.cases:
        recorder = RecordingExecutor(responder_for(case))
        execution_source = LocalExecutionRunner(case) if execute else SyntheticExecutionRunner(case)
        execution = RecordingExecutionRunner(case, execution_source)
        errors: list[str] = []
        for selection in spec.selections:
            run = replay_case(case, selection, executor=recorder, reviewer=spec.reviewer, execution_runner=execution)
            errors.extend(f"{selection.label}: {error}" for error in run.errors)
            if recorder.errors or execution.errors:
                break
        errors.extend(recorder.errors)
        errors.extend(execution.errors)
        calls += len(recorder.calls)
        if errors:
            failed[case.id] = tuple(dict.fromkeys(errors))
            if stop_on_failure:
                break
            continue
        written.append(
            write_case_fixtures(
                corpus.fixtures_dir,
                CaseFixtures(
                    case_id=case.id,
                    case_input_digest=case.input_digest(),
                    recorded_with=recorded_with.to_wire(),
                    calls=tuple(recorder.calls.values()),
                ),
            )
        )
        if case.execution:
            execution_written.append(
                write_execution_fixtures(
                    corpus.fixtures_dir,
                    CaseExecutionFixtures(
                        case_id=case.id,
                        case_input_digest=case.input_digest(),
                        recorded_with=recorded_with.to_wire(),
                        runs=tuple(execution.calls.values()),
                    ),
                )
            )
    return RecordResult(written=tuple(written), failed=failed, calls=calls, execution_written=tuple(execution_written))


__all__ = ["REPORT_VERSION", "EvalReport", "RecordResult", "ReplaySpec", "record_corpus", "replay_corpus"]

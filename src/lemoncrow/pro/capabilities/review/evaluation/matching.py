"""Deterministic matching of observed findings and behavior deltas to expectations.

A finding **matches** an expected defect when all hold:

* the paths are equal (repository-relative, POSIX);
* the finding's category is one of the expected ``categories``;
* when the expectation names ``sources``, the finding's source is one of them;
* the line ranges overlap once the expected range is widened by the line
  tolerance on both sides (the expectation's ``line_tolerance``, else the
  corpus ``line_tolerance``, default 3). A finding anchored on a whole target
  span therefore matches a defect anywhere inside that span.

Only findings *surfaced* under the effective finding threshold take part:
a folded finding neither catches a defect nor counts as noise.

A behavior delta matches an expected one when the kinds are equal, the
delta's subject matches the expected ``fnmatch`` pattern (case-sensitive) and,
when given, the change (added/changed/removed) is equal.

Assignment is one-to-one and maximum (augmenting paths), visiting
expectations in file order and candidates by (line gap, ref), so the same
inputs always produce the same pairs.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from lemoncrow.pro.capabilities.review.evaluation.corpus import ExpectedBehavior, ExpectedFinding
from lemoncrow.pro.capabilities.review.evaluation.pipeline import ObservedBehavior, ObservedFinding


@dataclass(frozen=True, slots=True)
class FindingMatch:
    expected: ExpectedFinding
    finding: ObservedFinding
    line_gap: int

    @property
    def severity_agrees(self) -> bool | None:
        if self.expected.severity is None:
            return None
        return self.expected.severity == self.finding.severity


@dataclass(frozen=True, slots=True)
class BehaviorMatch:
    expected: ExpectedBehavior
    observed: ObservedBehavior


def line_gap(expected: ExpectedFinding, finding: ObservedFinding) -> int:
    """0 when the ranges overlap, else the number of lines between them."""

    return max(0, expected.start_line - finding.end_line, finding.start_line - expected.end_line)


def finding_matches(expected: ExpectedFinding, finding: ObservedFinding, *, tolerance: int) -> bool:
    if finding.path != expected.path or finding.category not in expected.categories:
        return False
    if expected.sources and finding.source not in expected.sources:
        return False
    allowed = expected.line_tolerance if expected.line_tolerance is not None else tolerance
    return line_gap(expected, finding) <= allowed


def behavior_matches(expected: ExpectedBehavior, observed: ObservedBehavior) -> bool:
    if observed.kind != expected.kind or not fnmatch.fnmatchcase(observed.subject, expected.subject):
        return False
    return expected.change is None or expected.change == observed.change


def _assign[ExpectedT, ObservedT](
    expected: Sequence[ExpectedT],
    observed: Sequence[ObservedT],
    candidates: Callable[[ExpectedT], list[int]],
) -> dict[int, int]:
    """Maximum one-to-one assignment ``expected index -> observed index`` (Kuhn)."""

    owner: dict[int, int] = {}

    def augment(index: int, seen: set[int]) -> bool:
        for candidate in candidates(expected[index]):
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate not in owner or augment(owner[candidate], seen):
                owner[candidate] = index
                return True
        return False

    for index in range(len(expected)):
        augment(index, set())
    return {index: candidate for candidate, index in owner.items()}


def match_findings(
    expected: Sequence[ExpectedFinding],
    findings: Sequence[ObservedFinding],
    *,
    tolerance: int,
) -> tuple[list[FindingMatch], list[ExpectedFinding], list[ObservedFinding]]:
    """``(matches, missed expectations, unmatched surfaced findings)``."""

    surfaced = sorted((item for item in findings if item.surfaced), key=lambda item: item.ref)

    def candidates(item: ExpectedFinding) -> list[int]:
        rows = [
            (line_gap(item, finding), finding.ref, index)
            for index, finding in enumerate(surfaced)
            if finding_matches(item, finding, tolerance=tolerance)
        ]
        return [index for _gap, _ref, index in sorted(rows)]

    pairs = _assign(expected, surfaced, candidates)
    matches = [
        FindingMatch(expected[index], surfaced[found], line_gap(expected[index], surfaced[found]))
        for index, found in sorted(pairs.items())
    ]
    missed = [item for index, item in enumerate(expected) if index not in pairs]
    used = set(pairs.values())
    unmatched = [item for index, item in enumerate(surfaced) if index not in used]
    return matches, missed, unmatched


def match_behavior(
    expected: Sequence[ExpectedBehavior],
    observed: Sequence[ObservedBehavior],
) -> tuple[list[BehaviorMatch], list[ExpectedBehavior], list[ObservedBehavior]]:
    """``(matches, missed expectations, unexpected observed deltas)``."""

    rows = sorted(observed, key=lambda item: (item.kind, item.subject, item.ref))

    def candidates(item: ExpectedBehavior) -> list[int]:
        return [index for index, delta in enumerate(rows) if behavior_matches(item, delta)]

    pairs = _assign(expected, rows, candidates)
    matches = [BehaviorMatch(expected[index], rows[found]) for index, found in sorted(pairs.items())]
    missed = [item for index, item in enumerate(expected) if index not in pairs]
    used = set(pairs.values())
    unexpected = [item for index, item in enumerate(rows) if index not in used]
    return matches, missed, unexpected


__all__ = [
    "BehaviorMatch",
    "FindingMatch",
    "behavior_matches",
    "finding_matches",
    "line_gap",
    "match_behavior",
    "match_findings",
]

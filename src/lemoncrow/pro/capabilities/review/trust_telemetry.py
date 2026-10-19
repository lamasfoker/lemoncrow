"""Content-free operator observations of the trust projection (Trust plan §39.8 item 10).

An observation says what one projection build answered, in counts of codes
only: condition and validity states, reason codes, re-verification buckets and
plan statuses. It carries no Review, repository, person, path or free text, so
a composition may export it as low-cardinality metrics. Nothing here is stored
and nothing reads an observation back: the composition's ``trust_observer``
(``api.register_review_api``) receives each one once, or there is none.

Two builds are observed:

* ``trust_section`` -- one Review Status read built the trust section (the
  flag is ``shadow`` or ``on``): whether it built, each consistency check that
  failed, each item by state and reason, and each T1 validity statement behind
  the items;
* ``run_plan`` -- one read of the current Snapshot's run plan: its
  re-verification summary (what runs, is reused, is skipped, was broadened or
  stays unknown, and why) and whether the compute budget limited it.

Counts are per build. A Review that is read often weighs more, so a ratio of
these counts is a rate over answers given, not over Reviews.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from typing import Any, Final

from lemoncrow.pro.capabilities.review.readiness import CONDITION_STATES, TRUST_MODES
from lemoncrow.pro.capabilities.review.run_plan_reverification import BUCKETS
from lemoncrow.pro.capabilities.review.validity import VALIDITY_STATES

TRUST_SECTION: Final = "trust_section"
RUN_PLAN: Final = "run_plan"
OBSERVATION_KINDS: Final[tuple[str, ...]] = (TRUST_SECTION, RUN_PLAN)

OTHER: Final = "other"
#: The flag values under which a section is built at all.
BUILT_MODES: Final[tuple[str, ...]] = tuple(mode for mode in TRUST_MODES if mode != "off")
NONE: Final = "none"
BUDGET_LIMITED: Final = "limited_by_budget"
#: How a check result on the Snapshot came to be: carried forward, stood in by CI, or run again;
#: or that a due check's head did not execute (``not_run``, with its reason code).
CHECK_DISPOSITIONS: Final[tuple[str, ...]] = ("carried", "ci_equivalent", "ran", "not_run")
#: The section built and every consistency check held, or it could not be built at all.
OUTCOME_OK: Final = "ok"
OUTCOME_ERROR: Final = "error"

#: What a code looks like. Free text and identifiers do not, and count as ``other``.
_CODE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")


def code(value: object) -> str:
    """*value* as an open label: itself when it is a code, ``none`` when empty, else ``other``."""

    text = value if isinstance(value, str) else ""
    if not text:
        return NONE if value in (None, "") else OTHER
    return text if _CODE.fullmatch(text) else OTHER


def _member(value: object, allowed: Collection[str]) -> str:
    return value if isinstance(value, str) and value in allowed else OTHER


def _counted(counts: Counter[tuple[str, ...]], names: Sequence[str]) -> list[dict[str, Any]]:
    return [{**dict(zip(names, key, strict=True)), "count": count} for key, count in sorted(counts.items())]


def _rows(value: object) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, (list, tuple)) else []


def section_observation(
    section: Mapping[str, Any], *, mode: str, mismatches: Sequence[str], build_ms: float
) -> dict[str, Any]:
    """One built trust section: its outcomes, its items by state and reason, its validity statements."""

    items: Counter[tuple[str, ...]] = Counter()
    validity: Counter[tuple[str, ...]] = Counter()
    for item in _rows(section.get("items")):
        items[(_member(item.get("state"), CONDITION_STATES), code(item.get("reason")))] += 1
        for statement in _rows(item.get("validity")):
            validity[(_member(statement.get("state"), VALIDITY_STATES), code(statement.get("reason_code")))] += 1
    return {
        "mode": _member(mode, BUILT_MODES),
        # ``ok``, or each consistency check that failed (``readiness.trust_mismatches``).
        "outcomes": sorted({code(name) for name in mismatches}) or [OUTCOME_OK],
        "build_ms": max(0.0, float(build_ms)),
        "items": _counted(items, ("state", "reason")),
        "validity": _counted(validity, ("state", "reason")),
    }


def section_failure(*, mode: str, build_ms: float) -> dict[str, Any]:
    """A trust section that raised instead of building (the read fails exactly as it did)."""

    return {
        "mode": _member(mode, BUILT_MODES),
        "outcomes": [OUTCOME_ERROR],
        "build_ms": max(0.0, float(build_ms)),
        "items": [],
        "validity": [],
    }


def run_plan_observation(reverification: Mapping[str, Any]) -> dict[str, Any]:
    """One run plan's re-verification summary: bucket sizes, how check results came to be, what stayed unknown."""

    buckets: Counter[tuple[str, ...]] = Counter()
    checks: Counter[tuple[str, ...]] = Counter()
    unknown: Counter[tuple[str, ...]] = Counter()
    for bucket in BUCKETS:
        for row in _rows(reverification.get(bucket)):
            buckets[(bucket,)] += 1
            if bucket == "reuse":
                checks[(_member(row.get("kind"), CHECK_DISPOSITIONS[:2]), code(row.get("reason_code")))] += 1
            elif bucket in ("run", "unknown_broadened") and row.get("state") == "ran":
                # The carry lookup's reason for running this again rather than carrying it.
                checks[("ran", code(row.get("reason_code")))] += 1
            elif bucket == "not_run" and row.get("state") == "not_run":
                # A due check recipe whose head never executed, and why.
                checks[("not_run", code(row.get("reason_code")))] += 1
            elif bucket == "unknown_not_run":
                unknown[(code(row.get("status")),)] += 1
    return {
        "buckets": _counted(buckets, ("bucket",)),
        "checks": _counted(checks, ("disposition", "reason")),
        "unknown": _counted(unknown, ("status",)),
        "budget_limited": (BUDGET_LIMITED,) in unknown,
    }


__all__ = [
    "BUDGET_LIMITED",
    "BUILT_MODES",
    "CHECK_DISPOSITIONS",
    "OBSERVATION_KINDS",
    "OUTCOME_ERROR",
    "OUTCOME_OK",
    "RUN_PLAN",
    "TRUST_SECTION",
    "code",
    "run_plan_observation",
    "section_failure",
    "section_observation",
]

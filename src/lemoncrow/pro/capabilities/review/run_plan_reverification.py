"""The run plan's re-verification summary (Trust T3 items 2, 8, 9, 10 and 12).

Each plan entry (each recipe, for a check entry) lands in one bucket with its reason:

- ``run``: runs on this Snapshot. A check recipe says whether it is ``planned`` or
  already ``ran``, and once it ran, the carry lookup's reason code and detail
  (why it ran again rather than being carried).
- ``reuse``: a result this Snapshot relies on without running it. Either an
  earlier Snapshot's run was carried forward (``carried``, with the origin
  Snapshot and why it was safe), or a typed CI result stands in
  (``ci_equivalent``).
- ``skip_not_affected``: the change cannot reach it, because no changed path is in
  its scope or test impact proves no test reaches the change. An
  impact-selected recipe also lists the tests it left out here, so it appears
  in ``run`` or ``reuse`` as well.
- ``unknown_broadened``: the import graph cannot tell what the change reaches
  (no or stale map, truncation, a runtime read), so the full suite runs
  (``coverage_unknown``, never an ordinary full run).
- ``inapplicable``: the repository has nothing for it to check.
- ``not_run``: settings, the trigger or a missing request keep it from running;
  or a check recipe was due but its head did not execute (``state: not_run``
  with a reason code: ``head_definition_guard`` when the Change edits the file
  that defines the recipe, ``executor_unavailable`` when the runner could not
  run it).
- ``unknown_not_run``: budget-limited work, required work with no executor, and
  entries whose applicability a bounded detection could not decide
  (``coverage_unknown``). It stays unknown, and blocks wherever policy requires
  it; it is never shown as skipped.

A check recipe is placed by its *deciding* head attempt: the last plain head
run, the one its verdict rests on (reruns stop at the first pass). A verdict
resting on a carried rerun is carried, even when attempt 1 ran here.

Display only: it reads the plan and the Snapshot's newest check results and
decides nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from lemoncrow.pro.capabilities.review.capability_catalog import COVERAGE_UNKNOWN
from lemoncrow.pro.capabilities.review.run_plan import PATH_SCOPE_SKIP, PlanEntry, RunPlan
from lemoncrow.pro.capabilities.review.test_impact import RecipeSelection, selections_from_document

BUCKETS: Final[tuple[str, ...]] = (
    "run",
    "reuse",
    "skip_not_affected",
    "unknown_broadened",
    "inapplicable",
    "not_run",
    "unknown_not_run",
)
_CARRIED: Final = "carry_exact_key"
#: Why a due check recipe's head did not execute.
HEAD_GUARDED: Final = "head_definition_guard"
EXECUTOR_UNAVAILABLE: Final = "executor_unavailable"


def _deciding_attempt(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The plain head attempt the verdict rests on: the last one run (reruns stop at the first pass)."""

    deciding: Mapping[str, Any] | None = None
    for row in record.get("attempts") or ():
        if not isinstance(row, Mapping) or row.get("side") != "new" or row.get("variant", "run") != "run":
            continue
        if deciding is None or _number(row) > _number(deciding):
            deciding = row
    return deciding


def _number(row: Mapping[str, Any]) -> int:
    value = row.get("attempt")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _validity(record: Mapping[str, Any], recipe_id: str, attempt: int) -> Mapping[str, Any]:
    economy = record.get("economy")
    rows = economy.get("validity") if isinstance(economy, Mapping) else None
    row = rows.get(f"check:{recipe_id}#{attempt}") if isinstance(rows, Mapping) else None
    return row if isinstance(row, Mapping) else {}


def _why(validity: Mapping[str, Any]) -> dict[str, str]:
    code = str(validity.get("reason_code") or "")
    return {"reason_code": code, "detail": str(validity.get("detail") or "")} if code else {}


def _equivalence_first(row: dict[str, Any], entry: PlanEntry) -> None:
    """An equivalent result that did not stand in (kill switch, fresh pick) is why the entry runs.

    The carry lookup's code, when the recipe ran, follows in ``causes``.
    """

    equivalence = entry.equivalence or {}
    if equivalence.get("state") != "reverify" or not equivalence.get("reason_code"):
        return
    carry = str(row.get("reason_code") or "")
    row.update(_why(equivalence), causes=[carry] if carry else [])


def _not_run(record: Mapping[str, Any], head: Mapping[str, Any] | None) -> dict[str, str]:
    """Why a recorded check recipe's head did not execute, or ``{}`` when it did."""

    guard = str(record.get("head_guard") or "")
    if guard:
        return {"reason": guard, "reason_code": HEAD_GUARDED}
    if head is not None and head.get("status") == "not_run":
        return {
            "reason": str(head.get("reason") or "the head side was not executed"),
            "reason_code": EXECUTOR_UNAVAILABLE,
        }
    return {}


def _not_affected(entry: PlanEntry) -> bool:
    if entry.reason.startswith(PATH_SCOPE_SKIP):
        return True
    selections = selections_from_document(entry.selection)
    return bool(selections) and {row.mode for row in selections.values()} == {"none"}


def _check_rows(
    out: dict[str, list[dict[str, Any]]],
    base: Mapping[str, Any],
    entry: PlanEntry,
    differentials: Mapping[str, Mapping[str, Any]],
    recipes: Sequence[str],
) -> None:
    selections = selections_from_document(entry.selection)
    recipe_ids = list(dict.fromkeys((*recipes, *selections)))
    if not recipe_ids:
        out["run"].append({**base, "state": "planned", "reason": entry.reason})
        return
    for recipe_id in recipe_ids:
        row: dict[str, Any] = {**base, "recipe_id": recipe_id}
        chosen: RecipeSelection | None = selections.get(recipe_id)
        record = differentials.get(recipe_id)
        if record is None and chosen is not None and chosen.mode == "none":
            out["skip_not_affected"].append({**row, "reason": chosen.reason, "tests_skipped": chosen.total})
            continue
        origin: object = None
        validity: Mapping[str, Any] = {}
        head: Mapping[str, Any] | None = None
        if record is not None:
            head = _deciding_attempt(record)
            origin = head.get("reused_from") if head is not None else None
            validity = _validity(record, recipe_id, _number(head) if head is not None else 1)
        skipped = _not_run(record, head) if record is not None else {}
        if isinstance(origin, Mapping) and origin.get("kind") == "snapshot":
            out["reuse"].append(
                {
                    **row,
                    "kind": "carried",
                    "origin_snapshot": int(origin.get("snapshot") or 0),
                    "origin_revision_id": str(origin.get("revision_id") or ""),
                    "origin_evidence_id": str(origin.get("evidence_id") or ""),
                    "reason_code": str(validity.get("reason_code") or _CARRIED),
                    "detail": str(validity.get("detail") or ""),
                }
            )
        elif skipped:
            # Due, but the head never executed: no result to rely on, never shown as ran.
            out["not_run"].append({**row, "state": "not_run", **skipped})
        else:
            row.update(
                {"state": "ran", **_why(validity)} if record is not None else {"state": "planned"},
                reason=chosen.reason if chosen is not None else entry.reason,
            )
            _equivalence_first(row, entry)
            if chosen is not None and chosen.coverage == "unknown":
                out["unknown_broadened"].append({**row, "coverage": COVERAGE_UNKNOWN})
            else:
                out["run"].append(row)
        if chosen is not None and chosen.mode == "impact" and chosen.total > len(chosen.tests):
            # The tests an impact-selected run (or the run it carried) left out.
            out["skip_not_affected"].append(
                {
                    **base,
                    "recipe_id": recipe_id,
                    "reason": chosen.reason,
                    "tests_skipped": chosen.total - len(chosen.tests),
                }
            )


def reverification_summary(
    plan: RunPlan,
    differentials: Mapping[str, Mapping[str, Any]],
    *,
    recipes_for: Callable[[str], Sequence[str]],
    is_check: Callable[[str], bool],
) -> dict[str, list[dict[str, Any]]]:
    """Bucket *plan* (module docstring); *differentials* are the newest per recipe id."""

    out: dict[str, list[dict[str, Any]]] = {name: [] for name in BUCKETS}
    blockers = {row.id for row in plan.required_blockers()}
    for entry in plan.entries:
        base = {"entry_id": entry.id, "title": entry.title, "required": entry.required}
        status = entry.status
        if entry.id in blockers or status == "limited_by_budget" or entry.reason.startswith(COVERAGE_UNKNOWN):
            out["unknown_not_run"].append({**base, "status": status, "reason": entry.reason})
        elif status == "satisfied":
            out["reuse"].append(
                {
                    **base,
                    "kind": "ci_equivalent",
                    "reason": entry.reason,
                    "satisfied_by": dict(entry.satisfied_by or {}),
                    **_why(entry.equivalence or {}),
                }
            )
        elif status == "inapplicable":
            out["inapplicable"].append({**base, "reason": entry.reason})
        elif status == "skipped" and _not_affected(entry):
            out["skip_not_affected"].append({**base, "reason": entry.reason})
        elif status != "run":
            out["not_run"].append({**base, "status": status, "reason": entry.reason})
        elif is_check(entry.id):
            _check_rows(out, base, entry, differentials, recipes_for(entry.id))
        else:
            out["run"].append({**base, "reason": entry.reason})
    return out


__all__ = ["BUCKETS", "EXECUTOR_UNAVAILABLE", "HEAD_GUARDED", "reverification_summary"]

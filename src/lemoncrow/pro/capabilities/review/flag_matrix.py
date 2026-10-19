"""Feature-flag state matrix (Phase 9R item 44, ``behavior.flag_matrix``).

For the feature flags a Change reads or introduces -- the R3 inventory's
``feature_flag`` facts in the head text of its changed code files -- the
trusted-base command (``analyses.flags``) runs on base and head once per flag
state: each touched flag on, then off, one flag at a time. Every other flag stays
at its default: the driver's allowlisted environment never carries a flag
variable except the one of the state being run.

The matrix is bounded by depth (Thorough: 2 flags, Deep: 4 flags) and by the
plan's compute budget. A state that is not run is listed as
``limited_by_budget`` with the reason, never dropped; any such state keeps the
verdict from passing.

The command is reviewed code, so the driver is sealed (``SEAL_PRELUDE``): each
state runs in its own fresh copy of the Snapshot, and the per-test outcomes
(an optional JUnit XML report) are read only after the kill sweep.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Final

from lemoncrow.pro.capabilities.review.analysis_common import (
    FLAG_MATRIX_ENTRY,
    MAX_TIMEOUT,
    AnalysisSpec,
    Evaluation,
    Finding,
    Observation,
    SideRun,
    bounded,
    build_driver,
    relative_path,
    sealed_inspection_problem,
    sealed_problem,
    sha256_hex,
    valid_spec_id,
)
from lemoncrow.pro.capabilities.review.contracts import inventory_side_effects

# lc-debt: the matrix runs one flag at a time (2 states per flag) and at most 2 flags at
# Thorough, 4 at Deep; pairwise combinations are never run. Upgrade path: a covering-array
# planner that spends the remaining budget on pairs of flags read on the same code path.
MAX_FLAGS: Final[Mapping[str, int]] = {"thorough": 2, "deep": 4}
DEFAULT_MAX_FLAGS: Final[int] = 2
MAX_TESTS_PER_STATE: Final[int] = 2000
DEFAULT_TEMPLATE: Final[str] = "FEATURE_{FLAG}"
#: Longest flag name a state carries (the R3 inventory's ``feature_flag`` names are 2-100 characters);
#: a longer one is listed as not run, so the plan-time worst case below always holds.
MAX_FLAG_CHARS: Final[int] = 100
#: Longest ``env`` template (the environment variable name around ``{FLAG}``).
MAX_TEMPLATE_CHARS: Final[int] = 128
#: ``on``/``off`` values are at most this long.
MAX_VALUE_CHARS: Final[int] = 64
#: The widest a single code point gets under ``ensure_ascii`` JSON: a surrogate pair, 12 characters.
_WIDEST_ESCAPED_CHAR: Final[str] = "\U00020000"
_CODE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rb", ".java", ".kt", ".php", ".rs", ".cs"}
)
_TEMPLATE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\{FLAG\}|\{flag\})[A-Za-z0-9_]*$")
_RESERVED_ENV = re.compile(r"^(?:PATH|HOME|LANG|LC_ALL|TZ|CI|NO_COLOR|SOURCE_DATE_EPOCH|VIRTUAL_ENV|LD_\w*|PYTHON\w*)$")


# --------------------------------------------------------------------------- #
# the sealed driver (stdlib only; runs inside the sandbox)
# --------------------------------------------------------------------------- #

_BODY: Final[str] = r"""
import xml.etree.ElementTree as ET


def outcomes(data):
    out = {}
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    for case in root.iter("testcase"):
        if len(out) >= 2000:
            break
        name = (case.get("classname") or "") + "::" + (case.get("name") or "")
        state = "passed"
        for child in case:
            if child.tag in ("failure", "error"):
                state = "failed"
            elif child.tag == "skipped" and state == "passed":
                state = "skipped"
        out[name[:300]] = state
    return out


def main():
    seal_start()
    command = [str(item) for item in P.get("command") or []]
    if not command:
        finish(2, "no command is configured")
    budget = float(P.get("budget") or TIMEOUT)
    junit = str(P.get("junit") or "")
    rows = []
    for state in P.get("states") or []:
        row = {"id": str(state.get("id") or "")}
        if time.monotonic() - STARTED > budget:
            row.update(state="limited_by_budget", reason="the analysis budget ran out before this state")
            rows.append(row)
            continue
        work = copy_root()
        identity = dir_identity(work)
        extra = {str(k): str(v) for k, v in (state.get("env") or {}).items()}
        code, output = sealed_run(command, work, extra=extra)
        if code is None:
            finish(2, output)
        require_same_dir(work, identity)
        row.update(state="ran", exit=code, output_sha=hashlib.sha256(output.encode()).hexdigest()[:16],
                   tail=output[-800:])
        if junit:
            got, missing = read_files([(junit, os.path.join(work, junit))], work, 16 << 20, bound="report")
            tests = outcomes(got[0][1]) if got else None
            if tests is None:
                row["junit"] = (missing[0]["why"] if missing else "the JUnit report does not parse")
            else:
                row["tests"] = tests
        rows.append(row)
    R["states"] = rows
    R["inspection"] = "sealed_in_sandbox"
    finish(0)
"""

DRIVER: Final[str] = build_driver(_BODY, sealed=True)


# --------------------------------------------------------------------------- #
# trusted-base specs
# --------------------------------------------------------------------------- #


def _argv(value: Any) -> tuple[str, ...] | None:
    if isinstance(value, str):
        try:
            parts = tuple(shlex.split(value))
        except ValueError:
            return None
    elif isinstance(value, list) and all(isinstance(item, (str, int, float)) for item in value):
        parts = tuple(str(item) for item in value)
    else:
        return None
    return parts if parts and len(parts) <= 64 and all(len(part) <= 2000 for part in parts) else None


def parse_specs(rows: Sequence[Any], errors: list[str]) -> list[AnalysisSpec]:
    """``analyses.flags`` rows: ``{id, command, env?, on?, off?, junit?, root?, timeout?}``."""

    specs: list[AnalysisSpec] = []
    for index, row in enumerate(rows):
        where = f"analyses.flags[{index}]"
        if not isinstance(row, Mapping):
            errors.append(f"{where} must be a mapping")
            continue
        unknown = sorted(set(row) - {"id", "title", "command", "env", "on", "off", "junit", "root", "timeout"})
        if unknown:
            errors.append(f"{where} has unknown keys: {', '.join(unknown)}")
            continue
        spec_id = row.get("id")
        command = _argv(row.get("command"))
        template = str(row.get("env") or DEFAULT_TEMPLATE)
        on, off = str(row.get("on", "1")), str(row.get("off", "0"))
        junit_raw = row.get("junit")
        junit = relative_path(junit_raw, allow_root=False) if junit_raw else ""
        root = relative_path(row.get("root") or ".")
        try:
            timeout = max(10, min(int(row.get("timeout") or 300), MAX_TIMEOUT))
        except (TypeError, ValueError):
            errors.append(f"{where}.timeout must be a number of seconds")
            continue
        if not valid_spec_id(spec_id):
            errors.append(f"{where}.id must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}")
        elif command is None:
            errors.append(f"{where}.command must be a command line or a list of arguments")
        elif (
            len(template) > MAX_TEMPLATE_CHARS
            or not _TEMPLATE.fullmatch(template)
            or _RESERVED_ENV.match(template.replace("{FLAG}", "X"))
        ):
            errors.append(
                f"{where}.env must name one environment variable containing {{FLAG}} (e.g. FEATURE_{{FLAG}}), "
                f"at most {MAX_TEMPLATE_CHARS} characters"
            )
        elif len(on) > MAX_VALUE_CHARS or len(off) > MAX_VALUE_CHARS or on == off:
            errors.append(f"{where}.on and .off must be two different values of at most {MAX_VALUE_CHARS} characters")
        elif junit is None or root is None:
            errors.append(f"{where}.junit and .root must be repository-relative paths")
        else:
            specs.append(
                AnalysisSpec(
                    entry_id=FLAG_MATRIX_ENTRY,
                    id=str(spec_id),
                    tool="command",
                    root=root,
                    title=str(row.get("title") or "")[:200] or f"Flag matrix · {' '.join(command)[:80]}",
                    origin="configured",
                    source=where,
                    options={"command": list(command), "env": template, "on": on, "off": off, "junit": junit},
                    timeout=timeout,
                )
            )
    return specs


# --------------------------------------------------------------------------- #
# host-side preparation
# --------------------------------------------------------------------------- #


def env_name(template: str, flag: str) -> str:
    upper = re.sub(r"[^A-Za-z0-9]+", "_", flag).strip("_").upper()
    raw = re.sub(r"[^A-Za-z0-9_]+", "_", flag).strip("_")
    return template.replace("{FLAG}", upper).replace("{flag}", raw)


@dataclass(frozen=True, slots=True)
class FlagState:
    id: str
    flag: str
    value: str
    env: tuple[tuple[str, str], ...]

    def to_payload(self) -> dict[str, Any]:
        return {"id": self.id, "env": dict(self.env)}


@dataclass(slots=True)
class Prepared:
    #: Touched flag -> (how the Change touches it, paths), sorted by flag name.
    flags: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)
    states: list[FlagState] = field(default_factory=list)
    #: State id -> why it is not run (depth bound).
    limited: dict[str, str] = field(default_factory=dict)
    depth: str = ""
    max_flags: int = DEFAULT_MAX_FLAGS


def touched_flags(
    changed_paths: Sequence[str], read_side: Callable[[str, str], str | None]
) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Flags the Change reads or introduces: ``feature_flag`` facts of its changed code files' head text."""

    head: dict[str, list[str]] = {}
    base: set[str] = set()
    for path in sorted(set(changed_paths)):
        if PurePosixPath(path).suffix.lower() not in _CODE_SUFFIXES:
            continue
        new_text = read_side(path, "new")
        for fact in inventory_side_effects(path, new_text) if new_text is not None else ():
            if fact.category == "feature_flag":
                head.setdefault(fact.name, []).append(path)
        old_text = read_side(path, "old")
        for fact in inventory_side_effects(path, old_text) if old_text is not None else ():
            if fact.category == "feature_flag":
                base.add(fact.name)
    return {
        flag: ("read" if flag in base else "introduced", tuple(dict.fromkeys(paths))[:5])
        for flag, paths in sorted(head.items())
    }


def prepare(
    spec: AnalysisSpec,
    *,
    changed_paths: Sequence[str],
    read_side: Callable[[str, str], str | None],
    depth: str,
) -> Prepared:
    """The bounded state matrix: each touched flag on and off; flags past the depth bound are limited."""

    prepared = Prepared(depth=depth, max_flags=MAX_FLAGS.get(depth, DEFAULT_MAX_FLAGS))
    prepared.flags = touched_flags(changed_paths, read_side)
    template = str(spec.options.get("env") or DEFAULT_TEMPLATE)
    values = (("on", str(spec.options.get("on", "1"))), ("off", str(spec.options.get("off", "0"))))
    for index, flag in enumerate(prepared.flags):
        for label, value in values:
            state = FlagState(f"{flag}={label}", flag, label, ((env_name(template, flag), value),))
            if len(flag) > MAX_FLAG_CHARS:
                # Never past the size the plan reserved for a state (``worst_case_payload``).
                prepared.limited[state.id] = (
                    f"not run: the flag name is longer than {MAX_FLAG_CHARS} characters, past what the plan "
                    "reserves for one state in the executor command"
                )
            elif index >= prepared.max_flags:
                prepared.limited[state.id] = (
                    f"Limited by budget: the {depth or 'current'} depth runs at most {prepared.max_flags} flag(s); "
                    f"{flag} is flag {index + 1} of {len(prepared.flags)}"
                )
            prepared.states.append(state)
    return prepared


def payload(spec: AnalysisSpec, prepared: Prepared, *, budget_seconds: float | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "command": list(spec.options.get("command") or ()),
        "junit": spec.options.get("junit") or "",
        "states": [state.to_payload() for state in prepared.states if state.id not in prepared.limited],
    }
    if budget_seconds is not None and budget_seconds < spec.timeout:
        out["budget"] = max(0.0, round(budget_seconds, 1))
    return out


def worst_case_payload(spec: AnalysisSpec) -> dict[str, Any]:
    """The largest payload a run of *spec* can send, computed from the spec alone (plan time).

    Each state is the widest one the run can build: a flag name of :data:`MAX_FLAG_CHARS` code points
    that ``ensure_ascii`` escapes at their widest, the template filled with as many ASCII characters,
    the wider of the configured on/off values; as many states as the deepest depth runs; and the
    budget field. A spec whose worst case fits therefore always fits at run time.
    """

    template = str(spec.options.get("env") or DEFAULT_TEMPLATE)
    values = [str(spec.options.get("on", "1")), str(spec.options.get("off", "0"))]
    widest_value = max(values, key=lambda value: len(json.dumps(value, ensure_ascii=True)))
    variable = template.replace("{FLAG}", "F" * MAX_FLAG_CHARS).replace("{flag}", "f" * MAX_FLAG_CHARS)
    state = {"id": _WIDEST_ESCAPED_CHAR * MAX_FLAG_CHARS + "=off", "env": {variable: widest_value}}
    return {
        "command": list(spec.options.get("command") or ()),
        "junit": spec.options.get("junit") or "",
        "states": [state] * (2 * max(MAX_FLAGS.values())),
        "budget": round(max(0.0, spec.timeout - 0.1), 1),
    }


def inspect(report: dict[str, Any], exit_code: int | None) -> tuple[dict[str, Any] | None, str]:
    """Stage B: accept the sealed run's state rows only when the seal held."""

    problem = sealed_problem(report, exit_code)
    if problem:
        return None, problem
    if report.get("exit") == 0:
        problem = sealed_inspection_problem(report)
        if problem:
            return None, problem
    return report, ""


# --------------------------------------------------------------------------- #
# evaluation
# --------------------------------------------------------------------------- #


def _rows(run: SideRun | None) -> dict[str, Mapping[str, Any]]:
    if run is None or run.state not in {"ok", "findings"}:
        return {}
    return {str(row.get("id")): row for row in run.report.get("states") or () if isinstance(row, Mapping)}


def _side_reason(run: SideRun | None, side: str) -> str:
    if run is None:
        return f"the {side} side did not run"
    return run.reason or f"the {side} side is {run.state}"


def evaluate(spec: AnalysisSpec, prepared: Prepared, head: SideRun | None, base: SideRun | None) -> Evaluation:
    flags = [
        {"flag": flag, "touch": touch, "paths": list(paths), "env": env_name(str(spec.options.get("env")), flag)}
        for flag, (touch, paths) in prepared.flags.items()
    ]
    bound = {
        "depth": prepared.depth,
        "max_flags": prepared.max_flags,
        "touched": len(prepared.flags),
        "limited_flags": max(0, len(prepared.flags) - prepared.max_flags),
    }
    if not prepared.states:
        return Evaluation(
            "PASS",
            ["the Change reads or introduces no feature flag (R3 inventory of its changed code files)"],
            summary={"flags": [], "matrix": [], "bound": bound, "untouched": "default"},
        )
    head_rows, base_rows = _rows(head), _rows(base)
    matrix: list[dict[str, Any]] = []
    findings: list[Finding] = []
    regressed = limited = unrun = 0
    for state in prepared.states:
        row: dict[str, Any] = {"state": state.id, "flag": state.flag, "value": state.value, "env": dict(state.env)}
        if state.id in prepared.limited:
            limited += 1
            matrix.append({**row, "status": "limited_by_budget", "reason": prepared.limited[state.id]})
            continue
        old, new = base_rows.get(state.id), head_rows.get(state.id)
        if old is None or new is None or old.get("state") != "ran" or new.get("state") != "ran":
            budget = any(
                (item is not None and item.get("state") == "limited_by_budget")
                or (run is not None and run.reason.startswith("Limited by budget"))
                for item, run in ((old, base), (new, head))
            )
            reason = next(
                (
                    str(item.get("reason") or "")
                    for item in (old, new)
                    if item is not None and item.get("state") != "ran" and item.get("reason")
                ),
                "",
            ) or (_side_reason(base, "base") if old is None else _side_reason(head, "head"))
            if budget:
                limited += 1
                matrix.append({**row, "status": "limited_by_budget", "reason": f"Limited by budget: {reason}"})
            else:
                unrun += 1
                matrix.append({**row, "status": "not_run", "reason": reason})
            continue
        base_exit, head_exit = old.get("exit"), new.get("exit")
        base_tests = old.get("tests") if isinstance(old.get("tests"), Mapping) else {}
        head_tests = new.get("tests") if isinstance(new.get("tests"), Mapping) else {}
        assert isinstance(base_tests, Mapping) and isinstance(head_tests, Mapping)
        changed = sorted(
            name for name in set(base_tests) | set(head_tests) if base_tests.get(name) != head_tests.get(name)
        )
        status = "same"
        if base_exit == 0 and head_exit != 0:
            status = "regressed"
            regressed += 1
        elif base_exit != head_exit or changed:
            status = "changed"
        matrix.append(
            {
                **row,
                "status": status,
                "base": {"exit": base_exit, "tests": len(base_tests)},
                "head": {"exit": head_exit, "tests": len(head_tests)},
                "tests_changed": len(changed),
            }
        )
        label = f"{state.flag} {state.value}"
        if status != "same" and not changed:
            findings.append(
                Finding(
                    rule="suite_outcome_changed",
                    category="flag_state",
                    subject=f"{spec.id}::{state.id}::suite",
                    title=f"Test run {'fails' if status == 'regressed' else 'changes'} with {label}"[:200],
                    summary=bounded(
                        f"With {state.flag} {state.value} (and every other flag at its default) the command exits "
                        f"{base_exit} on base and {head_exit} on head."
                    ),
                    severity="high" if status == "regressed" else "medium",
                    critical=status == "regressed",
                    paths=prepared.flags[state.flag][1],
                    facts=(("Flag", state.flag), ("State", state.value), ("Variable", state.env[0][0])),
                    base=Observation(f"exit {base_exit}", f"exit {base_exit}", str(old.get("output_sha") or "")),
                    head=Observation(f"exit {head_exit}", f"exit {head_exit}", str(new.get("output_sha") or "")),
                )
            )
        for name in changed[:50]:
            was, now = str(base_tests.get(name) or "absent"), str(head_tests.get(name) or "absent")
            broke = was == "passed" and now == "failed"
            findings.append(
                Finding(
                    rule="test_outcome_changed",
                    category="flag_state",
                    subject=f"{spec.id}::{state.id}::{name}"[:300],
                    title=f"{name.split('::')[-1]} {was} → {now} with {label}"[:200],
                    summary=bounded(f"{name}: {was} on base, {now} on head, with {state.flag} {state.value}."),
                    severity="high" if broke else "medium",
                    critical=broke,
                    change="added" if was == "absent" else "removed" if now == "absent" else "changed",
                    paths=prepared.flags[state.flag][1],
                    facts=(("Flag", state.flag), ("State", state.value), ("Test", name[:200])),
                    base=Observation(was, was, sha256_hex([name, was])),
                    head=Observation(now, now, sha256_hex([name, now])),
                )
            )
    ran = len(matrix) - limited - unrun
    if regressed:
        status_ = "FAIL"
    elif limited or unrun:
        status_ = "NOT_RUN"
    else:
        status_ = "PASS"
    reasons = [
        f"{ran} of {len(matrix)} flag state(s) ran on base and head; {regressed} regressed",
        *([f"Limited by budget: {limited} flag state(s) were not run"] if limited else []),
        *([f"{unrun} flag state(s) did not run"] if unrun else []),
        "untouched flags stayed at their defaults",
    ]
    return Evaluation(
        status_,  # type: ignore[arg-type]
        reasons,
        findings,
        summary={"flags": flags, "matrix": matrix, "bound": bound, "untouched": "default"},
    )


__all__ = [
    "DEFAULT_MAX_FLAGS",
    "DRIVER",
    "MAX_FLAGS",
    "FlagState",
    "Prepared",
    "env_name",
    "evaluate",
    "inspect",
    "parse_specs",
    "payload",
    "prepare",
    "touched_flags",
]

"""Gate H: behavior evidence quality (plan section 19, Gate H).

Before a LemonCrow-executed recipe or surface result may satisfy or block a
readiness condition, or feed auto-approval, the recipe or surface must be
*qualified* on measured runs:

* **flake rate** -- repeat runs of a recipe on one fixed input that disagree
  (recipe status or any per-test status), over the measured inputs;
* **classification accuracy** -- the R5 base/head classifier (per report
  format) against labeled ground truth;
* **visual determinism** -- rendering one surface input twice yields an
  identical protected page (every aspect R7 would raise as a delta).

This module is the small typed contract (P10-LAND consumes it):

* :class:`QualificationSubject` -- what a result names: ``recipe`` (recipe
  digest + environment class + classifier) or ``surface`` (surface digest +
  environment class). Composite kind-entry results (``checks.test``) are the
  conjunction of their recipes.
* :func:`qualify` / :func:`qualification` -- ``qualified``,
  ``advisory(reason)`` or ``unmeasured``. Unidentified subjects, missing
  measurements and impossible measurements (render unavailable) are
  ``unmeasured``, never fabricated.
* :class:`QualificationEffect` -- the subject's qualification under the Gate
  H mode: ``advisory`` (enforce mode and not qualified: shown, never gating)
  and ``would_be_advisory`` (what enforcement would do). Report mode changes
  nothing; the would-be effect is recorded.
* :class:`QualificationContext` -- one Snapshot's thresholds, mode and
  measurement lookups (store-backed, memoized).

Thresholds and the report/enforce switch are the Gate H *contract*: the
versioned ``evals/review/gate-h.yaml`` (:func:`load_gate_h_config`), shipped
byte-for-byte as package data (:data:`CONTRACT_RESOURCE`) so an installed wheel,
the Review engine and the hosted server read the same contract the release
measured (:func:`runtime_gate_h_config`). Like Gate I, ``enforce`` may only be
selected once the thresholds are approved by the product owner, and the approval
is bound to the threshold values by a digest: an edited threshold counts as
unapproved until the owner re-approves. Until then everything is report mode. A repository may only
*tighten* them in its trusted-base ``check_policy.qualification`` section:
the Gate H thresholds are a floor (interpretation of "stay advisory until
they meet the threshold": no repository can set a looser one).

Measurements are repository-scoped ``review_derived`` rows (kind
:data:`QUALIFICATION_KIND`) on the Snapshot they were imported onto, looked
up across Snapshots by scope (the R6 shared-base pattern); the Snapshot
deletion cascade and an LRU cap (:data:`MAX_QUALIFICATION_ROWS`) close them.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal, cast

import yaml

QualificationState = Literal["qualified", "advisory", "unmeasured"]
GateHMode = Literal["report", "enforce"]
SubjectKind = Literal["recipe", "surface"]
Metric = Literal["flake", "classification", "visual"]

GATE_H_CONFIG: Final[str] = "gate-h.yaml"
#: The packaged copy of ``evals/review/gate-h.yaml`` (identical bytes; a test and ``release_gate --validate`` hold them equal).
CONTRACT_RESOURCE: Final[str] = "gate_h_contract.yaml"
GATE_H_VERSION: Final[int] = 1
MEASUREMENT_VERSION: Final[int] = 1
QUALIFICATION_KIND: Final[str] = "evidence_qualification"
#: lc-debt: one LRU cap per store (hosted: per tenant repository), evicted on
#: write by a full scan of the kind. Upgrade: an indexed, per-repository cap.
MAX_QUALIFICATION_ROWS: Final[int] = 512
#: A measurement import larger than this is refused whole (never truncated).
MAX_IMPORT_MEASUREMENTS: Final[int] = 256
APPROVED: Final[str] = "approved"
PENDING: Final[str] = "pending_product_owner_approval"
_EPSILON: Final[float] = 1e-9

#: Proposed defaults, pending product-owner approval (plan: Execution decisions).
#: ``*_max`` keys may only be lowered by a repository, ``*_min`` and ``min_*``
#: keys only raised. Mirrored by ``evals/review/gate-h.yaml``.
PROPOSED_THRESHOLDS: Final[Mapping[str, float | int]] = {
    # Any disagreement between repeat runs on one input disqualifies the recipe.
    "flake_rate_max": 0.0,
    # Repeat runs per input, and inputs, before a flake rate means anything: 10 runs
    # on each of 2 inputs catch a 10 %-per-run flake with ~88 % probability (5: ~65 %).
    "min_runs_per_input": 10,
    "min_flake_inputs": 2,
    # The R5 classifier must agree with labeled ground truth on 95 % of units; 0
    # errors in 60 units bound the true error rate below 5 % at 95 % confidence.
    "classification_accuracy_min": 0.95,
    "min_labeled_units": 60,
    # Any difference between two renders of one input disqualifies the surface; 5
    # pairs catch a surface that differs on 30 % of renders with ~83 % probability.
    "visual_nondeterminism_max": 0.0,
    "min_render_pairs": 5,
}
#: Refused, never clamped: far beyond any run, input, unit or pair count a measurement can have.
_MAX_INTEGER_THRESHOLD: Final[int] = 1_000_000
_INTEGER_KEYS: Final[frozenset[str]] = frozenset(
    {"min_runs_per_input", "min_flake_inputs", "min_labeled_units", "min_render_pairs"}
)
#: Keys a repository may only raise (everything else only lowers).
_RAISE_ONLY: Final[frozenset[str]] = frozenset({*_INTEGER_KEYS, "classification_accuracy_min"})

REASONS: Final[Mapping[str, QualificationState]] = {
    "qualified": "qualified",
    "unidentified": "unmeasured",
    "unmeasured": "unmeasured",
    "measurement_unavailable": "unmeasured",
    "insufficient_measurement": "advisory",
    "flaky": "advisory",
    "classification_inaccurate": "advisory",
    "visual_nondeterministic": "advisory",
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


# --------------------------------------------------------------------------- #
# identities
# --------------------------------------------------------------------------- #


def environment_class(executor_class: str, image_digest: str) -> str:
    """``<executor class>@<image digest>``; ``""`` (unknown, never qualified) when either is missing.

    The image is the worker container digest, or the ``host:sha256:`` identity
    of a local bubblewrap host (OS release, architecture, bubblewrap version).
    Lockfile and recipe digests are deliberately not part of the class: the
    recipe digest is its own key and a lockfile changes with every dependency
    bump. Toolchain and lockfile fingerprints are recorded in each measurement.
    """

    executor, image = str(executor_class or "").strip(), str(image_digest or "").strip()
    return f"{executor}@{image}" if executor and image else ""


def classifier_key(report_format: str) -> str:
    """The R5 classifier identity for a report format (``recipe`` when the recipe has no report)."""

    from lemoncrow.pro.capabilities.review.differential import DIFFERENTIAL_VERSION

    return f"classifier:r5-v{DIFFERENTIAL_VERSION}:{report_format or 'recipe'}"


def surface_digest(entry: str, config_digest: str, browser: str, fonts_digest: str, sandbox: str) -> str:
    """One rendered surface: the R7 entry, the trusted render config and the browser/font identity."""

    if not entry or not config_digest or not browser:
        return ""
    return "surface:sha256:" + _digest(
        {"v": 1, "entry": entry, "config": config_digest, "browser": browser, "fonts": fonts_digest, "sandbox": sandbox}
    )


@dataclass(frozen=True, slots=True)
class SubjectKey:
    """What one measurement measured: a metric over one digest in one environment class."""

    metric: Metric
    digest: str
    environment_class: str = ""

    def scope(self, repository: str) -> str:
        return "qualification:" + _digest(
            {"repository": repository, "metric": self.metric, "digest": self.digest, "env": self.environment_class}
        )

    def to_wire(self) -> dict[str, str]:
        return {"metric": self.metric, "digest": self.digest, "environment_class": self.environment_class}

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> SubjectKey:
        metric = str(raw.get("metric") or "")
        if metric not in {"flake", "classification", "visual"}:
            raise ValueError(f"unknown measurement metric {metric!r}")
        digest = str(raw.get("digest") or "")
        if not digest:
            raise ValueError("a measurement names no digest")
        return cls(cast(Metric, metric), digest, str(raw.get("environment_class") or ""))


@dataclass(frozen=True, slots=True)
class QualificationSubject:
    """A LemonCrow-executed result's subject. ``components`` makes a conjunction (kind entries)."""

    kind: SubjectKind
    id: str
    digest: str = ""
    environment_class: str = ""
    classifier: str = ""
    components: tuple[QualificationSubject, ...] = ()

    def keys(self) -> tuple[SubjectKey, ...]:
        if self.kind == "surface":
            return (SubjectKey("visual", self.digest, self.environment_class),)
        return (SubjectKey("flake", self.digest, self.environment_class), SubjectKey("classification", self.classifier))

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "kind": self.kind,
            "id": self.id,
            "digest": self.digest,
            "environment_class": self.environment_class,
        }
        if self.kind == "recipe":
            wire["classifier"] = self.classifier
        if self.components:
            wire["components"] = [item.to_wire() for item in self.components]
        return wire

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> QualificationSubject:
        kind: SubjectKind = "surface" if raw.get("kind") == "surface" else "recipe"
        components = raw.get("components") if isinstance(raw.get("components"), list) else []
        assert isinstance(components, list)
        return cls(
            kind,
            str(raw.get("id") or ""),
            digest=str(raw.get("digest") or ""),
            environment_class=str(raw.get("environment_class") or ""),
            classifier=str(raw.get("classifier") or ""),
            components=tuple(cls.from_wire(item) for item in components if isinstance(item, Mapping)),
        )


# --------------------------------------------------------------------------- #
# measurements
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Measurement:
    """One measured metric. ``observations`` / ``failures`` per metric:

    * flake: measured inputs / inputs whose repeat runs disagreed;
    * classification: labeled units / misclassified units;
    * visual: render pairs / pairs whose two renders differ.

    ``unavailable`` says why nothing could be measured (it is then unmeasured).
    """

    key: SubjectKey
    subject: str
    observations: int
    failures: int
    runs: int = 0
    min_runs_per_observation: int = 0
    unavailable: str = ""
    confusion: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    breakdown: Mapping[str, Any] = field(default_factory=dict)
    fingerprints: tuple[str, ...] = ()
    cost_seconds: float = 0.0
    measured_at: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        if self.observations < 0 or self.failures < 0 or self.failures > self.observations:
            raise ValueError("a measurement needs 0 <= failures <= observations")

    @property
    def rate(self) -> float | None:
        """Failure share (flake rate, misclassification share, nondeterminism); ``None`` with nothing observed."""

        return None if self.observations == 0 else self.failures / self.observations

    @property
    def accuracy(self) -> float | None:
        return None if self.rate is None else 1.0 - self.rate

    def to_wire(self) -> dict[str, Any]:
        return {
            "v": MEASUREMENT_VERSION,
            "key": self.key.to_wire(),
            "subject": self.subject,
            "observations": self.observations,
            "failures": self.failures,
            "rate": None if self.rate is None else round(self.rate, 6),
            "runs": self.runs,
            "min_runs_per_observation": self.min_runs_per_observation,
            "unavailable": self.unavailable,
            "confusion": {row: dict(cols) for row, cols in sorted(self.confusion.items())},
            "breakdown": dict(self.breakdown),
            "fingerprints": list(self.fingerprints),
            "cost_seconds": round(self.cost_seconds, 3),
            "measured_at": self.measured_at,
            "source": self.source,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> Measurement:
        if raw.get("v") != MEASUREMENT_VERSION:
            raise ValueError(f"measurement version {raw.get('v')!r} is not supported")
        raw_key = raw.get("key")
        key = SubjectKey.from_wire(raw_key if isinstance(raw_key, Mapping) else {})
        confusion = raw.get("confusion") if isinstance(raw.get("confusion"), Mapping) else {}
        assert isinstance(confusion, Mapping)

        def count(name: str) -> int:
            value = raw.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"measurement {name} must be a non-negative integer")
            return value

        cost = raw.get("cost_seconds", 0.0)
        return cls(
            key=key,
            subject=str(raw.get("subject") or "")[:300],
            observations=count("observations"),
            failures=count("failures"),
            runs=count("runs"),
            min_runs_per_observation=count("min_runs_per_observation"),
            unavailable=str(raw.get("unavailable") or "")[:500],
            confusion={
                str(row): {str(col): int(value) for col, value in cols.items() if isinstance(value, int)}
                for row, cols in confusion.items()
                if isinstance(cols, Mapping)
            },
            breakdown=dict(raw["breakdown"]) if isinstance(raw.get("breakdown"), Mapping) else {},
            fingerprints=tuple(str(item) for item in raw.get("fingerprints") or () if isinstance(item, str))[:32],
            cost_seconds=float(cost) if isinstance(cost, (int, float)) and math.isfinite(cost) else 0.0,
            measured_at=str(raw.get("measured_at") or ""),
            source=str(raw.get("source") or "")[:300],
        )


MeasurementLookup = Callable[[SubjectKey], "Measurement | None"]


def merge_measurements(measurements: Iterable[Measurement]) -> list[Measurement]:
    """One measurement per key: counts, runs, confusion and cost are summed across corpora.

    A classifier measured on two corpora is one classifier; so is a recipe
    measured on two input sets. Only measured observations count: an
    unavailable part contributes nothing, and the merge is unavailable only
    when every part was.
    """

    grouped: dict[SubjectKey, list[Measurement]] = {}
    for item in measurements:
        grouped.setdefault(item.key, []).append(item)
    out: list[Measurement] = []
    for key, items in grouped.items():
        if len(items) == 1:
            out.append(items[0])
            continue
        measured = [item for item in items if not item.unavailable]
        confusion: dict[str, dict[str, int]] = {}
        for item in measured:
            for expected, row in item.confusion.items():
                target = confusion.setdefault(expected, {})
                for predicted, count in row.items():
                    target[predicted] = target.get(predicted, 0) + count
        minimums = [item.min_runs_per_observation for item in measured if item.observations]
        out.append(
            Measurement(
                key=key,
                subject=items[0].subject,
                observations=sum(item.observations for item in measured),
                failures=sum(item.failures for item in measured),
                runs=sum(item.runs for item in items),
                min_runs_per_observation=min(minimums) if minimums else 0,
                unavailable="" if measured else "; ".join(dict.fromkeys(item.unavailable for item in items))[:500],
                confusion=confusion,
                breakdown={"merged": len(items)},
                fingerprints=tuple(sorted({fp for item in items for fp in item.fingerprints}))[:32],
                cost_seconds=sum(item.cost_seconds for item in items),
                measured_at=max(item.measured_at for item in items),
                source="; ".join(dict.fromkeys(item.source for item in items if item.source))[:300],
            )
        )
    return out


# --------------------------------------------------------------------------- #
# thresholds and the Gate H configuration
# --------------------------------------------------------------------------- #


def _same_number(recorded: Any, value: float | int) -> bool:
    return (
        isinstance(recorded, (int, float))
        and not isinstance(recorded, bool)
        and math.isfinite(recorded)
        and float(recorded) == float(value)
    )


@dataclass(frozen=True, slots=True)
class QualificationThresholds:
    flake_rate_max: float
    min_runs_per_input: int
    min_flake_inputs: int
    classification_accuracy_min: float
    min_labeled_units: int
    visual_nondeterminism_max: float
    min_render_pairs: int
    approval_status: str = PENDING
    approved_by: str = ""
    approved_at: str = ""
    #: :attr:`digest` of the values the owner approved; approval holds only while it matches (Gate I's pattern).
    approved_digest: str = ""
    #: The approved values themselves, to name what changed since approval.
    approved_values: Mapping[str, Any] | None = None

    @property
    def approved(self) -> bool:
        """Status approved, the digest equal to the current values' and any recorded map equal to them too.

        The digest is integrity, not authorization: ``approved_by`` and ``approved_at`` are free text, and
        anyone who can edit this file can write a digest. What it guarantees is that a threshold edited
        after the owner approved, without the approval being rewritten with it, reads unapproved.
        """

        return (
            self.approval_status == APPROVED
            and bool(self.approved_digest)
            and self.approved_digest == self.digest
            and not self.changed_since_approval
        )

    @property
    def changed_since_approval(self) -> tuple[str, ...]:
        """Thresholds whose value differs from the recorded approved map (empty without a map)."""

        if self.approval_status != APPROVED or self.approved_values is None:
            return ()
        recorded = self.approved_values
        return tuple(name for name, value in self.values().items() if not _same_number(recorded.get(name), value))

    def approval_problem(self) -> str | None:
        """Why these thresholds are not approved, or ``None`` when they are."""

        if self.approved:
            return None
        if self.approval_status != APPROVED:
            return "thresholds are proposed defaults pending product-owner approval"
        if not self.approved_digest:
            return (
                "the threshold approval records no thresholds_digest (it predates binding approval to the values), "
                "so the thresholds count as unapproved"
            )
        changed = self.changed_since_approval
        if changed:
            recorded = self.approved_values or {}
            current = self.values()
            detail = ", ".join(f"{name} (approved {recorded.get(name)!r}, now {current[name]!r})" for name in changed)
            if self.approved_digest == self.digest:
                return (
                    f"thresholds.approval.thresholds disagrees with the thresholds under a matching digest: {detail}; "
                    "they count as unapproved"
                )
            return f"thresholds changed since approval: {detail}; they count as unapproved"
        return "thresholds.approval.thresholds_digest does not match the thresholds, so they count as unapproved"

    def values(self) -> dict[str, float | int]:
        return {name: getattr(self, name) for name in PROPOSED_THRESHOLDS}

    def to_wire(self) -> dict[str, Any]:
        return {
            "approval": {
                "status": self.approval_status,
                "approved_by": self.approved_by or None,
                "approved_at": self.approved_at or None,
                "thresholds": None if self.approved_values is None else dict(self.approved_values),
                "thresholds_digest": self.approved_digest or None,
            },
            "approved": self.approved,
            "changed_since_approval": list(self.changed_since_approval),
            "digest": self.digest,
            **self.values(),
        }

    @property
    def digest(self) -> str:
        return "thresholds:sha256:" + _digest(self.values())


def default_thresholds() -> QualificationThresholds:
    return QualificationThresholds(**{name: value for name, value in PROPOSED_THRESHOLDS.items()})  # type: ignore[arg-type]


def _threshold(raw: Mapping[str, Any], name: str, problems: list[str], *, where: str) -> float | int:
    value = raw.get(name, PROPOSED_THRESHOLDS[name])
    integer = name in _INTEGER_KEYS
    bad = isinstance(value, bool) or not isinstance(value, (int, float))
    # A float is checked for finiteness; an int never goes through math.isfinite (a 400-digit int overflows it).
    if not bad and isinstance(value, float) and not math.isfinite(value):
        bad = True
    if not bad and (value < 0 or (integer and (value > _MAX_INTEGER_THRESHOLD or int(value) != value))):
        bad = True
    if not bad and name.endswith(("_max", "_min")) and value > 1:
        bad = True
    if bad:
        kind = "a non-negative integer" if integer else "a number from 0 to 1"
        problems.append(f"{where}.{name} must be {kind}")
        return PROPOSED_THRESHOLDS[name]
    return int(value) if integer else float(value)


@dataclass(frozen=True, slots=True)
class GateHConfig:
    """The versioned Gate H configuration: the report/enforce switch and the threshold floor."""

    mode: GateHMode
    thresholds: QualificationThresholds
    source: str = "built-in defaults"
    root: Path | None = None
    corpus: str = "gate-h/corpus"
    results: str = "gate-h/results"
    baseline: str = "gate-h/baseline.json"

    def to_wire(self) -> dict[str, Any]:
        return {"mode": self.mode, "source": self.source, "thresholds": self.thresholds.to_wire()}


DEFAULT_GATE_H_CONFIG: Final[GateHConfig] = GateHConfig(mode="report", thresholds=default_thresholds())


class GateHError(ValueError):
    pass


def parse_gate_h_config(raw: object, *, source: str, root: Path | None = None) -> GateHConfig:
    if not isinstance(raw, Mapping):
        raise GateHError(f"{source} must be a mapping")
    problems: list[str] = []
    if raw.get("version") != GATE_H_VERSION:
        problems.append(f"version must be {GATE_H_VERSION}")
    mode = raw.get("mode")
    if mode not in {"report", "enforce"}:
        problems.append("mode must be report or enforce")
        mode = "report"
    thresholds_raw = raw.get("thresholds") if isinstance(raw.get("thresholds"), Mapping) else {}
    assert isinstance(thresholds_raw, Mapping)
    approval = thresholds_raw.get("approval") if isinstance(thresholds_raw.get("approval"), Mapping) else {}
    assert isinstance(approval, Mapping)
    status = str(approval.get("status") or PENDING)
    if status not in {APPROVED, PENDING}:
        problems.append(f"thresholds.approval.status must be {APPROVED} or {PENDING}")
    approved_by, approved_at = str(approval.get("approved_by") or ""), str(approval.get("approved_at") or "")
    if status == APPROVED and (not approved_by or not approved_at):
        problems.append("approved thresholds must name approved_by and approved_at")
    approved_digest = approval.get("thresholds_digest")
    if approved_digest is not None and not isinstance(approved_digest, str):
        problems.append("thresholds.approval.thresholds_digest must be the approved values' digest or null")
    approved_values = approval.get("thresholds")
    if approved_values is not None and not isinstance(approved_values, Mapping):
        problems.append("thresholds.approval.thresholds must map each threshold to its approved value, or be null")
    unknown = sorted(set(thresholds_raw) - {*PROPOSED_THRESHOLDS, "approval"})
    if unknown:
        problems.append(f"thresholds has unknown keys: {', '.join(map(str, unknown))}")
    values = {name: _threshold(thresholds_raw, name, problems, where="thresholds") for name in PROPOSED_THRESHOLDS}
    thresholds = QualificationThresholds(
        **values,  # type: ignore[arg-type]
        approval_status=status,
        approved_by=approved_by,
        approved_at=approved_at,
        approved_digest=approved_digest if isinstance(approved_digest, str) else "",
        approved_values=dict(approved_values) if isinstance(approved_values, Mapping) else None,
    )
    if mode == "enforce" and not thresholds.approved:
        if status == APPROVED:
            problems.append(
                f"mode: enforce requires approved thresholds, but {thresholds.approval_problem()}. Have the product "
                "owner re-approve them: set thresholds.approval.thresholds to "
                f"{json.dumps(thresholds.values(), sort_keys=True)} and thresholds.approval.thresholds_digest to "
                f"{thresholds.digest}, with approved_by and approved_at (or keep mode: report)"
            )
        else:
            problems.append(
                "mode: enforce requires product-owner-approved thresholds; keep mode: report until they are approved"
            )
    if problems:
        raise GateHError(f"{source} is invalid:\n  - " + "\n  - ".join(problems))
    return GateHConfig(
        mode=cast(GateHMode, mode),
        thresholds=thresholds,
        source=source,
        root=root,
        corpus=str(raw.get("corpus") or "gate-h/corpus"),
        results=str(raw.get("results") or "gate-h/results"),
        baseline=str(raw.get("baseline") or "gate-h/baseline.json"),
    )


def load_gate_h_config(root: Path) -> GateHConfig:
    """``<root>/gate-h.yaml`` (``root`` is the ``evals/review`` directory)."""

    path = root / GATE_H_CONFIG
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:  # ValueError: not UTF-8
        raise GateHError(f"{path}: {type(exc).__name__}: {exc}") from exc
    return parse_gate_h_config(raw, source=str(path), root=root)


def source_checkout_gate_dir() -> Path:
    """``evals/review`` of the source checkout this module was loaded from (Gate I's configuration lives there)."""

    return Path(__file__).resolve().parents[5] / "evals" / "review"


def packaged_contract_path() -> Path:
    """The Gate H contract shipped beside this module, in every install (wheel, Review engine, hosted server)."""

    return Path(__file__).resolve().with_name(CONTRACT_RESOURCE)


def load_packaged_gate_h_config() -> GateHConfig:
    """The packaged contract, parsed and validated; :class:`GateHError` when missing or invalid."""

    path = packaged_contract_path()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:  # ValueError: not UTF-8
        raise GateHError(f"{path}: {type(exc).__name__}: {exc}") from exc
    return parse_gate_h_config(raw, source=str(path))


@lru_cache(maxsize=1)
def runtime_gate_h_config() -> GateHConfig:
    """The Gate H configuration readiness runs under: the packaged contract.

    The same file in a source checkout, an installed wheel, the Review engine and
    the hosted server (it ships as package data; no path outside the package is
    read). A missing, unreadable or invalid contract is report mode with the
    built-in defaults, labelled ``built-in defaults (...)`` so diagnostics report
    the gate unknown: a broken switch never enforces.
    """

    try:
        return load_packaged_gate_h_config()
    except Exception as exc:  # any parse failure: a broken switch is report mode, never a crash and never enforce
        return replace(DEFAULT_GATE_H_CONFIG, source=f"built-in defaults ({type(exc).__name__}: {str(exc)[:300]})")


@dataclass(frozen=True, slots=True)
class ThresholdResolution:
    """Effective thresholds: the Gate H floor, tightened by the trusted-base repository override."""

    thresholds: QualificationThresholds
    floor: QualificationThresholds
    override: Mapping[str, float | int]
    notes: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {
            "effective": self.thresholds.to_wire(),
            "floor": self.floor.to_wire(),
            "override": dict(self.override),
            "notes": list(self.notes),
        }


def parse_repository_override(config_content: str) -> tuple[dict[str, float | int], tuple[str, ...]]:
    """``check_policy.qualification`` of the trusted-base ``.lemoncrow/review.yaml``."""

    if not config_content.strip():
        return {}, ()
    try:
        decoded = yaml.safe_load(config_content)
    except yaml.YAMLError:
        return {}, ()  # reported where the review config is parsed
    policy = decoded.get("check_policy") if isinstance(decoded, Mapping) else None
    raw = policy.get("qualification") if isinstance(policy, Mapping) else None
    if raw is None:
        return {}, ()
    if not isinstance(raw, Mapping):
        return {}, ("check_policy.qualification must be a mapping",)
    problems: list[str] = []
    unknown = sorted(str(key) for key in raw if key not in PROPOSED_THRESHOLDS)
    if unknown:
        problems.append(f"check_policy.qualification has unknown keys: {', '.join(unknown)}")
    out: dict[str, float | int] = {}
    for name in PROPOSED_THRESHOLDS:
        if name in raw:
            before = len(problems)
            value = _threshold(raw, name, problems, where="check_policy.qualification")
            if len(problems) == before:
                out[name] = value
    return out, tuple(problems)


def resolve_thresholds(floor: QualificationThresholds, override: Mapping[str, float | int]) -> ThresholdResolution:
    """Apply a repository override: tighten-only; a looser value keeps the floor and says so."""

    values = floor.values()
    notes: list[str] = []
    for name, value in override.items():
        if name not in values:
            continue
        current = values[name]
        tighter = value >= current if name in _RAISE_ONLY else value <= current
        if tighter:
            values[name] = value
        else:
            notes.append(
                f"check_policy.qualification.{name}={value} would loosen the Gate H floor ({current}); the floor applies"
            )
    effective = replace(floor, **values)  # type: ignore[arg-type]
    if floor.approved:
        # A tighter value is within what the owner approved (a repository may only tighten the floor),
        # so the approval carries over; a floor that is not approved keeps its stale digest and stays so.
        effective = replace(effective, approved_digest=effective.digest, approved_values=None)
    return ThresholdResolution(effective, floor, dict(override), tuple(notes))


# --------------------------------------------------------------------------- #
# qualification
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Qualification:
    state: QualificationState
    reason: str
    detail: str
    subject: QualificationSubject
    measurements: tuple[Measurement, ...] = ()

    @property
    def qualified(self) -> bool:
        return self.state == "qualified"

    def to_wire(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "subject": self.subject.to_wire(),
            "measurements": [
                {
                    "metric": item.key.metric,
                    "observations": item.observations,
                    "failures": item.failures,
                    "rate": None if item.rate is None else round(item.rate, 6),
                    "measured_at": item.measured_at,
                    "source": item.source,
                }
                for item in self.measurements
            ],
        }


def _verdict(reason: str, detail: str, subject: QualificationSubject, found: Sequence[Measurement]) -> Qualification:
    return Qualification(REASONS[reason], reason, detail, subject, tuple(found))


def _exceeds(failures: int, observations: int, maximum: float) -> bool:
    """``failures / observations > maximum``, exact for the integer counts involved."""

    return failures > maximum * observations + _EPSILON


def _metric(
    key: SubjectKey,
    found: Measurement | None,
    thresholds: QualificationThresholds,
) -> tuple[str, str]:
    """``(reason, detail)`` for one metric; ``("qualified", "")`` when it meets the thresholds."""

    label = {"flake": "repeat-run", "classification": "classification", "visual": "render-determinism"}[key.metric]
    if found is None:
        return "unmeasured", f"no Gate H {label} measurement exists for this subject"
    if found.unavailable:
        return "measurement_unavailable", f"the {label} measurement could not run: {found.unavailable}"
    if key.metric == "flake":
        if (
            found.observations < thresholds.min_flake_inputs
            or found.min_runs_per_observation < thresholds.min_runs_per_input
        ):
            return "insufficient_measurement", (
                f"{found.observations} input(s) x at least {found.min_runs_per_observation} run(s) measured; "
                f"needs {thresholds.min_flake_inputs} x {thresholds.min_runs_per_input}"
            )
        if _exceeds(found.failures, found.observations, thresholds.flake_rate_max):
            return "flaky", (
                f"flaky: repeat runs disagreed on {found.failures}/{found.observations} input(s) "
                f"(flake rate {found.failures / found.observations:.0%}, max {thresholds.flake_rate_max:.0%})"
            )
        return "qualified", ""
    if key.metric == "classification":
        if found.observations < thresholds.min_labeled_units:
            return "insufficient_measurement", (
                f"{found.observations} labeled unit(s) measured; needs {thresholds.min_labeled_units}"
            )
        if _exceeds(found.failures, found.observations, 1.0 - thresholds.classification_accuracy_min):
            accuracy = 1.0 - found.failures / found.observations
            return "classification_inaccurate", (
                f"base/head classification accuracy {accuracy:.1%} on {found.observations} labeled unit(s) "
                f"(min {thresholds.classification_accuracy_min:.0%})"
            )
        return "qualified", ""
    if found.observations < thresholds.min_render_pairs:
        return "insufficient_measurement", (
            f"{found.observations} render pair(s) measured; needs {thresholds.min_render_pairs}"
        )
    if _exceeds(found.failures, found.observations, thresholds.visual_nondeterminism_max):
        return "visual_nondeterministic", (
            f"rendering the same input twice differed in {found.failures}/{found.observations} pair(s) "
            f"(max {thresholds.visual_nondeterminism_max:.0%})"
        )
    return "qualified", ""


_PRECEDENCE: Final[tuple[str, ...]] = (
    "flaky",
    "visual_nondeterministic",
    "classification_inaccurate",
    "insufficient_measurement",
    "measurement_unavailable",
    "unmeasured",
    "unidentified",
)


def qualify(
    subject: QualificationSubject, lookup: MeasurementLookup, thresholds: QualificationThresholds
) -> Qualification:
    """``qualified`` only when every metric the subject needs is measured and within thresholds."""

    if subject.components:
        parts = [qualify(item, lookup, thresholds) for item in subject.components]
        if all(item.qualified for item in parts):
            measured_all = tuple(m for item in parts for m in item.measurements)
            return _verdict("qualified", f"all {len(parts)} recipe(s) qualified", subject, measured_all)
        worst = min((item for item in parts if not item.qualified), key=lambda item: _PRECEDENCE.index(item.reason))
        return replace(worst, subject=subject, detail=f"{worst.subject.id}: {worst.detail}")
    missing = [
        name
        for name, value in (
            ("digest", subject.digest),
            ("environment class", subject.environment_class),
            *((("classifier", subject.classifier),) if subject.kind == "recipe" else ()),
        )
        if not value
    ]
    if missing:
        return _verdict(
            "unidentified",
            f"the result does not name its {', '.join(missing)}, so no measurement can apply",
            subject,
            (),
        )
    found: list[Measurement] = []
    outcomes: list[tuple[str, str]] = []
    for key in subject.keys():
        measured = lookup(key)
        if measured is not None:
            found.append(measured)
        reason, detail = _metric(key, measured, thresholds)
        if reason != "qualified":
            outcomes.append((reason, detail))
    if not outcomes:
        return _verdict("qualified", "measured within the Gate H thresholds", subject, found)
    reason, detail = min(outcomes, key=lambda row: _PRECEDENCE.index(row[0]))
    others = [text for name, text in outcomes if (name, text) != (reason, detail)]
    return _verdict(reason, "; ".join([detail, *others]), subject, found)


def qualification(
    repository: str,
    subject: QualificationSubject,
    *,
    measurements: Callable[[str, SubjectKey], Measurement | None],
    thresholds: QualificationThresholds,
) -> Qualification:
    """``qualification(repo, recipe/surface)`` -- the P10-LAND entry point."""

    return qualify(subject, lambda key: measurements(repository, key), thresholds)


@dataclass(frozen=True, slots=True)
class QualificationEffect:
    """A qualification under the Gate H mode, as rows and conditions carry it.

    ``advisory``: shown, but it neither satisfies nor blocks a readiness
    condition and never feeds auto-approval (enforce mode, not qualified).
    ``would_be_advisory``: what enforcement would do (report mode records it).
    """

    qualification: Qualification
    mode: GateHMode

    @property
    def would_be_advisory(self) -> bool:
        return not self.qualification.qualified

    @property
    def advisory(self) -> bool:
        return self.mode == "enforce" and self.would_be_advisory

    @property
    def may_gate(self) -> bool:
        """May this result satisfy or block a readiness condition, or feed auto-approval?"""

        return not self.advisory

    def to_wire(self) -> dict[str, Any]:
        return {
            **self.qualification.to_wire(),
            "mode": self.mode,
            "advisory": self.advisory,
            "would_be_advisory": self.would_be_advisory,
        }


def may_gate(annotation: object) -> bool:
    """For a wire annotation (``row["qualification"]``): ``False`` only when it is advisory now."""

    return not (isinstance(annotation, Mapping) and annotation.get("advisory") is True)


# --------------------------------------------------------------------------- #
# store: repository-scoped measurement rows
# --------------------------------------------------------------------------- #


def store_measurements(
    store: Any, *, review_id: str, revision_id: str, repository: str, measurements: Sequence[Measurement]
) -> int:
    """Write measurements as ``review_derived`` rows on one Snapshot (newest replaces older per key).

    An import of more than :data:`MAX_IMPORT_MEASUREMENTS` rows is refused whole.
    """

    from lemoncrow.pro.capabilities.review.store import DerivedResult

    if len(measurements) > MAX_IMPORT_MEASUREMENTS:
        raise ValueError(
            f"{len(measurements)} measurements exceed the import limit of {MAX_IMPORT_MEASUREMENTS}; split the import"
        )
    if not repository:
        raise ValueError("measurements need a repository")
    rows = [
        DerivedResult(
            review_id=review_id,
            revision_id=revision_id,
            kind=QUALIFICATION_KIND,
            scope_key=item.key.scope(repository),
            stamp=f"v{MEASUREMENT_VERSION}:{item.measured_at}",
            payload={"repository": repository, "measurement": item.to_wire()},
        )
        for item in measurements
    ]
    store.put_derived_results(rows)
    store.evict_derived_results(QUALIFICATION_KIND, keep=MAX_QUALIFICATION_ROWS)
    return len(rows)


def load_measurement(store: Any, repository: str, key: SubjectKey) -> Measurement | None:
    """The newest stored measurement of *key* for *repository*; unreadable rows are skipped."""

    for row in store.derived_rows_by_scope(QUALIFICATION_KIND, key.scope(repository)):
        payload = row.payload
        if not isinstance(payload, Mapping) or payload.get("repository") != repository:
            continue
        raw = payload.get("measurement")
        if not isinstance(raw, Mapping):
            continue
        try:
            measured = Measurement.from_wire(raw)
        except (TypeError, ValueError):
            continue
        if measured.key == key:
            return measured
    return None


# --------------------------------------------------------------------------- #
# one Snapshot's context and the readiness annotations
# --------------------------------------------------------------------------- #

RECIPE_ENTRY_PREFIX: Final[str] = "checks.recipe:"
#: Behavior producers whose deltas come from LemonCrow executing code (not static reads).
EXECUTED_SURFACE_PRODUCERS: Final[frozenset[str]] = frozenset(
    {
        "lemoncrow.render_diff",
        "lemoncrow.runtime_observation",
        "lemoncrow.artifact_analyses",
        "lemoncrow.runtime_validation",
        "lemoncrow.browser_checks",
    }
)
RECIPE_PRODUCER: Final[str] = "lemoncrow.check_recipes"


class QualificationContext:
    """Thresholds, mode and memoized measurement lookups for one Snapshot evaluation."""

    def __init__(
        self,
        *,
        mode: GateHMode,
        resolution: ThresholdResolution,
        lookup: MeasurementLookup,
        source: str = "",
    ) -> None:
        self.mode = mode
        self.resolution = resolution
        self.source = source
        self._lookup = lookup
        self._measured: dict[SubjectKey, Measurement | None] = {}
        self._qualified: dict[QualificationSubject, Qualification] = {}

    @property
    def thresholds(self) -> QualificationThresholds:
        return self.resolution.thresholds

    def measurement(self, key: SubjectKey) -> Measurement | None:
        if key not in self._measured:
            try:
                self._measured[key] = self._lookup(key)
            except Exception as exc:  # an unreadable measurement store is unmeasured, never qualified
                self._measured[key] = Measurement(
                    key=key,
                    subject="",
                    observations=0,
                    failures=0,
                    unavailable=f"the measurement store could not be read ({type(exc).__name__})",
                )
        return self._measured[key]

    def effect(self, subject: QualificationSubject) -> QualificationEffect:
        if subject not in self._qualified:
            self._qualified[subject] = qualify(subject, self.measurement, self.thresholds)
        return QualificationEffect(self._qualified[subject], self.mode)

    def summary(self, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        annotations = [row for row in rows if isinstance(row, Mapping)]
        return {
            "gate": "H",
            "mode": self.mode,
            "source": self.source,
            "subjects": len(annotations),
            "advisory": sum(1 for row in annotations if row.get("advisory") is True),
            "would_be_advisory": sum(1 for row in annotations if row.get("would_be_advisory") is True),
            "thresholds": self.resolution.to_wire(),
        }


def build_context(
    *,
    store: Any,
    repository: str,
    review_config: str,
    gate: GateHConfig | None = None,
) -> QualificationContext:
    """The context readiness and projections share: runtime Gate H config, trusted override, store rows."""

    config = gate or runtime_gate_h_config()
    override, problems = parse_repository_override(review_config)
    resolution = resolve_thresholds(config.thresholds, override)
    if problems:
        resolution = replace(resolution, notes=(*problems, *resolution.notes))
    return QualificationContext(
        mode=config.mode,
        resolution=resolution,
        lookup=lambda key: load_measurement(store, repository, key),
        source=config.source,
    )


RecordReader = Callable[[str], "Mapping[str, Any] | None"]
ReportFormat = Callable[[str, str], "str | None"]
"""``(recipe id, recipe digest) -> report format`` from the trusted catalog; ``None`` when unknown."""


def read_json_artifact(store: Any, item: Any) -> dict[str, Any] | None:
    """A stored JSON evidence record, only when its bytes match the recorded digest."""

    try:
        payload = store.read_evidence_artifact(item)
    except (OSError, RuntimeError, ValueError):
        return None
    if payload is None or not item.content_hash or hashlib.sha256(payload).hexdigest() != item.content_hash:
        return None
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def is_lemoncrow_row(row: Mapping[str, Any]) -> bool:
    """A result LemonCrow executed (CI, provider, reported and human rows are not Gate H subjects)."""

    return str(row.get("attestation_class") or "") == "lemoncrow_verification"


def _row_environment(row: Mapping[str, Any], record: Mapping[str, Any] | None) -> str:
    execution = row.get("execution") if isinstance(row.get("execution"), Mapping) else {}
    assert isinstance(execution, Mapping)
    image = str(execution.get("image_digest") or "")
    if not image and record is not None:
        data = record.get("data") if isinstance(record.get("data"), Mapping) else {}
        assert isinstance(data, Mapping)
        run = data.get("execution") if isinstance(data.get("execution"), Mapping) else {}
        assert isinstance(run, Mapping)
        verification = run.get("verification") if isinstance(run.get("verification"), Mapping) else {}
        assert isinstance(verification, Mapping)
        image = str(verification.get("image_digest") or "")
    return environment_class(str(row.get("executor_class") or execution.get("executor_class") or ""), image)


def _recipe_rows(record: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    data = record.get("data") if record is not None and isinstance(record.get("data"), Mapping) else {}
    assert isinstance(data, Mapping)
    out: list[tuple[str, str]] = []
    for item in data.get("recipes") or ():
        if isinstance(item, Mapping) and str(item.get("id") or ""):
            out.append((str(item["id"]), str(item.get("digest") or "")))
    return out


def verification_subjects(
    rows: Sequence[Mapping[str, Any]], *, read_record: RecordReader, report_format: ReportFormat
) -> dict[int, QualificationSubject]:
    """``index -> subject`` for every LemonCrow-executed verification row (others are not Gate H subjects).

    A recipe verdict (``checks.recipe:<id>``) names its recipe digest and head
    execution; a kind entry (``checks.test``) is the conjunction of the recipe
    rows of its run. A browser scenario or any other LemonCrow-executed row is
    a surface Gate H has not measured (no digest): unmeasured.
    """

    out: dict[int, QualificationSubject] = {}
    recipe_subjects: dict[str, QualificationSubject] = {}
    composites: list[tuple[int, str, list[tuple[str, str]]]] = []
    for index, row in enumerate(rows):
        if str(row.get("state") or "") not in {"current", "stale"} or not is_lemoncrow_row(row):
            continue
        stable_key = str(row.get("stable_key") or "")
        if row.get("browser"):
            browser = row.get("browser") if isinstance(row.get("browser"), Mapping) else {}
            assert isinstance(browser, Mapping)
            out[index] = QualificationSubject("surface", f"browser:{browser.get('scenario') or row.get('name') or ''}")
            continue
        evidence_id = str(row.get("evidence_id") or "")
        record = read_record(evidence_id) if evidence_id else None
        recipes = _recipe_rows(record)
        if stable_key.startswith(RECIPE_ENTRY_PREFIX):
            recipe_id = stable_key[len(RECIPE_ENTRY_PREFIX) :]
            digest = next((value for rid, value in recipes if rid == recipe_id), "")
            fmt = report_format(recipe_id, digest) if digest else None
            subject = QualificationSubject(
                "recipe",
                stable_key,
                digest=digest,
                environment_class=_row_environment(row, record),
                classifier=classifier_key(fmt) if fmt is not None else "",
            )
            out[index] = subject
            if row.get("state") == "current":
                recipe_subjects.setdefault(recipe_id, subject)
        elif stable_key.startswith("checks.") and recipes:
            composites.append((index, stable_key, recipes))
        else:
            out[index] = QualificationSubject("surface", stable_key or str(row.get("name") or ""))
    for index, stable_key, recipes in composites:
        components = tuple(
            recipe_subjects.get(recipe_id) or QualificationSubject("recipe", f"{RECIPE_ENTRY_PREFIX}{recipe_id}")
            for recipe_id, _digest_value in recipes
        )
        out[index] = QualificationSubject("recipe", stable_key, components=components)
    return out


def annotate_verification(
    projection: dict[str, Any],
    context: QualificationContext,
    *,
    read_record: RecordReader,
    report_format: ReportFormat,
) -> dict[str, Any]:
    """Stamp ``qualification`` on every LemonCrow-executed row and a Gate H summary on the projection.

    Readiness reads the stamps through :func:`readiness.verification_gate`;
    nothing else changes the observed statuses.
    """

    rows = projection.get("checks")
    if not isinstance(rows, list):
        return projection
    subjects = verification_subjects(rows, read_record=read_record, report_format=report_format)
    if not subjects:
        return projection
    annotations: list[dict[str, Any]] = []
    for index, subject in subjects.items():
        wire = context.effect(subject).to_wire()
        rows[index] = {**rows[index], "qualification": wire}
        annotations.append(wire)
    projection["qualification"] = context.summary(annotations)
    return projection


def behavior_subject(
    delta: Any,
    *,
    recipe_rows: Mapping[str, QualificationSubject],
    render_subject: Callable[[str], QualificationSubject | None],
) -> QualificationSubject | None:
    """The Gate H subject of a behavior delta; ``None`` for non-executed (static) producers."""

    producer = str(getattr(delta, "producer", "") or "")
    targets = tuple(getattr(delta, "target_ids", ()) or ())
    first = str(targets[0]) if targets else ""
    if producer == RECIPE_PRODUCER:
        return recipe_rows.get(first) or QualificationSubject("recipe", first or "checks.recipe:?")
    if producer not in EXECUTED_SURFACE_PRODUCERS:
        return None
    if producer == "lemoncrow.render_diff":
        for ref in getattr(delta, "evidence", ()) or ():
            evidence_id = str(getattr(ref, "evidence_id", "") or "")
            if evidence_id and getattr(ref, "role", "") == "record":
                found = render_subject(evidence_id)
                if found is not None:
                    return found
    return QualificationSubject("surface", f"{producer}:{first}" if first else producer)


def render_qualification_subject(record: Mapping[str, Any] | None) -> QualificationSubject | None:
    """The surface subject an R7 render diff record states (``qualification_subject``)."""

    if record is None:
        return None
    raw = record.get("qualification_subject")
    if not isinstance(raw, Mapping):
        return None
    return QualificationSubject(
        "surface",
        str(raw.get("id") or record.get("entry") or ""),
        digest=str(raw.get("digest") or ""),
        environment_class=str(raw.get("environment_class") or ""),
    )


def render_subject_wire(
    entry: str,
    config_digest: str,
    *,
    browser: str,
    fonts_digest: str,
    sandbox: str,
    executor_class: str,
    image_digest: str,
) -> dict[str, str]:
    """What an R7 render diff record states about its surface (same function the harness measures with)."""

    return {
        "id": entry,
        "digest": surface_digest(entry, config_digest, browser, fonts_digest, sandbox),
        "environment_class": environment_class(executor_class, image_digest),
    }


@dataclass(frozen=True, slots=True)
class BehaviorQualification:
    """Behavior target counts with Gate H applied, and what enforcement would count."""

    open: int
    blocked: int
    advisory: int
    would_be_open: int
    would_be_blocked: int
    would_be_advisory: int
    mode: GateHMode
    subjects: tuple[dict[str, Any], ...] = ()


def partition_behavior(
    targets: Sequence[Any],
    context: QualificationContext,
    *,
    recipe_rows: Mapping[str, QualificationSubject],
    render_subject: Callable[[str], QualificationSubject | None],
) -> BehaviorQualification:
    """Outstanding / blocked behavior targets that may gate now, and under enforcement."""

    counts = {"open": 0, "blocked": 0, "advisory": 0, "w_open": 0, "w_blocked": 0, "w_advisory": 0}
    seen: dict[QualificationSubject, dict[str, Any]] = {}
    for target in targets:
        delta = target.delta
        subject = behavior_subject(delta, recipe_rows=recipe_rows, render_subject=render_subject)
        effect = context.effect(subject) if subject is not None else None
        outstanding = bool(target.priority.outstanding)
        blocked = target.state == "needs_changes"
        if not (outstanding or blocked):
            continue
        if effect is not None:
            seen.setdefault(effect.qualification.subject, effect.to_wire())
        if effect is not None and effect.advisory:
            counts["advisory"] += 1
        else:
            counts["open"] += int(outstanding)
            counts["blocked"] += int(blocked)
        if effect is not None and effect.would_be_advisory:
            counts["w_advisory"] += 1
        else:
            counts["w_open"] += int(outstanding)
            counts["w_blocked"] += int(blocked)
    return BehaviorQualification(
        open=counts["open"],
        blocked=counts["blocked"],
        advisory=counts["advisory"],
        would_be_open=counts["w_open"],
        would_be_blocked=counts["w_blocked"],
        would_be_advisory=counts["w_advisory"],
        mode=context.mode,
        subjects=tuple(seen.values()),
    )


__all__ = [
    "APPROVED",
    "DEFAULT_GATE_H_CONFIG",
    "EXECUTED_SURFACE_PRODUCERS",
    "GATE_H_CONFIG",
    "MAX_IMPORT_MEASUREMENTS",
    "MAX_QUALIFICATION_ROWS",
    "PENDING",
    "PROPOSED_THRESHOLDS",
    "QUALIFICATION_KIND",
    "BehaviorQualification",
    "GateHConfig",
    "GateHError",
    "GateHMode",
    "Measurement",
    "Qualification",
    "QualificationContext",
    "QualificationEffect",
    "QualificationState",
    "QualificationSubject",
    "QualificationThresholds",
    "SubjectKey",
    "ThresholdResolution",
    "annotate_verification",
    "behavior_subject",
    "build_context",
    "classifier_key",
    "default_thresholds",
    "environment_class",
    "load_gate_h_config",
    "load_measurement",
    "may_gate",
    "merge_measurements",
    "parse_gate_h_config",
    "parse_repository_override",
    "partition_behavior",
    "qualification",
    "qualify",
    "render_qualification_subject",
    "render_subject_wire",
    "resolve_thresholds",
    "runtime_gate_h_config",
    "store_measurements",
    "surface_digest",
    "verification_subjects",
]

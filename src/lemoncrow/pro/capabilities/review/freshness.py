"""Evidence freshness classes for Review readiness (Phase 10 items 12-13).

Snapshot identity answers "was this observed on the code under review?"; it
does not answer "is this observation still true?". Three freshness classes
keep the two questions apart:

``snapshot_intrinsic``
    Immutable facts about the exact build (SBOM, provenance). They stay current
    for the Snapshot they describe, forever.
``execution_bound``
    Results of executing the exact Snapshot (CI, LemonCrow runtime validation,
    test runs, human observations of it). Current for that Snapshot; a newer
    Snapshot makes them stale through the existing revision binding.
``external_observation``
    Observations of a world that changes outside Git: vulnerability/advisory
    scans, typed ``trusted_security_feed`` attestations, ephemeral deployments.
    They carry a source timestamp (and, when known, a source/database
    revision) and expire after a configurable max-age or an explicit
    ``valid_until``. Expiry makes the observation UNKNOWN for readiness, never
    a silent PASS, even when the Snapshot is unchanged.

The module is pure: the clock is always an argument, so recomputing readiness
for the same inputs and instant yields the same answer.

Durable inputs (no new schema): the source timestamp is ``ReviewEvidence.
observed_at`` (falling back to ``created_at``); ``source_revision`` and
``valid_until`` live in the evidence's JSON ``detail`` metadata, the same
document supply-chain uploads already persist. A source timestamp later than
the moment LemonCrow recorded the row is clamped to that moment, so a writer
cannot extend validity by post-dating an observation.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence

FreshnessClass = Literal["snapshot_intrinsic", "execution_bound", "external_observation"]
FreshnessState = Literal["current", "expired", "unknown"]

FRESHNESS_CLASSES: Final[tuple[str, ...]] = ("snapshot_intrinsic", "execution_bound", "external_observation")
#: Built-in max-age for external observations when no policy names one.
DEFAULT_EXTERNAL_MAX_AGE_SECONDS: Final[int] = 24 * 3600
#: Policy may tighten freely but never configure an observation to live longer than this.
MAX_EXTERNAL_MAX_AGE_SECONDS: Final[int] = 90 * 24 * 3600

_INTRINSIC_KINDS: Final[frozenset[str]] = frozenset({"sbom", "provenance"})
_EXTERNAL_KINDS: Final[frozenset[str]] = frozenset({"vulnerability_scan", "live_preview", "deployment"})
_EXTERNAL_CLASSES: Final[frozenset[str]] = frozenset({"trusted_security_feed"})
_DURATION = re.compile(r"^\s*(\d{1,7})\s*([smhd]?)\s*$")
_UNIT_SECONDS: Final[dict[str, int]] = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_timestamp(value: object) -> datetime | None:
    """Aware UTC instant from an ISO-8601 string; ``None`` when unreadable or naive."""

    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        # A naive timestamp names no instant; never guess its zone.
        return None
    return parsed.astimezone(UTC)


def format_timestamp(value: datetime) -> str:
    """Canonical second-precision UTC form used in readiness details and wire."""

    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def freshness_class(evidence: ReviewEvidence) -> FreshnessClass:
    """Classify one evidence row by what its truth depends on.

    Classification is by evidence kind and typed attestation class only; a
    writer cannot opt an advisory scan out of expiry through display metadata.
    """

    if evidence.attestation_class in _EXTERNAL_CLASSES or evidence.kind in _EXTERNAL_KINDS:
        return "external_observation"
    if evidence.kind in _INTRINSIC_KINDS:
        return "snapshot_intrinsic"
    return "execution_bound"


def evidence_metadata(evidence: ReviewEvidence) -> Mapping[str, Any]:
    """The JSON ``detail`` document of a row, or ``{}`` when detail is prose."""

    detail = evidence.detail.strip()
    if not detail.startswith("{"):
        return {}
    try:
        decoded = json.loads(detail)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, Mapping) else {}


def observation_time(evidence: ReviewEvidence) -> datetime | None:
    """When the observed world was in the reported state, clamped to receipt time.

    Order of authority: metadata ``source_timestamp``, typed ``observed_at``,
    then ``created_at``. Never later than ``created_at``.
    """

    received = parse_timestamp(evidence.created_at)
    metadata = evidence_metadata(evidence)
    claimed = parse_timestamp(metadata.get("source_timestamp")) or parse_timestamp(evidence.observed_at)
    if claimed is None:
        return received
    if received is not None and claimed > received:
        return received
    return claimed


def observation_order_key(evidence: ReviewEvidence) -> tuple[str, str, str]:
    """Newest-observation-wins ordering: source time, then receipt time, then id."""

    observed = observation_time(evidence)
    return (format_timestamp(observed) if observed is not None else "", evidence.created_at, evidence.id)


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    """Max-age for external observations. Combining policies keeps the strictest."""

    external_max_age_seconds: int = DEFAULT_EXTERNAL_MAX_AGE_SECONDS
    source: str = "built_in"
    error: str = ""

    def stricter(self, other: FreshnessPolicy | None) -> FreshnessPolicy:
        if other is None:
            return self
        winner = other if other.external_max_age_seconds < self.external_max_age_seconds else self
        errors = "; ".join(item for item in (self.error, other.error) if item)
        return FreshnessPolicy(
            external_max_age_seconds=winner.external_max_age_seconds,
            source=winner.source,
            error=errors,
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "external_max_age_seconds": self.external_max_age_seconds,
            "source": self.source,
            "error": self.error,
        }


def parse_duration_seconds(value: object) -> int:
    """``86400``, ``"86400"``, ``"90m"``, ``"12h"`` or ``"7d"`` -> seconds."""

    if isinstance(value, bool):
        raise ValueError("a duration is seconds or a number with s/m/h/d")
    if isinstance(value, int):
        seconds = value
    else:
        match = _DURATION.fullmatch(str(value or ""))
        if match is None:
            raise ValueError("a duration is seconds or a number with s/m/h/d")
        seconds = int(match.group(1)) * _UNIT_SECONDS[match.group(2)]
    if seconds <= 0 or seconds > MAX_EXTERNAL_MAX_AGE_SECONDS:
        raise ValueError(f"a max-age must be between 1 second and {MAX_EXTERNAL_MAX_AGE_SECONDS // 86400} days")
    return seconds


def parse_freshness_policy(text: str, *, source: str) -> FreshnessPolicy:
    """Read the trusted-base ``evidence_freshness`` section of ``.lemoncrow/review.yaml``.

    ``evidence_freshness.external_observation_max_age`` may only shorten the
    built-in max-age. An invalid section fails closed: max-age 0 (every
    external observation is expired) with a visible error, never the default.
    A wholly unparsable file is reported where it is parsed and yields the
    default here.
    """

    import yaml

    default = FreshnessPolicy()
    try:
        decoded = yaml.safe_load(text) if text.strip() else None
    except yaml.YAMLError:
        return default
    if not isinstance(decoded, Mapping):
        return default
    section = decoded.get("evidence_freshness")
    if section is None:
        return default

    def broken(message: str) -> FreshnessPolicy:
        return FreshnessPolicy(external_max_age_seconds=0, source=source, error=message)

    if not isinstance(section, Mapping):
        return broken("evidence_freshness must be a mapping")
    unknown = sorted(str(key) for key in section if key != "external_observation_max_age")
    if unknown:
        return broken(f"evidence_freshness has an unknown key {unknown[0]!r}")
    raw = section.get("external_observation_max_age")
    if raw is None:
        return default
    try:
        seconds = parse_duration_seconds(raw)
    except ValueError as exc:
        return broken(f"evidence_freshness.external_observation_max_age: {exc}")
    return default.stricter(FreshnessPolicy(external_max_age_seconds=seconds, source=source))


@dataclass(frozen=True, slots=True)
class EvidenceFreshness:
    """One evidence row's freshness at one instant."""

    evidence_id: str
    freshness_class: FreshnessClass
    state: FreshnessState
    observed_at: str = ""
    source_revision: str = ""
    expires_at: str = ""
    reason: str = ""
    #: The :mod:`validity` reason code of the rule that decided ``state``; not on the wire.
    reason_code: str = "snapshot_bound"

    @property
    def current(self) -> bool:
        return self.state == "current"

    def to_wire(self) -> dict[str, Any]:
        return {
            "freshness_class": self.freshness_class,
            "state": self.state,
            "observed_at": self.observed_at,
            "source_revision": self.source_revision,
            "expires_at": self.expires_at,
            "reason": self.reason,
        }


def assess_freshness(
    evidence: ReviewEvidence,
    *,
    now: datetime | str,
    policy: FreshnessPolicy | None = None,
) -> EvidenceFreshness:
    """Classify and, for external observations, expire one row at ``now``.

    Snapshot-intrinsic and execution-bound rows are always ``current`` here:
    their staleness is the revision binding's job. An external observation is
    ``unknown`` when no trustworthy source time exists, and ``expired`` once
    ``now`` reaches ``min(observed + max_age, valid_until)``.
    """

    effective = policy or FreshnessPolicy()
    klass = freshness_class(evidence)
    observed = observation_time(evidence)
    metadata = evidence_metadata(evidence)
    source_revision = str(metadata.get("source_revision") or metadata.get("database_revision") or "")[:200]
    observed_text = format_timestamp(observed) if observed is not None else ""
    if klass != "external_observation":
        return EvidenceFreshness(
            evidence_id=evidence.id,
            freshness_class=klass,
            state="current",
            observed_at=observed_text,
            source_revision=source_revision,
        )
    instant = parse_timestamp(now)
    if instant is None:
        raise ValueError("freshness needs an aware evaluation instant")
    if observed is None:
        return EvidenceFreshness(
            evidence_id=evidence.id,
            freshness_class=klass,
            state="unknown",
            source_revision=source_revision,
            reason="the observation has no readable source timestamp",
            reason_code="observation_time_unknown",
        )
    expires = observed + timedelta(seconds=max(0, effective.external_max_age_seconds))
    raw_valid_until = metadata.get("valid_until")
    if raw_valid_until not in (None, ""):
        valid_until = parse_timestamp(raw_valid_until)
        if valid_until is None:
            return EvidenceFreshness(
                evidence_id=evidence.id,
                freshness_class=klass,
                state="unknown",
                observed_at=observed_text,
                source_revision=source_revision,
                reason="the observation's valid_until is unreadable",
                reason_code="valid_until_unreadable",
            )
        expires = min(expires, valid_until)
    expired = instant >= expires
    reason = ""
    code = "observation_current"
    if expired:
        reason = (
            f"evidence freshness policy could not be read ({effective.error})"
            if effective.error
            else f"external observation expired at {format_timestamp(expires)}"
        )
        code = "freshness_policy_unreadable" if effective.error else "observation_expired"
    return EvidenceFreshness(
        evidence_id=evidence.id,
        freshness_class=klass,
        state="expired" if expired else "current",
        observed_at=observed_text,
        source_revision=source_revision,
        expires_at=format_timestamp(expires),
        reason=reason,
        reason_code=code,
    )


__all__ = [
    "DEFAULT_EXTERNAL_MAX_AGE_SECONDS",
    "FRESHNESS_CLASSES",
    "MAX_EXTERNAL_MAX_AGE_SECONDS",
    "EvidenceFreshness",
    "FreshnessClass",
    "FreshnessPolicy",
    "FreshnessState",
    "assess_freshness",
    "evidence_metadata",
    "format_timestamp",
    "freshness_class",
    "observation_order_key",
    "observation_time",
    "parse_duration_seconds",
    "parse_freshness_policy",
    "parse_timestamp",
]

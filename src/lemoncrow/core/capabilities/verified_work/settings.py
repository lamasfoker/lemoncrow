"""Settings for Verified Local Work.

Runtime code reads environment variables seeded by the normal ``lc settings``
mechanism.  Defaults are deliberately conservative: the feature is off and the
first qualified kinds are lint/typecheck only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from .models import RepairKind

VerifiedWorkMode = Literal["off", "shadow", "enforce"]
ModelPolicy = Literal["local_only", "local_first", "explicit"]
_ALLOWED_KINDS: frozenset[str] = frozenset({"format", "lint", "typecheck", "compile", "unit_test", "syntax", "config"})


def _bounded_int(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(low, min(value, high))


def _bounded_float(name: str, default: float, low: float, high: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = default
    return max(low, min(value, high))


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


def _mode() -> VerifiedWorkMode:
    raw = os.environ.get("LEMONCROW_VERIFIED_WORK_MODE", "off").strip().lower()
    return raw if raw in {"off", "shadow", "enforce"} else "off"  # type: ignore[return-value]


def _model_policy() -> ModelPolicy:
    raw = os.environ.get("LEMONCROW_VERIFIED_WORK_MODEL_POLICY", "local_only").strip().lower()
    return raw if raw in {"local_only", "local_first", "explicit"} else "local_only"  # type: ignore[return-value]


def _kinds() -> tuple[RepairKind, ...]:
    raw = os.environ.get("LEMONCROW_VERIFIED_WORK_KINDS", "lint,typecheck")
    values: list[RepairKind] = []
    for token in raw.split(","):
        name = token.strip().lower()
        if name in _ALLOWED_KINDS and name not in values:
            values.append(name)  # type: ignore[arg-type]
    return tuple(values) or ("lint", "typecheck")


@dataclass(frozen=True, slots=True)
class VerifiedWorkSettings:
    mode: VerifiedWorkMode = "off"
    model: str = "auto"
    model_policy: ModelPolicy = "local_only"
    max_attempts: int = 2
    max_files: int = 4
    max_diff_lines: int = 120
    timeout_s: int = 90
    memory_mb: int = 2048
    max_parallel: int = 1
    max_repairs_session: int = 8
    max_seconds_session: int = 300
    kinds: tuple[RepairKind, ...] = ("lint", "typecheck")
    allow_test_edits: bool = False
    allow_manifest_edits: bool = False
    require_isolation: bool = True
    calibration_min_pairs: int = 20
    auto_min_samples: int = 20
    auto_min_success_rate: float = 0.9
    auto_max_avg_latency_ms: int = 20_000

    @property
    def enabled(self) -> bool:
        return self.mode != "off"


def load_verified_work_settings() -> VerifiedWorkSettings:
    return VerifiedWorkSettings(
        mode=_mode(),
        model=os.environ.get("LEMONCROW_VERIFIED_WORK_MODEL", "auto").strip() or "auto",
        model_policy=_model_policy(),
        max_attempts=_bounded_int("LEMONCROW_VERIFIED_WORK_MAX_ATTEMPTS", 2, 1, 4),
        max_files=_bounded_int("LEMONCROW_VERIFIED_WORK_MAX_FILES", 4, 1, 16),
        max_diff_lines=_bounded_int("LEMONCROW_VERIFIED_WORK_MAX_DIFF_LINES", 120, 1, 1000),
        timeout_s=_bounded_int("LEMONCROW_VERIFIED_WORK_TIMEOUT_S", 90, 5, 600),
        memory_mb=_bounded_int("LEMONCROW_VERIFIED_WORK_MEMORY_MB", 2048, 256, 32768),
        max_parallel=_bounded_int("LEMONCROW_VERIFIED_WORK_MAX_PARALLEL", 1, 1, 8),
        max_repairs_session=_bounded_int("LEMONCROW_VERIFIED_WORK_MAX_REPAIRS_SESSION", 8, 1, 100),
        max_seconds_session=_bounded_int("LEMONCROW_VERIFIED_WORK_MAX_SECONDS_SESSION", 300, 10, 3600),
        kinds=_kinds(),
        allow_test_edits=_bool("LEMONCROW_VERIFIED_WORK_ALLOW_TEST_EDITS", False),
        allow_manifest_edits=_bool("LEMONCROW_VERIFIED_WORK_ALLOW_MANIFEST_EDITS", False),
        require_isolation=_bool("LEMONCROW_VERIFIED_WORK_REQUIRE_ISOLATION", True),
        calibration_min_pairs=_bounded_int("LEMONCROW_VERIFIED_WORK_CALIBRATION_MIN_PAIRS", 20, 1, 200),
        auto_min_samples=_bounded_int("LEMONCROW_VERIFIED_WORK_AUTO_MIN_SAMPLES", 20, 1, 100),
        auto_min_success_rate=_bounded_float("LEMONCROW_VERIFIED_WORK_AUTO_MIN_SUCCESS_RATE", 0.9, 0.0, 1.0),
        auto_max_avg_latency_ms=_bounded_int("LEMONCROW_VERIFIED_WORK_AUTO_MAX_AVG_LATENCY_MS", 20_000, 100, 600_000),
    )


__all__ = ["ModelPolicy", "VerifiedWorkMode", "VerifiedWorkSettings", "load_verified_work_settings"]

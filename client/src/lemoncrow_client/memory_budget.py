"""Small memory-budget helpers shared by Core config and Runtime caches."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

__all__ = ["available_memory_bytes", "cache_budget_bytes"]

_KIB: Final[int] = 1024
_MIB: Final[int] = 1024 * _KIB
_GIB: Final[int] = 1024 * _MIB
_AUTO_FRACTION: Final[float] = 0.10
_MIN_AUTO_BUDGET: Final[int] = 8 * _MIB
_MAX_AUTO_BUDGET: Final[int] = 4 * _GIB
_FALLBACK_BUDGET: Final[int] = 256 * _MIB
_MAX_EXPLICIT_BUDGET: Final[int] = 16 * _GIB


def cache_budget_bytes(override: str | None = None, *, available_bytes: int | None = None) -> int:
    """Return a bounded response-cache ceiling without importing Runtime cache code."""
    if override is not None and override.strip():
        explicit = _parse_size(override)
        if explicit is not None:
            return min(explicit, _MAX_EXPLICIT_BUDGET)
    available = available_bytes if available_bytes is not None else available_memory_bytes()
    if available is None or available <= 0:
        return _FALLBACK_BUDGET
    target = int(available * _AUTO_FRACTION)
    pressure_cap = max(_MIN_AUTO_BUDGET, available // 4)
    return min(max(target, _MIN_AUTO_BUDGET), _MAX_AUTO_BUDGET, pressure_cap)


def available_memory_bytes() -> int | None:
    """Best-effort available RAM, respecting a tighter Linux cgroup limit."""
    candidates = [value for value in (_proc_available(), _sysconf_available(), _cgroup_available()) if value]
    return min(candidates) if candidates else None


def _proc_available() -> int | None:
    try:
        rows = Path("/proc/meminfo").read_text(encoding="ascii").splitlines()
    except OSError:
        return None
    for row in rows:
        if not row.startswith("MemAvailable:"):
            continue
        fields = row.split()
        if len(fields) >= 2:
            try:
                value = int(fields[1]) * _KIB
            except ValueError:
                return None
            return value if value > 0 else None
    return None


def _sysconf_available() -> int | None:
    try:
        pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    value = pages * page_size
    return value if value > 0 else None


def _cgroup_available() -> int | None:
    candidates: list[int] = []
    for limit_path, current_path in (
        (Path("/sys/fs/cgroup/memory.max"), Path("/sys/fs/cgroup/memory.current")),
        (
            Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
            Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
        ),
    ):
        limit = _read_memory_number(limit_path)
        current = _read_memory_number(current_path)
        if limit is not None and current is not None and limit > current:
            candidates.append(limit - current)
    return min(candidates) if candidates else None


def _read_memory_number(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="ascii").strip()
    except OSError:
        return None
    if not raw or raw == "max":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if value <= 0 or value >= (1 << 60):
        return None
    return value


def _parse_size(raw: str) -> int | None:
    text = raw.strip().lower()
    units = {
        "gib": _GIB,
        "gb": _GIB,
        "g": _GIB,
        "mib": _MIB,
        "mb": _MIB,
        "m": _MIB,
        "kib": _KIB,
        "kb": _KIB,
        "k": _KIB,
        "b": 1,
        "": 1,
    }
    for suffix in sorted(units, key=len, reverse=True):
        if suffix and not text.endswith(suffix):
            continue
        number = text[: -len(suffix)] if suffix else text
        try:
            value = float(number)
        except ValueError:
            continue
        if value < 0:
            return None
        return int(value * units[suffix])
    return None

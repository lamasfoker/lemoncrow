"""Gate and release state a diagnostic bundle reports, and the seam release state arrives through.

Gate I (the replay gate) is read from the versioned configuration of the source checkout and
Gate H (evidence qualification) from its packaged contract, never from a network or a service.
A gate with no readable configuration is reported ``unknown``: Gate I in an installed package
(its configuration is not shipped), Gate H only when the packaged contract is missing or invalid.
No value here is free text; the bundle builder re-sanitizes every field.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: Why release state is unknown. RC4 (release qualification) replaces ``release_state``'s
#: body with a reader of its ledger; until then the bundle says so rather than guessing.
RELEASE_UNKNOWN_REASON: Final[str] = "release_not_reported"


@dataclass(frozen=True, slots=True)
class GateState:
    """One gate's switch and threshold approval. ``mode`` is ``""`` when it could not be read."""

    gate: str
    mode: str = ""
    approved: bool | None = None
    #: Gate I only: the recorded baseline's decision and when it was recorded.
    baseline_decision: str = ""
    baseline_recorded_at: str = ""
    unreadable: bool = False


@dataclass(frozen=True, slots=True)
class ReleaseState:
    """Release qualification state; ``unknown`` carries a registered reason code."""

    unknown: str = RELEASE_UNKNOWN_REASON
    state: str = ""


def _baseline(path: Path) -> tuple[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", ""
    if not isinstance(raw, Mapping):
        return "", ""
    decision, recorded = raw.get("decision"), raw.get("recorded_at")
    return (decision if isinstance(decision, str) else ""), (recorded if isinstance(recorded, str) else "")


def gate_states() -> tuple[GateState, ...]:
    """Gate I then Gate H: Gate I as the running checkout configures it, Gate H as its packaged contract does."""

    states: list[GateState] = []
    try:
        from lemoncrow.pro.capabilities.review.evidence_qualification import source_checkout_gate_dir

        directory = source_checkout_gate_dir()
        from lemoncrow.pro.capabilities.review.evaluation.gate import load_gate_config

        config = load_gate_config(directory)
        decision, recorded = _baseline(config.baseline_path)
        states.append(
            GateState(
                "gate_i",
                mode=str(config.mode),
                approved=bool(config.thresholds.approved),
                baseline_decision=decision,
                baseline_recorded_at=recorded,
            )
        )
    except Exception:
        states.append(GateState("gate_i", unreadable=True))
    try:
        from lemoncrow.pro.capabilities.review.evidence_qualification import runtime_gate_h_config

        # Re-read the file each time: the runtime accessor caches for the process, and a
        # bundle must show the state on disk now. Its built-in fallback (a missing or invalid
        # packaged contract) is the default switch, not a configured one, so it is reported unknown.
        uncached = getattr(runtime_gate_h_config, "__wrapped__", runtime_gate_h_config)
        gate_h = uncached()
        if gate_h.source.startswith("built-in defaults"):
            states.append(GateState("gate_h", unreadable=True))
        else:
            states.append(GateState("gate_h", mode=str(gate_h.mode), approved=bool(gate_h.thresholds.approved)))
    except Exception:
        states.append(GateState("gate_h", unreadable=True))
    return tuple(states)


def release_state() -> ReleaseState:
    """The RC4 seam: release qualification state, or why it is unknown.

    lc-debt: RC4's release ledger is not on this base; upgrade: read its ledger here and return
    ``ReleaseState(unknown="", state=<one of its closed state words>)``.
    """

    return ReleaseState()


GateReader = Callable[[], tuple[GateState, ...]]
ReleaseReader = Callable[[], ReleaseState]

__all__ = [
    "RELEASE_UNKNOWN_REASON",
    "GateReader",
    "GateState",
    "ReleaseReader",
    "ReleaseState",
    "gate_states",
    "release_state",
]

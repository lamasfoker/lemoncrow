"""Value contracts for verifier-triggered local work."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RepairKind = Literal["format", "lint", "typecheck", "compile", "unit_test", "syntax", "config"]


@dataclass(frozen=True, slots=True)
class Diagnostic:
    path: str
    line: int
    column: int
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class VerifiedWorkOpportunity:
    id: str
    session_id: str
    workspace: str
    command: str
    cwd: str
    kind: RepairKind
    exit_code: int
    diagnostics: tuple[Diagnostic, ...]
    diagnostic_digest: str
    diagnostic_excerpt: str
    implicated_paths: tuple[str, ...]
    base_file_hashes: tuple[tuple[str, str], ...]
    original_result_digest: str


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    eligible: bool
    kind: RepairKind
    reason_codes: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    verifier_command_digest: str
    risk: Literal["low", "unknown", "high"]


@dataclass(frozen=True, slots=True)
class RepairEdit:
    path: str
    old: str
    new: str


@dataclass(frozen=True, slots=True)
class RepairProposal:
    summary: str
    edits: tuple[RepairEdit, ...]


@dataclass(frozen=True, slots=True)
class RepairAttemptResult:
    attempted: bool
    verified: bool
    reason: str
    model: str = ""
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    provider_cost_usd: float | None = None
    changed_paths: tuple[str, ...] = ()
    diff_lines: int = 0
    verifier_exit_code: int | None = None
    verifier_duration_ms: int = 0
    network_isolated: bool = False
    resource_limited: bool = False
    promoted: bool = False
    repair_record_id: str = ""


__all__ = [
    "Diagnostic",
    "EligibilityDecision",
    "RepairAttemptResult",
    "RepairEdit",
    "RepairKind",
    "RepairProposal",
    "VerifiedWorkOpportunity",
]

"""Deterministic eligibility ceiling for Verified Local Work."""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath

from .models import EligibilityDecision, VerifiedWorkOpportunity
from .settings import VerifiedWorkSettings

_MANIFESTS = frozenset(
    {
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "pyproject.toml",
        "uv.lock",
        "requirements.txt",
        "cargo.toml",
        "cargo.lock",
        "go.mod",
        "go.sum",
    }
)
_SENSITIVE_PARTS = frozenset({"migrations", ".github", "deploy", "deployment", "terraform", "security", "auth"})
_TEST_MARKERS = ("/tests/", "/test/", "/__tests__/", ".test.", ".spec.", "_test.py", "test_")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _looks_test(path: str) -> bool:
    pure = PurePosixPath(path.lower())
    parts = set(pure.parts)
    name = pure.name
    return bool(
        parts & {"tests", "test", "__tests__"}
        or name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
    )


def evaluate_eligibility(
    opportunity: VerifiedWorkOpportunity,
    settings: VerifiedWorkSettings,
) -> EligibilityDecision:
    reasons: list[str] = []
    risk: str = "low"
    if settings.mode == "off":
        reasons.append("mode_off")
    if opportunity.kind not in settings.kinds:
        reasons.append("kind_disabled")
    if not opportunity.diagnostics:
        reasons.append("no_structured_diagnostics")
    if len(opportunity.implicated_paths) > settings.max_files:
        reasons.append("too_many_files")

    for path in opportunity.implicated_paths:
        pure = PurePosixPath(path)
        lower_parts = {part.lower() for part in pure.parts}
        if not path or path.startswith("/") or ".." in pure.parts:
            reasons.append("path_escape")
            risk = "high"
            continue
        if lower_parts & _SENSITIVE_PARTS:
            reasons.append("sensitive_path")
            risk = "high"
        if pure.name.lower() in _MANIFESTS and not settings.allow_manifest_edits:
            reasons.append("manifest_edit_disabled")
            risk = "high"
        if _looks_test(path) and not settings.allow_test_edits:
            reasons.append("test_edit_disabled")
            risk = "high"

    # Missing base hashes mean the source disappeared or was unreadable between
    # verifier output and policy evaluation. Never repair against that race.
    if any(not digest for _, digest in opportunity.base_file_hashes):
        reasons.append("source_unreadable")
        risk = "unknown" if risk == "low" else risk

    unique = tuple(dict.fromkeys(reasons))
    return EligibilityDecision(
        eligible=not unique,
        kind=opportunity.kind,
        reason_codes=unique or ("eligible_low_risk",),
        allowed_paths=opportunity.implicated_paths if not unique else (),
        verifier_command_digest=_digest(opportunity.command),
        risk=risk,  # type: ignore[arg-type]
    )


__all__ = ["evaluate_eligibility"]

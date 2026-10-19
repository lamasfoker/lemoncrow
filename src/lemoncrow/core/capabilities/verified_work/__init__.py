"""Verified Local Work contracts and deterministic policy."""

from .classify import build_opportunity, classify_command_kind, parse_diagnostics
from .models import Diagnostic, EligibilityDecision, RepairKind, VerifiedWorkOpportunity
from .policy import evaluate_eligibility
from .settings import VerifiedWorkSettings, load_verified_work_settings

__all__ = [
    "Diagnostic",
    "EligibilityDecision",
    "RepairKind",
    "VerifiedWorkOpportunity",
    "VerifiedWorkSettings",
    "build_opportunity",
    "classify_command_kind",
    "evaluate_eligibility",
    "load_verified_work_settings",
    "parse_diagnostics",
]

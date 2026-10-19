"""A short, evidence-backed reading of the Snapshot, not a comment feed."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

# lc-debt: conservative extractive fallback when no model summary exists; replace
# with a validated, cached whole-change synthesis when that contract is available.
_CHANGE_REPORT = re.compile(
    r"^(?:implemented|added|updated|changed|fixed|moved|removed|replaced|reduced|"
    r"improved|enabled|disabled|preserved|introduced|resolved|hardened|"
    r"now\b|the change\b|this change\b)",
    re.IGNORECASE,
)


def is_change_summary(title: str) -> bool:
    """Conservative filter for captured prose; explicit guidance is trusted."""
    return bool(_CHANGE_REPORT.match(title))


def _short(value: Any, limit: int = 280) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(".,;:") + "…"


def _sentence(value: Any) -> str:
    text = re.sub(r"^(?:#{1,6}\s+|[-*+]\s+)", "", str(value or "").strip())
    return _short(re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0])


def build_brief_summary(projection: Mapping[str, Any], checks: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Distill current explanations; every inferred sentence keeps exact refs."""

    items = projection.get("items") or ()
    intents = projection.get("plan", {}).get("intents") or ()
    paragraphs: list[str] = []
    statements: list[dict[str, Any]] = []
    sources: set[str] = set()
    seen: set[str] = set()

    def append(
        text: str,
        source: str,
        *,
        target_ids: Sequence[str] = (),
        claim_ids: Sequence[str] = (),
    ) -> bool:
        identity = text.casefold().rstrip(".")
        if not text or identity in seen:
            return False
        seen.add(identity)
        paragraphs.append(text)
        statements.append(
            {
                "text": text,
                "origin": source,
                "target_ids": list(dict.fromkeys(str(value) for value in target_ids if str(value))),
                "claim_ids": list(dict.fromkeys(str(value) for value in claim_ids if str(value))),
            }
        )
        sources.add(source)
        return True

    for intent in intents:
        if append(_sentence(intent.get("intent")), "author"):
            break

    intent_count = len(paragraphs)
    explanations = 0
    for item in items:
        if item.get("stale") or item.get("rationale_origin") == "none":
            continue
        origin = str(item.get("rationale_origin") or "author")
        text = _sentence(item.get("after") or item.get("rationale") or item.get("title"))
        if str(item.get("id", "")).startswith("agent:"):
            text = _sentence(item.get("title"))
            if not is_change_summary(text):
                continue
        if text and text.casefold().rstrip(".") not in seen:
            explanations += 1
            if len(paragraphs) < 3:
                append(
                    text,
                    origin,
                    target_ids=tuple(item.get("primary_target_ids") or ())
                    + tuple(item.get("related_target_ids") or ()),
                    claim_ids=tuple(item.get("claim_ids") or ()),
                )

    areas = list(
        dict.fromkeys(
            str(item.get("title") or "")
            for item in items
            if item.get("kind") in ("area", "other") and item.get("primary_target_ids")
        )
    )
    coverage = projection.get("coverage") or {}
    scope = f"{coverage.get('file_count', 0)} files · {coverage.get('target_count', 0)} changed targets"
    if not paragraphs:
        if areas:
            names = ", ".join(areas[:3])
            remaining = len(areas) - 3
            suffix = f" and {remaining} other area{'s' if remaining != 1 else ''}" if remaining > 0 else ""
            append(f"Changes span {names}{suffix}.", "observed")
        else:
            append("No change explanation has been recorded for this Snapshot.", "observed")

    recorded: dict[str, str] = {}
    for check in checks:
        if check.get("state") != "current":
            continue
        key = str(check.get("name") or check.get("source_ref") or "")
        if key:
            recorded[key] = str(check.get("status") or "UNKNOWN").upper()
    if recorded:
        passed = sum(status == "PASS" for status in recorded.values())
        failed = sum(status == "FAIL" for status in recorded.values())
        unresolved = len(recorded) - passed - failed
        parts = [f"{passed} passed", f"{failed} failed"]
        if unresolved:
            parts.append(f"{unresolved} unresolved")
        verification = "Recorded checks: " + ", ".join(parts) + "."
    else:
        verification = "No current verification results recorded."

    outstanding = projection.get("decision_progress", {}).get("outstanding")
    if outstanding is None:
        outstanding = len(projection.get("decisions") or ())
    questions = projection.get("questions") or ()
    actions = projection.get("actions") or ()
    attention = []
    if outstanding:
        attention.append(f"{outstanding} open decision{'s' if outstanding != 1 else ''}")
    priority_targets = {
        target_id
        for item in items
        if item.get("attention") == "review_first"
        for target_id in item.get("primary_target_ids") or ()
    }
    if priority_targets:
        attention.append(
            f"{len(priority_targets)} changed target{'s' if len(priority_targets) != 1 else ''} need attention"
        )
    if questions or actions:
        attention.append(
            f"{len(questions) + len(actions)} issue{'s' if len(questions) + len(actions) != 1 else ''} to investigate"
        )
    return {
        "paragraphs": paragraphs,
        "statements": statements,
        "origin": next(iter(sources)) if len(sources) == 1 else "mixed",
        "scope": scope,
        "verification": verification,
        "attention": "; ".join(attention),
        "additional_explanations": max(0, explanations - (len(paragraphs) - intent_count)),
    }

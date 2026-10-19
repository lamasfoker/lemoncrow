"""Selective AI synthesis for the Brief (spec §7, §8, §10; Phase 4).

Pure request building and validation. The model may only *propose*:
explanations for change groups that have no author explanation, which
recorded check relates to an author claim, and investigation questions.
Everything it proposes is checked against the Snapshot before publication:
every target and check reference must exist, nothing it says can set a check
result (results always come from recorded rows), planner "decisions" become
questions, and no question can outrank tier 3. What the budget or the
validator leaves out is reported, never silently dropped.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from lemoncrow.pro.capabilities.review.review_plan import ReviewQuestion
from lemoncrow.pro.capabilities.review.review_priority import TIER_REVIEW_FIRST

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.review_plan import PlanArtifact
    from lemoncrow.pro.capabilities.review.targets import ReviewTarget

SYNTHESIS_SCHEMA_VERSION = 1
# 2: the prompt names the exact answer shape (backends only enforce JSON).
# 3: each item is explained only from its own code (reuse is per item).
PROMPT_VERSION = 3

QUESTION_KINDS = frozenset(
    {"author_observation_mismatch", "claim_evidence_scope_mismatch", "implementation_choice_unrecorded"}
)
_SUMMARY_LIMIT = 600
_WHY_LIMIT = 200
_TEXT_LIMIT = 400
# "the unit tests passed", "CI is green", "lint fails": results are recorded
# facts, never model prose.
# A relevance "why" is a short phrase about *relation*; any result word in it
# ("it passed", "green in CI") is a result claim.
_RESULT_WORD = re.compile(
    r"\b(pass(?:es|ed|ing)?|fail(?:s|ed|ing|ure|ures)?|green|red|succeed(?:s|ed)?|success(?:ful)?|verified|broken)\b",
    re.IGNORECASE,
)
_RESULT_CLAIM = re.compile(
    r"\b(tests?|checks?|ci|builds?|lint|suite|scenario)\b[^.\n]{0,60}?\b(pass(?:es|ed)?|fail(?:s|ed)?|green|red|verified|succeed(?:s|ed)?)\b"
    r"|\b(pass(?:es|ed)?|fail(?:s|ed)?|verified)\b[^.\n]{0,30}?\b(tests?|checks?|ci|builds?|suite)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SynthesisInputBounds:
    """Semantic context bounds only; provider-call budgets live in intelligence_runtime."""

    max_items: int = 12
    max_chars: int = 24000


@dataclass(frozen=True)
class SynthesisBudget:
    """Legacy Phase-4 runner budget retained for old callers and tests."""

    max_items: int = 12
    max_chars: int = 24000
    timeout_s: float = 90.0
    max_calls: int = 20

    @classmethod
    def from_env(cls) -> SynthesisBudget:
        def number(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, "") or default)
            except ValueError:
                return default

        return cls(
            max_items=max(1, int(number("LEMONCROW_REVIEW_SYNTHESIS_MAX_ITEMS", 12))),
            max_chars=max(1000, int(number("LEMONCROW_REVIEW_SYNTHESIS_MAX_CHARS", 24000))),
            timeout_s=max(5.0, number("LEMONCROW_REVIEW_SYNTHESIS_TIMEOUT_S", 90.0)),
            max_calls=max(0, int(number("LEMONCROW_REVIEW_SYNTHESIS_MAX_CALLS", 20))),
        )


@dataclass(frozen=True)
class SynthesisItem:
    item_id: str
    title: str
    target_ids: tuple[str, ...]
    snippets: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SynthesisCheck:
    key: str
    title: str
    status: str
    state: str


@dataclass(frozen=True)
class SynthesisClaim:
    claim_id: str
    statement: str
    target_ids: tuple[str, ...]


@dataclass(frozen=True)
class SynthesisRequest:
    items: tuple[SynthesisItem, ...]
    checks: tuple[SynthesisCheck, ...]
    claims: tuple[SynthesisClaim, ...]
    skipped_item_ids: tuple[str, ...] = ()
    skipped_target_ids: tuple[str, ...] = ()
    truncated_target_ids: tuple[str, ...] = ()
    # No new-side source could be read (unreadable, binary, pure deletion):
    # never sent, never explained.
    unreadable_target_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SynthesizedExplanation:
    item_id: str
    summary: str
    target_ids: tuple[str, ...]


@dataclass(frozen=True)
class InferredLink:
    claim_id: str
    check_key: str
    why: str


@dataclass(frozen=True)
class ValidatedSynthesis:
    explanations: dict[str, SynthesizedExplanation] = field(default_factory=dict)
    relevance: dict[str, tuple[InferredLink, ...]] = field(default_factory=dict)
    questions: tuple[ReviewQuestion, ...] = ()
    rejected: tuple[str, ...] = ()
    unsynthesized_target_ids: tuple[str, ...] = ()


def _check_key(row: Mapping[str, Any]) -> str:
    return str(row.get("name") or row.get("source_ref") or "")


def _structural(target: ReviewTarget) -> tuple[int, int, str, int, str]:
    """The queue's within-tier order; nothing a human does changes it."""

    rank = target.attention_rank
    return (1 if rank <= 0 else 0, rank, target.path, target.start_line, target.unit_key)


def _excerpt(target: ReviewTarget, text: str) -> str:
    lines = text.split("\n")
    rows: list[int] = []
    for span in target.spans:
        if span.side == "new" and span.start_line > 0:
            rows.extend(range(span.start_line - 1, min(len(lines), max(span.start_line, span.end_line))))
    return "\n".join(lines[row] for row in sorted(set(rows)))


def build_synthesis_request(
    plan: PlanArtifact,
    targets: Sequence[ReviewTarget],
    checks: Sequence[Mapping[str, Any]],
    read_new: Callable[[str], str | None],
    budget: SynthesisBudget | SynthesisInputBounds,
    *,
    allowed_target_ids: Collection[str] | None = None,
) -> SynthesisRequest:
    """Build bounded model input only from the already-authorized target set."""

    allowed = None if allowed_target_ids is None else frozenset(allowed_target_ids)
    known = {target.target_id: target for target in targets if allowed is None or target.target_id in allowed}
    far = (1, 1 << 30, "", 1 << 30, "")
    candidates = sorted(
        (
            item
            for item in plan.items
            if item.kind in ("area", "other") and item.primary_target_ids
            # Do not partially summarize a mixed allowed/denied group: its title
            # and deterministic grouping may themselves derive from denied bytes.
            and (allowed is None or set(item.primary_target_ids) <= allowed)
        ),
        key=lambda item: min(
            (_structural(known[target_id]) for target_id in item.primary_target_ids if target_id in known),
            default=far,
        ),
    )
    texts: dict[str, str | None] = {}
    items: list[SynthesisItem] = []
    skipped_items: list[str] = []
    skipped_targets: list[str] = []
    truncated: list[str] = []
    unreadable: list[str] = []
    used = 0
    for item in plan.items:
        if item.kind in ("area", "other") and item.primary_target_ids and item not in candidates:
            skipped_items.append(item.id)
            skipped_targets.extend(
                target_id for target_id in item.primary_target_ids if allowed is not None and target_id not in allowed
            )
    for item in candidates:
        if len(items) >= budget.max_items:
            skipped_items.append(item.id)
            skipped_targets.extend(item.primary_target_ids)
            continue
        snippets: list[tuple[str, str]] = []
        ordered = sorted(
            (known[target_id] for target_id in item.primary_target_ids if target_id in known),
            key=_structural,
        )
        for target in ordered:
            target_id = target.target_id
            if target.path not in texts:
                try:
                    texts[target.path] = read_new(target.path)
                except (OSError, ValueError):
                    texts[target.path] = None
            text = texts[target.path]
            excerpt = _excerpt(target, text) if text is not None else ""
            if not excerpt.strip():
                unreadable.append(target_id)
                continue
            if used + len(excerpt) > budget.max_chars:
                truncated.append(target_id)
                continue
            used += len(excerpt)
            snippets.append((target_id, excerpt))
        if not snippets:
            skipped_items.append(item.id)
            continue
        items.append(
            SynthesisItem(
                item_id=item.id,
                title=item.title,
                target_ids=tuple(target_id for target_id, _ in snippets),
                snippets=tuple(snippets),
            )
        )
    rows = [row for row in checks if _check_key(row) and str(row.get("state") or "") in ("current", "stale")]
    return SynthesisRequest(
        items=tuple(items),
        checks=tuple(
            SynthesisCheck(
                key=_check_key(row),
                title=str(row.get("name") or _check_key(row)),
                status=str(row.get("status") or "UNKNOWN").upper(),
                state=str(row.get("state") or ""),
            )
            for row in rows
        ),
        claims=tuple(
            SynthesisClaim(claim_id=claim.id, statement=claim.statement, target_ids=claim.target_ids)
            for claim in plan.claims
            if allowed is None or (claim.target_ids and set(claim.target_ids) <= allowed)
        ),
        skipped_item_ids=tuple(dict.fromkeys(skipped_items)),
        skipped_target_ids=tuple(dict.fromkeys(skipped_targets)),
        truncated_target_ids=tuple(truncated),
        unreadable_target_ids=tuple(unreadable),
    )


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def synthesis_key(request: SynthesisRequest, *, revision_id: str, model: str) -> str:
    """Exact input identity. Human review state never enters it (spec §10).

    Check *keys* enter (a new recorded check may be relevant); check statuses
    do not, because a rendered link always reads the recorded row.
    """

    document = {
        "v": SYNTHESIS_SCHEMA_VERSION,
        "prompt": PROMPT_VERSION,
        "revision": revision_id,
        "model": model,
        "items": [
            {
                "id": item.item_id,
                "targets": list(item.target_ids),
                "snippets": [hashlib.sha256(text.encode("utf-8")).hexdigest() for _, text in item.snippets],
            }
            # Canonical order: queue order moves with marks, the inputs do not.
            for item in sorted(request.items, key=lambda item: item.item_id)
        ],
        "checks": sorted({check.key for check in request.checks}),
        "claims": [[claim.claim_id, claim.statement, list(claim.target_ids)] for claim in request.claims],
    }
    return "syn-" + hashlib.sha256(_canonical(document).encode("utf-8")).hexdigest()


def _text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _ids(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _rows(raw: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
    value = raw.get(name)
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def validate_synthesis(raw: Mapping[str, Any], request: SynthesisRequest) -> ValidatedSynthesis:
    """Keep only what the Snapshot supports; record every rejection."""

    items = {item.item_id: item for item in request.items}
    owner = {target_id: item.item_id for item in request.items for target_id in item.target_ids}
    claim_targets = {target_id for claim in request.claims for target_id in claim.target_ids}
    askable = set(owner) | claim_targets
    claims = {claim.claim_id for claim in request.claims}
    checks = {check.key for check in request.checks}
    rejected: list[str] = []

    explanations: dict[str, SynthesizedExplanation] = {}
    for row in _rows(raw, "explanations"):
        item_id = str(row.get("item_id") or "")
        item = items.get(item_id)
        if item is None:
            rejected.append(f"explanation for unknown item '{item_id}'")
            continue
        cited = _ids(row.get("target_ids")) or list(item.target_ids)
        if not set(cited) <= set(item.target_ids):
            rejected.append(f"explanation for '{item_id}' cites targets outside it")
            continue
        summary = _text(row.get("summary"), _SUMMARY_LIMIT)
        if not summary:
            rejected.append(f"empty explanation for '{item_id}'")
            continue
        if _RESULT_CLAIM.search(summary):
            rejected.append(f"explanation for '{item_id}' asserts a check result")
            continue
        if item_id not in explanations:
            explanations[item_id] = SynthesizedExplanation(item_id, summary, tuple(dict.fromkeys(cited)))

    relevance: dict[str, list[InferredLink]] = {}
    for row in _rows(raw, "relevance"):
        claim_id = str(row.get("claim_id") or "")
        check_key = str(row.get("check_key") or "")
        if claim_id not in claims or check_key not in checks:
            rejected.append(f"relevance link '{claim_id}' → '{check_key}' names an unknown claim or check")
            continue
        why = _text(row.get("why"), _WHY_LIMIT)
        if _RESULT_CLAIM.search(why) or _RESULT_WORD.search(why):
            why = ""  # the relation stays; a result claim never does
        links = relevance.setdefault(claim_id, [])
        if all(link.check_key != check_key for link in links):
            links.append(InferredLink(claim_id, check_key, why))

    proposals = [(row, str(row.get("kind") or "")) for row in _rows(raw, "questions")]
    # A planner may think it found a decision; only author guidance creates
    # HumanDecisions, so it becomes an investigation question (spec §4.2).
    proposals.extend(
        (
            {
                "title": f"Unrecorded choice: {_text(row.get('question') or row.get('title'), 120)}",
                "detail": row.get("detail")
                or "The implementation appears to make a choice the author guidance does not record.",
                "target_ids": row.get("target_ids"),
            },
            "implementation_choice_unrecorded",
        )
        for row in _rows(raw, "decisions")
    )
    questions: list[ReviewQuestion] = []
    for row, kind in proposals:
        if kind not in QUESTION_KINDS:
            rejected.append(f"question kind '{kind}' is not allowed")
            continue
        target_ids = tuple(dict.fromkeys(_ids(row.get("target_ids"))))
        title = _text(row.get("title"), 160)
        detail = _text(row.get("detail"), _TEXT_LIMIT)
        if not target_ids or not set(target_ids) <= askable or not title:
            rejected.append(f"question '{title or kind}' cites unknown targets or has no title")
            continue
        if _RESULT_CLAIM.search(title) or _RESULT_CLAIM.search(detail):
            rejected.append(f"question '{title}' asserts a check result")
            continue
        questions.append(
            ReviewQuestion(
                id=f"q:ai:{kind}:{len(questions)}",
                reason=kind,
                title=title,
                detail=detail,
                item_ids=tuple(dict.fromkeys(owner[target_id] for target_id in target_ids if target_id in owner)),
                target_ids=target_ids,
                actions=("inspect_evidence", "ask", "open_implementation"),
                tier=TIER_REVIEW_FIRST,
                origin="ai",
            )
        )

    explained = {target_id for explanation in explanations.values() for target_id in explanation.target_ids}
    unsynthesized = [target_id for item in request.items for target_id in item.target_ids if target_id not in explained]
    unsynthesized.extend(request.skipped_target_ids)
    unsynthesized.extend(request.truncated_target_ids)
    unsynthesized.extend(request.unreadable_target_ids)
    return ValidatedSynthesis(
        explanations=explanations,
        relevance={claim_id: tuple(links) for claim_id, links in relevance.items()},
        questions=tuple(questions),
        rejected=tuple(rejected),
        unsynthesized_target_ids=tuple(dict.fromkeys(unsynthesized)),
    )


# --------------------------------------------------------------------------- #
# runner: one model call per exact input key, published by compare-and-set
# --------------------------------------------------------------------------- #

SYNTHESIS_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "explanations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "summary": {"type": "string"},
                    "target_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["item_id", "summary", "target_ids"],
            },
        },
        "relevance": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string"},
                    "check_key": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["claim_id", "check_key", "why"],
            },
        },
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": sorted(QUESTION_KINDS)},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "target_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["kind", "title", "detail", "target_ids"],
            },
        },
    },
    "required": ["explanations", "relevance", "questions"],
}

SYSTEM_PROMPT = """You explain changed code to a reviewer who supervises coding agents.

For each item, write one or two plain sentences describing the observable behavior the
changed code implements (before -> after when visible).
Explain each item only from its own changed_source; never rely on code shown for another item.
You are inferring intent from code:
never claim to know why the author did it, never quote an author, never describe test,
CI or check results (you are not shown them), and never recommend approval.
Only cite target_ids and item_ids given to you.

relevance: for an author claim, name a recorded check (by key) that plausibly exercises it,
and say why in a short phrase. Do not state results.

questions: raise at most a few concrete, material questions a careful reviewer would ask:
- author_observation_mismatch: the code appears to contradict an author claim;
- claim_evidence_scope_mismatch: a claim's scope appears wider than what exercises it;
- implementation_choice_unrecorded: the code appears to make a product/policy choice no one recorded.
No generic advice.

Return only one JSON object of exactly this shape (no other top-level keys):
{"explanations": [{"item_id": "<an item_id>", "summary": "<1-2 sentences>", "target_ids": ["<target_id>", ...]}],
 "relevance": [{"claim_id": "<a claim_id>", "check_key": "<a check key>", "why": "<short phrase>"}],
 "questions": [{"kind": "author_observation_mismatch" | "claim_evidence_scope_mismatch" | "implementation_choice_unrecorded",
                "title": "<short>", "detail": "<one sentence>", "target_ids": ["<target_id>", ...]}]}
Use empty lists when you have nothing to add."""


@dataclass(frozen=True)
class SynthesisOutcome:
    status: str
    """``ready``, ``pending`` or ``failed``."""
    synthesis: ValidatedSynthesis | None
    error: str
    row_id: str


def synthesis_messages(request: SynthesisRequest) -> list[dict[str, Any]]:
    payload = {
        "items": [
            {
                "item_id": item.item_id,
                "title": item.title,
                "targets": [{"target_id": target_id, "changed_source": text} for target_id, text in item.snippets],
            }
            for item in request.items
        ],
        # Keys and titles only: results are recorded facts, not model input.
        "checks": [{"key": check.key, "title": check.title} for check in request.checks],
        "claims": [
            {"claim_id": claim.claim_id, "statement": claim.statement, "target_ids": list(claim.target_ids)}
            for claim in request.claims
        ],
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _canonical(payload)},
    ]


def item_input_hash(item: SynthesisItem) -> str:
    """Everything one explanation depends on: its group title, targets and code.

    Item ids are chapter keys and may move between Snapshots; the hash is
    what reuse matches, so identical code keeps its explanation (spec §10).
    """

    document = {
        "v": SYNTHESIS_SCHEMA_VERSION,
        "prompt": PROMPT_VERSION,
        "title": item.title,
        "targets": [[target_id, hashlib.sha256(text.encode("utf-8")).hexdigest()] for target_id, text in item.snippets],
    }
    return "sit-" + hashlib.sha256(_canonical(document).encode("utf-8")).hexdigest()


def _context_hash(request: SynthesisRequest) -> str:
    """What relevance links and questions depend on beyond the items."""

    document = {
        "claims": [[claim.claim_id, claim.statement, list(claim.target_ids)] for claim in request.claims],
        "checks": sorted({check.key for check in request.checks}),
    }
    return "ctx-" + hashlib.sha256(_canonical(document).encode("utf-8")).hexdigest()


def synthesis_payload(
    validated: ValidatedSynthesis,
    request: SynthesisRequest | None = None,
    *,
    model_called: bool = True,
    reused_items: int = 0,
) -> dict[str, Any]:
    """The stored shape: the same schema the model answers in, already validated."""

    hashes = {item.item_id: item_input_hash(item) for item in request.items} if request is not None else {}
    return {
        "explanations": [
            {
                "item_id": item.item_id,
                "summary": item.summary,
                "target_ids": list(item.target_ids),
                "input_hash": hashes.get(item.item_id, ""),
            }
            for item in validated.explanations.values()
        ],
        "context_hash": _context_hash(request) if request is not None else "",
        "model_called": model_called,
        "reused_items": reused_items,
        "relevance": [
            {"claim_id": link.claim_id, "check_key": link.check_key, "why": link.why}
            for links in validated.relevance.values()
            for link in links
        ],
        "questions": [
            {
                "kind": question.reason,
                "title": question.title,
                "detail": question.detail,
                "target_ids": list(question.target_ids),
            }
            for question in validated.questions
        ],
        "rejected": list(validated.rejected),
    }


def load_synthesis(payload: Mapping[str, Any], request: SynthesisRequest) -> ValidatedSynthesis:
    """Re-validate a stored synthesis against the current request."""

    validated = validate_synthesis(payload, request)
    stored = tuple(str(item) for item in payload.get("rejected") or () if isinstance(item, str))
    return ValidatedSynthesis(
        explanations=validated.explanations,
        relevance=validated.relevance,
        questions=validated.questions,
        rejected=stored + validated.rejected,
        unsynthesized_target_ids=validated.unsynthesized_target_ids,
    )


def _call(chat_fn: Callable[..., Any], messages: list[dict[str, Any]], timeout_s: float) -> Any:
    """Run one model call under a wall-time limit.

    A timed-out call cannot be interrupted, so it runs on a daemon thread that
    is abandoned (never awaited, never blocking shutdown); its late answer has
    no pending row left to publish onto.
    """

    import threading

    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["value"] = chat_fn(messages, json_schema=SYNTHESIS_JSON_SCHEMA)
        except BaseException as exc:  # re-raised on the caller's thread
            outcome["error"] = exc

    worker = threading.Thread(target=target, name="lc-review-synthesis", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        raise TimeoutError(f"the model did not answer within {timeout_s:g}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def budget_spent_text(max_calls: int) -> str:
    return (
        f"This Review's AI budget is spent ({max_calls} of {max_calls} model calls); "
        "raise LEMONCROW_REVIEW_SYNTHESIS_MAX_CALLS to allow more."
    )


def stale_after_s(budget: SynthesisBudget) -> float:
    """How long a pending claim may run before it counts as abandoned."""

    return max(60.0, budget.timeout_s * 2)


def pending_is_abandoned(row: Any, budget: SynthesisBudget) -> bool:
    """A pending row nobody will complete (the server restarted mid-call)."""

    from datetime import UTC, datetime

    if str(getattr(row, "status", "")) != "pending":
        return False
    try:
        age = datetime.now(UTC) - datetime.fromisoformat(str(row.created_at))
    except ValueError:
        return True
    return age.total_seconds() >= stale_after_s(budget)


def run_synthesis(
    store: Any,
    *,
    review_id: str,
    revision_id: str,
    request: SynthesisRequest,
    model: str,
    chat_fn: Callable[..., Any],
    budget: SynthesisBudget,
    inputs_digest: str = "",
) -> SynthesisOutcome:
    """One model call per exact input key; concurrent requests coalesce.

    The result is published only onto the pending row this call claimed, so
    a late answer for Snapshot N can never become current for N+1 (whose key
    differs).
    """

    from lemoncrow.pro.capabilities.review.session_models import ReviewSynthesisRow

    key = synthesis_key(request, revision_id=revision_id, model=model)
    row, claimed = store.claim_synthesis(
        ReviewSynthesisRow(
            review_id=review_id,
            id="",
            revision_id=revision_id,
            synthesis_key=key,
            schema_version=SYNTHESIS_SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
            model=model,
            status="pending",
            inputs_digest=inputs_digest,
        ),
        stale_after_s=stale_after_s(budget),
    )
    if not claimed:
        if row.status == "ready":
            return SynthesisOutcome("ready", load_synthesis(json.loads(row.payload_json or "{}"), request), "", row.id)
        return SynthesisOutcome(row.status, None, row.error, row.id)
    if not request.items and not request.claims:
        validated = validate_synthesis({}, request)
        store.complete_synthesis(
            review_id, row.id, status="ready", payload_json=_canonical(synthesis_payload(validated))
        )
        return SynthesisOutcome("ready", validated, "", row.id)
    cached, todo, prior_questions, context = _reusable(store, review_id, model, request)
    called = bool(todo) or (bool(request.claims) and context is None)
    if called and not store.reserve_synthesis_call(review_id, limit=budget.max_calls):
        error = budget_spent_text(budget.max_calls)
        store.complete_synthesis(review_id, row.id, status="failed", error=error)
        return SynthesisOutcome("failed", None, error, row.id)
    try:
        if called:
            raw = _call(chat_fn, synthesis_messages(replace(request, items=todo)), budget.timeout_s)
            if isinstance(raw, str):
                raw = json.loads(raw)
            if not isinstance(raw, Mapping):
                raise ValueError("the model returned a non-object synthesis")
            if not any(key in raw for key in ("explanations", "relevance", "questions", "decisions")):
                # Stored as ready it would read as "nothing to explain" and cache the spend.
                raise ValueError(
                    "the model's answer has none of the expected keys (explanations, relevance, questions)"
                )
            # Judged against what the model was shown: a question citing a
            # reused item's code it never saw is rejected, not merged.
            shown = validate_synthesis(raw, replace(request, items=todo))
            answer: Mapping[str, Any] = synthesis_payload(shown)
            sub_rejected = shown.rejected
        else:
            sub_rejected = ()
            # Every item reused and claims/checks unchanged: the matching
            # synthesis's relevance and questions still describe these inputs.
            answer = (
                {"relevance": _rows(context, "relevance"), "questions": _rows(context, "questions")} if context else {}
            )
            prior_questions = []
        merged = {
            "explanations": [*_rows(answer, "explanations"), *cached],
            "relevance": _rows(answer, "relevance"),
            "questions": [*_rows(answer, "questions"), *prior_questions],
            "decisions": _rows(answer, "decisions"),
        }
        # Reused explanations are re-validated against *this* request like new ones.
        validated = validate_synthesis(merged, request)
        validated = replace(validated, rejected=(*sub_rejected, *validated.rejected))
    except Exception as exc:  # any model/transport failure is an honest, stored state
        error = _text(f"{type(exc).__name__}: {exc}", 300)
        store.complete_synthesis(review_id, row.id, status="failed", error=error)
        store.prune_syntheses(review_id, keep=3)
        return SynthesisOutcome("failed", None, error, row.id)
    payload = synthesis_payload(validated, request, model_called=called, reused_items=len(cached))
    store.complete_synthesis(review_id, row.id, status="ready", payload_json=_canonical(payload))
    store.prune_syntheses(review_id, keep=3)
    return SynthesisOutcome("ready", validated, "", row.id)


def _reusable(
    store: Any, review_id: str, model: str, request: SynthesisRequest
) -> tuple[list[dict[str, Any]], tuple[SynthesisItem, ...], list[Mapping[str, Any]], Mapping[str, Any] | None]:
    """``(cached explanations, items to send, reusable questions, matching context)``.

    Reuse reads only this Review's ready syntheses from the same model and
    recipe (pruning bounds them). An explanation is reused when its item's
    input hash is identical; a question when every target it cites belongs
    to reused items; relevance only when claims and checks are identical.
    """

    prior: list[Mapping[str, Any]] = []
    for stored in store.list_syntheses(review_id):
        if (stored.status, stored.model, stored.schema_version, stored.prompt_version) != (
            "ready",
            model,
            SYNTHESIS_SCHEMA_VERSION,
            PROMPT_VERSION,
        ):
            continue
        try:
            payload = json.loads(stored.payload_json or "{}")
        except ValueError:
            continue
        if isinstance(payload, Mapping):
            prior.append(payload)
    explained: dict[str, Mapping[str, Any]] = {}
    for payload in prior:  # newest first
        for row in _rows(payload, "explanations"):
            input_hash = str(row.get("input_hash") or "")
            if input_hash:
                explained.setdefault(input_hash, row)
    cached: list[dict[str, Any]] = []
    todo: list[SynthesisItem] = []
    for item in request.items:
        hit = explained.get(item_input_hash(item))
        if hit is None:
            todo.append(item)
        else:
            cached.append({**hit, "item_id": item.item_id})
    reused_targets = {target_id for row in cached for target_id in _ids(row.get("target_ids"))}
    context_hash = _context_hash(request)
    context = next((payload for payload in prior if payload.get("context_hash") == context_hash), None)
    source = context if context is not None else (prior[0] if prior else None)
    questions = [
        row
        for row in (_rows(source, "questions") if source is not None else [])
        if _ids(row.get("target_ids")) and set(_ids(row.get("target_ids"))) <= reused_targets
        # A claim-dependent question holds only while claims and checks match.
        and (context is not None or str(row.get("kind") or "") == "implementation_choice_unrecorded")
    ]
    return cached, tuple(todo), questions, context


# --------------------------------------------------------------------------- #
# lookup without a model: model identity, cheap input digest, stored questions
# --------------------------------------------------------------------------- #

SYNTHESIS_SOURCE_LABEL = "AI explanation"
SYNTHESIS_LIMITATION = "Intent inferred from the changed code; not stated by the author."
_MODEL_ENV = {
    "openai": "LEMONCROW_OPENAI_MODEL",
    "ollama": "LEMONCROW_OLLAMA_MODEL",
    "litellm": "LEMONCROW_LITELLM_MODEL",
}


def synthesis_model() -> tuple[str, str]:
    """``(model identity, disabled reason)`` from configuration; never calls a model."""

    backend = os.environ.get("LEMONCROW_LLM_BACKEND", "none").strip().lower()
    if backend == "openai_compatible":
        backend = "openai"
    if backend in ("", "none"):
        return "", "AI explanations are disabled; set LEMONCROW_LLM_BACKEND to enable them"
    if backend not in _MODEL_ENV:
        return "", f"AI explanations are unavailable: unsupported model backend '{backend}'"
    return f"{backend}:{os.environ.get(_MODEL_ENV[backend], '').strip() or 'default'}", ""


def request_check_keys(checks: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """The recorded check keys a request offers the model (same rule as the request)."""

    return tuple(
        sorted(
            {
                _check_key(row)
                for row in checks
                if _check_key(row) and str(row.get("state") or "") in ("current", "stale")
            }
        )
    )


def synthesis_inputs_digest(
    *,
    revision_id: str,
    guidance_ids: Sequence[str],
    check_keys: Sequence[str],
    model: str,
    budget: SynthesisBudget,
) -> str:
    """Cheap identity of everything a request is derived from (no source reads).

    The Snapshot is immutable, so its id stands for the source; guidance and
    evidence enter by record identity. Human review state never enters. Every
    reader surface finds the current synthesis by this digest without
    rebuilding the request, so the queue and the Brief agree.
    """

    from lemoncrow.pro.capabilities.review.review_plan import PLAN_SCHEMA_VERSION

    document = {
        "v": SYNTHESIS_SCHEMA_VERSION,
        "prompt": PROMPT_VERSION,
        "plan": PLAN_SCHEMA_VERSION,
        "revision": revision_id,
        "guidance": sorted(set(guidance_ids)),
        "checks": sorted(set(check_keys)),
        "model": model,
        "budget": [budget.max_items, budget.max_chars],
    }
    return "sin-" + hashlib.sha256(_canonical(document).encode("utf-8")).hexdigest()


def stored_questions(payload: Mapping[str, Any], known_target_ids: Collection[str]) -> tuple[ReviewQuestion, ...]:
    """Questions from a stored, already-validated synthesis, for the queue.

    Re-checked against the Snapshot's targets; never above tier 3.
    """

    out: list[ReviewQuestion] = []
    for row in _rows(payload, "questions"):
        kind = str(row.get("kind") or "")
        target_ids = tuple(dict.fromkeys(_ids(row.get("target_ids"))))
        title = _text(row.get("title"), 160)
        detail = _text(row.get("detail"), _TEXT_LIMIT)
        if kind not in QUESTION_KINDS or not title or not target_ids or not set(target_ids) <= set(known_target_ids):
            continue
        if _RESULT_CLAIM.search(title) or _RESULT_CLAIM.search(detail):
            continue
        out.append(
            ReviewQuestion(
                id=f"q:ai:{kind}:{len(out)}",
                reason=kind,
                title=title,
                detail=detail,
                item_ids=(),
                target_ids=target_ids,
                actions=("inspect_evidence", "ask", "open_implementation"),
                tier=TIER_REVIEW_FIRST,
                origin="ai",
            )
        )
    return tuple(out)


__all__ = [
    "PROMPT_VERSION",
    "QUESTION_KINDS",
    "SYNTHESIS_JSON_SCHEMA",
    "SYNTHESIS_LIMITATION",
    "SYNTHESIS_SCHEMA_VERSION",
    "SYNTHESIS_SOURCE_LABEL",
    "InferredLink",
    "SynthesisBudget",
    "SynthesisCheck",
    "SynthesisClaim",
    "SynthesisInputBounds",
    "SynthesisItem",
    "SynthesisOutcome",
    "SynthesisRequest",
    "SynthesizedExplanation",
    "ValidatedSynthesis",
    "build_synthesis_request",
    "item_input_hash",
    "load_synthesis",
    "pending_is_abandoned",
    "request_check_keys",
    "run_synthesis",
    "stale_after_s",
    "stored_questions",
    "synthesis_inputs_digest",
    "synthesis_key",
    "synthesis_messages",
    "synthesis_model",
    "synthesis_payload",
    "validate_synthesis",
]

"""Registered-model resolution and patch-only local worker calls."""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from lemoncrow.core.capabilities.verified_work.models import (
    RepairEdit,
    RepairKind,
    RepairProposal,
    VerifiedWorkOpportunity,
)
from lemoncrow.core.capabilities.verified_work.settings import VerifiedWorkSettings

_MAX_SOURCE_CHARS = 24_000
_MAX_EDIT_TEXT_CHARS = 16_000


@dataclass(frozen=True, slots=True)
class WorkerModelChoice:
    model: str
    endpoint: str
    local: bool
    structured_output: bool | None
    observed_tokens_per_second: float | None


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.strip().lower().strip("[]")
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def is_loopback_endpoint(base_url: str) -> bool:
    try:
        return _is_loopback_host(urlparse(base_url).hostname)
    except ValueError:
        return False


def resolve_worker_model(
    settings: VerifiedWorkSettings,
    *,
    kind: RepairKind | None = None,
    root: Path | None = None,
    allowed_models: tuple[str, ...] | None = None,
) -> tuple[WorkerModelChoice | None, str]:
    """Resolve one worker model once for a work item.

    ``local_only`` never treats a non-loopback custom endpoint as local.  This
    intentionally prefers false negatives over accidentally sending source to a
    remote gateway that merely speaks the OpenAI protocol.

    ``allowed_models`` (Auto) limits the choice to models the repository opted
    in to; ``None`` means unrestricted (Learning).
    """

    from lemoncrow.pro.capabilities.model_setup.endpoints import get_endpoint, list_endpoints
    from lemoncrow.pro.capabilities.model_setup.models import split_custom_model_id
    from lemoncrow.pro.capabilities.verified_work.calibration import calibrated_model_kind
    from lemoncrow.pro.capabilities.verified_work.outcomes import qualification_model_outcome

    requested = settings.model.strip() or "auto"
    if settings.model_policy == "explicit" and requested == "auto":
        return None, "explicit_model_required"

    if requested != "auto":
        if allowed_models is not None and requested not in allowed_models:
            return None, "configured_model_not_opted_in"
        parts = split_custom_model_id(requested)
        if parts is None:
            return None, "registered_custom_model_required"
        endpoint_name, raw_id = parts
        endpoint = get_endpoint(root, endpoint_name)
        if endpoint is None:
            return None, "model_endpoint_not_registered"
        model = next((item for item in endpoint.models if item.raw_id == raw_id), None)
        if model is None:
            return None, "model_not_registered"
        local = endpoint.locus == "local"
        if settings.model_policy == "local_only" and not local:
            return None, "model_endpoint_not_local"
        if settings.mode == "enforce" and settings.model_policy != "explicit" and kind is not None:
            stats = qualification_model_outcome(requested, kind, root=root, min_samples=settings.auto_min_samples)
            economics = calibrated_model_kind(requested, kind, root=root, min_pairs=settings.calibration_min_pairs)
            if (
                stats.attempts < settings.auto_min_samples
                or stats.verification_rate < settings.auto_min_success_rate
                or stats.average_latency_ms > settings.auto_max_avg_latency_ms
                or not economics
                or not economics.get("qualified")
                or float(economics.get("median_net_savings_usd") or 0.0) <= 0
            ):
                return None, "configured_model_not_auto_qualified"
        return (
            WorkerModelChoice(
                model=requested,
                endpoint=endpoint.base_url,
                local=local,
                structured_output=model.supports_structured_output,
                observed_tokens_per_second=model.observed_tokens_per_second,
            ),
            "explicit_model",
        )

    candidates: list[tuple[tuple[float, ...], WorkerModelChoice]] = []
    saw_candidate = False
    saw_evidence_candidate = False
    for endpoint in list_endpoints(root):
        local = endpoint.locus == "local"
        if settings.model_policy == "local_only" and not local:
            continue
        for model in endpoint.models:
            if allowed_models is not None and f"custom/{endpoint.name}/{model.raw_id}" not in allowed_models:
                continue
            saw_candidate = True
            choice = WorkerModelChoice(
                model=f"custom/{endpoint.name}/{model.raw_id}",
                endpoint=endpoint.base_url,
                local=local,
                structured_output=model.supports_structured_output,
                observed_tokens_per_second=model.observed_tokens_per_second,
            )
            candidate_stats = (
                qualification_model_outcome(choice.model, kind, root=root, min_samples=settings.auto_min_samples)
                if kind is not None
                else None
            )
            samples = candidate_stats.attempts if candidate_stats is not None else 0
            rate = candidate_stats.verification_rate if candidate_stats is not None else 0.0
            latency = candidate_stats.average_latency_ms if candidate_stats is not None else 0.0

            if settings.mode == "enforce":
                # Auto-enforce is evidence gated. Explicit model selection is the
                # user escape hatch for a newly installed/unproven local model.
                if kind is None:
                    continue
                if samples < settings.auto_min_samples:
                    continue
                if rate < settings.auto_min_success_rate:
                    continue
                if latency > settings.auto_max_avg_latency_ms:
                    continue
                if model.supports_structured_output is False:
                    continue
                economics = calibrated_model_kind(
                    choice.model, kind, root=root, min_pairs=settings.calibration_min_pairs
                )
                if (
                    not economics
                    or not economics.get("qualified")
                    or float(economics.get("median_net_savings_usd") or 0.0) <= 0
                ):
                    continue
                saw_evidence_candidate = True

            structured_rank = (
                0 if model.supports_structured_output is True else 1 if model.supports_structured_output is None else 2
            )
            locality_rank = 0 if local else 1
            throughput_rank = -(model.observed_tokens_per_second or 0.0)
            quality_qualified = (
                samples >= settings.auto_min_samples
                and rate >= settings.auto_min_success_rate
                and latency <= settings.auto_max_avg_latency_ms
                and model.supports_structured_output is not False
            )
            if settings.mode == "shadow":
                # Learning is an exploration policy, not a first-model-wins
                # heuristic. Keep a quality-qualified model first so it can
                # accumulate paired economics evidence; otherwise prefer the
                # least-sampled unproven model and push already-proven-bad
                # models behind unexplored alternatives.
                if quality_qualified:
                    learning_rank = 0
                    sample_rank = 0
                elif samples < settings.auto_min_samples:
                    learning_rank = 1
                    sample_rank = samples
                else:
                    learning_rank = 2
                    sample_rank = samples
            else:
                learning_rank = 0
                sample_rank = 0
            success_rank = -rate if samples else 0.0
            latency_rank = latency if samples else float("inf")
            candidates.append(
                (
                    (
                        float(locality_rank),
                        float(learning_rank),
                        float(sample_rank),
                        success_rank,
                        latency_rank,
                        float(structured_rank),
                        throughput_rank,
                        float(len(choice.model)),
                    ),
                    choice,
                )
            )
    if not candidates:
        if settings.mode == "enforce" and saw_candidate and not saw_evidence_candidate:
            return None, "no_evidence_qualified_auto_model"
        return None, "no_qualified_local_model"
    candidates.sort(key=lambda row: (row[0], row[1].model))
    return candidates[0][1], "auto_evidence_model" if kind is not None else "auto_local_model"


def _lite_context_packet(opportunity: VerifiedWorkOpportunity) -> str:
    """Best-effort bounded Runtime Lite context when the repository is already indexed."""
    workspace = Path(opportunity.workspace).expanduser().resolve()
    try:
        from lemoncrow_client.lite.index import LiteIndex

        index = LiteIndex(workspace)
        if not index.db_path.is_file():
            return ""
        query_parts: list[str] = []
        for row in opportunity.diagnostics[:6]:
            query_parts.extend((row.code, row.message))
        query = " ".join(part for part in query_parts if part).strip()[:1000]
        if not query:
            return ""
        result = index.search(query, limit=5)
        files = result.get("files") if isinstance(result, dict) else None
        if not isinstance(files, list):
            return ""
        sections: list[str] = []
        remaining = 6_000
        implicated = set(opportunity.implicated_paths)
        for item in files:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            source = str(item.get("source") or "")
            if not path or path in implicated or not source:
                continue
            block = f"RELATED {path}\n{source}"
            if len(block) > remaining:
                block = block[:remaining]
            if block:
                sections.append(block)
                remaining -= len(block)
            if remaining <= 0:
                break
        return "\n\n".join(sections)
    except Exception:
        return ""


def _source_packet(opportunity: VerifiedWorkOpportunity, candidate_root: Path) -> str:
    diagnostics_by_path: dict[str, list[int]] = {}
    for diag in opportunity.diagnostics:
        diagnostics_by_path.setdefault(diag.path, []).append(diag.line)

    remaining = _MAX_SOURCE_CHARS
    sections: list[str] = []
    for rel in opportunity.implicated_paths:
        path = candidate_root / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        wanted = diagnostics_by_path.get(rel) or [1]
        lo = max(1, min(wanted) - 45)
        hi = min(len(lines), max(wanted) + 45)
        body = "\n".join(f"{index:>6} | {lines[index - 1]}" for index in range(lo, hi + 1))
        header = f"FILE {rel} lines {lo}-{hi}\n"
        section = header + body
        if len(section) > remaining:
            section = section[:remaining]
        if section:
            sections.append(section)
            remaining -= len(section)
        if remaining <= 0:
            break
    related = _lite_context_packet(opportunity)
    if related:
        sections.append(related)
    return "\n\n".join(sections)


def _proposal_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["summary", "edits"],
        "properties": {
            "summary": {"type": "string"},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["path", "old", "new"],
                    "properties": {
                        "path": {"type": "string"},
                        "old": {"type": "string"},
                        "new": {"type": "string"},
                    },
                },
            },
        },
    }


def _parse_proposal(raw: dict[str, Any], *, allowed_paths: tuple[str, ...]) -> RepairProposal:
    summary = str(raw.get("summary") or "")[:500]
    rows = raw.get("edits")
    if not isinstance(rows, list) or not rows:
        raise ValueError("worker returned no edits")
    allowed = set(allowed_paths)
    edits: list[RepairEdit] = []
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("worker edit is not an object")
        path = str(item.get("path") or "").strip()
        old = item.get("old")
        new = item.get("new")
        if path not in allowed:
            raise ValueError(f"worker edit path is not allowed: {path}")
        if not isinstance(old, str) or not isinstance(new, str) or not old or old == new:
            raise ValueError("worker edit must contain distinct non-empty old/new text")
        if len(old) > _MAX_EDIT_TEXT_CHARS or len(new) > _MAX_EDIT_TEXT_CHARS:
            raise ValueError("worker edit exceeds text budget")
        edits.append(RepairEdit(path=path, old=old, new=new))
    return RepairProposal(summary=summary, edits=tuple(edits))


def propose_patch(
    opportunity: VerifiedWorkOpportunity,
    *,
    candidate_root: Path,
    allowed_paths: tuple[str, ...],
    choice: WorkerModelChoice,
    timeout_s: int,
    feedback: str = "",
) -> tuple[RepairProposal, dict[str, Any]]:
    """Ask one pinned model for structured edits; it receives no tool authority."""

    from lemoncrow.infra.internal_llm.litellm_client import chat_with_result

    diag_text = "\n".join(
        f"{row.path}:{row.line}:{row.column} {row.code}: {row.message}" for row in opportunity.diagnostics
    )
    packet = _source_packet(opportunity, candidate_root)
    user = (
        "Repair this mechanically verifiable coding failure. Return JSON only.\n"
        "You have NO shell/tool authority. You may edit only the allowed paths.\n"
        "Do not edit tests, manifests, lockfiles, or unrelated code.\n"
        "Each edit must quote an exact existing `old` string and its replacement `new`.\n"
        'Return exactly: {"summary":"...","edits":[{"path":"...","old":"...","new":"..."}]}\n'
        "Keep the patch minimal and targeted to the diagnostics.\n\n"
        f"Verifier kind: {opportunity.kind}\n"
        f"Allowed paths: {json.dumps(list(allowed_paths))}\n"
        f"Diagnostics:\n{diag_text}\n\n"
        f"Source excerpts:\n{packet}\n"
    )
    if feedback:
        user += f"\nPrevious candidate verifier feedback:\n{feedback[-6000:]}\n"
    result = chat_with_result(
        [{"role": "user", "content": user}],
        model=choice.model,
        json_schema=_proposal_schema(),
        extra_kwargs={"max_tokens": 2048, "timeout": timeout_s, "temperature": 0},
    )
    proposal = _parse_proposal(dict(result.parsed_json or {}), allowed_paths=allowed_paths)
    return proposal, {
        "model": result.model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "provider_cost_usd": result.provider_cost,
    }


__all__ = [
    "WorkerModelChoice",
    "is_loopback_endpoint",
    "propose_patch",
    "resolve_worker_model",
]

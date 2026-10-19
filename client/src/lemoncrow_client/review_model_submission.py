"""Explicit operator-approved model investigation -> bounded hosted job result.

The source and model execution are the P3m/P3n authenticated one-shot path.
Only an unverified, Snapshot-bound observation enters the existing durable
customer-worker result endpoint. This is NOT background agent dispatch or an
annotation/published finding; the server must return not_applicable publication.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .review_companion_protocol import CompanionPolicy, CompanionTask
from .review_model_broker import ModelObservation, OneShotReviewModelBroker
from .review_worker_client import ReviewWorkerClient, WorkerLease


@dataclass(frozen=True, slots=True)
class UnverifiedAgentReceipt:
    job_id: str
    snapshot_id: str
    observation: ModelObservation
    publication_state: str = "not_applicable"


def submit_approved_model_investigation(
    client: ReviewWorkerClient,
    lease: WorkerLease,
    task: CompanionTask,
    policy: CompanionPolicy,
    broker: OneShotReviewModelBroker,
    *,
    api_key: str,
    explicit_external_source_approval: bool,
) -> UnverifiedAgentReceipt:
    """Exactly one provider attempt and one durable report; never auto-retry.

    A client disconnect after dispatch has unknown publication outcome. Do not
    repeat the provider call; the signed attempt is consumed by the ledger.
    """
    result = broker.run(
        client,
        lease,
        task,
        policy,
        api_key=api_key,
        explicit_external_source_approval=explicit_external_source_approval,
    )
    if result.provenance != "author_agent_unverified" or result.usage_source != "provider_reported":
        raise ValueError("only unverified observations with reported usage can be submitted")
    assignment = lease.model_assignment
    if not isinstance(assignment, dict):
        raise ValueError("signed model assignment is unavailable")
    summary: dict[str, Any] = {
        "trace": {
            "execution_locus": "customer",
            "worker_id": client.binding.worker_id,
            "execution_class": "review_intelligence",
            "provider": result.provider,
            "model": result.model,
            "model_route_id": result.route_id,
            "model_data_locus": "external",
            "provider_calls": 1,
            "policy_digest": lease.policy_digest,
            # Unknown actual invoice, not a fabricated provider cost.
            "provider_cost": None,
        },
        "author_agent": {
            "task_id": task.task_id,
            "task_kind": task.task_kind,
            "snapshot_id": lease.snapshot_id,
            "provenance": result.provenance,
            "text": result.text,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "estimated_cost_micros": result.estimated_cost_micros,
            "cost_source": "operator_estimate_provider_usage",
            "included_source_paths": list(result.included_source_paths),
            "source_files_available": result.source_files_available,
        },
    }
    if client.heartbeat(lease) != "running":
        raise ValueError("Review task was superseded before unverified observation submission")
    reply = client.report(lease, summary=summary)
    publication = reply.get("publication")
    if (
        reply.get("job_id") != lease.job_id
        or reply.get("state") != "succeeded"
        or not isinstance(publication, dict)
        or publication.get("state") != "not_applicable"
    ):
        raise ValueError("hosted Review did not acknowledge safe unverified result handling")
    return UnverifiedAgentReceipt(lease.job_id, lease.snapshot_id, result)


__all__ = ["UnverifiedAgentReceipt", "submit_approved_model_investigation"]

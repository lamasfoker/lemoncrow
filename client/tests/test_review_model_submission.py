"""P3o: one-shot signed model result -> hosted unverified (not published)."""

from __future__ import annotations

from typing import Any

import pytest
from lemoncrow_client.review_companion_protocol import CompanionPolicy
from lemoncrow_client.review_model_attempts import ModelAttemptAlreadySpent, ProviderAttemptLedger
from lemoncrow_client.review_model_broker import ModelBrokerRefused, OneShotReviewModelBroker
from lemoncrow_client.review_model_submission import submit_approved_model_investigation
from lemoncrow_client.review_worker_client import ReviewWorkerClient, WorkerLease
from lemoncrow_client.transport import Response
from test_review_agent_snapshot import SnapshotTransport
from test_review_model_broker import ProviderTransport, route

pytest_plugins = ["test_review_model_broker", "test_review_agent_execution"]


def submit(job: tuple[Any, ...], ledger: ProviderAttemptLedger, provider: ProviderTransport) -> Any:
    client, lease, task, policy, _, _ = job
    return submit_approved_model_investigation(
        client,
        lease,
        task,
        policy,
        OneShotReviewModelBroker(route(), ledger=ledger, transport=provider),  # type: ignore[arg-type]
        api_key="explicit-operator-provider-token-123",
        explicit_external_source_approval=True,
    )


def test_exact_unverified_summary_and_no_automatic_annotation(
    model_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, SnapshotTransport, Any],
    ledger: ProviderAttemptLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = model_job[4]
    original = wire.post

    def result(path: str, *, body: Any, headers: Any, **kwargs: Any) -> Response:
        if path.endswith("/result"):
            wire.calls.append((path, dict(body), dict(headers)))
            return Response(
                200,
                {
                    "job_id": model_job[1].job_id,
                    "state": "succeeded",
                    "publication": {"state": "not_applicable", "provenance": "author_agent_unverified"},
                },
            )
        return original(path, body=body, headers=headers, **kwargs)

    monkeypatch.setattr(wire, "post", result)
    receipt = submit(model_job, ledger, ProviderTransport("openai"))
    assert receipt.publication_state == "not_applicable"
    assert receipt.observation.provenance == "author_agent_unverified"
    records = [body for path, body, _ in wire.calls if path.endswith("/result")]
    assert len(records) == 1
    report = records[0]["result"]
    assert report["result_refs"] == [] and report["evidence_refs"] == []
    assert set(report["summary"]) == {"trace", "author_agent"}
    trace, observation = report["summary"]["trace"], report["summary"]["author_agent"]
    assert trace["provider_calls"] == 1 and trace["provider_cost"] is None
    assert trace["model_route_id"] == "customer-route-1"
    assert observation["source_files_available"] == 1
    assert observation["included_source_paths"] == ["src/file.py"]
    assert observation["cost_source"] == "operator_estimate_provider_usage"
    assert observation["text"] == "Investigate line 1"
    assert not any(key in report["summary"] for key in ("machine_findings", "verified", "pass"))


def test_missing_safe_ack_never_claims_published(
    model_job: tuple[Any, ...],
    ledger: ProviderAttemptLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = model_job[4]
    original = wire.post

    def misleading(path: str, *, body: Any, headers: Any, **kwargs: Any) -> Response:
        if path.endswith("/result"):
            wire.calls.append((path, dict(body), dict(headers)))
            return Response(
                200,
                {
                    "job_id": model_job[1].job_id,
                    "state": "succeeded",
                    "publication": {"state": "published"},
                },
            )
        return original(path, body=body, headers=headers, **kwargs)

    monkeypatch.setattr(wire, "post", misleading)
    with pytest.raises(ValueError, match="unverified result"):
        submit(model_job, ledger, ProviderTransport("openai"))
    with pytest.raises(ModelAttemptAlreadySpent, match="already consumed"):
        submit(model_job, ledger, ProviderTransport("openai"))
    assert len([path for path, _, _ in wire.calls if path.endswith("/result")]) == 1


def test_lease_revocation_after_model_call_blocks_submission(
    model_job: tuple[Any, ...],
    ledger: ProviderAttemptLedger,
) -> None:
    wire = model_job[4]
    provider = ProviderTransport("openai")
    original = provider.post

    def revoke(*args: Any, **kwargs: Any) -> Response:
        answer = original(*args, **kwargs)
        wire.heartbeat_state = "cancel_requested"
        return answer

    provider.post = revoke  # type: ignore[method-assign]
    with pytest.raises(ModelBrokerRefused, match="revoked"):
        submit(model_job, ledger, provider)
    assert not any(path.endswith("/result") for path, _, _ in wire.calls)


def test_no_consent_never_downloads_or_sends_result(
    model_job: tuple[Any, ...],
    ledger: ProviderAttemptLedger,
) -> None:
    client, lease, task, policy, wire, _ = model_job
    with pytest.raises(ModelBrokerRefused, match="approval"):
        submit_approved_model_investigation(
            client,
            lease,
            task,
            policy,
            OneShotReviewModelBroker(route(), ledger=ledger, transport=ProviderTransport("openai")),  # type: ignore[arg-type]
            api_key="explicit-operator-provider-token-123",
            explicit_external_source_approval=False,
        )
    assert not wire.downloads and not any(path.endswith("/result") for path, _, _ in wire.calls)

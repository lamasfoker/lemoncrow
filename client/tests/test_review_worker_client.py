"""Review-only customer worker transport: exact signed leases, hard refusal before execution."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from lemoncrow_client.review_worker_client import (
    ReviewWorkerClient,
    WorkerBinding,
    validate_lease,
)
from lemoncrow_client.transport import Response

NOW = 1_900_000_000.0
D = "a" * 64


def encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@dataclass
class MemoryTransport:
    reply: dict[str, Any]
    heartbeat_state: str = "running"
    wrong_context: bool = False
    calls: list[tuple[str, dict[str, Any], dict[str, str]]] = field(default_factory=list)

    def post(self, path: str, *, body: Any, headers: Any, **kwargs: Any) -> Response:
        self.calls.append((path, dict(body), dict(headers)))
        if path.endswith("/claim"):
            return Response(200, self.reply)
        if path.endswith("/heartbeat"):
            return Response(200, {"job_id": "job-1", "state": self.heartbeat_state})
        if path.endswith("/context"):
            return Response(
                200,
                {
                    "job_id": "job-1",
                    "review_id": "review-1",
                    "revision_id": "wrong" if self.wrong_context else "revision-1",
                    "snapshot_id": "snapshot-1",
                },
            )
        if path.endswith("/result"):
            return Response(200, {"job_id": "job-1", "state": "succeeded"})
        if path.endswith("/fail"):
            return Response(200, {"job_id": "job-1", "state": "queued" if body.get("retryable") else "failed"})
        raise AssertionError(path)


@pytest.fixture
def authority() -> tuple[WorkerBinding, Ed25519PrivateKey]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    binding = WorkerBinding(
        origin="https://app.lemoncrow.com",
        org_id="org-1",
        worker_id="machine-1",
        repo_id="repo-1",
        bearer="worker-secret-at-least-20-chars",
        trusted_key_id="lease-key-1",
        trusted_public_key_hex=public.hex(),
    )
    return binding, private


def sign(
    private: Ed25519PrivateKey,
    *,
    spec_overrides: dict[str, Any] | None = None,
    document_overrides: dict[str, Any] | None = None,
) -> str:
    spec: dict[str, Any] = {
        "org_id": "org-1",
        "repo_id": "repo-1",
        "review_id": "review-1",
        "revision_id": "revision-1",
        "snapshot_id": "snapshot-1",
        "execution_locus": "customer",
        "execution_class": "runtime_validation",
        "requires_repository_execution": True,
        "policy_digest": D,
        "input_digest": D,
        "semantic_snapshot_digest": D,
        "capability": "runtime_validation:test",
        "deadline_at": NOW + 120,
    }
    spec.update(spec_overrides or {})
    document = {
        "kid": "lease-key-1",
        "aud": "machine-1",
        "iat": NOW - 1,
        "exp": NOW + 120,
        "lease_expires_at": NOW + 120,
        "job_id": "job-1",
        "attempt": 1,
        "lease_token": "lease-authority",
        "contract_digest": D,
        "spec": spec,
    }
    document.update(document_overrides or {})
    body = "lcw1." + encode(json.dumps(document, sort_keys=True, separators=(",", ":")).encode())
    return body + "." + encode(private.sign(body.encode("ascii")))


def fake_client(authority: tuple[WorkerBinding, Ed25519PrivateKey]) -> tuple[ReviewWorkerClient, MemoryTransport]:
    binding, key = authority
    transport = MemoryTransport(
        {
            "job": {
                "job_id": "job-1",
                "review_id": "review-1",
                "revision_id": "revision-1",
                "execution_class": "runtime_validation",
            },
            "envelope": sign(key),
        }
    )
    return ReviewWorkerClient(binding, transport=transport, clock=lambda: NOW), transport


def test_signed_claim_heartbeat_and_context_without_runtime_mcp(
    authority: tuple[WorkerBinding, Ed25519PrivateKey],
) -> None:
    client, wire = fake_client(authority)
    lease = client.claim(execution_class="runtime_validation")
    assert lease is not None
    assert lease.spec["repo_id"] == "repo-1"
    assert lease.snapshot_id == "snapshot-1"
    assert client.context(lease)["snapshot_id"] == "snapshot-1"
    assert client.report(lease, summary={"trace": {"execution_locus": "customer"}})["state"] == "succeeded"
    assert [path for path, _body, _headers in wire.calls] == [
        "/v1/review-workers/claim",
        "/v1/review-workers/job-1/heartbeat",
        "/v1/review-workers/job-1/context",
        "/v1/review-workers/job-1/heartbeat",
        "/v1/review-workers/job-1/result",
    ]
    assert all(headers["Authorization"] == "Bearer " + authority[0].bearer for _, _, headers in wire.calls)


@pytest.mark.parametrize(
    ("document", "spec"),
    [
        ({"aud": "another-machine"}, {}),
        ({"kid": "untrusted-key"}, {}),
        ({"exp": NOW - 10}, {}),
        ({"iat": NOW + 100}, {}),
        ({"attempt": 0}, {}),
        ({}, {"repo_id": "other-repo"}),
        ({}, {"org_id": "foreign-tenant"}),
        ({}, {"execution_locus": "managed"}),
        ({}, {"execution_class": "review_intelligence"}),
        ({}, {"requires_repository_execution": False}),
        ({}, {"deadline_at": NOW - 1}),
        ({}, {"snapshot_id": ""}),
        ({}, {"semantic_snapshot_digest": "invalid"}),
    ],
)
def test_signed_but_unauthorized_or_stale_lease_refused(
    authority: tuple[WorkerBinding, Ed25519PrivateKey], document: dict[str, Any], spec: dict[str, Any]
) -> None:
    binding, key = authority
    with pytest.raises(ValueError):
        validate_lease(
            sign(key, document_overrides=document, spec_overrides=spec),
            binding,
            now=NOW,
            expected_class="runtime_validation",
        )


def test_signed_typed_failure_is_fenced_and_never_reports_pass(
    authority: tuple[WorkerBinding, Ed25519PrivateKey],
) -> None:
    client, wire = fake_client(authority)
    lease = client.claim(execution_class="runtime_validation")
    assert lease is not None
    answer = client.fail(lease, reason="executor_unavailable", retryable=True)
    assert answer["state"] == "queued"
    assert wire.calls[-1] == (
        "/v1/review-workers/job-1/fail",
        {"envelope": lease.envelope, "reason": "executor_unavailable", "retryable": True},
        {"Authorization": "Bearer " + authority[0].bearer},
    )
    for reason in ("PASS", "success", "shell", "passed"):
        with pytest.raises(ValueError):
            client.fail(lease, reason=reason)
    assert len([item for item in wire.calls if item[0].endswith("/fail")]) == 1


def test_expired_worker_cannot_report_failure(
    authority: tuple[WorkerBinding, Ed25519PrivateKey],
) -> None:
    from dataclasses import replace

    client, wire = fake_client(authority)
    lease = client.claim(execution_class="runtime_validation")
    assert lease is not None
    with pytest.raises(ValueError, match="currently valid"):
        client.fail(replace(lease, expires_at=NOW - 1), reason="source_unavailable")
    assert not any(path.endswith("/fail") for path, _, _ in wire.calls)


def test_signature_tamper_refused(authority: tuple[WorkerBinding, Ed25519PrivateKey]) -> None:
    binding, key = authority
    signed = sign(key)
    split = signed.split(".")
    forged = (
        split[0]
        + "."
        + encode(json.dumps({"kid": binding.trusted_key_id, "aud": binding.worker_id}).encode())
        + "."
        + split[2]
    )
    for token in (forged, signed[:-2] + "ab", "lcw1.not base64.!!!"):
        with pytest.raises(ValueError):
            validate_lease(token, binding, now=NOW, expected_class="runtime_validation")


def test_unsigned_claim_mismatch_and_cancelled_heartbeat(authority: tuple[WorkerBinding, Ed25519PrivateKey]) -> None:
    client, wire = fake_client(authority)
    wire.reply["job"]["review_id"] = "other"
    with pytest.raises(ValueError, match="unsigned"):
        client.claim(execution_class="runtime_validation")
    wire.reply["job"]["review_id"] = "review-1"
    lease = client.claim(execution_class="runtime_validation")
    assert lease is not None
    wire.heartbeat_state = "cancel_requested"
    with pytest.raises(ValueError, match="cancelled"):
        client.context(lease)
    with pytest.raises(ValueError, match="cancelled"):
        client.report(lease, summary={"trace": {}})
    wire.heartbeat_state = "running"
    wire.wrong_context = True
    with pytest.raises(ValueError, match="exact"):
        client.context(lease)


def test_report_never_claims_unverified_pass_or_human_authority(
    authority: tuple[WorkerBinding, Ed25519PrivateKey],
) -> None:
    client, wire = fake_client(authority)
    lease = client.claim(execution_class="runtime_validation")
    assert lease is not None
    before = len(wire.calls)
    for payload in ({"PASS": True}, {"human_judgment": "LGTM"}, {"verified": True}):
        with pytest.raises(ValueError, match="unaudited"):
            client.report(lease, summary=payload)
    assert len(wire.calls) == before + 3  # heartbeats enforce cancellation before refusing result claims
    with pytest.raises(ValueError):
        client.claim(execution_class="shell", model_route_id="")
    with pytest.raises(ValueError):
        client.claim(execution_class="review_intelligence")
    with pytest.raises(ValueError):
        client.claim(execution_class="runtime_validation", model_route_id="untrusted")


def test_machine_binding_requires_https_scoped_identity_and_pinned_key(
    authority: tuple[WorkerBinding, Ed25519PrivateKey],
) -> None:
    binding, _ = authority
    for change in (
        {"origin": "http://internet.example"},
        {"origin": "https://example.test/path"},
        {"origin": "https://user:pw@example.test"},
        {"repo_id": "../foreign"},
        {"bearer": "bad"},
        {"trusted_public_key_hex": "deadbeef"},
    ):
        with pytest.raises(ValueError):
            WorkerBinding(**{**binding.__dict__, **change})


def test_missing_ed25519_dependency_is_fail_closed(
    authority: tuple[WorkerBinding, Ed25519PrivateKey], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Invalid key bytes / missing signature backend cannot be treated as admission.
    binding, key = authority
    token = sign(key)
    bad = WorkerBinding(**{**binding.__dict__, "trusted_public_key_hex": "00" * 32})
    with pytest.raises(ValueError):
        validate_lease(token, bad, now=NOW, expected_class="runtime_validation")


@pytest.mark.parametrize(
    ("status", "header", "body", "accepted"),
    [
        (200, True, b"exact bytes\n", True),
        (200, False, b"exact bytes\n", False),
        (200, True, b"tampered\n", False),
        (403, True, b"denied", False),
    ],
)
def test_content_download_requires_active_signed_lease_exact_digest_and_origin(
    authority: tuple[WorkerBinding, Ed25519PrivateKey],
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    header: bool,
    body: bytes,
    accepted: bool,
) -> None:
    digest = hashlib.sha256(b"exact bytes\n").hexdigest()
    client, wire = fake_client(authority)
    lease = client.claim(execution_class="runtime_validation")
    assert lease is not None
    assert client.transport is wire
    assert client.binding.origin == "https://app.lemoncrow.com"
    assert client.binding.worker_id == "machine-1"

    def read_binary(path: str, *, body: dict[str, str], headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        assert path.startswith("/v1/review-workers/")
        assert path == "/v1/review-workers/job-1/content"
        assert path.split("/")[3] == lease.job_id
        assert headers["Authorization"] == "Bearer " + authority[0].bearer
        assert body["envelope"] == lease.envelope
        assert body["digest"] == digest
        assert body["side"] == "new"
        assert body == {"envelope": lease.envelope, "digest": digest, "side": "new"}
        return (
            status,
            {"content-type": "application/octet-stream", "content-digest": digest if header else "0" * 64},
            body_bytes,
        )

    body_bytes = body
    monkeypatch.setattr(wire, "post_binary", read_binary, raising=False)
    if accepted:
        assert client.content(lease, digest) == b"exact bytes\n"
    else:
        with pytest.raises(ValueError):
            client.content(lease, digest)
    assert wire.calls[0][0] == "/v1/review-workers/claim"
    assert wire.calls[-1][0].endswith("/heartbeat")

"""P3n: route-bound one-shot model egress and provider-reported usage gates.

All provider responses are synthetic. No billing or live provider/network access.
The only network operations in production are through HttpTransport with pinned
first-party vendor origins. No provider data enters the sandbox or live checkout.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_client.review_companion_protocol import CompanionPolicy, parse_task
from lemoncrow_client.review_model_attempts import ModelAttemptAlreadySpent, ProviderAttemptLedger
from lemoncrow_client.review_model_broker import (
    ModelBrokerRefused,
    OneShotReviewModelBroker,
    PinnedModelRoute,
)
from lemoncrow_client.review_worker_client import ReviewWorkerClient, WorkerLease
from lemoncrow_client.transport import Response
from test_review_agent_snapshot import SnapshotTransport
from test_review_worker_client import NOW, MemoryTransport, sign

pytest_plugins = ["test_review_agent_execution"]


@pytest.fixture
def ledger(tmp_path: Path) -> ProviderAttemptLedger:
    review_state = tmp_path / "review-state"
    review_state.mkdir(mode=0o700)
    return ProviderAttemptLedger(review_state)


class ProviderTransport:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.posts: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.status = 200
        self.fail = False
        self.answer: dict[str, Any] = (
            {
                "model": "safe-model-1",
                "choices": [{"message": {"content": "Investigate line 1"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
            }
            if provider == "openai"
            else {
                "model": "safe-model-1",
                "content": [{"type": "text", "text": "Investigate line 1"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 120, "output_tokens": 40},
            }
        )

    def post(self, path: str, *, body: Any, headers: Any) -> Response:
        self.posts.append((path, dict(body), dict(headers)))
        if self.fail:
            raise TimeoutError("simulated provider request uncertain")
        return Response(self.status, self.answer)


@pytest.fixture
def model_job(
    fixture_authority: tuple[Any, Any],
    tmp_path: Path,
) -> tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, SnapshotTransport, Path]:
    binding, private = fixture_authority
    assignment = {
        "model_route_id": "customer-route-1",
        "driver": "openai",
        "model": "safe-model-1",
        "configuration_generation": "generation-1",
        "provider_identity": "openai",
        "model_data_locus": "external",
    }
    source = b"def feature(): return 5\n"
    digest = hashlib.sha256(source).hexdigest()
    envelope = sign(
        private,
        spec_overrides={
            "execution_class": "review_intelligence",
            "requires_repository_execution": False,
            "policy_digest": "b" * 64,
            "capability": "review_intelligence:agent_task",
            "scope": {"task_id": "task-1", "task_kind": "investigate", "max_model_calls": 1, "max_runtime_ms": 4_000},
        },
        document_overrides={"assignment": assignment},
    )
    claim = {
        "job": {
            "job_id": "job-1",
            "review_id": "review-1",
            "revision_id": "revision-1",
            "execution_class": "review_intelligence",
        },
        "envelope": envelope,
    }
    base = MemoryTransport(claim)
    context = {
        "job_id": "job-1",
        "review_id": "review-1",
        "revision_id": "revision-1",
        "snapshot_id": "snapshot-1",
        "semantic_snapshot_digest": "a" * 64,
        "attempt": 1,
        "side": "new",
        "manifest_truncated": False,
        "model_route_id": "customer-route-1",
        "manifest": {"src/file.py": {"content_digest": digest, "mode": 0o100644, "size": len(source)}},
    }
    wire = SnapshotTransport(base, context, {digest: source})
    client = ReviewWorkerClient(binding, transport=wire, clock=lambda: NOW)
    lease = client.claim(execution_class="review_intelligence", model_route_id="customer-route-1")
    assert lease is not None
    task = parse_task(
        {
            "schema_version": 1,
            "type": "task_offer",
            "task_id": "task-1",
            "review_id": "review-1",
            "repo_id": "repo-1",
            "snapshot_sha256": "a" * 64,
            "policy_sha256": "b" * 64,
            "task_kind": "investigate",
            "executor": "author_agent",
            "recipe_id": "",
            "deadline_unix_ms": int((NOW + 20) * 1_000),
            "max_runtime_ms": 4_000,
            "max_model_calls": 1,
        }
    )
    policy = CompanionPolicy(
        repo_id="repo-1",
        snapshot_sha256="a" * 64,
        policy_sha256="b" * 64,
        enabled_tasks=frozenset({"investigate"}),
        max_runtime_ms=10_000,
        max_model_calls=1,
    )
    repo = tmp_path / "mutable"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src/file.py").write_text("def feature(): return WRONG\n")
    return client, lease, task, policy, wire, repo


def route(provider: str = "openai", **overrides: Any) -> PinnedModelRoute:
    data = dict(
        route_id="customer-route-1",
        provider=provider,
        model="safe-model-1",
        configuration_generation="generation-1",
        input_micros_per_million=1_000_000,
        output_micros_per_million=2_000_000,
        max_cost_micros=100_000,
        max_output_tokens=256,
        max_source_bytes=2_048,
    )
    data.update(overrides)
    return PinnedModelRoute(**data)


def invoke(
    job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, SnapshotTransport, Path],
    broker: OneShotReviewModelBroker,
    *,
    approved: bool = True,
    api_key: str = "explicit-operator-provider-token-123",
) -> Any:
    client, lease, task, policy, _, _ = job
    return broker.run(
        client,
        lease,
        task,
        policy,
        api_key=api_key,
        explicit_external_source_approval=approved,
    )


def test_gated_openai_single_request_with_usage_and_no_publication(
    model_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, SnapshotTransport, Path],
    ledger: ProviderAttemptLedger,
) -> None:
    provider = ProviderTransport("openai")
    broker = OneShotReviewModelBroker(route(), transport=provider, ledger=ledger)  # type: ignore[arg-type]
    observed = invoke(model_job, broker)
    assert observed.provenance == "author_agent_unverified"
    assert observed.usage_source == "provider_reported"
    assert observed.externally_billed_cost_verified is False
    assert observed.estimated_cost_micros == 200
    assert observed.input_tokens == 120 and observed.output_tokens == 40
    assert observed.included_source_paths == ("src/file.py",)
    assert observed.source_files_available == 1
    assert len(provider.posts) == 1
    path, body, headers = provider.posts[0]
    assert path == "/v1/chat/completions" and body["stream"] is False and body["n"] == 1
    assert body["max_completion_tokens"] == 256
    assert "def feature(): return 5" in body["messages"][1]["content"]
    assert "WRONG" not in body["messages"][1]["content"]
    assert headers["Authorization"].startswith("Bearer explicit-operator")
    assert model_job[4].downloads == [(next(iter(model_job[4].contents)), "new")]
    assert not any(path.endswith("/result") for path, _, _ in model_job[4].calls)
    with pytest.raises(ModelBrokerRefused, match="consumed"):
        invoke(model_job, broker)
    assert len(provider.posts) == 1


def test_gated_anthropic_single_request_and_token_usage(
    model_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, SnapshotTransport, Path],
    ledger: ProviderAttemptLedger,
) -> None:
    client, lease, task, policy, wire, _ = model_job

    # A fresh signed assignment is necessary; changing a local lease alone is refused.
    # Fixture helper is a real signature supplied through the independent source.
    assert lease.model_assignment is not None
    # Rebuild only the test's signed authority so it matches the Anthropic route.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    wire.reply["envelope"] = sign(
        private,
        spec_overrides={
            "execution_class": "review_intelligence",
            "requires_repository_execution": False,
            "policy_digest": "b" * 64,
            "capability": "review_intelligence:agent_task",
            "scope": {"task_id": "task-1", "task_kind": "investigate", "max_model_calls": 1, "max_runtime_ms": 4_000},
        },
        document_overrides={
            "assignment": {
                "model_route_id": "customer-route-1",
                "driver": "anthropic",
                "model": "safe-model-1",
                "configuration_generation": "generation-1",
                "provider_identity": "anthropic",
                "model_data_locus": "external",
            }
        },
    )
    pub = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    client.binding = replace(client.binding, trusted_public_key_hex=pub.hex())
    new_lease = client.claim(execution_class="review_intelligence", model_route_id="customer-route-1")
    assert new_lease is not None
    provider = ProviderTransport("anthropic")
    broker = OneShotReviewModelBroker(route("anthropic"), transport=provider, ledger=ledger)  # type: ignore[arg-type]
    observed = broker.run(
        client,
        new_lease,
        task,
        policy,
        api_key="explicit-anthropic-operator-token",
        explicit_external_source_approval=True,
    )
    assert observed.input_tokens == 120 and observed.output_tokens == 40
    assert observed.provider == "anthropic" and observed.estimated_cost_micros == 200
    path, body, headers = provider.posts[0]
    assert path == "/v1/messages" and body["max_tokens"] == 256
    assert headers["x-api-key"].startswith("explicit-anthropic-")


@pytest.mark.parametrize(
    "mutation",
    [
        {"model_route_id": "foreign-route"},
        {"model_data_locus": "customer_local"},
        {"driver": "codex"},
        {"model": "foreign-model"},
        {"configuration_generation": "changed-generation"},
        {"provider_identity": "other-provider"},
    ],
)
def test_signed_route_mutations_refused_before_byte_fetch(
    model_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, SnapshotTransport, Path],
    ledger: ProviderAttemptLedger,
    mutation: dict[str, str],
) -> None:
    client, lease, task, policy, wire, _ = model_job
    tampered = replace(lease, model_assignment={**(lease.model_assignment or {}), **mutation})
    provider = ProviderTransport("openai")
    broker = OneShotReviewModelBroker(route(), transport=provider, ledger=ledger)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="signed contract"):
        broker.run(
            client,
            tampered,
            task,
            policy,
            api_key="explicit-operator-provider-token-123",
            explicit_external_source_approval=True,
        )
    assert not wire.downloads and not provider.posts


def test_wrong_validly_signed_route_fails_local_admission(
    model_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, SnapshotTransport, Path],
    ledger: ProviderAttemptLedger,
) -> None:
    wire = model_job[4]
    provider = ProviderTransport("openai")
    broker = OneShotReviewModelBroker(route(route_id="operator-changed-route"), transport=provider, ledger=ledger)  # type: ignore[arg-type]
    with pytest.raises(ModelBrokerRefused, match="signed lease assignment"):
        invoke(model_job, broker)
    assert not wire.downloads and not provider.posts


def test_missing_approval_or_credential_never_fetches_source(
    model_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, SnapshotTransport, Path],
    ledger: ProviderAttemptLedger,
) -> None:
    provider = ProviderTransport("openai")
    broker = OneShotReviewModelBroker(route(), transport=provider, ledger=ledger)  # type: ignore[arg-type]
    with pytest.raises(ModelBrokerRefused, match="approval"):
        invoke(model_job, broker, approved=False)
    with pytest.raises(ModelBrokerRefused, match="credential"):
        invoke(model_job, broker, api_key="short")
    assert not model_job[4].downloads and not provider.posts
    assert invoke(model_job, broker).input_tokens == 120


def test_untrusted_context_route_prevents_byte_download_or_external_egress(
    model_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, SnapshotTransport, Path],
    ledger: ProviderAttemptLedger,
) -> None:
    wire = model_job[4]
    wire.context_reply["model_route_id"] = "different-route"
    provider = ProviderTransport("openai")
    broker = OneShotReviewModelBroker(route(), transport=provider, ledger=ledger)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="source context model route"):
        invoke(model_job, broker)
    assert not wire.downloads and not provider.posts


def test_preflight_budget_refuses_before_provider_call(
    model_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, SnapshotTransport, Path],
    ledger: ProviderAttemptLedger,
) -> None:
    provider = ProviderTransport("openai")
    broker = OneShotReviewModelBroker(route(max_cost_micros=1), transport=provider, ledger=ledger)  # type: ignore[arg-type]
    with pytest.raises(ModelBrokerRefused, match="preflight"):
        invoke(model_job, broker)
    assert not provider.posts


@pytest.mark.parametrize(
    "failure", ["timeout", "unauthorized", "missing_usage", "truncated", "huge_usage", "wrong_model", "high_cost"]
)
def test_uncertain_or_invalid_provider_usage_is_not_accepted_or_retried(
    model_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, SnapshotTransport, Path],
    ledger: ProviderAttemptLedger,
    failure: str,
) -> None:
    provider = ProviderTransport("openai")
    cfg = route()
    if failure == "timeout":
        provider.fail = True
    elif failure == "unauthorized":
        provider.status = 403
        provider.answer = {"error": {"message": "credential invalid"}}
    elif failure == "missing_usage":
        provider.answer.pop("usage")
    elif failure == "truncated":
        provider.answer["choices"][0]["finish_reason"] = "length"
    elif failure == "huge_usage":
        provider.answer["usage"]["completion_tokens"] = 9000
    elif failure == "wrong_model":
        provider.answer["model"] = "another-model"
    elif failure == "high_cost":
        cfg = route(max_cost_micros=6_000)
        provider.answer["usage"]["prompt_tokens"] = 50_000
    broker = OneShotReviewModelBroker(cfg, transport=provider, ledger=ledger)  # type: ignore[arg-type]
    with pytest.raises(ModelBrokerRefused) as rejected:
        invoke(model_job, broker)
    assert rejected.value.may_have_charged is True
    with pytest.raises(ModelBrokerRefused, match="consumed"):
        invoke(model_job, broker)
    assert len(provider.posts) == 1
    assert not any(path.endswith("/result") for path, _, _ in model_job[4].calls)


def test_revoked_after_call_discards_model_observation(
    model_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, SnapshotTransport, Path],
    ledger: ProviderAttemptLedger,
) -> None:
    wire = model_job[4]
    provider = ProviderTransport("openai")
    original = provider.post

    def revoke(*args: Any, **kwargs: Any) -> Response:
        result = original(*args, **kwargs)
        wire.heartbeat_state = "cancel_requested"
        return result

    provider.post = revoke  # type: ignore[method-assign]
    broker = OneShotReviewModelBroker(route(), transport=provider, ledger=ledger)  # type: ignore[arg-type]
    with pytest.raises(ModelBrokerRefused, match="revoked") as rejected:
        invoke(model_job, broker)
    assert rejected.value.may_have_charged is True


def test_price_and_model_config_validate_explicit_caps() -> None:
    for args in (
        {"input_micros_per_million": 0},
        {"max_source_bytes": 100_000},
        {"output_micros_per_million": -1},
        {"max_cost_micros": True},
        {"model": "../foreign"},
        {"provider": "custom"},
    ):
        with pytest.raises(ValueError):
            route(**args)


def test_independent_broker_instances_cannot_spend_same_signed_attempt_twice(
    model_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, SnapshotTransport, Path],
    ledger: ProviderAttemptLedger,
) -> None:
    first = ProviderTransport("openai")
    assert invoke(model_job, OneShotReviewModelBroker(route(), ledger=ledger, transport=first)).input_tokens == 120  # type: ignore[arg-type]
    second = ProviderTransport("openai")
    with pytest.raises(ModelAttemptAlreadySpent, match="already consumed"):
        invoke(
            model_job,
            OneShotReviewModelBroker(route(), ledger=ProviderAttemptLedger(ledger.review_state_dir), transport=second),
        )  # type: ignore[arg-type]
    assert len(first.posts) == 1 and len(second.posts) == 0


def test_failed_request_is_persistently_consumed_without_replay(
    model_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, SnapshotTransport, Path],
    ledger: ProviderAttemptLedger,
) -> None:
    provider = ProviderTransport("openai")
    provider.fail = True
    with pytest.raises(ModelBrokerRefused) as refused:
        invoke(model_job, OneShotReviewModelBroker(route(), ledger=ledger, transport=provider))  # type: ignore[arg-type]
    assert refused.value.may_have_charged
    next_broker = OneShotReviewModelBroker(route(), ledger=ledger, transport=ProviderTransport("openai"))  # type: ignore[arg-type]
    with pytest.raises(ModelAttemptAlreadySpent):
        invoke(model_job, next_broker)


def test_missing_ledger_cannot_call_model_or_download_source(
    model_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, SnapshotTransport, Path],
    ledger: ProviderAttemptLedger,
) -> None:
    provider = ProviderTransport("openai")
    broker = OneShotReviewModelBroker(route(), transport=provider)  # type: ignore[arg-type]
    with pytest.raises(ModelBrokerRefused, match="ledger"):
        invoke(model_job, broker)
    assert not model_job[4].downloads and not provider.posts


def test_ledger_refuses_redirect_and_nonprivate_state(
    model_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, SnapshotTransport, Path],
    ledger: ProviderAttemptLedger,
    tmp_path: Path,
) -> None:
    from os import chmod

    target = ledger.review_state_dir
    chmod(target, 0o755)
    with pytest.raises(ModelAttemptAlreadySpent, match="owner-only"):
        invoke(model_job, OneShotReviewModelBroker(route(), ledger=ledger, transport=ProviderTransport("openai")))  # type: ignore[arg-type]
    chmod(target, 0o700)
    alias = tmp_path / "redirect"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ModelAttemptAlreadySpent, match="symlink"):
        invoke(
            model_job,
            OneShotReviewModelBroker(
                route(), ledger=ProviderAttemptLedger(alias), transport=ProviderTransport("openai")
            ),
        )  # type: ignore[arg-type]


def test_provider_https_origin_is_not_overridable(
    ledger: ProviderAttemptLedger,
) -> None:
    from lemoncrow_client.transport import HttpTransport

    transport = HttpTransport("http://127.0.0.1:9998", timeout_s=1)
    with pytest.raises(ModelBrokerRefused, match="canonical HTTPS"):
        OneShotReviewModelBroker(route(), ledger=ledger, transport=transport)


def test_secret_in_normal_source_filename_still_blocks_external_egress(
    model_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, SnapshotTransport, Path],
    ledger: ProviderAttemptLedger,
) -> None:
    wire = model_job[4]
    source = b"# embedded credentials\nkey = '-----BEGIN PRIVATE KEY-----'\n"
    digest = hashlib.sha256(source).hexdigest()
    wire.contents[digest] = source
    wire.context_reply["manifest"] = {
        "src/normal.py": {"content_digest": digest, "size": len(source), "mode": 0o100644},
    }
    provider = ProviderTransport("openai")
    broker = OneShotReviewModelBroker(route(), ledger=ledger, transport=provider)  # type: ignore[arg-type]
    with pytest.raises(ModelBrokerRefused, match="credential-like"):
        invoke(model_job, broker)
    assert not provider.posts
    assert not list((ledger.review_state_dir / "model-attempts").glob("*.spent"))


def test_bounded_source_excerpts_report_exact_file_coverage(
    model_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, SnapshotTransport, Path],
    ledger: ProviderAttemptLedger,
) -> None:
    wire = model_job[4]
    ignored = b"# long context\n" + b"X" * 600
    digest = hashlib.sha256(ignored).hexdigest()
    wire.contents[digest] = ignored
    wire.context_reply["manifest"]["src/large.py"] = {
        "content_digest": digest,
        "size": len(ignored),
        "mode": 0o100644,
    }
    provider = ProviderTransport("openai")
    broker = OneShotReviewModelBroker(route(max_source_bytes=256), ledger=ledger, transport=provider)  # type: ignore[arg-type]
    observed = invoke(model_job, broker)
    assert observed.included_source_paths == ("src/file.py",)
    assert observed.source_files_available == 2
    assert "src/large.py" not in provider.posts[0][1]["messages"][1]["content"]


def test_real_loopback_http_provider_protocol_with_pinned_production_origin(
    model_job: tuple[ReviewWorkerClient, WorkerLease, Any, CompanionPolicy, SnapshotTransport, Path],
    ledger: ProviderAttemptLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real HTTP request framing via HttpTransport; NOT live TLS or model API."""
    import http.client
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from lemoncrow_client.transport import HttpTransport

    received: list[dict[str, Any]] = []

    class FakeProvider(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            assert self.path == "/v1/chat/completions"
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            payload = json.loads(raw)
            received.append(payload)
            assert self.headers["Authorization"] == "Bearer explicit-operator-provider-token-123"
            reply = json.dumps(
                {
                    "model": "safe-model-1",
                    "choices": [
                        {
                            "message": {"content": "Found a possible regression"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 40},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    transport = HttpTransport("https://api.openai.com", timeout_s=5)
    original_acquire = HttpTransport._acquire

    def loopback_acquire(self: HttpTransport, budget: float) -> tuple[http.client.HTTPConnection, bool]:
        if self is transport:
            return http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=budget), False
        return original_acquire(self, budget)

    monkeypatch.setattr(HttpTransport, "_acquire", loopback_acquire)
    try:
        broker = OneShotReviewModelBroker(route(), ledger=ledger, transport=transport)
        observation = invoke(model_job, broker)
        assert observation.text == "Found a possible regression"
        assert observation.estimated_cost_micros == 200
        assert len(received) == 1
        assert "def feature(): return 5" in received[0]["messages"][1]["content"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        transport.close()

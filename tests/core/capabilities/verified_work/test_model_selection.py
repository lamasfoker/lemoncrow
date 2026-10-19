from __future__ import annotations

from lemoncrow.core.capabilities.verified_work.settings import VerifiedWorkSettings
from lemoncrow.pro.capabilities.model_setup.models import CustomEndpoint, CustomModel
from lemoncrow.pro.capabilities.verified_work import calibration, outcomes
from lemoncrow.pro.capabilities.verified_work.model import resolve_worker_model
from lemoncrow.pro.capabilities.verified_work.outcomes import ModelOutcomeStats


def _endpoint(*models: CustomModel, locus: str = "local") -> CustomEndpoint:
    return CustomEndpoint(
        name="box",
        base_url="http://127.0.0.1:11434/v1",
        locus=locus,  # type: ignore[arg-type]
        models=models,
    )


def test_shadow_auto_can_explore_unproven_local_model(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.model_setup import endpoints

    monkeypatch.setattr(
        endpoints,
        "list_endpoints",
        lambda root=None: [
            _endpoint(CustomModel("qwen", supports_structured_output=True, observed_tokens_per_second=30))
        ],
    )
    monkeypatch.setattr(outcomes, "load_model_outcome", lambda *args, **kwargs: ModelOutcomeStats())

    choice, reason = resolve_worker_model(VerifiedWorkSettings(mode="shadow"), kind="typecheck")

    assert choice is not None
    assert choice.model == "custom/box/qwen"
    assert reason == "auto_evidence_model"


def test_enforce_auto_refuses_unproven_model(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.model_setup import endpoints

    monkeypatch.setattr(
        endpoints,
        "list_endpoints",
        lambda root=None: [_endpoint(CustomModel("qwen", supports_structured_output=True))],
    )
    monkeypatch.setattr(
        outcomes, "load_model_outcome", lambda *args, **kwargs: ModelOutcomeStats(attempts=2, verified=2)
    )

    choice, reason = resolve_worker_model(VerifiedWorkSettings(mode="enforce"), kind="typecheck")

    assert choice is None
    assert reason == "no_evidence_qualified_auto_model"


def test_enforce_auto_chooses_evidence_qualified_model_over_faster_bad_model(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.model_setup import endpoints

    monkeypatch.setattr(
        endpoints,
        "list_endpoints",
        lambda root=None: [
            _endpoint(
                CustomModel("reliable", supports_structured_output=True, observed_tokens_per_second=20),
                CustomModel("fast-bad", supports_structured_output=True, observed_tokens_per_second=100),
            )
        ],
    )

    def stats(model: str, kind: str, root=None):  # type: ignore[no-untyped-def]
        if model.endswith("/reliable"):
            return ModelOutcomeStats(attempts=10, verified=9, total_latency_ms=20_000)
        return ModelOutcomeStats(attempts=10, verified=5, total_latency_ms=5_000)

    monkeypatch.setattr(outcomes, "load_model_outcome", stats)
    monkeypatch.setattr(
        calibration,
        "calibrated_model_kind",
        lambda model, kind, root=None, min_pairs=20: {
            "qualified": model.endswith("/reliable"),
            "median_net_savings_usd": 0.25 if model.endswith("/reliable") else 0.0,
        },
    )
    settings = VerifiedWorkSettings(mode="enforce", auto_min_samples=5, auto_min_success_rate=0.8)

    choice, reason = resolve_worker_model(settings, kind="typecheck")

    assert choice is not None
    assert choice.model == "custom/box/reliable"
    assert reason == "auto_evidence_model"


def test_explicit_model_bypasses_auto_history_but_respects_local_only(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.model_setup import endpoints

    local = _endpoint(CustomModel("qwen", supports_structured_output=True), locus="local")
    monkeypatch.setattr(endpoints, "get_endpoint", lambda root, name: local)
    settings = VerifiedWorkSettings(
        mode="enforce",
        model="custom/box/qwen",
        model_policy="explicit",
    )
    choice, reason = resolve_worker_model(settings, kind="typecheck")
    assert choice is not None
    assert reason == "explicit_model"

    unknown = _endpoint(CustomModel("qwen", supports_structured_output=True), locus="unknown")
    monkeypatch.setattr(endpoints, "get_endpoint", lambda root, name: unknown)
    settings = VerifiedWorkSettings(
        mode="enforce",
        model="custom/box/qwen",
        model_policy="local_only",
    )
    choice, reason = resolve_worker_model(settings, kind="typecheck")
    assert choice is None
    assert reason == "model_endpoint_not_local"


def test_enforce_configured_model_self_demotes_on_recent_quality_regression(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.model_setup import endpoints
    from lemoncrow.pro.capabilities.verified_work.outcomes import record_model_outcome

    endpoint = _endpoint(CustomModel("qwen", supports_structured_output=True), locus="local")
    monkeypatch.setattr(endpoints, "get_endpoint", lambda root, name: endpoint)
    monkeypatch.setattr(
        calibration,
        "calibrated_model_kind",
        lambda model, kind, root=None, min_pairs=20: {
            "qualified": True,
            "median_net_savings_usd": 0.25,
        },
    )
    model = "custom/box/qwen"
    for _ in range(40):
        record_model_outcome(
            model,
            "typecheck",
            verified=True,
            promoted=True,
            latency_ms=100,
            input_tokens=1,
            output_tokens=1,
            provider_cost_usd=0.0,
            mode="enforce",
            root=tmp_path,
        )
    for _ in range(20):
        record_model_outcome(
            model,
            "typecheck",
            verified=False,
            promoted=False,
            latency_ms=100,
            input_tokens=1,
            output_tokens=1,
            provider_cost_usd=0.0,
            mode="enforce",
            root=tmp_path,
        )

    settings = VerifiedWorkSettings(
        mode="enforce",
        model=model,
        model_policy="local_only",
        auto_min_samples=20,
        auto_min_success_rate=0.9,
        calibration_min_pairs=20,
    )
    choice, reason = resolve_worker_model(settings, kind="typecheck", root=tmp_path)

    assert choice is None
    assert reason == "configured_model_not_auto_qualified"


def test_shadow_auto_explores_unproven_model_before_proven_bad_model(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.model_setup import endpoints

    monkeypatch.setattr(
        endpoints,
        "list_endpoints",
        lambda root=None: [
            _endpoint(
                CustomModel("bad", supports_structured_output=True, observed_tokens_per_second=100),
                CustomModel("fresh", supports_structured_output=True, observed_tokens_per_second=10),
            )
        ],
    )

    def stats(model: str, kind: str, root=None, min_samples=20):  # type: ignore[no-untyped-def]
        if model.endswith("/bad"):
            return ModelOutcomeStats(attempts=20, verified=4, total_latency_ms=2_000)
        return ModelOutcomeStats()

    monkeypatch.setattr(outcomes, "qualification_model_outcome", stats)
    settings = VerifiedWorkSettings(mode="shadow", auto_min_samples=20, auto_min_success_rate=0.9)

    choice, reason = resolve_worker_model(settings, kind="typecheck")

    assert choice is not None
    assert choice.model == "custom/box/fresh"
    assert reason == "auto_evidence_model"


def test_shadow_auto_samples_least_observed_unproven_model(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.model_setup import endpoints

    monkeypatch.setattr(
        endpoints,
        "list_endpoints",
        lambda root=None: [
            _endpoint(
                CustomModel("five", supports_structured_output=True, observed_tokens_per_second=100),
                CustomModel("two", supports_structured_output=True, observed_tokens_per_second=10),
            )
        ],
    )

    def stats(model: str, kind: str, root=None, min_samples=20):  # type: ignore[no-untyped-def]
        attempts = 5 if model.endswith("/five") else 2
        return ModelOutcomeStats(attempts=attempts, verified=attempts, total_latency_ms=attempts * 100)

    monkeypatch.setattr(outcomes, "qualification_model_outcome", stats)
    settings = VerifiedWorkSettings(mode="shadow", auto_min_samples=20, auto_min_success_rate=0.9)

    choice, _ = resolve_worker_model(settings, kind="typecheck")

    assert choice is not None
    assert choice.model == "custom/box/two"


def test_auto_pool_is_limited_to_repository_opted_in_models(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.model_setup import endpoints

    monkeypatch.setattr(
        endpoints,
        "list_endpoints",
        lambda root=None: [
            _endpoint(
                CustomModel("granted", supports_structured_output=True, observed_tokens_per_second=10),
                CustomModel("other", supports_structured_output=True, observed_tokens_per_second=100),
            )
        ],
    )
    monkeypatch.setattr(outcomes, "load_model_outcome", lambda *args, **kwargs: ModelOutcomeStats())

    unrestricted, _ = resolve_worker_model(VerifiedWorkSettings(mode="shadow"), kind="typecheck")
    restricted, _ = resolve_worker_model(
        VerifiedWorkSettings(mode="shadow"), kind="typecheck", allowed_models=("custom/box/granted",)
    )
    nothing, reason = resolve_worker_model(VerifiedWorkSettings(mode="shadow"), kind="typecheck", allowed_models=())

    assert unrestricted is not None and unrestricted.model == "custom/box/other"
    assert restricted is not None and restricted.model == "custom/box/granted"
    assert nothing is None
    assert reason


def test_configured_model_must_be_repository_opted_in(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.model_setup import endpoints

    endpoint = _endpoint(CustomModel("pinned", supports_structured_output=True))
    monkeypatch.setattr(endpoints, "get_endpoint", lambda root, name: endpoint)
    settings = VerifiedWorkSettings(mode="enforce", model="custom/box/pinned", model_policy="explicit")

    allowed, _ = resolve_worker_model(settings, kind="typecheck", allowed_models=("custom/box/pinned",))
    refused, reason = resolve_worker_model(settings, kind="typecheck", allowed_models=("custom/box/other",))

    assert allowed is not None and allowed.model == "custom/box/pinned"
    assert refused is None
    assert reason == "configured_model_not_opted_in"

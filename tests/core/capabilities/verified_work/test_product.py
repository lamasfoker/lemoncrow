from __future__ import annotations

from pathlib import Path

from lemoncrow.core.capabilities.verified_work.settings import VerifiedWorkSettings
from lemoncrow.pro.capabilities.model_setup.endpoints import add_endpoint
from lemoncrow.pro.capabilities.verified_work.outcomes import record_model_outcome
from lemoncrow.pro.capabilities.verified_work.product import (
    IsolationStatus,
    KindQualification,
    local_model_candidates,
    product_status,
)


def _register_local(root: Path) -> str:
    endpoint = add_endpoint(
        root,
        "http://127.0.0.1:11434/v1",
        name="ollama",
        models=["test-coder"],
        locus="local",
    )
    return endpoint.model_ids()[0]


def test_local_model_candidates_exclude_remote(tmp_path: Path) -> None:
    local = _register_local(tmp_path)
    add_endpoint(
        tmp_path,
        "https://models.example.com/v1",
        name="remote",
        models=["remote-coder"],
        locus="remote",
    )

    assert local_model_candidates(tmp_path) == [local]


def test_product_status_ready_when_any_kind_is_fully_qualified(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    model = _register_local(tmp_path)
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.verified_work.product.probe_isolation",
        lambda: IsolationStatus(True, True, True, "qualified"),
    )
    settings = VerifiedWorkSettings(
        mode="shadow",
        model=model,
        model_policy="local_only",
        kinds=("lint", "typecheck"),
        auto_min_samples=3,
        auto_min_success_rate=0.9,
        auto_max_avg_latency_ms=5_000,
    )
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.verified_work.calibration.summarize_calibration",
        lambda root=None, min_pairs=20: {
            "by_model_kind": [
                {"model": model, "kind": "lint", "pairs": 3, "qualified": True, "median_net_savings_usd": 0.2},
                {"model": model, "kind": "typecheck", "pairs": 3, "qualified": True, "median_net_savings_usd": 0.3},
            ]
        },
    )

    for _ in range(3):
        record_model_outcome(
            model,
            "lint",
            verified=True,
            promoted=False,
            latency_ms=100,
            input_tokens=10,
            output_tokens=5,
            provider_cost_usd=0.0,
            mode="shadow",
            root=tmp_path,
        )
    status = product_status(tmp_path, settings)
    assert status.state == "ready"
    assert status.ready_for_auto is True
    by_kind = {row.kind: row for row in status.kinds}
    assert by_kind["lint"].auto_qualified is True
    assert by_kind["typecheck"].auto_qualified is False

    for _ in range(3):
        record_model_outcome(
            model,
            "typecheck",
            verified=True,
            promoted=False,
            latency_ms=100,
            input_tokens=10,
            output_tokens=5,
            provider_cost_usd=0.0,
            mode="shadow",
            root=tmp_path,
        )
    status = product_status(tmp_path, settings)
    assert status.state == "ready"
    assert status.ready_for_auto is True
    assert all(row.auto_qualified for row in status.kinds)


def test_auto_state_degrades_when_isolation_is_missing(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    model = _register_local(tmp_path)
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.verified_work.product.probe_isolation",
        lambda: IsolationStatus(False, False, True, "missing network isolation"),
    )
    settings = VerifiedWorkSettings(
        mode="enforce",
        model=model,
        model_policy="explicit",
        kinds=("lint",),
        auto_min_samples=1,
        auto_min_success_rate=0.9,
    )
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.verified_work.calibration.summarize_calibration",
        lambda root=None, min_pairs=20: {
            "by_model_kind": [
                {"model": model, "kind": "lint", "pairs": 1, "qualified": True, "median_net_savings_usd": 0.2}
            ]
        },
    )
    record_model_outcome(
        model,
        "lint",
        verified=True,
        promoted=True,
        latency_ms=100,
        input_tokens=1,
        output_tokens=1,
        provider_cost_usd=0.0,
        mode="enforce",
        root=tmp_path,
    )

    status = product_status(tmp_path, settings)

    assert status.state == "degraded"
    assert status.ready_for_auto is False


def test_product_status_stays_learning_when_repairs_do_not_save_money(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    model = _register_local(tmp_path)
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.verified_work.product.probe_isolation",
        lambda: IsolationStatus(True, True, True, "qualified"),
    )
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.verified_work.calibration.summarize_calibration",
        lambda root=None, min_pairs=20: {
            "by_model_kind": [
                {"model": model, "kind": "lint", "pairs": 20, "qualified": False, "median_net_savings_usd": 0.0}
            ]
        },
    )
    settings = VerifiedWorkSettings(
        mode="shadow", model=model, model_policy="local_only", kinds=("lint",), auto_min_samples=1
    )
    record_model_outcome(
        model,
        "lint",
        verified=True,
        promoted=False,
        latency_ms=100,
        input_tokens=1,
        output_tokens=1,
        provider_cost_usd=0.0,
        mode="shadow",
        root=tmp_path,
    )
    status = product_status(tmp_path, settings)
    assert status.state == "learning"
    assert status.economics_qualified is False
    assert status.ready_for_auto is False


def test_failed_probe_timestamp_is_not_treated_as_healthy(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.model_setup import endpoints
    from lemoncrow.pro.capabilities.model_setup.models import CustomEndpoint, CustomModel
    from lemoncrow.pro.capabilities.verified_work import product

    endpoint = CustomEndpoint(
        name="ollama",
        base_url="http://127.0.0.1:11434/v1",
        locus="local",
        models=(CustomModel("coder", probed_at="2026-10-04T00:00:00Z"),),
    )
    monkeypatch.setattr(product, "discover_local_endpoints", lambda root: [])
    monkeypatch.setattr(product, "local_model_candidates", lambda root: ["custom/ollama/coder"])
    monkeypatch.setattr(endpoints, "get_endpoint", lambda root, name: endpoint)
    calls: list[str] = []

    def fake_probe(root: Path, model_id: str, *, timeout: float) -> bool:
        calls.append(model_id)
        return True

    monkeypatch.setattr(product, "_probe_candidate", fake_probe)

    model, reason = product.ensure_local_worker(tmp_path)

    assert calls == ["custom/ollama/coder"]
    assert model == "custom/ollama/coder"
    assert reason == "probed_local_model"


def test_doctor_discovery_is_read_only(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from lemoncrow.core.capabilities.providers.config import providers_config_path
    from lemoncrow.pro.capabilities.model_setup import endpoints, probe
    from lemoncrow.pro.capabilities.verified_work.product import diagnose_local_worker

    monkeypatch.setattr(endpoints, "discover_endpoint_models", lambda *args, **kwargs: ["test-coder"])
    monkeypatch.setattr(probe, "probe_model", lambda *args, **kwargs: SimpleNamespace(ok=True))

    before = providers_config_path(tmp_path)
    assert before.exists() is False

    result = diagnose_local_worker(tmp_path)

    assert result.model is not None
    assert result.model.endswith("/test-coder")
    assert result.model_reason == "probed_local_model"
    assert result.detected_endpoints
    assert before.exists() is False


def _git_repo(path: Path) -> Path:
    import subprocess

    path.mkdir(parents=True)
    (path / "a.txt").write_text("x\n", encoding="utf-8")
    for args in (("init",), ("add", "."), ("-c", "user.email=t@e", "-c", "user.name=t", "commit", "-m", "b")):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    return path


def test_ready_never_implies_auto_and_auto_is_per_repository(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.core.foundation.paths import default_store_root
    from lemoncrow.pro.capabilities.verified_work.consent import grant_auto, repository_identity

    root = default_store_root()
    model = _register_local(root)
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.verified_work.product.probe_isolation",
        lambda: IsolationStatus(True, True, True, "qualified"),
    )
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.verified_work.calibration.summarize_calibration",
        lambda root=None, min_pairs=20: {
            "by_model_kind": [
                {"model": model, "kind": "lint", "pairs": 3, "qualified": True, "median_net_savings_usd": 0.2}
            ]
        },
    )
    repo = repository_identity(_git_repo(tmp_path / "repo"))
    other = repository_identity(_git_repo(tmp_path / "other"))
    assert repo is not None and other is not None
    for _ in range(3):
        record_model_outcome(
            model,
            "lint",
            verified=True,
            promoted=False,
            latency_ms=100,
            input_tokens=1,
            output_tokens=1,
            provider_cost_usd=0.0,
            mode="shadow",
            root=root,
            repo=repo.key,
        )
    enforce = VerifiedWorkSettings(mode="enforce", model="auto", kinds=("lint",), auto_min_samples=3)
    shadow = VerifiedWorkSettings(mode="shadow", model="auto", kinds=("lint",), auto_min_samples=3)

    # Ready with Auto requested in settings is still not Auto without opt-in.
    status = product_status(root, enforce, repo=repo)
    assert status.state == "ready"
    assert status.reason == "auto_needs_repository_opt_in"
    assert status.repository is not None
    assert status.repository.grant is None
    assert status.repository.grantable == ((model, "lint"),)
    assert product_status(root, shadow, repo=repo).state == "ready"

    grant_auto(repo, [(model, "lint")], root=root)
    status = product_status(root, enforce, repo=repo)
    assert status.state == "auto"
    assert status.repository is not None
    assert status.repository.auto_pairs == ((model, "lint"),)
    # Opt-in never turns Learning mode into Auto.
    assert product_status(root, shadow, repo=repo).state == "ready"

    # A repository without its own window is Learning even though the model is globally ready.
    other_status = product_status(root, shadow, repo=other)
    assert other_status.state == "learning"
    assert other_status.repository is not None
    assert other_status.repository.grantable == ()
    grant_auto(other, [(model, "lint")], root=root)
    assert product_status(root, enforce, repo=other).state == "degraded"

    # Outside any repository Auto never applies.
    assert product_status(root, enforce).state != "auto"


def test_auto_pool_status_surfaces_ready_model_while_other_model_is_explored(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.verified_work import calibration as calibration_module
    from lemoncrow.pro.capabilities.verified_work import product as product_module

    ready = "custom/box/ready"
    exploring = "custom/box/exploring"
    monkeypatch.setattr(product_module, "probe_isolation", lambda: IsolationStatus(True, True, True, "qualified"))
    monkeypatch.setattr(product_module, "local_model_candidates", lambda root: [exploring, ready])

    def quality(root, model, settings):  # type: ignore[no-untyped-def]
        if model == ready:
            return (KindQualification("lint", 20, 0.95, 100.0, True),)
        return (KindQualification("lint", 2, 1.0, 100.0, False),)

    monkeypatch.setattr(product_module, "qualification_for_model", quality)
    monkeypatch.setattr(
        calibration_module,
        "summarize_calibration",
        lambda root=None, min_pairs=20: {
            "by_model_kind": [
                {
                    "model": ready,
                    "kind": "lint",
                    "pairs": 20,
                    "qualified": True,
                    "median_net_savings_usd": 0.2,
                }
            ]
        },
    )
    settings = VerifiedWorkSettings(mode="shadow", model="auto", kinds=("lint",))

    status = product_module.product_status(tmp_path, settings)

    assert status.state == "ready"
    assert status.model == ready
    assert status.ready_for_auto is True


def _repository_with_windows(tmp_path: Path, monkeypatch, *, isolated: bool = True, ready_kinds=("lint", "typecheck")):  # type: ignore[no-untyped-def]
    """A repository whose own lint and typecheck windows both qualify."""
    from lemoncrow.core.foundation.paths import default_store_root
    from lemoncrow.pro.capabilities.verified_work.consent import repository_identity

    root = default_store_root()
    model = _register_local(root)
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.verified_work.product.probe_isolation",
        lambda: IsolationStatus(isolated, isolated, True, "qualified" if isolated else "missing network isolation"),
    )
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.verified_work.calibration.summarize_calibration",
        lambda root=None, min_pairs=20: {
            "by_model_kind": [
                {"model": model, "kind": kind, "pairs": 3, "qualified": True, "median_net_savings_usd": 0.2}
                for kind in ready_kinds
            ]
        },
    )
    repo = repository_identity(_git_repo(tmp_path / "repo"))
    assert repo is not None
    for kind in ("lint", "typecheck"):
        for _ in range(3):
            record_model_outcome(
                model,
                kind,  # type: ignore[arg-type]
                verified=True,
                promoted=False,
                latency_ms=100,
                input_tokens=1,
                output_tokens=1,
                provider_cost_usd=0.0,
                mode="shadow",
                root=root,
                repo=repo.key,
            )
    settings = VerifiedWorkSettings(mode="enforce", model="auto", kinds=("lint", "typecheck"), auto_min_samples=3)
    return root, model, repo, settings


def test_unqualified_isolation_makes_nothing_grantable(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root, _model, repo, settings = _repository_with_windows(tmp_path, monkeypatch, isolated=False)

    status = product_status(root, settings, repo=repo)

    assert status.repository is not None
    assert {window.kind for window in status.repository.windows if window.qualified} == {"lint", "typecheck"}
    assert status.repository.grantable == ()
    assert status.state != "auto"


def test_repository_window_alone_does_not_make_a_kind_grantable(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root, model, repo, settings = _repository_with_windows(tmp_path, monkeypatch, ready_kinds=("lint",))

    status = product_status(root, settings, repo=repo)

    assert status.repository is not None
    # typecheck qualifies in this repository but is not globally Ready (economics).
    assert {window.kind for window in status.repository.windows if window.qualified} == {"lint", "typecheck"}
    assert status.repository.grantable == ((model, "lint"),)


def test_auto_applies_only_to_opted_in_pairs_when_more_are_ready(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.verified_work.consent import grant_auto

    root, model, repo, settings = _repository_with_windows(tmp_path, monkeypatch)
    grant_auto(repo, [(model, "lint")], root=root)

    status = product_status(root, settings, repo=repo)

    assert status.state == "auto"
    assert status.repository is not None
    assert status.repository.grantable == ((model, "lint"), (model, "typecheck"))
    # typecheck graduated after the opt-in: Ready here, but not Auto.
    assert status.repository.auto_pairs == ((model, "lint"),)

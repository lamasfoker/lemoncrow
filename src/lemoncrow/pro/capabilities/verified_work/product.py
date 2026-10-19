"""Product-facing qualification and local-worker discovery for Verified Local Work."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from lemoncrow.core.capabilities.verified_work.models import RepairKind
from lemoncrow.core.capabilities.verified_work.settings import VerifiedWorkSettings

from .consent import AutoGrant, RepositoryIdentity

SavingsState = Literal["off", "blocked", "learning", "ready", "auto", "degraded"]

# Standard loopback OpenAI-compatible endpoints. Discovery is bounded and never
# scans the LAN; source must not leave the machine during automatic onboarding.
_LOCAL_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("ollama", "http://127.0.0.1:11434/v1"),
    ("lmstudio", "http://127.0.0.1:1234/v1"),
    ("llamacpp", "http://127.0.0.1:8080/v1"),
    ("vllm", "http://127.0.0.1:8000/v1"),
)

_CODE_HINTS: tuple[str, ...] = (
    "coder",
    "code",
    "devstral",
    "qwen",
    "deepseek",
    "starcoder",
    "codestral",
    "gpt-oss",
    "granite",
)


@dataclass(frozen=True, slots=True)
class KindQualification:
    kind: RepairKind
    attempts: int
    verification_rate: float
    average_latency_ms: float
    qualified: bool
    economics_qualified: bool = False
    calibration_pairs: int = 0
    median_net_savings_usd: float | None = None

    @property
    def auto_qualified(self) -> bool:
        return self.qualified and self.economics_qualified


@dataclass(frozen=True, slots=True)
class IsolationStatus:
    qualified: bool
    network_isolated: bool
    resource_limited: bool
    detail: str


@dataclass(frozen=True, slots=True)
class RepositoryWindow:
    """One repository's own recent outcome window for a (model, kind) pair."""

    model: str
    kind: RepairKind
    attempts: int
    verification_rate: float
    average_latency_ms: float
    qualified: bool


@dataclass(frozen=True, slots=True)
class RepositoryStatus:
    """Per-repository Auto readiness: consent plus repository confidence.

    ``grantable`` are pairs Ready in this repository (global qualification,
    calibrated economics, isolation and this repository's own window);
    ``auto_pairs`` are the opted-in subset that Auto applies to right now.
    """

    key: str
    root: str
    grant: AutoGrant | None
    windows: tuple[RepositoryWindow, ...]
    grantable: tuple[tuple[str, str], ...]
    auto_pairs: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class SavingsProductStatus:
    state: SavingsState
    mode: str
    model: str
    model_reason: str
    isolation: IsolationStatus
    kinds: tuple[KindQualification, ...]
    economics_qualified: bool
    calibration_pairs: int
    calibrated_net_per_repair_usd: float | None
    reason: str
    repository: RepositoryStatus | None = None

    @property
    def ready_for_auto(self) -> bool:
        """Globally Ready (model pool). Ready never implies Auto: see ``repository``."""
        return (
            bool(self.model)
            and self.isolation.qualified
            and any(row.auto_qualified for row in self.kinds)
            and self.economics_qualified
        )


def _model_name_rank(model_id: str) -> tuple[int, int, str]:
    lower = model_id.lower()
    hinted = any(token in lower for token in _CODE_HINTS)
    # Prefer code-looking names for *onboarding only*. Runtime authority still
    # comes from measured verifier outcomes, never from this name heuristic.
    return (0 if hinted else 1, len(model_id), model_id)


@dataclass(frozen=True, slots=True)
class LocalDoctorResult:
    model: str | None
    model_reason: str
    detected_endpoints: tuple[str, ...]


def diagnose_local_worker(root: Path, *, timeout: float = 2.0) -> LocalDoctorResult:
    """Read-only local runtime discovery/probing for `savings doctor`.

    Unlike `ensure_local_worker`, this never writes providers.json or probe
    results. It may make bounded loopback HTTP requests.
    """
    from lemoncrow.pro.capabilities.model_setup.endpoints import (
        discover_endpoint_models,
        list_endpoints,
    )
    from lemoncrow.pro.capabilities.model_setup.probe import probe_model
    from lemoncrow.pro.capabilities.model_setup.transport import NO_KEY_PLACEHOLDER

    candidates: list[tuple[str, str, str, int | None]] = []
    detected: list[str] = []
    seen_urls: set[str] = set()

    for endpoint in list_endpoints(root):
        if endpoint.locus != "local":
            continue
        url = endpoint.base_url.rstrip("/")
        seen_urls.add(url)
        detected.append(endpoint.name)
        for model in endpoint.models:
            candidates.append((endpoint.name, endpoint.base_url, model.raw_id, model.context_window))

    for name, url in _LOCAL_ENDPOINTS:
        normalized = url.rstrip("/")
        if normalized in seen_urls:
            continue
        models = discover_endpoint_models(normalized, NO_KEY_PLACEHOLDER, timeout=min(timeout, 0.75))
        if not models:
            continue
        detected.append(name)
        seen_urls.add(normalized)
        for raw_id in models:
            candidates.append((name, normalized, raw_id, None))

    if not candidates:
        return LocalDoctorResult(None, "no_local_runtime_found", tuple(dict.fromkeys(detected)))

    candidates.sort(key=lambda row: _model_name_rank(f"custom/{row[0]}/{row[2]}"))
    for endpoint_name, base_url, raw_id, context_window in candidates[:3]:
        result = probe_model(
            base_url,
            raw_id,
            endpoint_name=endpoint_name,
            api_key=NO_KEY_PLACEHOLDER,
            timeout=timeout,
            context_window=context_window,
        )
        if result.ok:
            return LocalDoctorResult(
                f"custom/{endpoint_name}/{raw_id}",
                "probed_local_model",
                tuple(dict.fromkeys(detected)),
            )
    return LocalDoctorResult(None, "no_reachable_local_model", tuple(dict.fromkeys(detected)))


def discover_local_endpoints(root: Path, *, timeout: float = 0.75) -> list[str]:
    """Discover/register standard loopback runtimes. Returns endpoint names added."""
    from lemoncrow.pro.capabilities.model_setup.endpoints import (
        add_endpoint,
        discover_endpoint_models,
        list_endpoints,
    )
    from lemoncrow.pro.capabilities.model_setup.transport import NO_KEY_PLACEHOLDER

    existing = list_endpoints(root)
    existing_urls = {endpoint.base_url.rstrip("/"): endpoint for endpoint in existing}
    existing_names = {endpoint.name for endpoint in existing}
    added: list[str] = []
    for base_name, url in _LOCAL_ENDPOINTS:
        normalized = url.rstrip("/")
        if normalized in existing_urls:
            continue
        models = discover_endpoint_models(normalized, NO_KEY_PLACEHOLDER, timeout=timeout)
        if not models:
            continue
        name = base_name
        suffix = 2
        while name in existing_names:
            name = f"{base_name}-{suffix}"
            suffix += 1
        try:
            endpoint = add_endpoint(root, normalized, name=name, models=models, locus="local", timeout=timeout)
        except ValueError:
            continue
        existing_names.add(endpoint.name)
        existing_urls[normalized] = endpoint
        added.append(endpoint.name)
    return added


def _probe_candidate(root: Path, model_id: str, *, timeout: float) -> bool:
    from lemoncrow.pro.capabilities.model_setup.endpoints import get_endpoint, record_probe
    from lemoncrow.pro.capabilities.model_setup.models import split_custom_model_id
    from lemoncrow.pro.capabilities.model_setup.probe import probe_model
    from lemoncrow.pro.capabilities.model_setup.transport import NO_KEY_PLACEHOLDER

    parts = split_custom_model_id(model_id)
    if parts is None:
        return False
    endpoint_name, raw_id = parts
    endpoint = get_endpoint(root, endpoint_name)
    if endpoint is None or endpoint.locus != "local":
        return False
    model = next((row for row in endpoint.models if row.raw_id == raw_id), None)
    if model is None:
        return False
    result = probe_model(
        endpoint.base_url,
        raw_id,
        endpoint_name=endpoint.name,
        api_key=NO_KEY_PLACEHOLDER,
        timeout=timeout,
        context_window=model.context_window,
    )
    record_probe(root, result)
    return result.ok


def local_model_candidates(root: Path) -> list[str]:
    from lemoncrow.pro.capabilities.model_setup.endpoints import list_endpoints

    rows: list[str] = []
    for endpoint in list_endpoints(root):
        if endpoint.locus != "local":
            continue
        rows.extend(endpoint.model_ids())
    return sorted(set(rows), key=_model_name_rank)


def ensure_local_worker(root: Path, *, requested: str = "auto", timeout: float = 5.0) -> tuple[str | None, str]:
    """Return one reachable local worker, probing only a small bounded shortlist."""
    from lemoncrow.pro.capabilities.model_setup.endpoints import get_endpoint
    from lemoncrow.pro.capabilities.model_setup.models import split_custom_model_id

    discover_local_endpoints(root)
    candidates = local_model_candidates(root)
    if requested != "auto":
        if requested not in candidates:
            return None, "requested_model_not_registered_local"
        candidates = [requested]
    if not candidates:
        return None, "no_local_runtime_found"

    # A previously successful probe is enough for onboarding. Otherwise probe
    # at most three candidates so `enable` cannot unexpectedly hammer a local box.
    unprobed: list[str] = []
    for model_id in candidates:
        parts = split_custom_model_id(model_id)
        if parts is None:
            continue
        endpoint = get_endpoint(root, parts[0])
        model = next((m for m in endpoint.models if m.raw_id == parts[1]), None) if endpoint else None
        # `probed_at` means only that a probe *ran*; failed probes persist the
        # timestamp too. Reuse a prior probe only when it produced positive
        # reachability/capability evidence. Otherwise retry during onboarding.
        if (
            model is not None
            and model.probed_at
            and (
                model.observed_tokens_per_second is not None
                or model.supports_structured_output is not None
                or model.supports_tool_use is not None
            )
        ):
            return model_id, "existing_local_probe"
        unprobed.append(model_id)
    for model_id in unprobed[:3]:
        if _probe_candidate(root, model_id, timeout=timeout):
            return model_id, "probed_local_model"
    return None, "no_reachable_local_model"


def probe_isolation() -> IsolationStatus:
    """Actually execute a harmless verifier to qualify local containment."""
    from lemoncrow.pro.capabilities.verified_work.workspace import run_exact_verifier

    try:
        with TemporaryDirectory(prefix="lc-savings-doctor-") as raw:
            root = Path(raw).resolve()
            result = run_exact_verifier("true", cwd=root, root=root, timeout_s=5, memory_mb=256)
        qualified = result.exit_code == 0 and result.network_isolated and result.resource_limited
        if qualified:
            detail = "network isolated + resource limited"
        elif result.exit_code != 0:
            detail = f"sandbox probe failed (exit {result.exit_code})"
        else:
            missing = []
            if not result.network_isolated:
                missing.append("network isolation")
            if not result.resource_limited:
                missing.append("resource limits")
            detail = "missing " + " and ".join(missing)
        return IsolationStatus(qualified, result.network_isolated, result.resource_limited, detail)
    except Exception as exc:
        return IsolationStatus(False, False, False, f"sandbox probe error: {type(exc).__name__}")


def qualification_for_model(
    root: Path,
    model: str,
    settings: VerifiedWorkSettings,
) -> tuple[KindQualification, ...]:
    from lemoncrow.pro.capabilities.verified_work.outcomes import qualification_model_outcome

    rows: list[KindQualification] = []
    for kind in settings.kinds:
        stats = qualification_model_outcome(model, kind, root=root, min_samples=settings.auto_min_samples)
        qualified = (
            stats.attempts >= settings.auto_min_samples
            and stats.verification_rate >= settings.auto_min_success_rate
            and stats.average_latency_ms <= settings.auto_max_avg_latency_ms
        )
        rows.append(
            KindQualification(
                kind=kind,
                attempts=stats.attempts,
                verification_rate=stats.verification_rate,
                average_latency_ms=stats.average_latency_ms,
                qualified=qualified,
            )
        )
    return tuple(rows)


def _decorate_qualification_with_economics(
    root: Path,
    model: str,
    settings: VerifiedWorkSettings,
    calibration: dict[str, object],
) -> tuple[KindQualification, ...]:
    kinds = qualification_for_model(root, model, settings)
    raw_econ_rows = calibration.get("by_model_kind")
    calibration_rows = raw_econ_rows if isinstance(raw_econ_rows, list) else []
    econ_rows = {
        str(row.get("kind")): row for row in calibration_rows if isinstance(row, dict) and row.get("model") == model
    }
    decorated: list[KindQualification] = []
    for quality in kinds:
        econ = econ_rows.get(quality.kind)
        econ_ok = (
            isinstance(econ, dict)
            and bool(econ.get("qualified"))
            and float(econ.get("median_net_savings_usd") or 0.0) > 0
        )
        decorated.append(
            KindQualification(
                kind=quality.kind,
                attempts=quality.attempts,
                verification_rate=quality.verification_rate,
                average_latency_ms=quality.average_latency_ms,
                qualified=quality.qualified,
                economics_qualified=econ_ok,
                calibration_pairs=int(econ.get("pairs") or 0) if isinstance(econ, dict) else 0,
                median_net_savings_usd=(
                    float(econ.get("median_net_savings_usd") or 0.0) if isinstance(econ, dict) else None
                ),
            )
        )
    return tuple(decorated)


def _repository_status(
    root: Path,
    settings: VerifiedWorkSettings,
    repo: RepositoryIdentity,
    *,
    isolation: IsolationStatus,
    calibration: dict[str, object],
    known: dict[str, tuple[KindQualification, ...]],
) -> RepositoryStatus:
    from lemoncrow.pro.capabilities.verified_work.consent import load_grant, repository_window, window_qualifies

    requested = settings.model.strip()
    models = local_model_candidates(root) if requested in {"", "auto"} else [requested]
    windows: list[RepositoryWindow] = []
    grantable: list[tuple[str, str]] = []
    for model in models:
        rows = known.get(model)
        if rows is None:
            rows = _decorate_qualification_with_economics(root, model, settings, calibration)
        globally_ready = {row.kind for row in rows if row.auto_qualified}
        for kind in settings.kinds:
            stats = repository_window(model, kind, repo.key, settings, root=root)
            qualified = window_qualifies(stats, settings)
            if stats.attempts:
                windows.append(
                    RepositoryWindow(
                        model, kind, stats.attempts, stats.verification_rate, stats.average_latency_ms, qualified
                    )
                )
            if qualified and isolation.qualified and kind in globally_ready:
                grantable.append((model, kind))
    grant = load_grant(repo.key, root=root)
    granted = set(grant.pairs) if grant is not None else set()
    return RepositoryStatus(
        key=repo.key,
        root=repo.root,
        grant=grant,
        windows=tuple(windows),
        grantable=tuple(grantable),
        auto_pairs=tuple(pair for pair in grantable if pair in granted),
    )


def _status_from_model(
    *,
    settings: VerifiedWorkSettings,
    isolation: IsolationStatus,
    model: str,
    reason: str,
    kinds: tuple[KindQualification, ...],
    repository: RepositoryStatus | None = None,
) -> SavingsProductStatus:
    auto_rows = [row for row in kinds if row.auto_qualified]
    economics_qualified = bool(auto_rows)
    calibration_pairs = min((row.calibration_pairs for row in auto_rows), default=0)
    calibrated_net = (
        sum(float(row.median_net_savings_usd or 0.0) for row in auto_rows) / len(auto_rows) if auto_rows else None
    )
    ready = isolation.qualified and bool(auto_rows)
    # Ready is global (model pool); Auto is per repository and needs an explicit
    # opt-in there plus the repository's own qualified window.
    repo_ready = repository is not None and bool(repository.grantable)
    state: SavingsState
    if settings.mode == "enforce":
        if repository is None:
            state = "ready" if ready else "degraded"
            final_reason = "auto_requires_repository" if ready else "auto_prerequisites_not_met"
        elif repository.grant is None:
            state = "ready" if repo_ready else "learning"
            final_reason = "auto_needs_repository_opt_in" if repo_ready else "collecting_repository_evidence"
        elif repository.auto_pairs:
            state = "auto"
            final_reason = "qualified_auto"
        else:
            state = "degraded"
            final_reason = "auto_prerequisites_not_met"
    elif ready and (repository is None or repo_ready):
        state = "ready"
        final_reason = "ready_for_auto"
    else:
        state = "learning"
        final_reason = "collecting_repository_evidence" if ready else (reason or "collecting_shadow_evidence")
    return SavingsProductStatus(
        state,
        settings.mode,
        model,
        reason,
        isolation,
        kinds,
        economics_qualified,
        calibration_pairs,
        calibrated_net,
        final_reason,
        repository,
    )


def product_status(
    root: Path,
    settings: VerifiedWorkSettings,
    repo: RepositoryIdentity | None = None,
) -> SavingsProductStatus:
    """Savings state, globally and (with ``repo``) for one repository."""
    from dataclasses import replace

    from lemoncrow.pro.capabilities.verified_work.calibration import summarize_calibration
    from lemoncrow.pro.capabilities.verified_work.model import resolve_worker_model

    isolation = probe_isolation()
    if settings.mode == "off":
        return SavingsProductStatus("off", "off", "", "disabled", isolation, (), False, 0, None, "disabled")

    calibration = summarize_calibration(root=root, min_pairs=settings.calibration_min_pairs)
    known: dict[str, tuple[KindQualification, ...]] = {}

    def repository_for() -> RepositoryStatus | None:
        if repo is None:
            return None
        return _repository_status(root, settings, repo, isolation=isolation, calibration=calibration, known=known)

    # With automatic model selection, Ready/Auto is a pool property: one model
    # may already be qualified for lint while Learning is currently sampling a
    # different model for typecheck. Surface a ready worker if any exists.
    if settings.model.strip() in {"", "auto"}:
        ready_candidates: list[tuple[int, float, str, tuple[KindQualification, ...]]] = []
        for model_id in local_model_candidates(root):
            rows = _decorate_qualification_with_economics(root, model_id, settings, calibration)
            known[model_id] = rows
            auto_rows = [row for row in rows if row.auto_qualified]
            if not auto_rows:
                continue
            net = sum(float(row.median_net_savings_usd or 0.0) for row in auto_rows)
            ready_candidates.append((-len(auto_rows), -net, model_id, rows))
        if ready_candidates:
            ready_candidates.sort(key=lambda row: (row[0], row[1], row[2]))
            _, _, model_id, rows = ready_candidates[0]
            return _status_from_model(
                settings=settings,
                isolation=isolation,
                model=model_id,
                reason="ready_auto_pool_model",
                kinds=rows,
                repository=repository_for(),
            )

    # Otherwise describe the configured/current exploration worker. In enforce
    # mode, fall back to shadow semantics when no kind is currently enforceable
    # so status still explains the local model rather than showing an empty pool.
    first_kind = settings.kinds[0] if settings.kinds else None
    choice, reason = resolve_worker_model(settings, kind=first_kind, root=root)
    if choice is None and settings.mode == "enforce":
        choice, reason = resolve_worker_model(replace(settings, mode="shadow"), kind=first_kind, root=root)
    if choice is None:
        return SavingsProductStatus(
            "blocked", settings.mode, "", reason, isolation, (), False, 0, None, reason, repository_for()
        )

    kinds = known.get(choice.model) or _decorate_qualification_with_economics(root, choice.model, settings, calibration)
    known[choice.model] = kinds
    return _status_from_model(
        settings=settings,
        isolation=isolation,
        model=choice.model,
        reason=reason,
        kinds=kinds,
        repository=repository_for(),
    )


__all__ = [
    "IsolationStatus",
    "KindQualification",
    "LocalDoctorResult",
    "RepositoryStatus",
    "RepositoryWindow",
    "SavingsProductStatus",
    "diagnose_local_worker",
    "discover_local_endpoints",
    "ensure_local_worker",
    "local_model_candidates",
    "probe_isolation",
    "product_status",
    "qualification_for_model",
]

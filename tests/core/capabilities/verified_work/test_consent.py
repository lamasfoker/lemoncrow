"""Repository-scoped Auto consent and repository confidence (G4-VLW)."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lemoncrow.core.capabilities.verified_work.models import (
    Diagnostic,
    EligibilityDecision,
    RepairEdit,
    RepairProposal,
    VerifiedWorkOpportunity,
)
from lemoncrow.core.capabilities.verified_work.settings import VerifiedWorkSettings
from lemoncrow.core.foundation.paths import default_store_root
from lemoncrow.pro.capabilities.verified_work import consent
from lemoncrow.pro.capabilities.verified_work import runtime as runtime_module
from lemoncrow.pro.capabilities.verified_work.consent import (
    RepositoryIdentity,
    auto_gate,
    claim_consent_notice,
    consent_history,
    grant_auto,
    load_grant,
    local_actor,
    repository_identity,
    revoke_all_auto,
    revoke_auto,
)
from lemoncrow.pro.capabilities.verified_work.model import WorkerModelChoice
from lemoncrow.pro.capabilities.verified_work.outcomes import record_model_outcome
from lemoncrow.pro.capabilities.verified_work.repairs import list_repairs, undo_repair
from lemoncrow.pro.capabilities.verified_work.runtime import run_enforced_repair
from lemoncrow.pro.capabilities.verified_work.workspace import VerifierExecution

MODEL = "custom/local/fake"
OTHER = "custom/local/other"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    (repo / "src" / "a.txt").write_text("bad\n", encoding="utf-8")
    return repo


def _identity(repo: Path) -> RepositoryIdentity:
    identity = repository_identity(repo)
    assert identity is not None
    return identity


def _opportunity(
    repo: Path, rels: tuple[str, ...] = ("src/a.txt",), kind: str = "typecheck"
) -> VerifiedWorkOpportunity:
    return VerifiedWorkOpportunity(
        id="opp",
        session_id="session",
        workspace=str(repo),
        command="check",
        cwd=str(repo),
        kind=kind,  # type: ignore[arg-type]
        exit_code=1,
        diagnostics=tuple(Diagnostic(rel, 1, 1, "T", "bad") for rel in rels),
        diagnostic_digest="d",
        diagnostic_excerpt="bad",
        implicated_paths=rels,
        base_file_hashes=tuple((rel, hashlib.sha256((repo / rel).read_bytes()).hexdigest()) for rel in rels),
        original_result_digest="r",
    )


def _settings(**overrides) -> VerifiedWorkSettings:  # type: ignore[no-untyped-def]
    values = {
        "mode": "enforce",
        "max_attempts": 1,
        "kinds": ("typecheck", "lint"),
        "auto_min_samples": 2,
        "auto_min_success_rate": 0.9,
        "auto_max_avg_latency_ms": 5_000,
    }
    values.update(overrides)
    return VerifiedWorkSettings(**values)  # type: ignore[arg-type]


def _seed(
    repo_key: str | None,
    count: int,
    *,
    model: str = MODEL,
    kind: str = "typecheck",
    verified: bool = True,
    latency_ms: int = 100,
) -> None:
    for _ in range(count):
        record_model_outcome(
            model,
            kind,  # type: ignore[arg-type]
            verified=verified,
            promoted=False,
            latency_ms=latency_ms,
            input_tokens=1,
            output_tokens=1,
            provider_cost_usd=0.0,
            mode="shadow",
            repo=repo_key,
        )


def _proposal(*args, **kwargs):  # type: ignore[no-untyped-def]
    return RepairProposal("fix", (RepairEdit("src/a.txt", "bad\n", "good\n"),)), {
        "input_tokens": 1,
        "output_tokens": 1,
    }


def _run(
    repo: Path,
    settings: VerifiedWorkSettings | None = None,
    *,
    host: str = "claude",
    proposal=_proposal,
    rels=("src/a.txt",),
    kind="typecheck",
):  # type: ignore[no-untyped-def]
    decision = EligibilityDecision(True, kind, ("eligible_low_risk",), rels, "cmd", "low")  # type: ignore[arg-type]
    return run_enforced_repair(
        _opportunity(repo, rels, kind),
        decision,
        settings or _settings(),
        proposal_fn=proposal,
        model_choice=WorkerModelChoice(MODEL, "http://127.0.0.1:11434/v1", True, True, 100.0),
        host=host,
    )


@pytest.fixture(autouse=True)
def _qualified_sandbox(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        runtime_module,
        "run_exact_verifier",
        lambda *args, **kwargs: VerifierExecution(0, "", "", 1, network_isolated=True, resource_limited=True),
    )


def test_enforce_without_repository_opt_in_never_promotes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _seed(_identity(repo).key, 5)

    result = _run(repo)

    assert result.verified is True
    assert result.promoted is False
    assert result.reason == "repository_not_opted_in"
    assert (repo / "src" / "a.txt").read_text(encoding="utf-8") == "bad\n"
    assert list_repairs() == []


def test_opt_in_records_who_and_when_and_enables_auto(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    identity = _identity(repo)
    grant = grant_auto(identity, [(MODEL, "typecheck")])

    assert grant.granted_by == local_actor()
    assert grant.granted_by
    assert grant.via == "cli"
    granted_at = datetime.fromisoformat(grant.granted_at)
    assert abs((datetime.now(UTC) - granted_at).total_seconds()) < 60
    assert load_grant(identity.key) == grant

    _seed(identity.key, 2)
    result = _run(repo)

    assert result.promoted is True
    assert result.reason == "promoted_verified_candidate"
    assert result.repair_record_id
    assert (repo / "src" / "a.txt").read_text(encoding="utf-8") == "good\n"


def test_revocation_stops_auto_and_is_recorded(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    identity = _identity(repo)
    grant_auto(identity, [(MODEL, "typecheck")])
    _seed(identity.key, 5)

    assert revoke_auto(identity.key) is True
    assert load_grant(identity.key) is None
    result = _run(repo)

    assert result.promoted is False
    assert result.reason == "repository_not_opted_in"
    assert (repo / "src" / "a.txt").read_text(encoding="utf-8") == "bad\n"
    history = consent_history(identity.key)
    assert [event["action"] for event in history] == ["grant", "revoke"]
    assert all(event["by"] and event["at"] for event in history)
    assert revoke_auto(identity.key) is False


def test_revoke_all_clears_every_repository(tmp_path: Path) -> None:
    first = _identity(_repo(tmp_path, "one"))
    second = _identity(_repo(tmp_path, "two"))
    grant_auto(first, [(MODEL, "typecheck")])
    grant_auto(second, [(MODEL, "lint")])

    assert revoke_all_auto() == 2
    assert load_grant(first.key) is None
    assert load_grant(second.key) is None


def test_settings_enforce_alone_or_shadow_with_grant_is_not_auto(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    identity = _identity(repo)
    _seed(identity.key, 5)

    no_grant = auto_gate(_settings(), workspace=repo, kind="typecheck", host="claude")
    assert no_grant.allowed is False
    assert no_grant.reason == "repository_not_opted_in"

    grant_auto(identity, [(MODEL, "typecheck")])
    learning = auto_gate(_settings(mode="shadow"), workspace=repo, kind="typecheck", host="claude")
    assert learning.allowed is False
    assert learning.reason == "mode_not_enforce"

    allowed = auto_gate(_settings(), workspace=repo, kind="typecheck", host="claude")
    assert allowed.allowed is True
    assert allowed.models == (MODEL,)


def test_grant_covers_only_opted_in_kind(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    identity = _identity(repo)
    grant_auto(identity, [(MODEL, "lint")])
    _seed(identity.key, 5)

    result = _run(repo)

    assert result.promoted is False
    assert result.reason == "kind_not_opted_in"


def test_grant_covers_only_opted_in_model(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    identity = _identity(repo)
    grant_auto(identity, [(OTHER, "typecheck")])
    _seed(identity.key, 5)

    result = _run(repo)

    assert result.promoted is False
    assert result.reason == "model_not_opted_in"


def test_grants_accumulate_and_graduation_never_extends_them(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    identity = _identity(repo)
    grant_auto(identity, [(MODEL, "typecheck")])
    # Another model and another kind graduate in this repository afterwards.
    _seed(identity.key, 5, model=OTHER, kind="typecheck")
    _seed(identity.key, 5, model=MODEL, kind="lint")
    _seed(identity.key, 5, model=MODEL, kind="typecheck")

    other_model = auto_gate(_settings(), workspace=repo, kind="typecheck", host="claude", model=OTHER)
    other_kind = auto_gate(_settings(), workspace=repo, kind="lint", host="claude", model=MODEL)
    pool = auto_gate(_settings(), workspace=repo, kind="typecheck", host="claude")

    assert other_model.reason == "model_not_opted_in"
    assert other_kind.reason == "kind_not_opted_in"
    assert pool.allowed is True
    assert pool.models == (MODEL,)

    grant = grant_auto(identity, [(MODEL, "lint")])
    assert set(grant.pairs) == {(MODEL, "typecheck"), (MODEL, "lint")}


@pytest.mark.parametrize(("samples", "allowed"), [(1, False), (2, True)])
def test_repository_window_gates_auto_at_min_samples(tmp_path: Path, samples: int, allowed: bool) -> None:
    repo = _repo(tmp_path)
    identity = _identity(repo)
    grant_auto(identity, [(MODEL, "typecheck")])
    _seed(identity.key, samples)

    gate = auto_gate(_settings(), workspace=repo, kind="typecheck", host="claude", model=MODEL)

    assert gate.allowed is allowed
    if not allowed:
        assert gate.reason == "repository_window_not_qualified"


def test_repository_window_ignores_other_repositories_and_global_evidence(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    other = _repo(tmp_path, "two")
    identity = _identity(repo)
    grant_auto(identity, [(MODEL, "typecheck")])
    _seed(_identity(other).key, 20)
    _seed(None, 20)

    gate = auto_gate(_settings(), workspace=repo, kind="typecheck", host="claude", model=MODEL)
    result = _run(repo)

    assert gate.reason == "repository_window_not_qualified"
    assert result.promoted is False
    assert result.reason == "repository_window_not_qualified"


@pytest.mark.parametrize(("failures", "allowed"), [(1, True), (2, False)])
def test_repository_window_success_rate_bound(tmp_path: Path, failures: int, allowed: bool) -> None:
    repo = _repo(tmp_path)
    identity = _identity(repo)
    grant_auto(identity, [(MODEL, "typecheck")])
    _seed(identity.key, 9)
    _seed(identity.key, failures, verified=False)

    gate = auto_gate(_settings(), workspace=repo, kind="typecheck", host="claude", model=MODEL)

    assert gate.allowed is allowed  # 9/10 = 0.9 passes; 9/11 < 0.9 fails


@pytest.mark.parametrize(("latency_ms", "allowed"), [(5_000, True), (5_001, False)])
def test_repository_window_latency_bound(tmp_path: Path, latency_ms: int, allowed: bool) -> None:
    repo = _repo(tmp_path)
    identity = _identity(repo)
    grant_auto(identity, [(MODEL, "typecheck")])
    _seed(identity.key, 4, latency_ms=latency_ms)

    gate = auto_gate(_settings(), workspace=repo, kind="typecheck", host="claude", model=MODEL)

    assert gate.allowed is allowed


def test_repository_window_uses_only_recent_outcomes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    identity = _identity(repo)
    grant_auto(identity, [(MODEL, "typecheck")])
    _seed(identity.key, 50)
    _seed(identity.key, 20, verified=False)

    gate = auto_gate(_settings(), workspace=repo, kind="typecheck", host="claude", model=MODEL)

    assert gate.reason == "repository_window_not_qualified"


@pytest.mark.parametrize("host", ["codex", "opencode", ""])
def test_unsupported_hosts_never_promote(tmp_path: Path, host: str) -> None:
    repo = _repo(tmp_path)
    identity = _identity(repo)
    grant_auto(identity, [(MODEL, "typecheck")])
    _seed(identity.key, 5)

    result = _run(repo, host=host)

    assert result.promoted is False
    assert result.reason == "host_unsupported"
    assert (repo / "src" / "a.txt").read_text(encoding="utf-8") == "bad\n"


def test_outside_a_repository_auto_is_refused(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    gate = auto_gate(_settings(), workspace=plain, kind="typecheck", host="claude")
    assert gate.allowed is False
    assert gate.reason == "no_repository"


def test_consent_notice_is_claimed_once_per_repository(tmp_path: Path) -> None:
    first = _identity(_repo(tmp_path, "one"))
    second = _identity(_repo(tmp_path, "two"))

    assert claim_consent_notice(first) is True
    assert claim_consent_notice(first) is False
    assert claim_consent_notice(second) is True


def test_repository_identity_is_shared_by_worktrees_and_distinct_across_repositories(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "main")
    other = _repo(tmp_path, "other")
    (repo / "sub").mkdir()
    _git(repo, "worktree", "add", "--detach", str(tmp_path / "wt"), "HEAD")
    plain = tmp_path / "plain"
    plain.mkdir()

    main_id = _identity(repo)
    assert _identity(repo / "sub").key == main_id.key
    assert _identity(tmp_path / "wt").key == main_id.key
    assert _identity(other).key != main_id.key
    assert repository_identity(plain) is None


def test_end_to_end_dirty_and_untracked_repair_is_undoable(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "src" / "new.txt").write_text("untracked bad\n", encoding="utf-8")
    identity = _identity(repo)
    grant_auto(identity, [(MODEL, "typecheck")])
    _seed(identity.key, 2)
    status_before = _git(repo, "status", "--porcelain")

    def proposal(*args, **kwargs):  # type: ignore[no-untyped-def]
        return RepairProposal(
            "fix",
            (
                RepairEdit("src/a.txt", "bad\n", "good\n"),
                RepairEdit("src/new.txt", "untracked bad\n", "untracked good\n"),
            ),
        ), {"input_tokens": 1, "output_tokens": 1}

    result = _run(repo, proposal=proposal, rels=("src/a.txt", "src/new.txt"))
    assert result.promoted is True
    assert (repo / "src" / "new.txt").read_text(encoding="utf-8") == "untracked good\n"

    undone = undo_repair(result.repair_record_id)

    assert undone.ok is True
    assert set(undone.restored_paths) == {"src/a.txt", "src/new.txt"}
    assert (repo / "src" / "a.txt").read_text(encoding="utf-8") == "bad\n"
    assert (repo / "src" / "new.txt").read_text(encoding="utf-8") == "untracked bad\n"
    assert _git(repo, "status", "--porcelain") == status_before


@pytest.mark.parametrize(("extra", "evicted"), [(0, False), (1, True)])
def test_consent_store_repository_bound_evicts_ungranted_first(monkeypatch, extra: int, evicted: bool) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(consent, "MAX_REPOSITORIES", 3)
    granted = RepositoryIdentity(key="granted", root="/r/granted", common_dir="/r/granted/.git")
    grant_auto(granted, [(MODEL, "typecheck")])
    noticed = [RepositoryIdentity(key=f"k{i}", root=f"/r/{i}", common_dir=f"/r/{i}/.git") for i in range(2 + extra)]
    for identity in noticed:
        claim_consent_notice(identity)

    assert load_grant("granted") is not None
    # The oldest ungranted entry is evicted first; its notice may be shown again.
    assert claim_consent_notice(noticed[0]) is evicted


@pytest.mark.parametrize(("events", "kept"), [(20, 20), (21, 20)])
def test_consent_history_bound(events: int, kept: int) -> None:
    identity = RepositoryIdentity(key="hist", root="/r/hist", common_dir="/r/hist/.git")
    for index in range(events):
        if index % 2 == 0:
            grant_auto(identity, [(MODEL, "typecheck")])
        else:
            revoke_auto(identity.key)

    history = consent_history(identity.key)

    assert len(history) == kept
    assert history[-1]["action"] == ("grant" if (events - 1) % 2 == 0 else "revoke")


def test_store_root_is_the_local_state_directory(tmp_path: Path) -> None:
    identity = _identity(_repo(tmp_path))
    grant_auto(identity, [(MODEL, "typecheck")])
    assert (default_store_root() / "verified_work_consent.json").is_file()


@pytest.mark.parametrize(("extra", "evicted"), [(0, False), (1, True)])
def test_repository_outcome_bound_evicts_least_recent_repository(monkeypatch, extra: int, evicted: bool) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.verified_work import outcomes
    from lemoncrow.pro.capabilities.verified_work.outcomes import load_repository_outcome

    monkeypatch.setattr(outcomes, "_MAX_REPOS", 3)
    keys = [f"repo-{index}" for index in range(3 + extra)]
    for key in keys:
        _seed(key, 1)

    assert (load_repository_outcome(MODEL, "typecheck", keys[0]).attempts == 0) is evicted
    assert load_repository_outcome(MODEL, "typecheck", keys[-1]).attempts == 1


@pytest.mark.parametrize(("extra", "evicted"), [(0, False), (1, True)])
def test_repository_outcome_bound_evicts_least_recent_model(monkeypatch, extra: int, evicted: bool) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.verified_work import outcomes
    from lemoncrow.pro.capabilities.verified_work.outcomes import load_repository_outcome

    monkeypatch.setattr(outcomes, "_MAX_MODELS_PER_REPO", 2)
    models = [f"custom/local/m{index}" for index in range(2 + extra)]
    for model in models:
        _seed("repo", 1, model=model)

    assert (load_repository_outcome(models[0], "typecheck", "repo").attempts == 0) is evicted
    assert load_repository_outcome(models[-1], "typecheck", "repo").attempts == 1


@pytest.mark.parametrize(("samples", "kept"), [(3, 3), (4, 3)])
def test_repository_outcome_window_rows_are_bounded(monkeypatch, samples: int, kept: int) -> None:  # type: ignore[no-untyped-def]
    import json

    from lemoncrow.pro.capabilities.verified_work import outcomes

    monkeypatch.setattr(outcomes, "_MAX_RECENT_OUTCOMES", 3)
    _seed("repo", samples)

    raw = json.loads(outcomes.outcome_path().read_text(encoding="utf-8"))
    assert len(raw["repos"]["repo"]["models"][MODEL]["typecheck"]) == kept


@pytest.mark.parametrize(("exit_code", "verified"), [(0, 1), (1, 0)])
def test_learning_outcomes_land_in_their_repository_window(
    tmp_path: Path, monkeypatch, exit_code: int, verified: int
) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.verified_work.outcomes import load_repository_outcome
    from lemoncrow.pro.capabilities.verified_work.runtime import run_shadow_repair

    monkeypatch.setattr(
        runtime_module,
        "run_exact_verifier",
        lambda *args, **kwargs: VerifierExecution(exit_code, "", "", 1, network_isolated=True, resource_limited=True),
    )
    repo = _repo(tmp_path)
    decision = EligibilityDecision(True, "typecheck", ("eligible_low_risk",), ("src/a.txt",), "cmd", "low")

    run_shadow_repair(
        _opportunity(repo),
        decision,
        _settings(mode="shadow"),
        proposal_fn=_proposal,
        model_choice=WorkerModelChoice(MODEL, "http://127.0.0.1:11434/v1", True, True, 100.0),
    )

    stats = load_repository_outcome(MODEL, "typecheck", _identity(repo).key)
    assert stats.attempts == 1
    assert stats.verified == verified


def test_opt_in_question_is_never_asked_for_other_refusals(tmp_path: Path) -> None:
    for case_index, (kind, model, samples, host, expected) in enumerate(
        [
            ("lint", None, 2, "claude", "kind_not_opted_in"),
            ("typecheck", OTHER, 2, "claude", "model_not_opted_in"),
            ("typecheck", MODEL, 1, "claude", "repository_window_not_qualified"),
            ("typecheck", MODEL, 2, "codex", "host_unsupported"),
        ]
    ):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        from lemoncrow.pro.capabilities.verified_work.consent import CONSENT_NOTICE, consent_notice_for

        repo = _repo(case_tmp_path)
        identity = _identity(repo)
        grant_auto(identity, [(MODEL, "typecheck")])
        _seed(identity.key, samples)
        gate = auto_gate(_settings(), workspace=repo, kind=kind, host=host, model=model)
        assert gate.allowed is False
        assert gate.reason == expected
        assert consent_notice_for(gate) == ""
        revoke_auto(identity.key)
        asked = auto_gate(_settings(), workspace=repo, kind="typecheck", host="claude")
        assert consent_notice_for(asked) == CONSENT_NOTICE

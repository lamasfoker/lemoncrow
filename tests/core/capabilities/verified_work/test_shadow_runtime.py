from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from lemoncrow.core.capabilities.verified_work.models import (
    Diagnostic,
    EligibilityDecision,
    RepairEdit,
    RepairProposal,
    VerifiedWorkOpportunity,
)
from lemoncrow.core.capabilities.verified_work.settings import VerifiedWorkSettings
from lemoncrow.pro.capabilities.verified_work.model import WorkerModelChoice, is_loopback_endpoint
from lemoncrow.pro.capabilities.verified_work.runtime import run_shadow_repair


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _opt_in(repo: Path, model: str = "custom/local/fake", kind: str = "typecheck", samples: int = 2) -> None:
    """Grant repository-scoped Auto and seed this repository's own window."""
    from lemoncrow.pro.capabilities.verified_work.consent import grant_auto, repository_identity
    from lemoncrow.pro.capabilities.verified_work.outcomes import record_model_outcome

    identity = repository_identity(repo)
    assert identity is not None
    grant_auto(identity, [(model, kind)])
    for _ in range(samples):
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
            repo=identity.key,
        )


def _opportunity(repo: Path, command: str) -> VerifiedWorkOpportunity:
    live = repo / "frontend" / "src" / "a.txt"
    digest = hashlib.sha256(live.read_bytes()).hexdigest()
    return VerifiedWorkOpportunity(
        id="opp-1",
        session_id="session-1",
        workspace=str(repo / "frontend"),
        command=command,
        cwd=str(repo / "frontend"),
        kind="typecheck",
        exit_code=1,
        diagnostics=(Diagnostic(path="src/a.txt", line=1, column=1, code="TEST", message="must say good"),),
        diagnostic_digest="diag",
        diagnostic_excerpt="src/a.txt:1:1 TEST must say good",
        implicated_paths=("src/a.txt",),
        base_file_hashes=(("src/a.txt", digest),),
        original_result_digest="result",
    )


def test_loopback_endpoint_detection_is_conservative() -> None:
    assert is_loopback_endpoint("http://127.0.0.1:11434/v1") is True
    assert is_loopback_endpoint("http://[::1]:8000/v1") is True
    assert is_loopback_endpoint("http://localhost:1234/v1") is True
    assert is_loopback_endpoint("https://models.example.com/v1") is False


def test_shadow_repair_verifies_isolated_dirty_workspace_without_mutating_live(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    source = repo / "frontend" / "src"
    source.mkdir(parents=True)
    (source / "a.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    # The candidate must include this dirty live state, not just HEAD.
    (source / "a.txt").write_text("bad\n", encoding="utf-8")

    command = (
        'python -c "from pathlib import Path; '
        "raise SystemExit(0 if Path('src/a.txt').read_text() == 'good\\n' else 1)\""
    )
    opportunity = _opportunity(repo, command)
    decision = EligibilityDecision(
        eligible=True,
        kind="typecheck",
        reason_codes=("eligible_low_risk",),
        allowed_paths=("src/a.txt",),
        verifier_command_digest="cmd",
        risk="low",
    )
    settings = VerifiedWorkSettings(mode="shadow", max_attempts=1)
    choice = WorkerModelChoice(
        model="custom/local/fake",
        endpoint="http://127.0.0.1:11434/v1",
        local=True,
        structured_output=True,
        observed_tokens_per_second=100.0,
    )

    def fake_proposal(*args, **kwargs):  # type: ignore[no-untyped-def]
        candidate_root = kwargs["candidate_root"]
        # Proves the dirty overlay was copied into the isolated candidate.
        assert (candidate_root / "src" / "a.txt").read_text(encoding="utf-8") == "bad\n"
        return RepairProposal(
            summary="fix value",
            edits=(RepairEdit(path="src/a.txt", old="bad\n", new="good\n"),),
        ), {"input_tokens": 10, "output_tokens": 5, "provider_cost_usd": 0.0}

    result = run_shadow_repair(
        opportunity,
        decision,
        settings,
        proposal_fn=fake_proposal,
        model_choice=choice,
    )

    assert result.attempted is True
    assert result.verified is True
    assert result.attempts == 1
    assert result.changed_paths == ("src/a.txt",)
    assert result.input_tokens == 10
    # Shadow mode must never promote the candidate.
    assert (source / "a.txt").read_text(encoding="utf-8") == "bad\n"


def test_enforce_repair_promotes_only_after_exact_verifier_passes(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.verified_work.runtime import run_enforced_repair

    repo = tmp_path / "repo"
    source = repo / "frontend" / "src"
    source.mkdir(parents=True)
    (source / "a.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    (source / "a.txt").write_text("bad\n", encoding="utf-8")

    command = (
        'python -c "from pathlib import Path; '
        "raise SystemExit(0 if Path('src/a.txt').read_text() == 'good\\n' else 1)\""
    )
    opportunity = _opportunity(repo, command)
    decision = EligibilityDecision(
        eligible=True,
        kind="typecheck",
        reason_codes=("eligible_low_risk",),
        allowed_paths=("src/a.txt",),
        verifier_command_digest="cmd",
        risk="low",
    )
    settings = VerifiedWorkSettings(mode="enforce", max_attempts=1, auto_min_samples=2)
    _opt_in(repo)
    choice = WorkerModelChoice(
        model="custom/local/fake",
        endpoint="http://127.0.0.1:11434/v1",
        local=True,
        structured_output=True,
        observed_tokens_per_second=100.0,
    )

    def fake_proposal(*args, **kwargs):  # type: ignore[no-untyped-def]
        return RepairProposal(
            summary="fix value",
            edits=(RepairEdit(path="src/a.txt", old="bad\n", new="good\n"),),
        ), {"input_tokens": 10, "output_tokens": 5, "provider_cost_usd": 0.0}

    result = run_enforced_repair(
        opportunity,
        decision,
        settings,
        proposal_fn=fake_proposal,
        model_choice=choice,
        host="claude",
    )

    assert result.verified is True
    assert result.promoted is True
    assert result.reason == "promoted_verified_candidate"
    assert (source / "a.txt").read_text(encoding="utf-8") == "good\n"


def test_enforce_repair_rejects_live_hash_race(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.verified_work.runtime import run_enforced_repair

    repo = tmp_path / "repo"
    source = repo / "frontend" / "src"
    source.mkdir(parents=True)
    live = source / "a.txt"
    live.write_text("base\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    live.write_text("bad\n", encoding="utf-8")
    command = (
        'python -c "from pathlib import Path; '
        "raise SystemExit(0 if Path('src/a.txt').read_text() == 'good\\n' else 1)\""
    )
    opportunity = _opportunity(repo, command)
    decision = EligibilityDecision(True, "typecheck", ("eligible_low_risk",), ("src/a.txt",), "cmd", "low")
    settings = VerifiedWorkSettings(mode="enforce", max_attempts=1, auto_min_samples=2)
    _opt_in(repo)
    choice = WorkerModelChoice("custom/local/fake", "http://127.0.0.1:11434/v1", True, True, 100.0)

    def racing_proposal(*args, **kwargs):  # type: ignore[no-untyped-def]
        live.write_text("host changed\n", encoding="utf-8")
        return RepairProposal("fix", (RepairEdit("src/a.txt", "bad\n", "good\n"),)), {
            "input_tokens": 1,
            "output_tokens": 1,
        }

    result = run_enforced_repair(
        opportunity,
        decision,
        settings,
        proposal_fn=racing_proposal,
        model_choice=choice,
        host="claude",
    )

    assert result.verified is True
    assert result.promoted is False
    assert result.reason == "base_hash_conflict"
    assert live.read_text(encoding="utf-8") == "host changed\n"


def test_repair_records_model_outcome_evidence(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.verified_work import outcomes

    repo = tmp_path / "repo"
    source = repo / "frontend" / "src"
    source.mkdir(parents=True)
    (source / "a.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    (source / "a.txt").write_text("bad\n", encoding="utf-8")
    command = (
        'python -c "from pathlib import Path; '
        "raise SystemExit(0 if Path('src/a.txt').read_text() == 'good\\n' else 1)\""
    )
    opportunity = _opportunity(repo, command)
    decision = EligibilityDecision(True, "typecheck", ("eligible_low_risk",), ("src/a.txt",), "cmd", "low")
    settings = VerifiedWorkSettings(mode="shadow", max_attempts=1)
    choice = WorkerModelChoice("custom/local/fake", "http://127.0.0.1:11434/v1", True, True, 100.0)
    captured: dict[str, object] = {}

    def record(model, kind, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(model=model, kind=kind, **kwargs)

    monkeypatch.setattr(outcomes, "record_model_outcome", record)

    def fake_proposal(*args, **kwargs):  # type: ignore[no-untyped-def]
        return RepairProposal("fix", (RepairEdit("src/a.txt", "bad\n", "good\n"),)), {
            "input_tokens": 11,
            "output_tokens": 7,
            "provider_cost_usd": 0.0,
        }

    result = run_shadow_repair(
        opportunity,
        decision,
        settings,
        proposal_fn=fake_proposal,
        model_choice=choice,
    )

    assert result.verified is True
    assert captured["model"] == "custom/local/fake"
    assert captured["kind"] == "typecheck"
    assert captured["verified"] is True
    assert captured["promoted"] is False
    assert captured["input_tokens"] == 11
    assert captured["output_tokens"] == 7


def test_enforce_refuses_promotion_without_required_isolation(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.verified_work import runtime as runtime_module
    from lemoncrow.pro.capabilities.verified_work.runtime import run_enforced_repair
    from lemoncrow.pro.capabilities.verified_work.workspace import VerifierExecution

    repo = tmp_path / "repo"
    source = repo / "frontend" / "src"
    source.mkdir(parents=True)
    live = source / "a.txt"
    live.write_text("base\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    live.write_text("bad\n", encoding="utf-8")
    opportunity = _opportunity(repo, "python -c 'raise SystemExit(1)'")
    decision = EligibilityDecision(True, "typecheck", ("eligible_low_risk",), ("src/a.txt",), "cmd", "low")
    settings = VerifiedWorkSettings(mode="enforce", max_attempts=1, require_isolation=True)
    choice = WorkerModelChoice("custom/local/fake", "http://127.0.0.1:11434/v1", True, True, 100.0)

    def fake_proposal(*args, **kwargs):  # type: ignore[no-untyped-def]
        return RepairProposal("fix", (RepairEdit("src/a.txt", "bad\n", "good\n"),)), {
            "input_tokens": 1,
            "output_tokens": 1,
        }

    monkeypatch.setattr(
        runtime_module,
        "run_exact_verifier",
        lambda *args, **kwargs: VerifierExecution(0, "", "", 1, network_isolated=False, resource_limited=True),
    )

    result = run_enforced_repair(
        opportunity,
        decision,
        settings,
        proposal_fn=fake_proposal,
        model_choice=choice,
    )

    assert result.verified is True
    assert result.promoted is False
    assert result.reason == "isolation_not_qualified"
    assert live.read_text(encoding="utf-8") == "bad\n"

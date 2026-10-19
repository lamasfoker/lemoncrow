from __future__ import annotations

import json
from pathlib import Path

import click
from click.testing import CliRunner

from lemoncrow.core.capabilities.verified_work.models import RepairAttemptResult
from lemoncrow.core.capabilities.verified_work.settings import VerifiedWorkSettings
from lemoncrow.gateway.cli import cli
from lemoncrow.gateway.cli.commands import work as work_cmd


def _payload(tmp_path: Path) -> dict[str, object]:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "a.ts").write_text("export const x = nope\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"scripts":{"typecheck":"tsc --noEmit"}}\n', encoding="utf-8")
    return {
        "host": "claude",
        "session_id": "session-1",
        "workspace": str(tmp_path),
        "cwd": str(tmp_path),
        "command": "npm run typecheck",
        "exit_code": 2,
        "stdout": "src/a.ts(1,18): error TS2304: Cannot find name 'nope'.",
        "stderr": "",
    }


def test_work_is_runtime_namespaced_only() -> None:
    root_ctx = click.Context(cli, info_name="lc")
    assert cli.get_command(root_ctx, "work") is None
    runtime = cli.get_command(root_ctx, "runtime")
    assert isinstance(runtime, click.Group)
    assert "work" in runtime.commands
    result = CliRunner().invoke(cli, ["runtime", "work", "--help"])
    assert result.exit_code == 0, result.output
    assert "repair" in result.output


def test_after_bash_off_is_noop(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(work_cmd, "load_verified_work_settings", lambda: VerifiedWorkSettings(mode="off"))
    result = work_cmd.process_after_bash_payload(_payload(tmp_path), root=tmp_path / "lc")
    assert result["handled"] is False
    assert result["promoted"] is False
    assert result["reason"] == "mode_off"


_MODEL = "custom/local/fake"


def _enforce_settings() -> VerifiedWorkSettings:
    return VerifiedWorkSettings(mode="enforce", kinds=("typecheck",), auto_min_samples=2)


def _repo_payload(tmp_path: Path, root: Path, *, opt_in: bool) -> dict[str, object]:
    import subprocess

    from lemoncrow.pro.capabilities.verified_work.consent import grant_auto, repository_identity
    from lemoncrow.pro.capabilities.verified_work.outcomes import record_model_outcome

    payload = _payload(tmp_path)
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    identity = repository_identity(tmp_path)
    assert identity is not None
    for _ in range(2):
        record_model_outcome(
            _MODEL,
            "typecheck",
            verified=True,
            promoted=False,
            latency_ms=10,
            input_tokens=1,
            output_tokens=1,
            provider_cost_usd=0.0,
            root=root,
            repo=identity.key,
        )
    if opt_in:
        grant_auto(identity, [(_MODEL, "typecheck")], root=root)
    return payload


def test_after_bash_enforce_reports_promoted_repair(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "lc"
    payload = _repo_payload(tmp_path, root, opt_in=True)
    monkeypatch.setattr(work_cmd, "load_verified_work_settings", _enforce_settings)
    from lemoncrow.pro.capabilities.verified_work import runtime

    calls: list[dict[str, object]] = []

    def fake_enforced(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs)
        return RepairAttemptResult(
            attempted=True,
            verified=True,
            reason="promoted_verified_candidate",
            model=_MODEL,
            attempts=1,
            changed_paths=("src/a.ts",),
            diff_lines=2,
            verifier_duration_ms=10,
            promoted=True,
            repair_record_id="abc123def456",
        )

    monkeypatch.setattr(runtime, "run_enforced_repair_bounded", fake_enforced)
    monkeypatch.setattr(
        "lemoncrow.infra.runtime.run_ledger.RunLedger.persist", lambda self, root=None: tmp_path / "run.json"
    )

    result = work_cmd.process_after_bash_payload(payload, root=root)

    assert result["handled"] is True
    assert result["promoted"] is True
    assert result["verified"] is True
    assert "verified local repair applied" in result["message"]
    assert "src/a.ts" in result["message"]
    assert "lc rt savings undo abc123def456" in result["message"]
    assert calls[0]["host"] == "claude"
    assert calls[0]["allowed_models"] == (_MODEL,)


def _never(*args, **kwargs):  # type: ignore[no-untyped-def]
    raise AssertionError("must not promote")


def test_after_bash_enforce_without_opt_in_queues_learning_and_asks_once(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "lc"
    payload = _repo_payload(tmp_path, root, opt_in=False)
    monkeypatch.setattr(work_cmd, "load_verified_work_settings", _enforce_settings)
    from lemoncrow.pro.capabilities.verified_work import jobs, runtime

    queued: list[dict[str, object]] = []
    monkeypatch.setattr(jobs, "enqueue_and_spawn", lambda root, item: queued.append(item) or ("job", True))
    monkeypatch.setattr(runtime, "run_enforced_repair_bounded", _never)

    first = work_cmd.process_after_bash_payload(dict(payload), root=root)
    second = work_cmd.process_after_bash_payload(dict(payload), root=root)

    assert first["promoted"] is False
    assert first["reason"] == "repository_not_opted_in"
    assert "lc rt savings auto" in first["notice"]
    assert "notice" not in second
    assert len(queued) == 2


def test_cli_repair_without_opt_in_runs_as_learning_and_asks_once(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`lc rt work repair` (synchronous, host cli) in enforce mode with no opt-in."""
    import subprocess

    from lemoncrow.core.capabilities.verified_work.models import RepairEdit, RepairProposal
    from lemoncrow.pro.capabilities.verified_work import runtime
    from lemoncrow.pro.capabilities.verified_work.consent import repository_identity
    from lemoncrow.pro.capabilities.verified_work.model import WorkerModelChoice
    from lemoncrow.pro.capabilities.verified_work.outcomes import load_repository_outcome
    from lemoncrow.pro.capabilities.verified_work.workspace import VerifierExecution

    root = tmp_path / "lc"
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = {**_payload(repo), "host": "cli"}
    for args in (("init",), ("add", "."), ("-c", "user.email=t@e", "-c", "user.name=t", "commit", "-m", "b")):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    monkeypatch.setattr(work_cmd, "load_verified_work_settings", _enforce_settings)
    choice = WorkerModelChoice(_MODEL, "http://127.0.0.1:11434/v1", True, True, 100.0)
    monkeypatch.setattr(runtime, "resolve_worker_model", lambda *args, **kwargs: (choice, "test_model"))
    monkeypatch.setattr(
        runtime,
        "run_exact_verifier",
        lambda *args, **kwargs: VerifierExecution(0, "", "", 1, network_isolated=True, resource_limited=True),
    )

    def proposal(*args, **kwargs):  # type: ignore[no-untyped-def]
        edit = RepairEdit("src/a.ts", "export const x = nope\n", "export const x = 1\n")
        return RepairProposal("fix", (edit,)), {"input_tokens": 1, "output_tokens": 1}

    real_run_repair = runtime.run_repair
    monkeypatch.setattr(
        runtime,
        "run_repair",
        lambda opportunity, decision, cfg, **kwargs: real_run_repair(
            opportunity,
            decision,
            cfg,
            sink=kwargs.get("sink"),
            proposal_fn=proposal,
            model_choice=kwargs.get("model_choice"),
            host=kwargs.get("host", ""),
        ),
    )
    monkeypatch.setattr(runtime, "run_enforced_repair_bounded", _never)
    monkeypatch.setattr(
        "lemoncrow.infra.runtime.run_ledger.RunLedger.persist", lambda self, root=None: tmp_path / "run.json"
    )

    first = work_cmd.process_after_bash_payload(dict(payload), root=root, synchronous_learning=True)
    second = work_cmd.process_after_bash_payload(
        {**payload, "session_id": "session-2"}, root=root, synchronous_learning=True
    )

    assert first["mode"] == "shadow"
    assert first["reason"] == "repository_not_opted_in"
    assert first["handled"] is True
    assert first["verified"] is True
    assert first["promoted"] is False
    assert "lc rt savings auto" in first["notice"]
    assert second["handled"] is True
    assert "notice" not in second
    assert (repo / "src" / "a.ts").read_text(encoding="utf-8") == "export const x = nope\n"
    identity = repository_identity(repo)
    assert identity is not None
    stats = load_repository_outcome(_MODEL, "typecheck", identity.key)
    assert stats.attempts == 2
    assert stats.shadow_attempts == 2


def test_drain_never_promotes_even_with_auto_opt_in(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "lc"
    payload = _repo_payload(tmp_path, root, opt_in=True)
    monkeypatch.setattr(work_cmd, "load_verified_work_settings", _enforce_settings)
    from lemoncrow.pro.capabilities.verified_work import runtime

    seen: list[str] = []

    def fake_shadow(*args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(kwargs["settings"].mode)
        return RepairAttemptResult(attempted=True, verified=True, reason="shadow_candidate_verified")

    monkeypatch.setattr(runtime, "run_enforced_repair_bounded", _never)
    monkeypatch.setattr(runtime, "run_shadow_repair_bounded", fake_shadow)
    monkeypatch.setattr(
        "lemoncrow.infra.runtime.run_ledger.RunLedger.persist", lambda self, root=None: tmp_path / "run.json"
    )

    result = work_cmd.process_after_bash_payload(payload, root=root, learning_only=True)

    assert result["promoted"] is False
    assert seen == ["shadow"]


def test_drain_command_runs_jobs_learning_only(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.verified_work.jobs import enqueue_after_bash

    root = tmp_path / "lc"
    enqueue_after_bash(root, {"command": "npm run typecheck", "exit_code": 2})
    seen: list[dict[str, object]] = []

    def fake_process(payload, *, root, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(kwargs)
        return {"handled": False, "promoted": False}

    monkeypatch.setattr(work_cmd, "process_after_bash_payload", fake_process)

    result = CliRunner().invoke(cli, ["--root", str(root), "runtime", "work", "drain", "--one"])

    assert result.exit_code == 0, result.output
    assert seen == [{"learning_only": True}]


def test_after_bash_command_reads_json_and_deletes_input(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps({"command": "x"}), encoding="utf-8")
    monkeypatch.setattr(
        work_cmd,
        "process_after_bash_payload",
        lambda payload, root: {"schema_version": 1, "mode": "off", "handled": False, "promoted": False},
    )

    result = CliRunner().invoke(
        cli,
        ["--root", str(tmp_path / "lc"), "runtime", "work", "after-bash", "--input", str(input_path)],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["promoted"] is False
    assert input_path.exists() is False


def test_repair_refuses_when_feature_is_off(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(work_cmd, "load_verified_work_settings", lambda: VerifiedWorkSettings(mode="off"))
    result = CliRunner().invoke(
        cli,
        ["runtime", "work", "repair", "--cwd", str(tmp_path), "--verify", "python -c 'raise SystemExit(1)'"],
    )
    assert result.exit_code != 0
    assert "verified_work.mode is off" in result.output


def test_work_report_labels_observed_facts_and_unqualified_savings(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.verified_work import outcomes

    monkeypatch.setattr(
        outcomes,
        "summarize_outcomes",
        lambda root=None: {
            "schema_version": 1,
            "model_count": 1,
            "by_kind": {},
            "estimated_frontier_savings": None,
            "savings_status": "not_qualified_without_paired_baseline",
            "totals": {
                "attempts": 3,
                "verified": 2,
                "promoted": 1,
                "shadow_attempts": 2,
                "enforce_attempts": 1,
                "input_tokens": 100,
                "output_tokens": 25,
                "provider_cost_usd": 0.0,
                "verification_rate": 2 / 3,
                "average_latency_ms": 500.0,
            },
        },
    )
    result = CliRunner().invoke(cli, ["--root", str(tmp_path), "runtime", "work", "report"])
    assert result.exit_code == 0, result.output
    assert "Promoted repairs:        1" in result.output
    assert "Frontier loops contained:" in result.output
    assert "not claimed (paired baseline not qualified)" in result.output


def test_record_baseline_command_persists_and_deletes_input(tmp_path: Path) -> None:
    input_path = tmp_path / "baseline.json"
    input_path.write_text(
        json.dumps(
            {
                "host": "claude",
                "session_id": "s1",
                "command": "npm run typecheck",
                "started_at_ms": 1000,
                "ended_at_ms": 2000,
                "start": {
                    "total_steps": 1,
                    "total_turns": 1,
                    "agent_steps": 1,
                    "agent_turns": 1,
                    "model_usage": {},
                    "session_usage": {"cost": {"total_cost_usd": 1.0}},
                },
                "end": {
                    "total_steps": 2,
                    "total_turns": 2,
                    "agent_steps": 2,
                    "agent_turns": 2,
                    "model_usage": {},
                    "session_usage": {"cost": {"total_cost_usd": 1.2}},
                },
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        ["--root", str(tmp_path / "lc"), "runtime", "work", "record-baseline", "--input", str(input_path)],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["clean"] is True
    assert payload["measured_cost_usd"] == 0.2
    assert input_path.exists() is False
    assert (tmp_path / "lc" / "verified_work_calibration.json").exists()

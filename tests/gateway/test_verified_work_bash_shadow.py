from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.gateway.adapters.mcp import bash as bash_adapter


def test_failed_typecheck_queues_shadow_without_mutating_result(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.ts").write_text("export const x = nope\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"scripts":{"typecheck":"tsc --noEmit"}}\n', encoding="utf-8")

    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_VERIFIED_WORK_MODE", "shadow")
    monkeypatch.setenv("LEMONCROW_VERIFIED_WORK_KINDS", "typecheck")
    monkeypatch.delenv("LEMONCROW_BASH_WORKSPACE_ROOT", raising=False)

    from lemoncrow.gateway.adapters.mcp import ledger
    from lemoncrow.pro.capabilities.verified_work import jobs

    monkeypatch.setattr(ledger, "_get_claude_session_id", lambda: "claude-session")
    captured: dict[str, object] = {}

    def fake_enqueue(root, payload):  # type: ignore[no-untyped-def]
        captured.update(root=root, payload=payload)
        return "job-1", True

    monkeypatch.setattr(jobs, "enqueue_and_spawn", fake_enqueue)
    result = {
        "exit_code": 2,
        "stdout": "src/a.ts(1,18): error TS2304: Cannot find name 'nope'.",
        "stderr": "",
        "truncated": False,
    }
    before = dict(result)

    returned = bash_adapter._apply_verified_work_after_bash("npm run typecheck", str(tmp_path), result)

    assert returned == before
    assert result == before
    payload = captured["payload"]
    assert payload["session_id"] == "claude-session"
    assert payload["command"] == "npm run typecheck"
    assert payload["exit_code"] == 2


def test_shadow_adapter_is_noop_when_disabled(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_VERIFIED_WORK_MODE", "off")
    result = {"exit_code": 2, "stdout": "src/a.ts(1,1): error TS2304: x", "stderr": ""}
    assert bash_adapter._apply_verified_work_after_bash("npm run typecheck", str(tmp_path), result) == result


_MODEL = "custom/local/fake"


def _enforce_repo(tmp_path: Path, monkeypatch, *, opt_in: bool) -> None:  # type: ignore[no-untyped-def]
    import subprocess

    from lemoncrow.pro.capabilities.verified_work.consent import grant_auto, repository_identity
    from lemoncrow.pro.capabilities.verified_work.outcomes import record_model_outcome

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.ts").write_text("export const x = nope\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"scripts":{"typecheck":"tsc --noEmit"}}\n', encoding="utf-8")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_VERIFIED_WORK_MODE", "enforce")
    monkeypatch.setenv("LEMONCROW_VERIFIED_WORK_KINDS", "typecheck")
    monkeypatch.setenv("LEMONCROW_VERIFIED_WORK_AUTO_MIN_SAMPLES", "2")
    monkeypatch.delenv("LEMONCROW_BASH_WORKSPACE_ROOT", raising=False)
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
            repo=identity.key,
        )
    if opt_in:
        grant_auto(identity, [(_MODEL, "typecheck")])

    from lemoncrow.gateway.adapters.mcp import ledger

    monkeypatch.setattr(ledger, "_get_claude_session_id", lambda: "claude-session")
    monkeypatch.setattr(ledger, "_get_ledger", lambda: object())


def _failed() -> dict[str, object]:
    return {
        "exit_code": 2,
        "stdout": "src/a.ts(1,18): error TS2304: Cannot find name 'nope'.",
        "stderr": "",
        "truncated": False,
    }


def test_enforce_changes_result_only_after_promoted_repair(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _enforce_repo(tmp_path, monkeypatch, opt_in=True)

    from lemoncrow.core.capabilities.verified_work.models import RepairAttemptResult
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
            verifier_duration_ms=12,
            promoted=True,
            repair_record_id="abc123def456",
        )

    monkeypatch.setattr(runtime, "run_enforced_repair_bounded", fake_enforced)
    original = _failed()

    result = bash_adapter._apply_verified_work_after_bash("npm run typecheck", str(tmp_path), original)

    assert original["exit_code"] == 2
    assert result["exit_code"] == 0
    assert result["stderr"] == ""
    assert "verified local repair applied" in result["stdout"]
    assert "src/a.ts" in result["stdout"]
    assert "lc rt savings undo abc123def456" in result["stdout"]
    assert result["verified_work"]["promoted"] is True
    assert result["verified_work"]["repair_record_id"] == "abc123def456"
    assert calls[0]["host"] == "claude"
    assert calls[0]["allowed_models"] == (_MODEL,)


def test_enforce_keeps_original_failure_when_repair_not_promoted(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _enforce_repo(tmp_path, monkeypatch, opt_in=True)

    from lemoncrow.core.capabilities.verified_work.models import RepairAttemptResult
    from lemoncrow.pro.capabilities.verified_work import runtime

    monkeypatch.setattr(
        runtime,
        "run_enforced_repair_bounded",
        lambda *args, **kwargs: RepairAttemptResult(
            attempted=True, verified=True, reason="base_hash_conflict", promoted=False
        ),
    )
    original = _failed()

    assert bash_adapter._apply_verified_work_after_bash("npm run typecheck", str(tmp_path), original) == original


def _no_enforce(*args, **kwargs):  # type: ignore[no-untyped-def]
    raise AssertionError("Auto must not run without consent on a supported host")


def test_enforce_without_opt_in_learns_and_asks_once(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _enforce_repo(tmp_path, monkeypatch, opt_in=False)

    from lemoncrow.pro.capabilities.verified_work import jobs, runtime

    queued: list[dict[str, object]] = []
    monkeypatch.setattr(jobs, "enqueue_and_spawn", lambda root, payload: queued.append(payload) or ("job", True))
    monkeypatch.setattr(runtime, "run_enforced_repair_bounded", _no_enforce)

    first = bash_adapter._apply_verified_work_after_bash("npm run typecheck", str(tmp_path), _failed())
    second = bash_adapter._apply_verified_work_after_bash("npm run typecheck", str(tmp_path), _failed())

    assert first["exit_code"] == 2
    assert str(first["stdout"]).startswith(str(_failed()["stdout"]))
    assert "lc rt savings auto" in str(first["stdout"])
    assert second == _failed()
    assert len(queued) == 2


def test_enforce_on_unsupported_host_only_learns(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _enforce_repo(tmp_path, monkeypatch, opt_in=True)
    monkeypatch.setenv("LEMONCROW_AGENT", "codex")

    from lemoncrow.pro.capabilities.verified_work import jobs, runtime

    queued: list[dict[str, object]] = []
    monkeypatch.setattr(jobs, "enqueue_and_spawn", lambda root, payload: queued.append(payload) or ("job", True))
    monkeypatch.setattr(runtime, "run_enforced_repair_bounded", _no_enforce)

    result = bash_adapter._apply_verified_work_after_bash("npm run typecheck", str(tmp_path), _failed())

    assert result == _failed()
    assert len(queued) == 1


@pytest.mark.parametrize("host", ["codex", "opencode", None])
def test_enforce_on_unsupported_or_unidentified_host_only_learns(tmp_path: Path, monkeypatch, host) -> None:  # type: ignore[no-untyped-def]
    _enforce_repo(tmp_path, monkeypatch, opt_in=True)
    for name in ("LEMONCROW_AGENT", "CLAUDE_CODE", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    if host is not None:
        monkeypatch.setenv("LEMONCROW_AGENT", host)
    # Without any host label, detect_host falls back to "claude"; that fallback
    # must not qualify the unknown host for transparent repair.
    monkeypatch.setattr("lemoncrow.core.foundation.paths.detect_host", lambda: host or "claude")

    from lemoncrow.pro.capabilities.verified_work import jobs, runtime

    queued: list[dict[str, object]] = []
    monkeypatch.setattr(jobs, "enqueue_and_spawn", lambda root, payload: queued.append(payload) or ("job", True))
    monkeypatch.setattr(runtime, "run_enforced_repair_bounded", _no_enforce)

    result = bash_adapter._apply_verified_work_after_bash("npm run typecheck", str(tmp_path), _failed())

    assert result == _failed()
    assert len(queued) == 1

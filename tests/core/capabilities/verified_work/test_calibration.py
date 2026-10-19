from __future__ import annotations

import json
import time
from pathlib import Path

from lemoncrow.core.capabilities.verified_work.models import Diagnostic, VerifiedWorkOpportunity
from lemoncrow.pro.capabilities.verified_work.calibration import (
    command_digest,
    record_host_baseline,
    record_local_observation,
    summarize_calibration,
)


def _opportunity(root: Path, command: str = "npm run typecheck") -> VerifiedWorkOpportunity:
    return VerifiedWorkOpportunity(
        id="opp",
        session_id="session-1",
        workspace=str(root),
        command=command,
        cwd=str(root),
        kind="typecheck",
        exit_code=2,
        diagnostics=(Diagnostic("src/a.ts", 1, 1, "TS1", "bad"),),
        diagnostic_digest="diag",
        diagnostic_excerpt="bad",
        implicated_paths=("src/a.ts",),
        base_file_hashes=(("src/a.ts", "hash"),),
        original_result_digest="result",
    )


def _baseline(*, total_steps: int = 2, agent_steps: int = 2, cost_delta: float = 0.5) -> dict[str, object]:
    now = int(time.time() * 1000)
    return {
        "host": "claude",
        "session_id": "session-1",
        "agent_id": None,
        "command": "npm run typecheck",
        "started_at_ms": now - 1000,
        "ended_at_ms": now + 1000,
        "start": {
            "total_steps": 10,
            "total_turns": 4,
            "agent_steps": 10,
            "agent_turns": 4,
            "model_usage": {},
            "session_usage": {"cost": {"total_cost_usd": 1.0}},
        },
        "end": {
            "total_steps": 10 + total_steps,
            "total_turns": 5,
            "agent_steps": 10 + agent_steps,
            "agent_turns": 5,
            "model_usage": {},
            "session_usage": {"cost": {"total_cost_usd": 1.0 + cost_delta}},
        },
    }


def test_host_baseline_marks_parallel_agent_work_unclean(tmp_path: Path) -> None:
    row = record_host_baseline(_baseline(total_steps=3, agent_steps=2), root=tmp_path)
    assert row["clean"] is False
    assert row["measured_cost_usd"] == 0.5


def test_paired_shadow_baseline_calibrates_promoted_savings(tmp_path: Path) -> None:
    model = "custom/ollama/test-coder"
    opp = _opportunity(tmp_path)
    for _ in range(2):
        record_local_observation(
            opp,
            model=model,
            verified=True,
            promoted=False,
            latency_ms=200,
            input_tokens=100,
            output_tokens=20,
            provider_cost_usd=0.0,
            mode="shadow",
            root=tmp_path,
        )
        record_host_baseline(_baseline(cost_delta=0.5), root=tmp_path)

    # Two later Auto promotions are valued only after the paired Learning
    # baseline is qualified; only calibration-layer promotion records count.
    for _ in range(2):
        record_local_observation(
            opp,
            model=model,
            verified=True,
            promoted=True,
            latency_ms=150,
            input_tokens=80,
            output_tokens=15,
            provider_cost_usd=0.0,
            mode="enforce",
            root=tmp_path,
        )

    summary = summarize_calibration(root=tmp_path, min_pairs=2)

    assert summary["status"] == "qualified"
    assert summary["pairs"] == 2
    assert summary["qualified_promotions"] == 2
    assert summary["calibrated_net_savings_usd"] == 1.0
    row = summary["by_model_kind"][0]
    assert row["median_host_cost_usd"] == 0.5
    assert row["median_local_cost_usd"] == 0.0
    assert row["median_net_savings_usd"] == 0.5
    assert row["median_host_turns"] == 1.0


def test_unclean_baseline_never_qualifies_savings(tmp_path: Path) -> None:
    model = "custom/ollama/test-coder"
    record_local_observation(
        _opportunity(tmp_path),
        model=model,
        verified=True,
        promoted=False,
        latency_ms=100,
        input_tokens=1,
        output_tokens=1,
        provider_cost_usd=0.0,
        mode="shadow",
        root=tmp_path,
    )
    record_host_baseline(_baseline(total_steps=2, agent_steps=1), root=tmp_path)

    summary = summarize_calibration(root=tmp_path, min_pairs=1)
    assert summary["pairs"] == 0
    assert summary["calibrated_net_savings_usd"] is None


def test_calibrated_savings_subtracts_actual_auto_provider_cost(tmp_path: Path) -> None:
    model = "custom/local/paid-loopback"
    opp = _opportunity(tmp_path)
    record_local_observation(
        opp,
        model=model,
        verified=True,
        promoted=False,
        latency_ms=100,
        input_tokens=1,
        output_tokens=1,
        provider_cost_usd=0.05,
        mode="shadow",
        root=tmp_path,
    )
    record_host_baseline(_baseline(cost_delta=0.5), root=tmp_path)
    record_local_observation(
        opp,
        model=model,
        verified=True,
        promoted=True,
        latency_ms=100,
        input_tokens=1,
        output_tokens=1,
        provider_cost_usd=0.10,
        mode="enforce",
        root=tmp_path,
    )

    summary = summarize_calibration(root=tmp_path, min_pairs=1)

    assert summary["calibrated_net_savings_usd"] == 0.4
    assert summary["by_model_kind"][0]["median_net_savings_usd"] == 0.45
    assert summary["by_model_kind"][0]["calibrated_savings_usd"] == 0.4


def test_recent_negative_economics_demotes_stale_profitable_history(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.verified_work.calibration import (
        calibrated_model_kind,
        calibration_path,
    )

    model = "custom/local/coder"
    command = "npm run typecheck"
    digest = command_digest(command)
    local_rows = []
    baseline_rows = []
    at = 1_000_000

    # Old history was strongly profitable.
    for i in range(60):
        session = f"old-{i}"
        local_rows.append(
            {
                "at_ms": at,
                "session_id": session,
                "command_digest": digest,
                "kind": "typecheck",
                "model": model,
                "verified": True,
                "promoted": False,
                "mode": "shadow",
                "latency_ms": 100,
                "provider_cost_usd": 0.01,
            }
        )
        baseline_rows.append(
            {
                "at_ms": at + 100,
                "started_at_ms": at - 100,
                "session_id": session,
                "command_digest": digest,
                "clean": True,
                "host_steps": 1,
                "host_turns": 1,
                "measured_cost_usd": 1.0,
                "priced_cost_usd": 1.0,
            }
        )
        at += 1000

    # Current economics regressed: the local worker is now more expensive than
    # the host repair. The recent window must dominate lifetime history.
    for i in range(40):
        session = f"new-{i}"
        local_rows.append(
            {
                "at_ms": at,
                "session_id": session,
                "command_digest": digest,
                "kind": "typecheck",
                "model": model,
                "verified": True,
                "promoted": False,
                "mode": "shadow",
                "latency_ms": 100,
                "provider_cost_usd": 0.50,
            }
        )
        baseline_rows.append(
            {
                "at_ms": at + 100,
                "started_at_ms": at - 100,
                "session_id": session,
                "command_digest": digest,
                "clean": True,
                "host_steps": 1,
                "host_turns": 1,
                "measured_cost_usd": 0.10,
                "priced_cost_usd": 0.10,
            }
        )
        at += 1000

    calibration_path(tmp_path).write_text(
        json.dumps({"schema_version": 1, "local": local_rows, "baseline": baseline_rows}),
        encoding="utf-8",
    )

    row = calibrated_model_kind(model, "typecheck", root=tmp_path, min_pairs=20)

    assert row is not None
    assert row["pairs"] == 40
    assert row["median_net_savings_usd"] < 0
    assert row["qualified"] is False


def test_calibration_reports_signed_net_not_clamped_loss(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.verified_work.calibration import calibrated_model_kind, calibration_path

    model = "custom/local/coder"
    digest = command_digest("ruff check .")
    local_rows = []
    baseline_rows = []
    for i in range(20):
        session = f"s-{i}"
        at = 10_000 + i * 1000
        local_rows.append(
            {
                "at_ms": at,
                "session_id": session,
                "command_digest": digest,
                "kind": "lint",
                "model": model,
                "verified": True,
                "promoted": False,
                "mode": "shadow",
                "latency_ms": 10,
                "provider_cost_usd": 0.25,
            }
        )
        baseline_rows.append(
            {
                "at_ms": at + 100,
                "started_at_ms": at - 100,
                "session_id": session,
                "command_digest": digest,
                "clean": True,
                "host_steps": 1,
                "host_turns": 1,
                "measured_cost_usd": 0.05,
                "priced_cost_usd": 0.05,
            }
        )
    calibration_path(tmp_path).write_text(
        json.dumps({"schema_version": 1, "local": local_rows, "baseline": baseline_rows}),
        encoding="utf-8",
    )

    row = calibrated_model_kind(model, "lint", root=tmp_path, min_pairs=20)

    assert row is not None
    assert row["median_net_savings_usd"] == -0.2
    assert row["qualified"] is False


def test_session_calibrated_savings_only_prices_qualified_promotions(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.verified_work.calibration import (
        calibration_path,
        session_calibrated_savings,
    )

    model = "custom/local/coder"
    digest = command_digest("npm run typecheck")
    local_rows = []
    baseline_rows = []
    for i in range(20):
        session = f"learn-{i}"
        at = 100_000 + i * 1000
        local_rows.append(
            {
                "at_ms": at,
                "session_id": session,
                "command_digest": digest,
                "kind": "typecheck",
                "model": model,
                "verified": True,
                "promoted": False,
                "mode": "shadow",
                "provider_cost_usd": 0.05,
            }
        )
        baseline_rows.append(
            {
                "at_ms": at + 100,
                "started_at_ms": at - 100,
                "session_id": session,
                "command_digest": digest,
                "clean": True,
                "host_steps": 1,
                "host_turns": 1,
                "measured_cost_usd": 0.50,
                "priced_cost_usd": 0.50,
            }
        )
    local_rows.extend(
        [
            {
                "at_ms": 999_000,
                "session_id": "target",
                "command_digest": digest,
                "kind": "typecheck",
                "model": model,
                "verified": True,
                "promoted": True,
                "mode": "enforce",
                "provider_cost_usd": 0.10,
            },
            {
                "at_ms": 999_100,
                "session_id": "target",
                "command_digest": command_digest("ruff check ."),
                "kind": "lint",
                "model": model,
                "verified": True,
                "promoted": True,
                "mode": "enforce",
                "provider_cost_usd": 0.02,
            },
        ]
    )
    calibration_path(tmp_path).write_text(
        json.dumps({"schema_version": 1, "local": local_rows, "baseline": baseline_rows}),
        encoding="utf-8",
    )

    row = session_calibrated_savings("target", root=tmp_path, min_pairs=20)

    assert row["promoted_repairs"] == 2
    assert row["qualified_repairs"] == 1
    assert row["unqualified_repairs"] == 1
    assert row["local_cost_usd"] == 0.12
    assert row["calibrated_net_savings_usd"] == 0.40

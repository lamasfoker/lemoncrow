from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from lemoncrow_server_core.local_maintenance import LocalMaintenance


class _BaseMaintenance:
    def __init__(self) -> None:
        self.calls = 0
        self.last_report = SimpleNamespace(marker="base")

    def run_once(self) -> object:
        self.calls += 1
        return self.last_report


def test_local_maintenance_imports_sessions_at_most_every_interval(monkeypatch, tmp_path: Path) -> None:
    now = [1_000.0]
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        assert kwargs["timeout"] == 300.0
        return subprocess.CompletedProcess(command, 0, stdout='{"claude": 2, "codex": 3}\n', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    base = _BaseMaintenance()
    runtime_root = tmp_path / "runtime"
    maintenance = LocalMaintenance(
        base,
        runtime_root=runtime_root,
        state_root=tmp_path / "server",
        import_interval_s=1_800,
        clock=lambda: now[0],
    )

    assert maintenance.run_once() is base.last_report
    assert len(calls) == 1
    assert calls[0][1:] == ["-m", "lemoncrow.core.service.session_import", "--root", str(runtime_root)]

    maintenance.run_once()
    assert len(calls) == 1

    now[0] += 1_799
    maintenance.run_once()
    assert len(calls) == 1

    now[0] += 1
    maintenance.run_once()
    assert len(calls) == 2
    assert base.calls == 4

    state = json.loads((tmp_path / "server" / "session-import.json").read_text())
    assert state["last_attempt_succeeded"] is True
    assert state["imported_sessions"] == {"claude": 2, "codex": 3}


def test_local_maintenance_runs_the_landing_watch_on_every_pass(tmp_path: Path) -> None:
    watched = 0

    def watch() -> None:
        nonlocal watched
        watched += 1

    maintenance = LocalMaintenance(
        _BaseMaintenance(),
        runtime_root=tmp_path / "runtime",
        state_root=tmp_path / "server",
        import_interval_s=0,
        landing_watch=watch,
    )

    maintenance.run_once()
    maintenance.run_once()

    assert watched == 2


def test_a_failing_landing_watch_never_breaks_maintenance(tmp_path: Path, caplog) -> None:
    base = _BaseMaintenance()

    def watch() -> None:
        raise RuntimeError("gh is unavailable")

    maintenance = LocalMaintenance(
        base,
        runtime_root=tmp_path / "runtime",
        state_root=tmp_path / "server",
        import_interval_s=0,
        landing_watch=watch,
    )

    assert maintenance.run_once() is base.last_report
    assert "landing watch failed" in caplog.text


def test_a_failed_index_pass_still_watches_landings(tmp_path: Path) -> None:
    watched = 0

    class FailingMaintenance(_BaseMaintenance):
        def run_once(self) -> object:
            raise OSError("index is unavailable")

    def watch() -> None:
        nonlocal watched
        watched += 1

    maintenance = LocalMaintenance(
        FailingMaintenance(),
        runtime_root=tmp_path / "runtime",
        state_root=tmp_path / "server",
        import_interval_s=0,
        landing_watch=watch,
    )

    with pytest.raises(OSError, match="index is unavailable"):
        maintenance.run_once()
    assert watched == 1


def test_local_maintenance_retries_failed_import_after_five_minutes(monkeypatch, tmp_path: Path) -> None:
    now = [2_000.0]
    calls = 0

    def fake_run(command, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="host scan failed")

    monkeypatch.setattr(subprocess, "run", fake_run)
    maintenance = LocalMaintenance(
        _BaseMaintenance(),
        runtime_root=tmp_path / "runtime",
        state_root=tmp_path / "server",
        import_interval_s=1_800,
        clock=lambda: now[0],
    )

    maintenance.run_once()
    assert calls == 1

    now[0] += 299
    maintenance.run_once()
    assert calls == 1

    now[0] += 1
    maintenance.run_once()
    assert calls == 2


def test_local_maintenance_flushes_both_daily_rollups(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.core.service.telemetry import event_rollup, public_rollup

    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(public_rollup, "maybe_flush_public_rollup", lambda root: calls.append(("savings", root)))

    def boom(root: Path) -> None:
        calls.append(("events", root))
        raise RuntimeError("network down")

    monkeypatch.setattr(event_rollup, "maybe_flush_event_rollup", boom)
    base = SimpleNamespace(run_once=lambda: "report", last_report=None)
    maintenance = LocalMaintenance(
        base, runtime_root=tmp_path / "runtime", state_root=tmp_path / "server", import_interval_s=0
    )

    assert maintenance.run_once() == "report"  # a failing flush never breaks maintenance
    assert calls == [("savings", tmp_path / "runtime"), ("events", tmp_path / "runtime")]

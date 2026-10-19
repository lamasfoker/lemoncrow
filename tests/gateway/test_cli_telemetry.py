from __future__ import annotations

from pathlib import Path

import click
import pytest

from lemoncrow.core.service.telemetry.local_store import LocalTelemetryStore
from lemoncrow.gateway.cli import cli
from lemoncrow.gateway.cli import run_telemetry as rt


@pytest.fixture()
def telemetry_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "telemetry.db"
    monkeypatch.setenv("LEMONCROW_TELEMETRY_DB", str(db_path))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_CONFIG", str(tmp_path / "telemetry.toml"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ID_PATH", str(tmp_path / "telemetry_id"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ACK", str(tmp_path / "telemetry_ack"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY", "0")
    return db_path


def _events(db: Path) -> list[tuple[str, dict[str, object]]]:
    return [(e["event"], e["props"]) for e in reversed(LocalTelemetryStore(db).list_events(limit=50))]


def test_telemetry_names_come_from_the_registered_command_tree() -> None:
    for argv, expected in [
        (["-p", "fix the login bug"], ("root", "")),
        (["--root", "/tmp/x", "runtime", "index", "--repo-root", "/repo"], ("runtime", "index")),
        (["code", "fix the login bug"], ("code", "")),
        (["runtime", "savings", "--segment"], ("runtime", "savings")),
        (["definitely-not-a-command"], ("unknown", "")),
        ([], ("root", "")),
    ]:
        assert rt.telemetry_names(cli, argv) == expected


def test_invocation_kind_separates_users_from_machines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rt, "has_terminal", lambda: False)
    assert rt.invocation_kind(["runtime", "savings", "--segment"], "runtime", "savings") == "statusline"
    assert rt.invocation_kind(["worker"], "worker", "") == "background"
    assert rt.invocation_kind(["runtime", "index"], "runtime", "index") == "background"
    assert rt.invocation_kind(["runtime", "savings"], "runtime", "savings") == "interactive"
    monkeypatch.setattr(rt, "has_terminal", lambda: True)
    assert rt.invocation_kind(["runtime", "index"], "runtime", "index") == "interactive"


def test_error_kind_is_the_failing_class_never_its_message() -> None:
    try:
        try:
            raise ValueError("/home/me/secret.py is broken")
        except ValueError:
            raise SystemExit(1) from None
    except SystemExit as exc:
        # `from None` still records __context__; the class is what is kept.
        assert rt.error_kind(exc, 1) == "ValueError"
    try:
        try:
            raise click.exceptions.Exit(3)
        except click.exceptions.Exit:
            raise SystemExit(3) from None
    except SystemExit as exc:
        assert rt.error_kind(exc, 3) == "exit_3"
    assert rt.error_kind(SystemExit(2), 2) == "exit_2"
    assert rt.error_kind(KeyError("x"), 1) == "KeyError"


def test_interactive_run_is_a_session_with_error_detail(telemetry_db: Path) -> None:
    session_id, started = rt.begin_cli_telemetry("code", subcommand="index")
    rt.finish_cli_telemetry(
        command_name="code",
        subcommand="index",
        session_id=session_id,
        started_at=started,
        ok=False,
        exit_reason="error",
        error_kind="ValueError",
        exit_code=1,
    )
    events = _events(telemetry_db)
    assert [name for name, _ in events] == [
        "session_start",
        "cli_command_invoked",
        "cli_command_completed",
        "session_end",
    ]
    completed = events[2][1]
    assert completed["subcommand"] == "index"
    assert completed["error_kind"] == "ValueError"
    assert completed["exit_code"] == 1
    assert completed["background"] is False
    assert completed["session_id"] == session_id


def test_background_run_records_one_event_and_no_session(telemetry_db: Path) -> None:
    session_id, started = rt.begin_cli_telemetry("worker", invocation="background")
    assert session_id == ""
    rt.finish_cli_telemetry(
        command_name="worker",
        session_id=session_id,
        started_at=started,
        ok=True,
        exit_reason="success",
        invocation="background",
        exit_code=0,
    )
    ((name, props),) = _events(telemetry_db)
    assert name == "cli_command_completed"
    assert props["background"] is True
    assert "session_id" not in props


def test_statusline_records_only_failures(telemetry_db: Path) -> None:
    for ok in (True, False):
        session_id, started = rt.begin_cli_telemetry("savings", invocation="statusline")
        rt.finish_cli_telemetry(
            command_name="savings",
            session_id=session_id,
            started_at=started,
            ok=ok,
            exit_reason="success" if ok else "error",
            invocation="statusline",
            error_kind="" if ok else "OSError",
        )
    ((name, props),) = _events(telemetry_db)
    assert name == "cli_command_completed"
    assert props["ok"] is False and props["error_kind"] == "OSError"


def test_cli_run_treats_a_bare_exit_as_success(telemetry_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rt, "has_terminal", lambda: True)
    for exc in (SystemExit(None), SystemExit(0), SystemExit("usage message")):
        run = rt.CliRun(cli, ["runtime", "index"])
        run.finish_from(exc)
    completed = [props for name, props in _events(telemetry_db) if name == "cli_command_completed"]
    assert [(p["ok"], p.get("exit_code"), p.get("error_kind")) for p in completed] == [
        (True, 0, None),
        (True, 0, None),
        (False, 1, "exit_1"),
    ]

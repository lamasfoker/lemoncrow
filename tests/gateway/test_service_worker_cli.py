"""Terminal service log behavior."""

from __future__ import annotations

import itertools
import subprocess

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli.commands import service_worker


@pytest.mark.parametrize("follow", [False, True])
def test_service_log_journal(monkeypatch, follow):
    monkeypatch.setenv("LEMONCROW_LOCAL_SERVER_SUPERVISOR", "systemd")
    monkeypatch.setattr(service_worker.shutil, "which", lambda name: name)
    calls = []
    monkeypatch.setattr(service_worker.os, "execvp", lambda name, args: calls.append(args))
    result = CliRunner().invoke(service_worker.service_group, ["log", *(["-f"] if follow else [])])
    assert result.exit_code == 0, result.output
    assert calls == [
        ["journalctl", "--user", "-u", "lemoncrow-local-server.service", "-n", "100", "--no-pager"]
        + (["-f"] if follow else [])
    ]


def test_service_log_file(monkeypatch, tmp_path):
    for case_index, (follow, custom_state) in enumerate(itertools.product([False, True], [False, True])):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        with monkeypatch.context() as case_monkeypatch:
            case_monkeypatch.setenv("LEMONCROW_LOCAL_SERVER_SUPERVISOR", "none")
            case_monkeypatch.setenv("LEMONCROW_HOME", str(case_tmp_path))
            case_monkeypatch.delenv("LEMONCROW_LOCAL_SERVER_DIR", raising=False)
            state_dir = case_tmp_path / ("custom" if custom_state else "server")
            if custom_state:
                case_monkeypatch.setenv("LEMONCROW_LOCAL_SERVER_DIR", str(state_dir))
            case_monkeypatch.setattr(service_worker.shutil, "which", lambda name: name)
            calls = []
            case_monkeypatch.setattr(service_worker.os, "execvp", lambda name, args, _calls=calls: _calls.append(args))
            result = CliRunner().invoke(service_worker.service_group, ["log", *(["-f"] if follow else [])])
            assert result.exit_code == 0, result.output
            assert calls == [["tail", "-n", "100", *(["-f"] if follow else []), str(state_dir / "server.log")]]
    assert calls == [["tail", "-n", "100", *(["-f"] if follow else []), str(state_dir / "server.log")]]


def test_service_log_prints_last_100_lines(monkeypatch, tmp_path):
    for case_index, count in enumerate([99, 100, 101, 200]):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        with monkeypatch.context() as case_monkeypatch:
            case_monkeypatch.setenv("LEMONCROW_LOCAL_SERVER_SUPERVISOR", "none")
            case_monkeypatch.setenv("LEMONCROW_LOCAL_SERVER_DIR", str(case_tmp_path))
            lines = [f"line {i}" for i in range(count)]
            (case_tmp_path / "server.log").write_text("\n".join(lines) + "\n")

            def run_command(name, args):
                completed = subprocess.run(args, check=True, capture_output=True, text=True)
                service_worker.click.echo(completed.stdout, nl=False)

            case_monkeypatch.setattr(service_worker.os, "execvp", run_command)
            result = CliRunner().invoke(service_worker.service_group, ["log"])
            assert result.exit_code == 0, result.output
            assert result.output.splitlines() == lines[-100:]

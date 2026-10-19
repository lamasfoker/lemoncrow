"""P3 Review-only service, checkpoint and outbound work-hint qualification."""

from __future__ import annotations

import io
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from lemoncrow_client import review as review_cli
from lemoncrow_client import review_service as service
from lemoncrow_client.review_hooks import hook_main


def test_hook_fails_closed_for_unknown_remote_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("lemoncrow_client.review_hooks.watch_main", lambda args, *, sink: calls.append(args) or 0)
    out = io.StringIO()
    for event in ("after-edit", "after-checkpoint", "on-exit"):
        assert hook_main([event], sink=out) == 0
    assert calls[0] == ["--once", "--json", "--no-impact"]
    assert calls[1:] == [["--once", "--json", "--no-impact", "--include-clean-head"]] * 2
    assert hook_main(["on-exit", "--staged"], sink=out) == 0
    assert calls[-1][-1] == "--staged"
    assert hook_main(["after-edit", "--remote-command"], sink=out) == 2
    assert len(calls) == 4


def test_public_dispatch_isolated_from_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(service, "serve_main", lambda args, *, sink: calls.append("serve") or 0)
    monkeypatch.setattr(service, "service_main", lambda args, *, sink: calls.append("service") or 0)
    monkeypatch.setattr("lemoncrow_client.review_hooks.hook_main", lambda args, *, sink: calls.append("hook") or 0)
    assert review_cli.review_main(["device", "serve", "--once"], sink=io.StringIO()) == 0
    assert review_cli.review_main(["service", "install", "--dry-run"], sink=io.StringIO()) == 0
    assert review_cli.review_main(["hook", "after-checkpoint"], sink=io.StringIO()) == 0
    assert calls == ["serve", "service", "hook"]


def test_service_unit_is_restricted_and_no_secrets(tmp_path: Path) -> None:
    unit = service._unit_text(binary=tmp_path / "lcr", repo=tmp_path / "repo", state_dir=tmp_path / "state")
    assert "device serve" in unit and "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit and "UMask=0077" in unit
    assert "ReadWritePaths=" in unit and "WantedBy=default.target" in unit
    for forbidden in ("Bearer", "machine_token", "--model", "ExecStartPre", "ListenStream"):
        assert forbidden not in unit
    assert service._service_name(tmp_path / "repo") != service._service_name(tmp_path / "other")
    quoted = service._systemd_quote('/tmp/a "quoted" %path')
    assert "%%path" in quoted and chr(92) + chr(34) in quoted
    with pytest.raises(ValueError):
        service._systemd_quote("bad\npath")


def test_service_is_never_installed_without_operator_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, state = tmp_path / "repo", tmp_path / "state"
    repo.mkdir()
    state.mkdir()
    binary = tmp_path / "lcr"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service.shutil, "which", lambda _: str(binary))
    monkeypatch.setattr(
        service, "load_config", lambda **_: SimpleNamespace(repo_root=repo, state_dir=state, hosted=True)
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(service, "_run_systemctl", lambda *args: commands.append(args) or SimpleNamespace(returncode=0))
    assert service.service_main(["install", "--dry-run"], sink=io.StringIO()) == 0
    assert not (tmp_path / "config").exists() and commands == []
    assert service.service_main(["install"], sink=io.StringIO()) == 0
    preflights: list[str] = []
    monkeypatch.setattr(service, "_require_ready_machine", lambda _: preflights.append("checked"))
    unit = tmp_path / "config" / "systemd" / "user" / service._service_name(repo)
    assert stat.S_IMODE(unit.stat().st_mode) == 0o600
    assert service.service_main(["start"], sink=io.StringIO()) == 0
    assert preflights == ["checked"]
    assert ("enable", "--now", service._service_name(repo)) in commands
    assert service.service_main(["stop"], sink=io.StringIO()) == 0
    assert service.service_main(["remove"], sink=io.StringIO()) == 0
    assert not unit.exists()


def test_symlink_unit_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, state = tmp_path / "repo", tmp_path / "state"
    repo.mkdir()
    state.mkdir()
    binary = tmp_path / "lcr"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service.shutil, "which", lambda _: str(binary))
    monkeypatch.setattr(
        service, "load_config", lambda **_: SimpleNamespace(repo_root=repo, state_dir=state, hosted=True)
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit = tmp_path / "config" / "systemd" / "user" / service._service_name(repo)
    unit.parent.mkdir(parents=True)
    marker = tmp_path / "marker"
    marker.write_text("unchanged")
    unit.symlink_to(marker)
    assert service.service_main(["install"], sink=io.StringIO()) == 1
    assert marker.read_text() == "unchanged"


def test_foreground_service_fails_closed_without_pairing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service,
        "load_config",
        lambda **_: SimpleNamespace(
            hosted=True,
            repo_root=Path("/repo"),
            review_state_dir=Path("/state"),
            url="https://app.lemoncrow.com",
        ),
    )
    monkeypatch.setattr(service, "_paired_binding", lambda _: None)
    assert service.serve_main([], sink=io.StringIO()) == 1
    calls: list[list[str]] = []
    monkeypatch.setattr(service, "_paired_binding", lambda _: object())
    monkeypatch.setattr(
        service, "load_pairing", lambda *_args, **_kwargs: {"execution_classes": ["review_intelligence"]}
    )
    monkeypatch.setattr(service, "companion_watch_main", lambda args, *, sink: calls.append(args) or 0)
    assert service.serve_main([], sink=io.StringIO()) == 0
    assert service.serve_main(["--once"], sink=io.StringIO()) == 0
    assert calls == [["--no-impact"], ["--once", "--no-impact"]]
    assert service.serve_main(["--run-all"], sink=io.StringIO()) == 2


def test_observer_uses_wss_hints_only_without_claim_or_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from lemoncrow_client.review_worker_client import WorkerBinding

    machine = WorkerBinding(
        origin="https://app.lemoncrow.com",
        org_id="orgA",
        repo_id="repoA",
        worker_id="machineA",
        bearer="separate-review-worker-machine-token-12345678",
        trusted_key_id="keyA",
        trusted_public_key_hex="11" * 32,
    )
    stop = threading.Event()
    events: list[str] = []

    class Transport:
        def __init__(self, origin: str, *, timeout_s: int) -> None:
            assert origin == machine.origin

        def websocket_hints(self, path: str, *, bearer: str, timeout_s: int, max_messages: int):
            assert path.endswith("review_intelligence") and bearer == machine.bearer
            yield {"execution_class": "review_intelligence", "automatic": 0, "manual": 1, "poll_after_s": 10}
            events.append("wss")
            stop.set()

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(service, "HttpTransport", Transport)
    monkeypatch.setattr(service, "check_machine_keys", lambda *_a, **_k: events.append("keys"))
    monkeypatch.setattr(service, "pending_work_hint", lambda *_a, **_k: pytest.fail("healthy WSS uses no fallback"))
    output = io.StringIO()
    service._observe_work_hints(machine, stop, output)
    assert events == ["keys", "wss", "close"]
    assert "review_intelligence pending" in output.getvalue()
    assert "1 manual" in output.getvalue() and "no work claimed" in output.getvalue()
    assert machine.bearer not in output.getvalue()


def test_observer_wss_error_falls_back_to_bounded_https_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from lemoncrow_client.review_worker_client import WorkerBinding

    machine = WorkerBinding(
        origin="https://app.lemoncrow.com",
        org_id="orgA",
        repo_id="repoA",
        worker_id="machineA",
        bearer="separate-review-worker-machine-token-12345678",
        trusted_key_id="keyA",
        trusted_public_key_hex="11" * 32,
    )
    stop = threading.Event()
    events: list[str] = []

    class Transport:
        def __init__(self, origin: str, *, timeout_s: int) -> None:
            pass

        def websocket_hints(self, *_args, **_kw):
            events.append("wss")
            raise OSError("gateway does not upgrade")
            yield {}

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(service, "HttpTransport", Transport)
    monkeypatch.setattr(service, "check_machine_keys", lambda *_a, **_k: events.append("keys"))

    def fallback(*_args, **_kwargs):
        events.append("https")
        stop.set()
        return (0, 2, 10)

    monkeypatch.setattr(service, "pending_work_hint", fallback)
    output = io.StringIO()
    service._observe_work_hints(machine, stop, output)
    assert events == ["keys", "wss", "https", "close"]
    assert "2 manual" in output.getvalue()
    assert machine.bearer not in output.getvalue()


def test_observer_refuses_revoked_key_without_transport_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from lemoncrow_client.review_worker_client import WorkerBinding

    machine = WorkerBinding(
        origin="https://app.lemoncrow.com",
        org_id="orgA",
        repo_id="repoA",
        worker_id="machineA",
        bearer="separate-review-worker-machine-token-12345678",
        trusted_key_id="keyA",
        trusted_public_key_hex="11" * 32,
    )
    events: list[str] = []

    class Transport:
        def __init__(self, *_args, **_kw):
            pass

        def websocket_hints(self, *_args, **_kw):
            pytest.fail("revoked signer must not connect")

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(service, "HttpTransport", Transport)
    monkeypatch.setattr(service, "check_machine_keys", lambda *_a, **_k: (_ for _ in ()).throw(ValueError("revoked")))
    out = io.StringIO()
    service._observe_work_hints(machine, threading.Event(), out)
    assert "no longer authorized" in out.getvalue()
    assert events == ["close"]


def test_generated_linux_unit_verifies_with_spaces_in_paths(tmp_path: Path) -> None:
    import shutil
    import subprocess

    verifier = shutil.which("systemd-analyze")
    if verifier is None:
        pytest.skip("systemd-analyze unavailable on this platform")
    repo = tmp_path / "checkout directory"
    state = tmp_path / "state directory"
    repo.mkdir()
    state.mkdir()
    unit = tmp_path / "lemoncrow-review-test.service"
    unit.write_text(service._unit_text(binary=Path("/usr/bin/true"), repo=repo, state_dir=state))
    result = subprocess.run([verifier, "verify", str(unit)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stderr == "", result.stderr


def test_mac_launchd_plist_has_no_secrets_and_opt_in_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import plistlib

    from lemoncrow_client import review_launchd

    repo, state = tmp_path / "checkout", tmp_path / "state"
    repo.mkdir()
    state.mkdir()
    binary = tmp_path / "lcr"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    payload = review_launchd._plist(executable=binary, repo=repo, state_dir=state)
    entry = plistlib.loads(payload)
    assert entry["ProgramArguments"] == [str(binary), "device", "serve"]
    assert entry["WorkingDirectory"] == str(repo)
    assert entry["EnvironmentVariables"] == {"LEMONCROW_HOME": str(state), "GIT_OPTIONAL_LOCKS": "0"}
    assert "machine_token" not in payload.decode()
    assert "Bearer" not in payload.decode()
    monkeypatch.setattr(review_launchd.Path, "home", lambda: tmp_path)
    events: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        review_launchd, "_launchctl", lambda *args: events.append(args) or SimpleNamespace(returncode=0)
    )
    out = io.StringIO()
    assert (
        review_launchd.manage_launchd("install", binary=binary, repo=repo, state_dir=state, sink=out, dry_run=True) == 0
    )
    path = tmp_path / "Library" / "LaunchAgents" / (review_launchd._label(repo) + ".plist")
    assert not path.exists() and events == []
    assert review_launchd.manage_launchd("install", binary=binary, repo=repo, state_dir=state, sink=out) == 0
    assert path.exists() and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert review_launchd.manage_launchd("start", binary=binary, repo=repo, state_dir=state, sink=out) == 0
    assert events[0][0] == "bootstrap"
    assert review_launchd.manage_launchd("stop", binary=binary, repo=repo, state_dir=state, sink=out) == 0
    assert events[-1][0] == "bootout"
    assert review_launchd.manage_launchd("remove", binary=binary, repo=repo, state_dir=state, sink=out) == 0
    assert not path.exists()


def test_mac_service_dispatches_without_systemd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, state = tmp_path / "checkout", tmp_path / "state"
    repo.mkdir()
    state.mkdir()
    binary = tmp_path / "lcr"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service.shutil, "which", lambda _: str(binary))
    monkeypatch.setattr(
        service, "load_config", lambda **_: SimpleNamespace(repo_root=repo, state_dir=state, hosted=True)
    )
    from lemoncrow_client import review_launchd

    actions: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        review_launchd, "manage_launchd", lambda action, **kw: actions.append((action, kw["dry_run"])) or 0
    )
    preflights: list[str] = []
    monkeypatch.setattr(service, "_require_ready_machine", lambda _: preflights.append("checked"))
    assert service.service_main(["install", "--dry-run"], sink=io.StringIO()) == 0
    assert service.service_main(["start"], sink=io.StringIO()) == 0
    assert preflights == ["checked"]
    assert actions == [("install", True), ("start", False)]


@pytest.mark.parametrize(
    "allowed",
    [
        ["runtime_validation"],
        ["review_intelligence"],
        ["review_intelligence", "runtime_validation"],
    ],
)
def test_serve_observes_only_machine_granted_classes(
    monkeypatch: pytest.MonkeyPatch,
    allowed: list[str],
) -> None:
    config = SimpleNamespace(
        hosted=True,
        repo_root=Path("/repo"),
        review_state_dir=Path("/state"),
        url="https://app.lemoncrow.com",
    )
    observations: list[str] = []
    monkeypatch.setattr(service, "load_config", lambda **_: config)
    monkeypatch.setattr(service, "_paired_binding", lambda _: object())
    monkeypatch.setattr(service, "load_pairing", lambda *_args, **_kwargs: {"execution_classes": allowed})
    monkeypatch.setattr(
        service,
        "_observe_work_hints",
        lambda _b, _s, _out, *, execution_class: observations.append(execution_class),
    )
    monkeypatch.setattr(service, "companion_watch_main", lambda *_args, **_kwargs: 0)
    assert service.serve_main([], sink=io.StringIO()) == 0
    assert sorted(observations) == sorted(allowed)


def test_runtime_only_device_never_subscribes_to_ai_hints(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from lemoncrow_client.review_worker_client import WorkerBinding

    machine = WorkerBinding(
        origin="https://app.lemoncrow.com",
        org_id="orgA",
        repo_id="repoA",
        worker_id="machineA",
        bearer="separate-review-worker-machine-token-12345678",
        trusted_key_id="keyA",
        trusted_public_key_hex="11" * 32,
    )
    stop = threading.Event()
    events: list[str] = []

    class Transport:
        def __init__(self, origin: str, *, timeout_s: int) -> None:
            assert origin == machine.origin

        def websocket_hints(self, path: str, *, bearer: str, timeout_s: int, max_messages: int):
            assert path.endswith("runtime_validation") and bearer == machine.bearer
            yield {"execution_class": "runtime_validation", "automatic": 3, "manual": 0, "poll_after_s": 10}
            stop.set()

        def close(self) -> None:
            events.append("closed")

    monkeypatch.setattr(service, "HttpTransport", Transport)
    monkeypatch.setattr(service, "check_machine_keys", lambda *_a, **_k: events.append("keys"))
    monkeypatch.setattr(service, "pending_work_hint", lambda *_a, **_k: pytest.fail("unexpected fallback"))
    out = io.StringIO()
    service._observe_work_hints(machine, stop, out, execution_class="runtime_validation")
    assert "runtime_validation pending: 3 automatic" in out.getvalue()
    assert machine.bearer not in out.getvalue()
    assert events == ["keys", "closed"]


def test_service_start_preflight_requires_capture_auth_and_pinned_pairing(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow_client.review_worker_client import WorkerBinding

    config = SimpleNamespace(hosted=True, authenticated=False, request_timeout_s=3.0)
    monkeypatch.setattr(service, "_paired_binding", lambda _: pytest.fail("unauthenticated preflight read pairing"))
    with pytest.raises(ValueError, match="signed-in"):
        service._require_ready_machine(config)
    config.authenticated = True
    monkeypatch.setattr(service, "_paired_binding", lambda _: None)
    with pytest.raises(ValueError, match="paired"):
        service._require_ready_machine(config)
    binding = WorkerBinding(
        origin="https://app.lemoncrow.com",
        org_id="orgA",
        repo_id="repoA",
        worker_id="machineA",
        bearer="separate-review-worker-machine-token-12345678",
        trusted_key_id="keyA",
        trusted_public_key_hex="11" * 32,
    )
    monkeypatch.setattr(service, "_paired_binding", lambda _: binding)
    events: list[str] = []

    class Transport:
        def __init__(self, origin: str, *, timeout_s: float) -> None:
            assert origin == binding.origin and timeout_s == 3.0
            events.append("opened")

        def close(self) -> None:
            events.append("closed")

    monkeypatch.setattr(service, "HttpTransport", Transport)
    monkeypatch.setattr(service, "check_machine_keys", lambda *_a, **_kw: events.append("pinned"))
    service._require_ready_machine(config)
    assert events == ["opened", "pinned", "closed"]
    events.clear()
    monkeypatch.setattr(service, "check_machine_keys", lambda *_a, **_kw: (_ for _ in ()).throw(ValueError("rotated")))
    with pytest.raises(ValueError, match="rotated"):
        service._require_ready_machine(config)
    assert events == ["opened", "closed"]  # Always close even on signer mismatch.


def test_linux_service_start_refuses_before_systemctl_for_unready_machine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, state = tmp_path / "repo", tmp_path / "state"
    repo.mkdir()
    state.mkdir()
    binary = tmp_path / "lcr"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service.shutil, "which", lambda _: str(binary))
    monkeypatch.setattr(
        service,
        "load_config",
        lambda **_: SimpleNamespace(
            hosted=True,
            repo_root=repo,
            state_dir=state,
        ),
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit = tmp_path / "config" / "systemd" / "user" / service._service_name(repo)
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=/usr/bin/true\n")
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(service, "_run_systemctl", lambda *args: commands.append(args) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(service, "_require_ready_machine", lambda _: (_ for _ in ()).throw(ValueError("unready")))
    output = io.StringIO()
    assert service.service_main(["start"], sink=output) == 1
    assert commands == []
    assert "unready" not in output.getvalue()
    assert "refused" in output.getvalue()

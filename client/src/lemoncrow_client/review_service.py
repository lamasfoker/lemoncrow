"""Explicit opt-in Review user service. No Runtime, shell, model or job dispatch.

A single POSIX foreground watcher owns capture and check-ins. The optional
systemd user unit starts *that same command* at login, not a root daemon.
All credentials remain in existing owner-private Review state; none enter a
unit, process argv or journal. No local network listener is created.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import TextIO

from .config import ClientConfig, load_config
from .errors import ClientError
from .review_companion_cli import (
    _paired_binding,
    check_machine_keys,
    companion_watch_main,
    pending_work_hint,
    validate_pending_hint,
    work_hint_stream,
)
from .review_device_connect import load_pairing
from .transport import HttpTransport

_UNIT = re.compile(r"[a-z0-9_.-]{1,80}\Z")


def _service_name(repo: Path) -> str:
    digest = hashlib.sha256(os.fsencode(str(repo.resolve()))).hexdigest()[:16]
    return f"lemoncrow-review-{digest}.service"


def _systemd_quote(value: str) -> str:
    if not value or any(ord(char) < 32 for char in value):
        raise ValueError("unsafe Review service path")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _systemd_path(value: str) -> str:
    # A systemd path field is not a shell word: unlike ExecStart and
    # Environment, enclosing quotes are interpreted as filename characters.
    if not value.startswith("/") or any(ord(ch) < 32 or ch in '"' + chr(92) for ch in value):
        raise ValueError("unsafe Review service path")
    return value.replace("%", "%%")


def _unit_text(*, binary: Path, repo: Path, state_dir: Path) -> str:
    # systemd unit escaping is not a shell. No user-provided command or environment.
    return (
        "[Unit]\nDescription=LemonCrow Review capture for this checkout\n"
        "After=network-online.target\nWants=network-online.target\n"
        "StartLimitIntervalSec=300\nStartLimitBurst=5\n\n"
        "[Service]\nType=simple\n"
        f"WorkingDirectory={_systemd_path(str(repo))}\n"
        f"Environment={_systemd_quote('LEMONCROW_HOME=' + str(state_dir))}\n"
        "Environment=GIT_OPTIONAL_LOCKS=0\n"
        f"ExecStart={_systemd_quote(str(binary))} device serve\n"
        "Restart=on-failure\nRestartSec=10s\n"
        "UMask=0077\nNoNewPrivileges=true\nPrivateTmp=true\n"
        "ProtectSystem=strict\n"
        f"ReadWritePaths={_systemd_quote(str(state_dir))}\n"
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX\n"
        "\n[Install]\nWantedBy=default.target\n"
    )


def _secure_unit_destination(path: Path) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise ValueError("unsafe Review user service directory")
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Review user service file is not owned regular data")


def _run_systemctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )


def _require_ready_machine(config: ClientConfig) -> None:
    """Refuse service start before OS activation if capture or pairing is unready.

    No secrets are printed, no pairing or credentials are changed, no job is
    claimed and no model is called. This is explicit start-time network access,
    never performed during install, dry-run, status, stop or remove.
    """
    if not config.hosted or not config.authenticated:
        raise ValueError("Hosted Review service requires a signed-in account")
    binding = _paired_binding(config)
    if binding is None:
        raise ValueError("Review service requires an authorized paired machine")
    transport = HttpTransport(binding.origin, timeout_s=min(15.0, config.request_timeout_s))
    try:
        check_machine_keys(binding, transport=transport)
    finally:
        transport.close()


def service_main(arguments: list[str], *, sink: TextIO) -> int:
    """Explicit unit management. Only install mutates service files; no sudo."""
    usage = "Usage: lcr service install [--dry-run] | start | stop | status | remove\n"
    if not arguments or arguments[0] in {"help", "-h", "--help"}:
        sink.write(usage)
        sink.write("Optional systemd --user service for one checkout; no agent tasks are auto-run.\n")
        return 0 if arguments else 2
    if arguments not in (["install"], ["install", "--dry-run"], ["start"], ["stop"], ["status"], ["remove"]):
        sink.write(usage)
        return 2
    if (
        (sys.platform == "linux" and not shutil.which("systemctl"))
        or (sys.platform == "darwin" and not shutil.which("launchctl"))
        or sys.platform not in {"linux", "darwin"}
    ):
        sink.write("Review user services need systemd --user on Linux or launchctl on macOS.\n")
        return 2
    try:
        config = load_config(product="review")
        repo = config.repo_root.resolve(strict=True)
        state_dir = config.state_dir.expanduser().resolve()
        if not config.hosted:
            sink.write("Review service is for paired Hosted machines; local OSS uses lcr watch.\n")
            return 2
        binary_str = shutil.which("lcr")
        if not binary_str:
            sink.write("Installed lcr executable is not on PATH. Install Review before enabling its service.\n")
            return 2
        binary = Path(binary_str).resolve(strict=True)
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError("Review executable is not runnable")
        if sys.platform == "darwin":
            if arguments[0] == "start":
                _require_ready_machine(config)
            from .review_launchd import manage_launchd

            return manage_launchd(
                arguments[0],
                binary=binary,
                repo=repo,
                state_dir=state_dir,
                sink=sink,
                dry_run=arguments == ["install", "--dry-run"],
            )
        name = _service_name(repo)
        if not _UNIT.fullmatch(name):
            raise ValueError("invalid Review service identifier")
        unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd" / "user"
        unit = unit_dir / name
        action = arguments[0]
        if action == "install":
            rendered = _unit_text(binary=binary, repo=repo, state_dir=state_dir)
            if arguments == ["install", "--dry-run"]:
                sink.write(rendered)
                return 0
            _secure_unit_destination(unit)
            fd, pending = tempfile.mkstemp(prefix=".review-unit-", dir=unit.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(rendered)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(pending, 0o600)
                os.replace(pending, unit)
            finally:
                if os.path.exists(pending):
                    os.unlink(pending)
            result = _run_systemctl("daemon-reload")
            sink.write(
                "Review unit installed (not started). Run lcr service start.\n"
                if result.returncode == 0
                else "Review unit written but systemd reload failed; do not assume it is active.\n"
            )
            return 0 if result.returncode == 0 else 1
        if action != "status" and (not unit.is_file() or unit.is_symlink()):
            sink.write("Review unit is not installed for this checkout.\n")
            return 1
        if action == "start":
            _require_ready_machine(config)
        if action == "remove":
            stopped = _run_systemctl("disable", "--now", name)
            if stopped.returncode != 0:
                sink.write("Review service could not be stopped; unit left installed.\n")
                return 1
            _secure_unit_destination(unit)
            unit.unlink()
            _run_systemctl("daemon-reload")
            sink.write("Review user service removed. Existing pairing unchanged.\n")
            return 0
        operation = {
            "start": ("enable", "--now", name),
            "stop": ("disable", "--now", name),
            "status": ("is-active", name),
        }[action]
        result = _run_systemctl(*operation)
        if action == "status":
            sink.write(f"Review service {name}: {'active' if result.returncode == 0 else 'inactive or unavailable'}\n")
            return 0 if result.returncode == 0 else 1
        sink.write(f"Review service {action}: {'accepted' if result.returncode == 0 else 'failed'}\n")
        return 0 if result.returncode == 0 else 1
    except (ClientError, ValueError, OSError, subprocess.TimeoutExpired):
        sink.write("Review service command refused; check Hosted mode, checkout and user service access.\n")
        return 1


def _observe_work_hints(
    binding: object, stop: threading.Event, sink: TextIO, *, execution_class: str = "review_intelligence"
) -> None:
    """Outbound WSS/HTTPS hints in the *capture process*, with no job authority.

    The stream is strictly informational. A command/prompt in a WebSocket frame
    is not parsed as work and cannot start a subprocess or spend provider funds.
    Each reconnect checks the pinned signer; revocation stops rather than
    trying another token or host. HTTPS polls use server-provided bounded delay.
    """
    from .review_worker_client import WorkerBinding

    if not isinstance(binding, WorkerBinding):
        return
    if execution_class not in {"review_intelligence", "runtime_validation"}:
        raise ValueError("unsupported paired Review work-hint class")
    transport = HttpTransport(binding.origin, timeout_s=15)
    previous: tuple[int, int] | None = None
    try:
        while not stop.is_set():
            try:
                check_machine_keys(binding, transport=transport)
            except (ClientError, OSError, TimeoutError, ValueError):
                sink.write("Review work-hint connection no longer authorized; no work claimed.\n")
                sink.flush()
                break
            try:
                for event in work_hint_stream(binding, transport, execution_class=execution_class):
                    counters = validate_pending_hint(event, execution_class=execution_class)
                    if counters[:2] != previous:
                        sink.write(
                            f"Review {execution_class} pending: {counters[0]} automatic, {counters[1]} manual; no work claimed.\n"
                        )
                        sink.flush()
                        previous = counters[:2]
                    if stop.is_set():
                        break
                if stop.wait(1):
                    break
                continue
            except (ClientError, OSError, TimeoutError, ValueError):
                # Temporary gateway/proxy WebSocket failure: HTTPS fallback
                # retains the same scoped, read-only machine identity.
                try:
                    auto, manual, interval = pending_work_hint(
                        binding, transport=transport, execution_class=execution_class
                    )
                except (ClientError, OSError, TimeoutError, ValueError):
                    # Credentials may have been revoked; the next key check
                    # fails closed. Bound retries even when the gateway fails.
                    stop.wait(15)
                    continue
                if previous != (auto, manual):
                    sink.write(
                        f"Review {execution_class} pending: {auto} automatic, {manual} manual; no work claimed.\n"
                    )
                    sink.flush()
                    previous = (auto, manual)
                stop.wait(interval)
    finally:
        transport.close()


def serve_main(arguments: list[str], *, sink: TextIO) -> int:
    """Single Review capture+paired-presence process; safe on headless machines."""
    if arguments in (["--help"], ["help"], ["-h"]):
        sink.write(
            "Usage: lcr device serve [--once]\n"
            "Capture + authenticated machine presence, without a listener or unattended job execution.\n"
        )
        return 0
    if arguments not in ([], ["--once"]):
        sink.write("Usage: lcr device serve [--once]\n")
        return 2
    if arguments:
        return companion_watch_main(["--once", "--no-impact"], sink=sink)
    # A paired Hosted machine uses one foreground process for settled capture,
    # presence and work hints; no second sidecar/daemon or task runner.
    stop = threading.Event()
    monitors: list[threading.Thread] = []
    try:
        config = load_config(product="review")
        if not config.hosted:
            sink.write("Paired Review service requires Hosted mode; use lcr watch locally.\n")
            return 2
        binding = _paired_binding(config)
        if binding is None:
            sink.write("No paired Review machine. Run lcr connect first.\n")
            return 1
        paired = load_pairing(config.repo_root, config.review_state_dir, expected_origin=config.url)
        if paired is None or not isinstance(paired.get("execution_classes"), list):
            raise ValueError("Review service has no authorized work-hint capabilities")
        # Each class has its own bounded receive-only WSS stream. Never ask for
        # a capability omitted from the signed-in operator's paired machine.
        for execution_class in paired["execution_classes"]:
            if execution_class not in {"runtime_validation", "review_intelligence"}:
                raise ValueError("Review service pairing has unsupported capabilities")
            monitor = threading.Thread(
                target=_observe_work_hints,
                args=(binding, stop, sink),
                kwargs={"execution_class": execution_class},
                name=f"review-work-hints-{execution_class}",
                daemon=True,
            )
            monitor.start()
            monitors.append(monitor)
        return companion_watch_main(["--no-impact"], sink=sink)
    except (ClientError, OSError, ValueError):
        sink.write("Review service cannot start without an authenticated paired machine.\n")
        return 1
    finally:
        stop.set()
        for monitor in monitors:
            monitor.join(timeout=1)

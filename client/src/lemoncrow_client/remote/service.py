"""User-level supervision of the persistent remote MCP gateway and its tunnel.

Two machine-wide units, whatever the number of connector hostnames:

* ``lemoncrow-mcp-gateway.service`` runs ``python -m lemoncrow_client runtime
  mcp gateway`` on loopback 7421 (Runtime Lite, or the hosted thin client).
* ``lemoncrow-mcp-tunnel.service`` runs the shared cloudflared named tunnel.

launchd gets the same pair as ``com.lemoncrow.mcp.gateway``/``.tunnel``. The
unit names are the ones the server-hosted gateway used, so ``repair`` rewrites
an existing installation in place.
"""

from __future__ import annotations

import getpass
import os
import plistlib
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

from . import tunnel
from .state import load_all_connectors

GATEWAY_UNIT: Final[str] = "lemoncrow-mcp-gateway.service"
TUNNEL_UNIT: Final[str] = "lemoncrow-mcp-tunnel.service"
GATEWAY_LABEL: Final[str] = "com.lemoncrow.mcp.gateway"
TUNNEL_LABEL: Final[str] = "com.lemoncrow.mcp.tunnel"
DEFAULT_ORIGIN_PORT: Final[int] = 7421


class ServiceError(RuntimeError):
    pass


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _is_macos() -> bool:
    return sys.platform == "darwin"


def systemd_user_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def launchd_user_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def supervisor_kind() -> str | None:
    # A test run must never install or restart the login's real units.
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("LEMONCROW_MCP_ALLOW_SERVICE", "") != "1":
        return None
    if _is_macos() and shutil.which("launchctl") is not None:
        return "launchd"
    if _is_linux() and shutil.which("systemctl") is not None:
        return "systemd"
    return None


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(part for part in (result.stdout, result.stderr) if part).strip()


def origin_port(environment: Mapping[str, str]) -> int:
    raw = environment.get("LEMONCROW_MCP_GATEWAY_PORT", "").strip() or str(DEFAULT_ORIGIN_PORT)
    try:
        port = int(raw)
    except ValueError as exc:
        raise ServiceError("LEMONCROW_MCP_GATEWAY_PORT must be an integer") from exc
    if not 1 <= port <= 65_535:
        raise ServiceError("LEMONCROW_MCP_GATEWAY_PORT must be between 1 and 65535")
    return port


def gateway_command(port: int) -> list[str]:
    return [sys.executable, "-m", "lemoncrow_client", "runtime", "mcp", "gateway", "--port", str(port)]


def pinned_environment(environment: Mapping[str, str], store_root: Path) -> dict[str, str]:
    """The state location the installing shell used, so the unit serves the same state."""

    pinned = {
        name: value for name in ("LEMONCROW_HOME", "LEMONCROW_ROOT") if (value := environment.get(name, "").strip())
    }
    return pinned or {"LEMONCROW_HOME": str(store_root)}


def _systemd_environment(pinned: Mapping[str, str]) -> str:
    lines = []
    for name, value in sorted(pinned.items()):
        # systemd expands %-specifiers in Environment=; %% is a literal %.
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
        lines.append(f'Environment="{name}={escaped}"\n')
    return "".join(lines)


_SYSTEMD_PLAIN_WORD: Final = re.compile(r"[A-Za-z0-9_@+=:,./-]+")


def _systemd_exec(argv: Sequence[str]) -> str:
    """An ``ExecStart=`` command line systemd parses back into exactly ``argv``.

    systemd expands ``%`` specifiers and ``$`` variables in command lines, so
    both are doubled; any other unsafe word is double-quoted with C escapes.
    """

    words = []
    for value in argv:
        if _SYSTEMD_PLAIN_WORD.fullmatch(value):
            words.append(value)
            continue
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        words.append(f'"{escaped}"')
    return " ".join(words)


def render_systemd_units(*, gateway: list[str], tunnel_command: list[str], pinned: Mapping[str, str]) -> dict[str, str]:
    return {
        GATEWAY_UNIT: (
            "[Unit]\n"
            "Description=LemonCrow remote MCP gateway\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n"
            "StartLimitIntervalSec=0\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={_systemd_exec(gateway)}\n"
            f"{_systemd_environment(pinned)}"
            "Restart=always\n"
            "RestartSec=1\n"
            "TimeoutStopSec=10\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        ),
        TUNNEL_UNIT: (
            "[Unit]\n"
            "Description=LemonCrow remote MCP shared tunnel\n"
            f"After=network-online.target {GATEWAY_UNIT}\n"
            f"Wants=network-online.target {GATEWAY_UNIT}\n"
            "StartLimitIntervalSec=0\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={_systemd_exec(tunnel_command)}\n"
            "Restart=always\n"
            "RestartSec=5\n"
            "TimeoutStopSec=5\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        ),
    }


def _enable_linger(narrate: Callable[[str], None]) -> None:
    if shutil.which("loginctl") is None:
        return
    user = getpass.getuser()
    shown = _run(["loginctl", "show-user", user, "--property=Linger"])
    if shown.returncode == 0 and "Linger=yes" in shown.stdout:
        return
    if _run(["loginctl", "enable-linger", user]).returncode == 0:
        narrate("Enabled linger: the connector also runs while you are logged out.")
    else:
        narrate(f"Could not enable linger; run: sudo loginctl enable-linger {user}")


def _register_systemd(units: Mapping[str, str], narrate: Callable[[str], None]) -> str:
    directory = systemd_user_dir()
    directory.mkdir(parents=True, exist_ok=True)
    changed: dict[str, bool] = {}
    for name, content in units.items():
        path = directory / name
        changed[name] = not path.exists() or path.read_text(encoding="utf-8") != content
        if changed[name]:
            path.write_text(content, encoding="utf-8")
            narrate(f"Installed {path}")
    if any(changed.values()):
        reloaded = _run(["systemctl", "--user", "daemon-reload"])
        if reloaded.returncode != 0:
            raise ServiceError(f"systemctl --user daemon-reload failed: {_output(reloaded)}")
    for name in (GATEWAY_UNIT, TUNNEL_UNIT):
        enabled = _run(["systemctl", "--user", "enable", name])
        if enabled.returncode != 0:
            raise ServiceError(f"systemctl --user enable {name} failed: {_output(enabled)}")
    for name in (GATEWAY_UNIT, TUNNEL_UNIT):
        active = _run(["systemctl", "--user", "is-active", "--quiet", name]).returncode == 0
        if changed[name] or not active:
            restarted = _run(["systemctl", "--user", "restart", name])
            if restarted.returncode != 0:
                raise ServiceError(f"systemctl --user restart {name} failed: {_output(restarted)}")
    _enable_linger(narrate)
    return TUNNEL_UNIT


def _register_launchd(
    jobs: Mapping[str, tuple[list[str], Mapping[str, str]]], store_root: Path, narrate: Callable[[str], None]
) -> str:
    directory = launchd_user_dir()
    directory.mkdir(parents=True, exist_ok=True)
    log_dir = store_root / "mcp"
    log_dir.mkdir(parents=True, exist_ok=True)
    for label, (command, environment) in jobs.items():
        plist = directory / f"{label}.plist"
        log = str(log_dir / f"{label.rsplit('.', 1)[-1]}.log")
        document: dict[str, object] = {
            "Label": label,
            "ProgramArguments": command,
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": log,
            "StandardErrorPath": log,
        }
        if environment:
            document["EnvironmentVariables"] = dict(environment)
        plist.write_bytes(plistlib.dumps(document))
        _run(["launchctl", "unload", str(plist)])
        loaded = _run(["launchctl", "load", str(plist)])
        if loaded.returncode != 0:
            raise ServiceError(f"launchctl load failed for {label}: {_output(loaded)}")
        narrate(f"Installed {plist}")
    return TUNNEL_LABEL


def register_shared_service(
    *,
    binary: str,
    state: tunnel.TunnelState,
    port: int,
    store_root: Path,
    environment: Mapping[str, str],
    narrate: Callable[[str], None],
) -> str:
    """Install (or rewrite) and start the gateway and tunnel units; return the tunnel unit."""

    gateway = gateway_command(port)
    pinned = pinned_environment(environment, store_root)
    tunnel_command = tunnel.named_tunnel_command(
        binary=binary, tunnel_ref=state.tunnel_id, credentials_path=state.credentials_path, origin_port=port
    )
    kind = supervisor_kind()
    if kind == "systemd":
        return _register_systemd(
            render_systemd_units(gateway=gateway, tunnel_command=tunnel_command, pinned=pinned), narrate
        )
    if kind == "launchd":
        return _register_launchd(
            {GATEWAY_LABEL: (gateway, pinned), TUNNEL_LABEL: (tunnel_command, {})}, store_root, narrate
        )
    raise ServiceError("no systemd/launchd user session is available")


def repair_shared_service(
    *, store_root: Path, environment: Mapping[str, str], narrate: Callable[[str], None]
) -> str | None:
    """Recreate the units from durable state; never provisions Cloudflare or DNS.

    This is also the migration path: units written for the server-hosted
    gateway differ from the rendered ones, so they are rewritten and restarted.
    """

    if not load_all_connectors(store_root):
        return None
    state = tunnel.load_tunnel_state(tunnel.tunnel_state_path(store_root))
    if state is None:
        raise ServiceError("persistent MCP connectors exist but the shared tunnel state is missing")
    credentials = Path(state.credentials_path).expanduser()
    if not credentials.is_file():
        raise ServiceError(f"shared tunnel credentials are missing: {credentials}")
    binary = tunnel.resolve_cloudflared(store_root)
    if binary is None:
        raise ServiceError("cloudflared is required to repair the persistent MCP tunnel")
    return register_shared_service(
        binary=binary,
        state=tunnel.TunnelState(state.tunnel_name, state.tunnel_id, state.hostname, str(credentials.resolve())),
        port=origin_port(environment),
        store_root=store_root,
        environment=environment,
        narrate=narrate,
    )


def shared_service_state() -> tuple[str, str, int | None]:
    if _is_linux():
        if not (systemd_user_dir() / TUNNEL_UNIT).exists():
            return "inactive", "disabled", None
        shown = _run(
            ["systemctl", "--user", "show", TUNNEL_UNIT, "-p", "ActiveState", "-p", "UnitFileState", "-p", "MainPID"]
        )
        props = dict(line.partition("=")[::2] for line in shown.stdout.splitlines() if "=" in line)
        raw_pid = props.get("MainPID", "0").strip()
        pid = int(raw_pid) if raw_pid.isdigit() and raw_pid != "0" else None
        return props.get("ActiveState", "unknown").strip(), props.get("UnitFileState", "unknown").strip(), pid
    if not (launchd_user_dir() / f"{TUNNEL_LABEL}.plist").exists():
        return "inactive", "disabled", None
    listed = _run(["launchctl", "list", TUNNEL_LABEL])
    return ("active" if listed.returncode == 0 else "inactive"), "enabled", None


def control_shared_service(action: str) -> None:
    """start/stop/restart only the units this HOME installed.

    ``systemctl --user`` addresses the login's real manager whatever HOME says,
    so an alternate-HOME run must never stop the login's live connector.
    """

    order = (TUNNEL_UNIT, GATEWAY_UNIT) if action == "stop" else (GATEWAY_UNIT, TUNNEL_UNIT)
    if _is_linux():
        units = [name for name in order if (systemd_user_dir() / name).exists()]
        if not units:
            raise ServiceError(f"no persistent MCP service is installed in {systemd_user_dir()}")
        for name in units:
            result = _run(["systemctl", "--user", action, name])
            if result.returncode != 0:
                raise ServiceError(f"systemctl --user {action} {name} failed: {_output(result)}")
        return
    labels = (TUNNEL_LABEL, GATEWAY_LABEL) if action == "stop" else (GATEWAY_LABEL, TUNNEL_LABEL)
    plists = [launchd_user_dir() / f"{label}.plist" for label in labels]
    plists = [plist for plist in plists if plist.exists()]
    if not plists:
        raise ServiceError(f"no persistent MCP service is installed in {launchd_user_dir()}")
    for plist in plists:
        if action in {"stop", "restart"}:
            _run(["launchctl", "unload", str(plist)])
        if action in {"start", "restart"}:
            loaded = _run(["launchctl", "load", str(plist)])
            if loaded.returncode != 0:
                raise ServiceError(f"launchctl load failed for {plist.stem}: {_output(loaded)}")


def remove_shared_service() -> bool:
    removed = False
    for name in (TUNNEL_UNIT, GATEWAY_UNIT):
        path = systemd_user_dir() / name
        if path.exists():
            _run(["systemctl", "--user", "disable", "--now", name])
            path.unlink(missing_ok=True)
            removed = True
    if removed:
        _run(["systemctl", "--user", "daemon-reload"])
    for label in (TUNNEL_LABEL, GATEWAY_LABEL):
        plist = launchd_user_dir() / f"{label}.plist"
        if plist.exists():
            _run(["launchctl", "unload", str(plist)])
            plist.unlink(missing_ok=True)
            removed = True
    return removed


def log_command(*, lines: int, follow: bool, store_root: Path) -> list[str]:
    if _is_linux():
        return [
            "journalctl",
            "--user",
            "-u",
            GATEWAY_UNIT,
            "-u",
            TUNNEL_UNIT,
            f"-n{lines}",
            *(["-f"] if follow else []),
        ]
    logs = [str(store_root / "mcp" / "gateway.log"), str(store_root / "mcp" / "tunnel.log")]
    return ["tail", *(["-f"] if follow else []), "-n", str(lines), *logs]


def management_hints(unit: str) -> list[str]:
    if unit.endswith(".service"):
        return [
            f"systemctl --user status {GATEWAY_UNIT} {TUNNEL_UNIT}",
            "lc runtime mcp service logs",
            "lc runtime mcp service stop",
        ]
    return [f"launchctl list {unit}", "lc runtime mcp service logs", "lc runtime mcp service stop"]


__all__ = [
    "DEFAULT_ORIGIN_PORT",
    "GATEWAY_LABEL",
    "GATEWAY_UNIT",
    "TUNNEL_LABEL",
    "TUNNEL_UNIT",
    "ServiceError",
    "control_shared_service",
    "gateway_command",
    "log_command",
    "management_hints",
    "origin_port",
    "register_shared_service",
    "remove_shared_service",
    "render_systemd_units",
    "repair_shared_service",
    "shared_service_state",
    "supervisor_kind",
]

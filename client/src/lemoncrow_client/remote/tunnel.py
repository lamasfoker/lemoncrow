"""cloudflared for the remote MCP gateway: quick tunnels and the shared named tunnel.

The client never downloads cloudflared: it uses one on ``PATH``, or the copy an
older LemonCrow already placed at ``<store>/chatgpt/bin/cloudflared``.

Quick tunnel: ``cloudflared tunnel --url`` prints a rotating
``https://<words>.trycloudflare.com`` URL on stderr; no account is needed.

Shared named tunnel (``serve --persistent``): one Cloudflare tunnel,
``lemoncrow-mcp``, carries every connector hostname to the loopback gateway.
Its id and credentials path persist in ``<store>/mcp/tunnel.json``; the
Cloudflare-side secret stays in the credentials file cloudflared wrote.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from .state import write_private_json

SHARED_TUNNEL_NAME: Final[str] = "lemoncrow-mcp"
INSTALL_URL: Final[str] = "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
QUICK_TUNNEL_TIMEOUT_S: Final[float] = 30.0

# The quick-tunnel host is <words>.trycloudflare.com; the control plane
# (api.trycloudflare.com) also appears in cloudflared's stderr and is not it.
_TUNNEL_URL_RE: Final[re.Pattern[str]] = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_CONTROL_PLANE_URL: Final[str] = "https://api.trycloudflare.com"
_CREATED_TUNNEL_RE: Final[re.Pattern[str]] = re.compile(r"Created tunnel \S+ with id (\S+)")
_CREDENTIALS_WRITTEN_RE: Final[re.Pattern[str]] = re.compile(r"Tunnel credentials written to (\S+)\.")


class TunnelSetupError(RuntimeError):
    """A tunnel step failed; the caller reports it and starts nothing."""


def resolve_cloudflared(store_root: Path) -> str | None:
    """An operator-managed cloudflared on PATH, else the legacy managed copy."""

    found = shutil.which("cloudflared")
    if found is not None:
        return found
    managed = store_root / "chatgpt" / "bin" / "cloudflared"
    if managed.is_file() and os.access(managed, os.X_OK):
        return str(managed)
    return None


def missing_cloudflared_help(port: int) -> str:
    return (
        "cloudflared is required for a public MCP URL and was not found.\n"
        f"  Install it:  {INSTALL_URL}\n"
        "  Or use --no-tunnel and expose the loopback port yourself:\n"
        f"     cloudflared tunnel --url http://localhost:{port}\n"
    )


# -- quick tunnel -------------------------------------------------------------- #


def extract_tunnel_url(line: str) -> str | None:
    match = _TUNNEL_URL_RE.search(line)
    if match is None or match.group(0) == _CONTROL_PLANE_URL:
        return None
    return match.group(0)


def start_quick_tunnel(
    binary: str, port: int, timeout: float = QUICK_TUNNEL_TIMEOUT_S
) -> tuple[subprocess.Popen[str], str | None]:
    """Launch a quick tunnel to ``localhost:port``; the caller owns the process.

    A drain thread reads stderr for the life of the process: first for the URL,
    then discarding, because an undrained pipe eventually blocks cloudflared.
    """

    process: subprocess.Popen[str] = subprocess.Popen(
        [binary, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    found = threading.Event()
    captured: list[str] = []

    def drain() -> None:
        stream = process.stderr
        if stream is None:
            return
        for line in stream:
            if not found.is_set() and (url := extract_tunnel_url(line)) is not None:
                captured.append(url)
                found.set()

    threading.Thread(target=drain, name="cloudflared-stderr", daemon=True).start()
    return process, (captured[0] if found.wait(timeout) else None)


# -- shared named tunnel ----------------------------------------------------------- #


@dataclass(frozen=True)
class TunnelState:
    tunnel_name: str
    tunnel_id: str
    hostname: str
    credentials_path: str


def tunnel_state_path(store_root: Path) -> Path:
    return store_root / "mcp" / "tunnel.json"


def load_tunnel_state(path: Path) -> TunnelState | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return TunnelState(
            tunnel_name=str(raw["tunnel_name"]),
            tunnel_id=str(raw["tunnel_id"]),
            hostname=str(raw["hostname"]),
            credentials_path=str(raw["credentials_path"]),
        )
    except (OSError, ValueError, TypeError, KeyError):
        return None


def save_tunnel_state(path: Path, state: TunnelState) -> None:
    write_private_json(path, asdict(state), prefix=".tunnel_state.")


def default_cert_path() -> Path:
    """What ``cloudflared tunnel login`` writes."""

    return Path.home() / ".cloudflared" / "cert.pem"


def _cloudflared(binary: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([binary, *arguments], capture_output=True, text=True, check=False)


def find_existing_tunnel(binary: str, name: str) -> tuple[str, str] | None:
    result = _cloudflared(binary, "tunnel", "list", "--name", name, "-o", "json")
    if result.returncode != 0:
        raise TunnelSetupError(f"`cloudflared tunnel list --name {name}` failed: {result.stderr.strip()}")
    try:
        tunnels = json.loads(result.stdout or "[]")
    except ValueError as exc:
        raise TunnelSetupError(f"could not parse `cloudflared tunnel list` output: {exc}") from exc
    if not tunnels:
        return None
    tunnel_id = str(tunnels[0].get("ID") or tunnels[0].get("id") or "")
    if not tunnel_id:
        raise TunnelSetupError(f"`cloudflared tunnel list` returned a tunnel with no id: {tunnels[0]!r}")
    credentials = default_cert_path().parent / f"{tunnel_id}.json"
    if not credentials.exists():
        raise TunnelSetupError(
            f"tunnel {name!r} (id={tunnel_id}) exists but {credentials} is missing; run "
            f"`cloudflared tunnel token --cred-file {credentials} {tunnel_id}`, or --reset-tunnel"
        )
    return tunnel_id, str(credentials)


def create_tunnel(binary: str, name: str) -> tuple[str, str]:
    result = _cloudflared(binary, "tunnel", "create", name)
    if result.returncode != 0:
        raise TunnelSetupError(f"`cloudflared tunnel create {name}` failed: {result.stderr.strip()}")
    combined = f"{result.stdout}\n{result.stderr}"
    created = _CREATED_TUNNEL_RE.search(combined)
    written = _CREDENTIALS_WRITTEN_RE.search(combined)
    if created is None or written is None:
        raise TunnelSetupError(f"unexpected `cloudflared tunnel create` output: {combined.strip()!r}")
    return created.group(1), written.group(1)


def route_dns(binary: str, tunnel_ref: str, hostname: str) -> None:
    """``--overwrite-dns`` re-points a hostname that CNAMEs to another tunnel."""

    result = _cloudflared(binary, "tunnel", "route", "dns", "--overwrite-dns", tunnel_ref, hostname)
    if result.returncode != 0:
        raise TunnelSetupError(
            f"`cloudflared tunnel route dns {tunnel_ref} {hostname}` failed: {result.stderr.strip()}"
        )


def provision_shared_tunnel(
    *,
    store_root: Path,
    hostname: str,
    binary: str,
    narrate: Callable[[str], None],
    reset: bool = False,
) -> TunnelState:
    """Ensure the one shared tunnel exists and route ``hostname`` to it."""

    path = tunnel_state_path(store_root)
    state = None if reset else load_tunnel_state(path)
    if state is None:
        if not default_cert_path().exists():
            narrate("Not logged in to Cloudflare; running `cloudflared tunnel login`...")
            # Interactive on purpose: the operator must see and open the login URL.
            login = subprocess.run([binary, "tunnel", "login"], check=False)
            if login.returncode != 0:
                raise TunnelSetupError(f"`cloudflared tunnel login` exited {login.returncode}")
        existing = find_existing_tunnel(binary, SHARED_TUNNEL_NAME)
        if existing is None:
            narrate(f"Creating shared tunnel {SHARED_TUNNEL_NAME!r}...")
            tunnel_id, credentials = create_tunnel(binary, SHARED_TUNNEL_NAME)
        else:
            tunnel_id, credentials = existing
            narrate(f"Reusing shared tunnel {SHARED_TUNNEL_NAME!r} (id={tunnel_id}).")
        state = TunnelState(SHARED_TUNNEL_NAME, tunnel_id, "*", credentials)
        save_tunnel_state(path, state)
    else:
        narrate(f"Using shared tunnel {state.tunnel_name!r} (id={state.tunnel_id}).")
    narrate(f"Routing DNS: {hostname} -> {state.tunnel_name}...")
    route_dns(binary, state.tunnel_id, hostname)
    return state


def named_tunnel_command(*, binary: str, tunnel_ref: str, credentials_path: str, origin_port: int) -> list[str]:
    # A 2s grace period: the default 30s turns every gateway restart into an outage.
    return [
        binary,
        "tunnel",
        "--no-autoupdate",
        "--grace-period",
        "2s",
        "run",
        "--credentials-file",
        credentials_path,
        "--url",
        f"http://127.0.0.1:{origin_port}",
        tunnel_ref,
    ]


def start_named_tunnel(binary: str, state: TunnelState, origin_port: int) -> subprocess.Popen[str]:
    command = named_tunnel_command(
        binary=binary, tunnel_ref=state.tunnel_id, credentials_path=state.credentials_path, origin_port=origin_port
    )
    return subprocess.Popen(command, text=True)


__all__ = [
    "SHARED_TUNNEL_NAME",
    "TunnelSetupError",
    "TunnelState",
    "extract_tunnel_url",
    "load_tunnel_state",
    "missing_cloudflared_help",
    "named_tunnel_command",
    "provision_shared_tunnel",
    "resolve_cloudflared",
    "save_tunnel_state",
    "start_named_tunnel",
    "start_quick_tunnel",
    "tunnel_state_path",
]

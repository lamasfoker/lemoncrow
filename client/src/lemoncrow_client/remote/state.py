"""Durable remote-MCP state: connector bindings, OAuth, pairing codes, projects.

File names and formats are unchanged from the server-hosted gateway this
replaces, so connectors that are already paired keep working:

``<store>/mcp/connectors/<slug>.json``   hostname -> workspace binding
``<store>/mcp/oauth-<slug>.json``        registered clients and token hashes
``<store>/mcp/pairing-<slug>.json``      the human pairing code
``<store>/projects/proj_*.json``         registered project roots (read only)

Every file is written 0600 via tempfile + rename.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import html
import json
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

# 30 days: chat apps hold a connector for a long time. The refresh token rotates
# on every use, so a stolen access token still expires on its own.
ACCESS_TOKEN_TTL_SECONDS: Final[int] = 2_592_000
# Authorization codes are one-shot and exchanged seconds after the redirect.
AUTH_CODE_TTL_SECONDS: Final[float] = 120.0
# The pairing code is the only human secret behind a public URL: brute force is
# gated by a lockout regardless of how fast an attacker retries. The lockout is
# per client_id (a stranger's wrong guesses must not lock the owner out) and
# doubles on each repeat, up to MAX_PAIRING_LOCKOUT_SECONDS.
MAX_PAIRING_FAILURES: Final[int] = 5
PAIRING_LOCKOUT_SECONDS: Final[float] = 60.0
MAX_PAIRING_LOCKOUT_SECONDS: Final[float] = 3600.0
# Dynamic registration is unauthenticated, so a guesser can mint client_ids
# freely and reset the per-client counter at will. A store-wide backstop counts
# every failure since the last successful pairing: past this many, every new
# pairing waits with the same doubling backoff. Already-paired connectors keep
# their tokens; only new pairings pause while the guessing continues.
MAX_STORE_PAIRING_FAILURES: Final[int] = 25
# A pairing code authorizes one connector, once, within this window of being
# minted: a leaked or phished code goes stale instead of staying valid forever.
PAIRING_CODE_TTL_SECONDS: Final[float] = 900.0
PAIRING_CODE_TTL_TEXT: Final[str] = "single use, valid for 15 minutes"
# Dynamic registration is unauthenticated by design; past this many, the oldest
# dynamic registrations are dropped (issued tokens stay valid).
MAX_DCR_CLIENTS: Final[int] = 256

AUTHORIZE_FIELDS: Final[tuple[str, ...]] = (
    "client_id",
    "redirect_uri",
    "response_type",
    "state",
    "code_challenge",
    "code_challenge_method",
    "scope",
    "resource",
)

_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")
_PROJECT_ID_PREFIX: Final[str] = "proj_"
_PROJECT_ID_HEX: Final[int] = 20


def store_root_from(environment: Mapping[str, str]) -> Path:
    """``LEMONCROW_ROOT``, else ``LEMONCROW_HOME``, else ``~/.lemoncrow``."""

    for name in ("LEMONCROW_ROOT", "LEMONCROW_HOME"):
        configured = environment.get(name, "").strip()
        if configured:
            return Path(configured).expanduser().resolve()
    home = environment.get("HOME", "").strip()
    return ((Path(home) if home else Path.home()) / ".lemoncrow").resolve()


def write_private_json(target: Path, document: Any, *, prefix: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# -- connector bindings ------------------------------------------------------ #


@dataclass(frozen=True, slots=True)
class ConnectorBinding:
    hostname: str
    workspace: str


def connector_slug(hostname: str) -> str:
    return _SLUG_RE.sub("-", hostname.strip().lower()).strip("-") or "default"


def connector_dir(store_root: Path) -> Path:
    return store_root / "mcp" / "connectors"


def connector_file(store_root: Path, hostname: str) -> Path:
    return connector_dir(store_root) / f"{connector_slug(hostname)}.json"


def save_connector(binding: ConnectorBinding, *, store_root: Path) -> Path:
    hostname = binding.hostname.strip().lower()
    workspace = Path(binding.workspace).expanduser().resolve()
    if not hostname or "." not in hostname:
        raise ValueError("connector hostname must be a fully-qualified hostname")
    if not workspace.is_dir():
        raise ValueError(f"connector workspace is not a directory: {workspace}")
    target = connector_file(store_root, hostname)
    write_private_json(target, {"hostname": hostname, "workspace": str(workspace)}, prefix=".connector.")
    return target


def load_connector(path: Path) -> ConnectorBinding | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    hostname = str(raw.get("hostname") or "").strip().lower()
    workspace = str(raw.get("workspace") or "").strip()
    if not hostname or not workspace:
        return None
    # Hosted connectors were bound to an org principal and are served by the
    # hosted server, never by a local gateway.
    if str(raw.get("org_id") or "").strip() or str(raw.get("subject") or "").strip():
        return None
    root = Path(workspace).expanduser()
    if not root.is_absolute() or not root.is_dir():
        return None
    return ConnectorBinding(hostname=hostname, workspace=str(root.resolve()))


def load_connector_for_hostname(store_root: Path, hostname: str) -> ConnectorBinding | None:
    wanted = hostname.strip().lower().split(":", 1)[0]
    if not wanted:
        return None
    binding = load_connector(connector_file(store_root, wanted))
    if binding is None or binding.hostname != wanted:
        return None
    return binding


def load_all_connectors(store_root: Path) -> list[ConnectorBinding]:
    try:
        paths = sorted(connector_dir(store_root).glob("*.json"))
    except OSError:
        return []
    rows = [binding for path in paths if (binding := load_connector(path)) is not None]
    return sorted(rows, key=lambda binding: binding.hostname)


def remove_connector(store_root: Path, hostname: str) -> bool:
    try:
        connector_file(store_root, hostname).unlink()
        return True
    except FileNotFoundError:
        return False


# -- pairing code and OAuth state files ------------------------------------- #


def state_path(store_root: Path, scope: str | None = None) -> Path:
    return store_root / "mcp" / ("oauth.json" if scope is None else f"oauth-{scope}.json")


def pairing_path(store_root: Path, scope: str | None = None) -> Path:
    return store_root / "mcp" / ("pairing.json" if scope is None else f"pairing-{scope}.json")


def _move_first_existing(candidates: list[Path], target: Path) -> None:
    if target.exists():
        return
    for legacy in candidates:
        if not legacy.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(legacy, target)
        except FileNotFoundError:
            continue
        return


def migrate_legacy_state(store_root: Path, scope: str | None = None) -> None:
    """Move pre-rename ``chatgpt/`` OAuth and pairing state, never regenerate it."""

    legacy = store_root / "chatgpt"
    state_candidates = [legacy / state_path(store_root, scope).name]
    pairing_candidates = [legacy / pairing_path(store_root, scope).name]
    if scope is not None:
        state_candidates.append(legacy / "oauth.json")
        pairing_candidates.append(legacy / "pairing.json")
    _move_first_existing(state_candidates, state_path(store_root, scope))
    _move_first_existing(pairing_candidates, pairing_path(store_root, scope))


def current_pairing_code(path: Path) -> str | None:
    """The stored pairing code while it is unused and unexpired; never mints one.

    A code stored without an expiry predates short-lived codes and is stale.
    """

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    code, expires_at = data.get("pairing_code"), data.get("expires_at")
    if not (isinstance(code, str) and code and isinstance(expires_at, (int, float))):
        return None
    return code if time.time() < float(expires_at) else None


def load_or_create_pairing_code(path: Path, *, rotate: bool = False) -> str:
    """The live pairing code, minting a fresh one when it expired, was used or ``rotate``."""

    if not rotate:
        existing = current_pairing_code(path)
        if existing is not None:
            return existing
    code = secrets.token_urlsafe(9)
    expires_at = time.time() + PAIRING_CODE_TTL_SECONDS
    write_private_json(path, {"pairing_code": code, "expires_at": expires_at}, prefix=".pairing.")
    return code


def consume_pairing_code(path: Path, code: str) -> None:
    """Retire ``code`` after it authorized a connector (a newer code is kept)."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(data, dict) and data.get("pairing_code") == code:
        reset_file(path)


def reset_file(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _sha256_hex(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def pkce_s256(code_verifier: str) -> str:
    """BASE64URL-NoPad(SHA256(verifier)), the RFC 7636 S256 challenge."""

    digest = hashlib.sha256(code_verifier.encode("ascii", "replace")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def is_allowed_redirect_uri(uri: str) -> bool:
    """Public clients must use https; only loopback http is tolerated."""

    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")


def append_query(url: str, params: Mapping[str, str]) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.extend(params.items())
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))


class OAuthStore:
    """Thread-safe OAuth state with a 0600 JSON persistence layer.

    Persisted: registered clients, access-token hashes, refresh-token hashes.
    In memory only: authorization codes and the pairing brute-force counter.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._clients: dict[str, dict[str, Any]] = {}
        self._access_tokens: dict[str, dict[str, Any]] = {}
        self._refresh_tokens: dict[str, dict[str, Any]] = {}
        self._auth_codes: dict[str, dict[str, Any]] = {}
        # client_id -> (failures since the last lockout, locked until, lockouts so far)
        self._pairing_failures: dict[str, tuple[int, float, int]] = {}
        # The same triple across every client_id (see MAX_STORE_PAIRING_FAILURES).
        self._store_pairing_failures: tuple[int, float, int] = (0, 0.0, 0)
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for attribute, key in (
            ("_clients", "clients"),
            ("_access_tokens", "access_tokens"),
            ("_refresh_tokens", "refresh_tokens"),
        ):
            value = data.get(key)
            if isinstance(value, dict):
                setattr(self, attribute, value)

    def _save(self) -> None:
        write_private_json(
            self.path,
            {
                "clients": self._clients,
                "access_tokens": self._access_tokens,
                "refresh_tokens": self._refresh_tokens,
            },
            prefix=".oauth.",
        )

    def register_client(
        self,
        *,
        redirect_uris: list[str],
        client_name: str | None,
        grant_types: list[str],
        response_types: list[str],
        user_defined: bool = False,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "client_id": secrets.token_urlsafe(24),
            "client_id_issued_at": int(time.time()),
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": grant_types,
            "response_types": response_types,
        }
        if client_name is not None:
            record["client_name"] = client_name
        if user_defined:
            record["user_defined"] = True
        with self._lock:
            dynamic = [cid for cid, known in self._clients.items() if not known.get("user_defined")]
            for stale in dynamic[: max(0, len(dynamic) - MAX_DCR_CLIENTS + 1)]:
                del self._clients[stale]
            self._clients[record["client_id"]] = record
            self._save()
        return record

    def ensure_user_client(self, redirect_uris: list[str]) -> dict[str, Any]:
        wanted = set(redirect_uris)
        with self._lock:
            for record in self._clients.values():
                if record.get("user_defined") and set(record.get("redirect_uris", [])) == wanted:
                    return record
        return self.register_client(
            redirect_uris=redirect_uris,
            client_name="Remote MCP client (user-defined)",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            user_defined=True,
        )

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._clients.get(client_id)

    def create_auth_code(self, *, client_id: str, redirect_uri: str, code_challenge: str, resource: str) -> str:
        code = secrets.token_urlsafe(32)
        with self._lock:
            self._auth_codes[code] = {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "resource": resource,
                "expires": time.monotonic() + AUTH_CODE_TTL_SECONDS,
            }
        return code

    def consume_auth_code(self, code: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._auth_codes.pop(code, None)
        if record is None or time.monotonic() > record["expires"]:
            return None
        return record

    def issue_tokens(self, client_id: str) -> tuple[str, str]:
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            for digest in [d for d, held in self._access_tokens.items() if now > float(held["expires_at"])]:
                del self._access_tokens[digest]
            self._access_tokens[_sha256_hex(access_token)] = {
                "client_id": client_id,
                "expires_at": now + ACCESS_TOKEN_TTL_SECONDS,
            }
            self._refresh_tokens[_sha256_hex(refresh_token)] = {"client_id": client_id}
            self._save()
        return access_token, refresh_token

    def access_token_client_id(self, token: str) -> str | None:
        """The durable client identity behind a valid access token, or ``None``."""

        digest = _sha256_hex(token)
        with self._lock:
            record = self._access_tokens.get(digest)
            if record is None:
                return None
            if time.time() > float(record["expires_at"]):
                del self._access_tokens[digest]
                self._save()
                return None
            client_id = record.get("client_id")
            return client_id if isinstance(client_id, str) and client_id else None

    def rotate_refresh_token(self, refresh_token: str) -> tuple[str, str] | None:
        with self._lock:
            record = self._refresh_tokens.pop(_sha256_hex(refresh_token), None)
            if record is None:
                return None
            client_id = str(record["client_id"])
        return self.issue_tokens(client_id)

    def pairing_lockout_remaining(self, client_id: str) -> float:
        with self._lock:
            now = time.monotonic()
            entry = self._pairing_failures.get(client_id)
            own = max(0.0, entry[1] - now) if entry else 0.0
            return max(own, self._store_pairing_failures[1] - now)

    def record_pairing_failure(self, client_id: str) -> None:
        with self._lock:
            self._pairing_failures[client_id] = _count_pairing_failure(
                self._pairing_failures.get(client_id, (0, 0.0, 0)), MAX_PAIRING_FAILURES
            )
            self._store_pairing_failures = _count_pairing_failure(
                self._store_pairing_failures, MAX_STORE_PAIRING_FAILURES
            )
            if len(self._pairing_failures) > len(self._clients):
                # Closer: counters for clients that are gone (DCR eviction) are dropped.
                for stale in [key for key in self._pairing_failures if key not in self._clients]:
                    del self._pairing_failures[stale]

    def reset_pairing_failures(self, client_id: str) -> None:
        """A correct pairing code: clear this client's counter and the store-wide one."""

        with self._lock:
            self._pairing_failures.pop(client_id, None)
            self._store_pairing_failures = (0, 0.0, 0)


def _count_pairing_failure(entry: tuple[int, float, int], limit: int) -> tuple[int, float, int]:
    """One more failure on a (failures, locked until, lockouts) counter; lock at ``limit``."""

    failures, until, strikes = entry
    failures += 1
    if failures >= limit:
        lockout = min(PAIRING_LOCKOUT_SECONDS * 2**strikes, MAX_PAIRING_LOCKOUT_SECONDS)
        return 0, time.monotonic() + lockout, strikes + 1
    return failures, until, strikes


# -- registered projects (read only) ---------------------------------------- #


def _project_id_for_root(root: Path) -> str:
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:_PROJECT_ID_HEX]
    return f"{_PROJECT_ID_PREFIX}{digest}"


def resolve_registered_project(store_root: Path, reference: str) -> Path | None:
    """A ``proj_`` id, or an exact registered root, from ``<store>/projects``."""

    value = reference.strip()
    directory = store_root / "projects"
    if not value or not directory.is_dir():
        return None
    for path in sorted(directory.glob(f"{_PROJECT_ID_PREFIX}*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            root = Path(str(payload["root"])).expanduser().resolve()
            project_id = str(payload["project_id"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not root.is_dir() or project_id != _project_id_for_root(root):
            continue
        if value == project_id:
            return root
        if not value.startswith(_PROJECT_ID_PREFIX):
            with contextlib.suppress(OSError):
                if Path(value).expanduser().resolve() == root:
                    return root
    return None


# -- pages ------------------------------------------------------------------ #

# Self-contained (no external assets): it renders inside a chat app's OAuth
# popup, which blocks third-party CSS/JS.
_PAGE_STYLE: Final[str] = (
    "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
    "background:#0f1115;color:#e6e6e6;display:flex;min-height:100vh;margin:0;"
    "align-items:center;justify-content:center}"
    ".card{background:#1b1e26;border:1px solid #2a2e38;border-radius:12px;padding:32px;"
    "max-width:380px;width:100%;box-shadow:0 8px 30px rgba(0,0,0,.4)}"
    "h1{font-size:18px;margin:0 0 8px}p{font-size:14px;line-height:1.5;color:#a0a6b0}"
    "input[type=text]{width:100%;box-sizing:border-box;padding:10px 12px;margin:14px 0;"
    "font-size:15px;border-radius:8px;border:1px solid #3a3f4b;background:#11141a;color:#fff}"
    "button{width:100%;padding:11px;font-size:15px;font-weight:600;border:0;border-radius:8px;"
    "background:#f2c94c;color:#1b1e26;cursor:pointer}"
    ".error{color:#ff6b6b;font-size:13px;margin-top:4px}"
)


def error_page(message: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>LemonCrow</title>"
        f"<style>{_PAGE_STYLE}</style></head><body><div class='card'>"
        f"<h1>Cannot continue</h1><p>{html.escape(message)}</p></div></body></html>"
    )


def render_form(params: Mapping[str, str], error: str | None, *, client_name: str | None = None) -> str:
    hidden = "".join(
        f"<input type='hidden' name='{html.escape(key)}' value='{html.escape(params.get(key, ''))}'>"
        for key in AUTHORIZE_FIELDS
    )
    error_html = f"<p class='error'>{html.escape(error)}</p>" if error else ""
    # The redirect host is where the grant goes; the client name is self-reported.
    redirect_host = urllib.parse.urlsplit(params.get("redirect_uri", "")).hostname or "an unknown address"
    name = (client_name or "").strip() or "An unnamed client"
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Connect to LemonCrow</title>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<style>{_PAGE_STYLE}</style></head><body><div class='card'>"
        "<h1>Connect to LemonCrow</h1>"
        f"<p><b>{html.escape(name)}</b> (as the client names itself) asks for shell-grade tool access "
        f"to this machine. Access goes to <b>{html.escape(redirect_host)}</b>.</p>"
        "<p>Only continue if you started this connection from that site yourself. "
        "Enter the pairing code shown in your terminal to authorize this connector.</p>"
        f"<form method='post' action='/authorize'>{hidden}"
        "<input type='text' name='pairing_code' autocomplete='off' autofocus "
        f"placeholder='pairing code' spellcheck='false'>{error_html}"
        "<button type='submit'>Authorize</button></form></div></body></html>"
    )


__all__ = [
    "ACCESS_TOKEN_TTL_SECONDS",
    "AUTHORIZE_FIELDS",
    "ConnectorBinding",
    "OAuthStore",
    "append_query",
    "connector_dir",
    "connector_file",
    "connector_slug",
    "consume_pairing_code",
    "current_pairing_code",
    "error_page",
    "is_allowed_redirect_uri",
    "load_all_connectors",
    "load_connector",
    "load_connector_for_hostname",
    "load_or_create_pairing_code",
    "migrate_legacy_state",
    "pairing_path",
    "pkce_s256",
    "remove_connector",
    "render_form",
    "reset_file",
    "resolve_registered_project",
    "save_connector",
    "state_path",
    "store_root_from",
]

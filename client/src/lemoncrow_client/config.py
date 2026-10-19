"""Everything the client is allowed to know about its environment.

Four decisions live here, and each one is a claim an enterprise reviewer will
check:

**One product endpoint, one discovered auth origin.** ``LEMONCROW_URL`` is the
only LemonCrow data-plane destination. Hosted interactive login may additionally
contact the Authward issuer advertised by that server; Authward discovery must
repeat the issuer exactly and every OAuth endpoint must remain on its origin.
Each :class:`~lemoncrow_client.transport.HttpTransport` is still bound to one
origin, so neither discovery document can create arbitrary egress.

**One writable directory outside the repository.** ``~/.lemoncrow`` (or
``LEMONCROW_HOME``). It holds four things and nothing else: the IT-provisioned
``token``; product-owned ``review/env`` / ``runtime/env`` files of
``LEMONCROW_*`` settings (plus the legacy shared ``env`` as read-only migration
input) -- the real environment always wins, so a session can override what is on
disk and never the other way round -- ``walk-cache/``, one
file per worktree recording which files were unchanged when they were last hashed,
and ``live-views/`` containing rebuildable, credential-free View lease metadata
that lets another local LemonCrow process reuse an already-synced source View (see :mod:`lemoncrow_client.walkcache`). The cache holds
paths and digests, never content; deleting it costs one slow session and nothing
else. ``traces/`` holds per-call runtime trace records (see
:mod:`lemoncrow_client.trace`): arguments minus bulk content (hashed in hosted
mode), bounded to 14 days / 256 MB, disabled by ``LEMONCROW_RUNTIME_TRACE=off``.
No unit directory, no ``~/.claude/settings.json`` rewrite, and no local
index -- an unreachable server degrades to a documented reduced mode rather than
quietly rebuilding one here.

**Credentials have explicit ownership.** ``LEMONCROW_TOKEN`` from the
environment, else an IT-provisioned file, always wins. Hosted interactive
login may additionally persist one managed access/refresh pair below the same
state directory; local mode never reads it. The client refuses group- or
world-readable credential files rather than quietly using them.

**Repository identity is never a git remote.** The server issues ``repo_id``;
what the client may present is a canonical SCM identity or an opaque
fingerprint over the root commit. A remote URL may carry credentials, may be
one of several, and differs between forks, so it is not identity and this
module has nowhere to put one.
"""

from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from .credentials import managed_credentials_path, read_managed_credentials
from .errors import AgentAction, ClientError, ErrorCode
from .memory_budget import cache_budget_bytes
from .workspace_paths import LOCAL_FS_PROOF_PATH, REVIEW_CONFIG_PATH

__all__ = [
    "DEFAULT_URL",
    "LOCAL_FS_PROOF_PATH",
    "REVIEW_CONFIG_PATH",
    "ClientConfig",
    "RepoIdentity",
    "load_config",
    "product_settings_path",
    "resolve_token",
]

#: Compatibility endpoint for server-backed local/hosted client paths. Runtime
#: Lite has an explicit lite install mode and does not consume this endpoint.
DEFAULT_URL: Final[str] = "http://127.0.0.1:7420"

_TOKEN_ENV: Final[str] = "LEMONCROW_TOKEN"
_TOKEN_FILE_ENV: Final[str] = "LEMONCROW_TOKEN_FILE"
_LOCAL_TOKEN_PREFIX: Final[str] = "lc_local_"  # the local server machine credential (bootstrap.py)
_URL_ENV: Final[str] = "LEMONCROW_URL"
_MODE_ENV: Final[str] = "LEMONCROW_INSTALL_MODE"
_HOME_ENV: Final[str] = "LEMONCROW_HOME"
_MIN_TOKEN_LEN: Final[int] = 16
_MAX_TOKEN_BYTES: Final[int] = 4096
_MAX_SETTINGS_BYTES: Final[int] = 8192
_MAX_SETTINGS_LINES: Final[int] = 64


@dataclass(frozen=True, slots=True)
class RepoIdentity:
    """What the client may claim about which repository a view belongs to.

    There is deliberately no field for a remote URL. The server refuses a
    request carrying one, so a client that had somewhere to put it would only
    be able to produce refusals.
    """

    scm_provider: str
    scm_repo_id: str
    fingerprint: str

    def to_wire(self) -> dict[str, str]:
        payload: dict[str, str] = {}
        if self.scm_provider and self.scm_repo_id:
            payload["scm_provider"] = self.scm_provider
            payload["scm_repo_id"] = self.scm_repo_id
        if self.fingerprint:
            payload["fingerprint"] = self.fingerprint
        return payload


@dataclass(frozen=True, slots=True)
class ClientConfig:
    """Resolved configuration for one short-lived client process."""

    url: str
    token: str
    repo_root: Path
    state_dir: Path
    #: Explicit runtime topology. ``hosted`` is a security boundary: code using
    #: this config must not start listeners or background LemonCrow processes.
    install_mode: str
    #: Whole-of-MCP-initialize bootstrap budget. Initialization is fail-open:
    #: past this the same stdio process continues with reduced local tools.
    startup_budget_s: float
    #: Per-request ceiling for the sync and tool routes.
    request_timeout_s: float
    #: Longer ceiling for a tool call, which may legitimately take a while.
    tool_timeout_s: float
    #: Memory-only, server-validated response cache. This is an eviction budget,
    #: not a reservation; zero disables it. Automatic sizing is 10% of available
    #: memory, capped by the cache implementation. Forced to 0 by
    #: ``LEMONCROW_CACHE_DISABLED`` -- the same global kill switch the private
    #: server's own tool-result caches read (``lemoncrow.core.environment.cache_disabled``)
    #: -- even when ``LEMONCROW_RESOLUTION_CACHE_BYTES`` asks for a nonzero budget.
    resolution_cache_bytes: int
    #: Offer the same-host read optimization. Off unless explicitly enabled:
    #: it writes a proof file into the worktree, and a client should not do
    #: that because it guessed it was on the same host as the server.
    offer_local_fs: bool
    #: Where the token came from, for the client audit output. Never the token
    #: itself.
    token_source: str
    #: Managed hosted refresh credential. Operator-provided access credentials
    #: intentionally suppress this field so they can never be mixed with a
    #: stale interactive-login family.
    refresh_token: str = ""
    refresh_token_source: str = ""
    #: How long a pure/idempotent server call waits for a vanished server to
    #: come back before failing. Zero disables transparent reconnect.
    reconnect_budget_s: float = 8.0
    #: Product whose persisted endpoint/settings this process resolved.
    product: str = "core"

    @property
    def account_state_dir(self) -> Path:
        """Target Core/account state root under the shared LemonCrow home.

        Existing credentials/env remain readable at their legacy locations while
        the state migration is phased in. New product-specific writers should not
        add more files directly under ``state_dir``.
        """
        return self.state_dir / "account"

    @property
    def review_state_dir(self) -> Path:
        """Review-owned local state and rebuildable capture scratch."""
        return self.state_dir / "review"

    @property
    def runtime_state_dir(self) -> Path:
        """Target Runtime-owned state root for caches, traces and live views."""
        return self.state_dir / "runtime"

    @property
    def endpoint_origin(self) -> tuple[str, str, int]:
        """``(scheme, host, port)`` -- the only origin the client may reach."""
        parts = urlsplit(self.url)
        scheme = parts.scheme or "http"
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if scheme == "https" else 80)
        return scheme, host, int(port)

    def endpoint(self, path: str) -> str:
        return f"{self.url.rstrip('/')}{path}"

    @property
    def authenticated(self) -> bool:
        return bool(self.token)

    @property
    def hosted(self) -> bool:
        return self.install_mode == "hosted"


def read_settings_file(state_dir: Path) -> dict[str, str]:
    """``LEMONCROW_*`` settings persisted by ``lemoncrow-client target``.

    A flat ``KEY=value`` file, bounded, and filtered to this product's own
    prefix: a settings file that could set ``PATH`` or ``LD_PRELOAD`` would be
    a privilege-escalation primitive dressed as a convenience.
    """
    target = state_dir / "env"
    try:
        if target.stat().st_size > _MAX_SETTINGS_BYTES:
            return {}
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    settings: dict[str, str] = {}
    for line in text.splitlines()[:_MAX_SETTINGS_LINES]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key.startswith("LEMONCROW_") and key != _HOME_ENV:
            # ``LEMONCROW_HOME`` is how this file was found; letting it point
            # somewhere else from inside itself is a loop, not a setting.
            settings[key] = value.strip().strip("'\"")
    return settings


def product_settings_path(state_dir: Path, product: str) -> Path:
    """Persisted settings file owned by one product.

    Review and Runtime own independent files so one product can be hosted while
    the other is local. The legacy shared ``env`` is deliberately read-only.
    """
    if product not in {"review", "runtime"}:
        raise ValueError(f"product-owned settings require review or runtime, got: {product}")
    return state_dir / product / "env"


def _read_product_settings(state_dir: Path, product: str) -> dict[str, str]:
    if product == "core":
        return read_settings_file(state_dir)
    return read_settings_file(state_dir / product)


def _validated_url(raw: str) -> str:
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ClientError(
            ErrorCode.NOT_CONFIGURED,
            f"{_URL_ENV} must be an http(s) URL with a host",
            details={"url": raw},
            action=AgentAction.FIX_REQUEST,
        )
    if parts.username or parts.password:
        # A credential in the endpoint would end up in every log line that
        # records where the client connected.
        raise ClientError(
            ErrorCode.NOT_CONFIGURED,
            f"{_URL_ENV} must not embed credentials",
            action=AgentAction.FIX_REQUEST,
        )
    host = parts.hostname.strip("[]").lower()
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if parts.scheme == "http" and not loopback:
        raise ClientError(
            ErrorCode.NOT_CONFIGURED,
            f"{_URL_ENV} must use HTTPS outside literal loopback development",
            details={"host": host},
            action=AgentAction.FIX_REQUEST,
        )
    return raw.rstrip("/")


def _read_token_file(path: Path) -> str:
    try:
        info = path.stat()
    except OSError as exc:
        raise ClientError(
            ErrorCode.NOT_CONFIGURED,
            f"token file is not readable: {path}",
            action=AgentAction.REAUTHENTICATE,
        ) from exc
    if info.st_size > _MAX_TOKEN_BYTES:
        raise ClientError(
            ErrorCode.NOT_CONFIGURED,
            f"token file is larger than {_MAX_TOKEN_BYTES} bytes: {path}",
            action=AgentAction.REAUTHENTICATE,
        )
    if info.st_mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        # The same rule the server applies to its own registry. A silently
        # world-readable credential store is the finding a security review
        # opens with, and using it anyway would make the client complicit.
        raise ClientError(
            ErrorCode.NOT_CONFIGURED,
            f"token file must not be group- or world-accessible (chmod 600): {path}",
            details={"path": str(path)},
            action=AgentAction.REAUTHENTICATE,
        )
    try:
        return path.read_text(encoding="utf-8", errors="strict").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ClientError(
            ErrorCode.NOT_CONFIGURED,
            f"token file is not valid UTF-8: {path}",
            action=AgentAction.REAUTHENTICATE,
        ) from exc


def resolve_token(env: dict[str, str], state_dir: Path) -> tuple[str, str]:
    """Resolve operator-owned access credentials only.

    Environment wins over the explicit/default provisioned token file. Managed
    hosted-login credentials are resolved separately after runtime mode is
    known, so they can never override an operator credential or leak into local
    mode.
    """
    from_env = env.get(_TOKEN_ENV, "").strip()
    if from_env:
        if len(from_env) < _MIN_TOKEN_LEN:
            raise ClientError(
                ErrorCode.NOT_CONFIGURED,
                f"{_TOKEN_ENV} is shorter than {_MIN_TOKEN_LEN} characters",
                action=AgentAction.REAUTHENTICATE,
            )
        return from_env, _TOKEN_ENV

    explicit = env.get(_TOKEN_FILE_ENV, "").strip()
    candidates = [Path(explicit)] if explicit else [state_dir / "token"]
    for candidate in candidates:
        if candidate.is_file():
            token = _read_token_file(candidate)
            if token:
                return token, str(candidate)
    return "", ""


def _run_git(repo_root: Path, *args: str, timeout: float = 5.0) -> str:
    """Run one bounded read-only git command. Absent git is not an error."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def discover_repo_root(start: Path) -> Path:
    """The worktree root, by ``.git`` marker, falling back to ``start``."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def is_index_candidate(repo_root: Path) -> bool:
    """Only a git repository or worktree is indexed.

    :func:`discover_repo_root` falls back to the start directory when no
    ``.git`` exists above it. Indexing that directory -- a home or a folder of
    projects -- would copy every repository beneath it into one oversized view.
    """
    return (repo_root / ".git").exists() and _run_git(repo_root, "rev-parse", "--is-inside-work-tree") == "true"


def not_indexed_error(repo_root: Path, tool: str) -> ClientError:
    """The terminal answer for an index tool outside a git repository."""
    return ClientError(
        ErrorCode.NOT_INDEXED,
        (
            f"{tool} is unavailable: {repo_root} is not a git repository, so LemonCrow does not index it. "
            "Retrying or re-indexing will not change this. Use grep, read and bash here, "
            "or start the session inside a git repository."
        ),
        details={"tool": tool, "root": str(repo_root)},
        action=AgentAction.USE_CLIENT_TOOL,
    )


_UNBORN_REPO_IDENTITY_FILE = "lemoncrow-unborn-repo-id"


def _persistent_unborn_repo_fingerprint(repo_root: Path) -> str:
    """Return a checkout-stable identity only for repos first seen before commit 1.

    Existing committed repositories continue to use their root commit.  The
    marker lives inside Git metadata (never the worktree), and is created only
    while HEAD is unborn so ordinary repositories do not migrate identities.
    """

    import hashlib

    git_dir_raw = _run_git(repo_root, "rev-parse", "--git-dir")
    if not git_dir_raw:
        return ""
    git_dir = Path(git_dir_raw)
    if not git_dir.is_absolute():
        git_dir = repo_root / git_dir
    marker = git_dir.resolve(strict=False) / _UNBORN_REPO_IDENTITY_FILE
    try:
        existing = marker.read_text(encoding="utf-8").strip().lower()
    except OSError:
        existing = ""
    if len(existing) == 64 and all(ch in "0123456789abcdef" for ch in existing):
        return existing

    # Never introduce a marker for an already committed repository -- including
    # one whose HEAD is only an unborn orphan branch (``--all`` includes HEAD).
    if _run_git(repo_root, "rev-list", "-n", "1", "--all"):
        return ""
    fingerprint = hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(fingerprint + "\n", encoding="utf-8")
    except OSError:
        # Preserve the old path-hash fallback if Git metadata is read-only.
        return fingerprint
    return fingerprint


def repo_identity(repo_root: Path, env: dict[str, str] | None = None) -> RepoIdentity:
    """A claim the server can canonicalize into an org-scoped ``repo_id``.

    Canonical SCM identity when the deployment provisioned one, otherwise the
    root-commit digest: stable across clones, mirrors and forks, and it names
    no host and carries no credential. A repository first seen before commit 1
    keeps a Git-metadata-local path fingerprint across that first commit so an
    already-created local Review does not move repositories underneath the
    reviewer; that rare fallback is checkout-stable rather than clone-stable.
    A directory with no usable Git identity gets the same opaque path digest.
    """
    environment = env if env is not None else dict(os.environ)
    provider = environment.get("LEMONCROW_SCM_PROVIDER", "").strip()
    scm_repo = environment.get("LEMONCROW_SCM_REPO_ID", "").strip()
    has_canonical_scm = bool(provider and scm_repo)
    persistent_unborn = "" if has_canonical_scm else _persistent_unborn_repo_fingerprint(repo_root)
    roots = _run_git(repo_root, "rev-list", "--max-parents=0", "HEAD")
    fingerprint = persistent_unborn
    if not fingerprint and roots:
        # A repository with several root commits (a merged history) still has
        # one canonical first line in ``rev-list`` order.
        fingerprint = roots.splitlines()[0].strip()
    if not fingerprint:
        import hashlib

        fingerprint = hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()
    return RepoIdentity(scm_provider=provider, scm_repo_id=scm_repo, fingerprint=fingerprint)


def base_revision(repo_root: Path) -> str:
    """The commit the view is based on, or a stable placeholder."""
    head = _run_git(repo_root, "rev-parse", "HEAD")
    return head or "0" * 40


def load_config(
    env: dict[str, str] | None = None,
    *,
    cwd: Path | None = None,
    product: str = "core",
) -> ClientConfig:
    """Resolve configuration. Raises only for a *misconfiguration*.

    An absent token is not a misconfiguration here: the hook must still be able
    to report one line and let the session continue with local tools, so an
    unauthenticated config is a valid object whose :attr:`authenticated` is
    ``False``.
    """
    supplied = env if env is not None else dict(os.environ)
    home = supplied.get(_HOME_ENV, "").strip()
    state_dir = Path(home) if home else Path(supplied.get("HOME", str(Path.home()))) / ".lemoncrow"
    if product not in {"core", "review", "runtime"}:
        raise ValueError(f"unknown LemonCrow product: {product}")
    # Migration precedence: legacy shared settings < product-owned settings <
    # process environment. Existing installs keep working, while Review and
    # Runtime can now point at independent deployment modes/endpoints.
    environment = {
        **read_settings_file(state_dir),
        **_read_product_settings(state_dir, product),
        **supplied,
    }
    url = _validated_url(environment.get(_URL_ENV, "").strip() or DEFAULT_URL)
    install_mode = _runtime_mode(environment, url, product)
    repo_root = discover_repo_root(cwd if cwd is not None else Path.cwd())
    token, source = resolve_token(environment, state_dir)
    if install_mode == "hosted" and token:
        # The default token file is the local server's machine credential. It
        # must never reach a hosted origin, nor shadow the managed login: only
        # an explicit LEMONCROW_TOKEN / LEMONCROW_TOKEN_FILE overrides it, and a
        # local machine token is never sent to a hosted origin at all.
        explicit = source in {_TOKEN_ENV, environment.get(_TOKEN_FILE_ENV, "").strip()}
        if not explicit or token.startswith(_LOCAL_TOKEN_PREFIX):
            token, source = "", ""
    refresh_token = ""
    refresh_token_source = ""
    if install_mode == "hosted" and not token:
        account_state_dir = state_dir / "account"
        managed_access, managed_refresh = read_managed_credentials(account_state_dir)
        managed_path = managed_credentials_path(account_state_dir)
        if not managed_access and not managed_refresh:
            # Compatibility: pre-isolation clients stored the managed Authward
            # pair directly below LEMONCROW_HOME/auth. Read it without eagerly
            # rewriting state; the next login/refresh migrates into account/.
            managed_access, managed_refresh = read_managed_credentials(state_dir)
            managed_path = managed_credentials_path(state_dir)
        if managed_access:
            token = managed_access
            source = str(managed_path)
        if managed_refresh:
            refresh_token = managed_refresh
            refresh_token_source = str(managed_path)
    return ClientConfig(
        url=url,
        token=token,
        repo_root=repo_root,
        state_dir=state_dir,
        install_mode=install_mode,
        startup_budget_s=_float_env(environment, "LEMONCROW_STARTUP_BUDGET_S", 1.0),
        request_timeout_s=_float_env(environment, "LEMONCROW_REQUEST_TIMEOUT_S", 10.0),
        tool_timeout_s=_float_env(environment, "LEMONCROW_TOOL_TIMEOUT_S", 120.0),
        resolution_cache_bytes=(
            0
            if _bool_env(environment, "LEMONCROW_CACHE_DISABLED", False)
            else cache_budget_bytes(environment.get("LEMONCROW_RESOLUTION_CACHE_BYTES"))
        ),
        offer_local_fs=_bool_env(environment, "LEMONCROW_LOCAL_FS", False),
        token_source=source,
        refresh_token=refresh_token,
        refresh_token_source=refresh_token_source,
        reconnect_budget_s=_float_env(environment, "LEMONCROW_RECONNECT_BUDGET_S", 8.0),
        product=product,
    )


def _runtime_mode(environment: dict[str, str], url: str, product: str = "core") -> str:
    """Resolve Lite/local/hosted, with a compatibility fallback for older installs.

    Runtime has no ``local`` mode: the local server does not execute Runtime
    tools, so a pre-Lite local Runtime install resolves to Runtime Lite.
    """
    explicit = environment.get(_MODE_ENV, "").strip().lower()
    if explicit:
        if explicit not in {"lite", "local", "hosted"}:
            raise ClientError(
                ErrorCode.NOT_CONFIGURED,
                f"{_MODE_ENV} must be 'lite', 'local', or 'hosted', got {explicit!r}",
                action=AgentAction.FIX_REQUEST,
            )
        mode = explicit
    else:
        host = (urlsplit(url).hostname or "").strip("[]").lower()
        mode = "local" if host in {"127.0.0.1", "localhost", "::1"} else "hosted"
    return "lite" if product == "runtime" and mode == "local" else mode


def _float_env(env: dict[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _bool_env(env: dict[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}

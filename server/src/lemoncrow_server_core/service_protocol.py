"""The contract between the lc core process and the services it fronts.

The core (coding-agent tools) is the one public entry point. Review and
Dashboard run as separate services -- worker processes today, possibly
separate deployments later -- reached by URL and authenticated by a per-boot
service token in both directions:

* core -> service: every forwarded request carries the token and the
  principal the core already authenticated. A service trusts nothing else.
* service -> core: the ``/internal/v1`` back-channel (code intelligence,
  content) accepts the same token and is invisible without it.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Iterable, Mapping
from typing import Any, Final

from aiohttp import web

from .context import Principal

__all__ = [
    "CORE_URL_ENV",
    "HEADER_PRINCIPAL",
    "HEADER_SERVICE_TOKEN",
    "INTERNAL_PREFIX",
    "PRINCIPAL_KEY",
    "SERVICE_DASHBOARD",
    "SERVICE_NAMES",
    "SERVICE_REVIEW",
    "SERVICE_TOKEN_ENV",
    "decode_principal",
    "encode_principal",
    "jsonable",
    "token_matches",
]

SERVICE_REVIEW: Final[str] = "review"
SERVICE_DASHBOARD: Final[str] = "dashboard"
SERVICE_NAMES: Final[tuple[str, ...]] = (SERVICE_REVIEW, SERVICE_DASHBOARD)

HEADER_SERVICE_TOKEN: Final[str] = "X-LemonCrow-Service-Token"
HEADER_PRINCIPAL: Final[str] = "X-LemonCrow-Principal"
INTERNAL_PREFIX: Final[str] = "/internal/v1/"

#: Environment a service worker reads; never argv, which every local user can list.
SERVICE_TOKEN_ENV: Final[str] = "LEMONCROW_SERVICE_TOKEN"
CORE_URL_ENV: Final[str] = "LEMONCROW_CORE_URL"

PRINCIPAL_KEY: Final[web.RequestKey[Principal]] = web.RequestKey("lemoncrow_local_principal", Principal)


def token_matches(presented: str, expected: Iterable[str]) -> bool:
    """Constant-time membership: a short-circuit would time which token is live."""
    candidate = presented.encode("utf-8")
    matched = False
    for token in expected:
        if token and hmac.compare_digest(candidate, token.encode("utf-8")):
            matched = True
    return bool(presented) and matched


def encode_principal(principal: Principal) -> str:
    return json.dumps(
        {
            "subject": principal.subject,
            "org_id": principal.org_id,
            "token_id": principal.token_id,
            "auth_method": principal.auth_method,
            "expires_at": principal.expires_at,
        },
        separators=(",", ":"),
    )


def decode_principal(raw: str) -> Principal | None:
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    fields = {key: payload.get(key) for key in ("subject", "org_id", "token_id", "auth_method")}
    if not all(isinstance(value, str) and value for value in fields.values()):
        return None
    expires_at = payload.get("expires_at")
    if expires_at is not None and (isinstance(expires_at, bool) or not isinstance(expires_at, (int, float))):
        return None
    return Principal(
        subject=str(fields["subject"]),
        org_id=str(fields["org_id"]),
        token_id=str(fields["token_id"]),
        auth_method=str(fields["auth_method"]),
        expires_at=None if expires_at is None else float(expires_at),
    )


def jsonable(value: Any) -> Any:
    """Plain JSON types for a value crossing the service boundary."""
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    return value

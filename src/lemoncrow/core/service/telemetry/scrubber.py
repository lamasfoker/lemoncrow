"""PII scrubber for product telemetry string values (lives in :mod:`lemoncrow.core.scrubbing`)."""

from __future__ import annotations

from lemoncrow.core.scrubbing import (
    EMAIL_RE,
    IPV4_RE,
    IPV6_RE,
    REPO_URL_RE,
    SECRET_RE,
    UNIX_PATH_RE,
    WINDOWS_PATH_RE,
    scrub_props,
    scrub_string,
    scrub_value,
)

__all__ = [
    "EMAIL_RE",
    "IPV4_RE",
    "IPV6_RE",
    "REPO_URL_RE",
    "SECRET_RE",
    "UNIX_PATH_RE",
    "WINDOWS_PATH_RE",
    "scrub_props",
    "scrub_string",
    "scrub_value",
]

"""Analytics scope: which Reviews one analytics read covers, and its bounds (Phase 13 item 5).

Filters are *selection*, never authority. Hosts resolve access first (hosted:
``review:read`` at each repository, inside the caller's organization) and only
then apply a scope, so a filter naming an unreadable repository, another
organization or a team the caller cannot see selects nothing and reveals
nothing.

Every list and string is bounded and an out-of-range value is an error that
names its parameter, never a silent clamp. The period is required to be
finite: an analytics read always covers a bounded window of Review creation
times (default the last :data:`DEFAULT_PERIOD_DAYS` days).
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, Final

from lemoncrow.pro.capabilities.review.freshness import parse_timestamp

DEFAULT_PERIOD_DAYS: Final[int] = 30
MAX_PERIOD_DAYS: Final[int] = 366
MAX_SCOPE_REPOS: Final[int] = 50
MAX_SCOPE_TEAMS: Final[int] = 20
MAX_SCOPE_PATHS: Final[int] = 20
MAX_SCOPE_PATH_CHARS: Final[int] = 512
MAX_SCOPE_ID_CHARS: Final[int] = 200
MAX_DRILL_KEY_CHARS: Final[int] = 1024
#: Reviews analyzed per read (newest first by creation). Past it the report says
#: ``truncated`` and how many candidates existed; nothing is dropped silently.
# lc-debt: on-demand aggregation reads at most 300 Reviews per request (several
# bounded store reads each); upgrade path: a per-Review analytics row refreshed on
# Review writes, aggregated with SQL.
MAX_REVIEWS: Final[int] = 300
_GLOB: Final[re.Pattern[str]] = re.compile(r"[*?\[]")
_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.:@/ -]{1,200}$")


class AnalyticsQueryError(ValueError):
    """A malformed analytics parameter; ``field`` names it."""

    def __init__(self, field: str, message: str, *, limit: int | None = None) -> None:
        super().__init__(message)
        self.field = field
        self.limit = limit


@dataclass(frozen=True, slots=True)
class AnalyticsScope:
    """One analytics read: organization, teams, repositories, paths and a period."""

    since: datetime
    until: datetime
    org: str = ""
    repos: tuple[str, ...] = ()
    teams: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()

    def in_period(self, created_at: str) -> bool:
        instant = parse_timestamp(created_at)
        return instant is not None and self.since <= instant < self.until

    def path_selected(self, path: str) -> bool:
        """Whether one repository path is inside the path filter (no filter selects every path)."""

        return not self.paths or any(_path_match(path, pattern) for pattern in self.paths)

    def to_wire(self) -> dict[str, Any]:
        return {
            "org": self.org,
            "repos": list(self.repos),
            "teams": list(self.teams),
            "paths": list(self.paths),
            "since": _exact(self.since),
            "until": _exact(self.until),
        }

    def query_items(self) -> list[tuple[str, str]]:
        """The scope as query parameters (what a drill-down link repeats)."""

        items: list[tuple[str, str]] = [
            ("since", _exact(self.since)),
            ("until", _exact(self.until)),
        ]
        if self.org:
            items.append(("org", self.org))
        items.extend(("repo", repo) for repo in self.repos)
        items.extend(("team", team) for team in self.teams)
        items.extend(("path", path) for path in self.paths)
        return items


def _exact(instant: datetime) -> str:
    """Microsecond UTC form: a drill-down repeating the report's window selects exactly its Reviews."""

    return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _path_match(path: str, pattern: str) -> bool:
    normalized = str(PurePosixPath(str(path or "").replace("\\", "/")))
    if _GLOB.search(pattern):
        return fnmatch.fnmatchcase(normalized, pattern)
    return normalized == pattern or normalized.startswith(pattern + "/")


def _safe_pattern(raw: str) -> str:
    text = raw.strip().replace("\\", "/")
    if len(text) > MAX_SCOPE_PATH_CHARS:
        raise AnalyticsQueryError("path", f"a path filter is at most {MAX_SCOPE_PATH_CHARS} characters")
    pure = PurePosixPath(text)
    if not text or pure.is_absolute() or ".." in pure.parts or "\0" in text:
        raise AnalyticsQueryError("path", "a path filter must be a safe repository-relative path or glob")
    return str(pure)


def _ids(values: Sequence[str], *, field: str, limit: int) -> tuple[str, ...]:
    items = list(dict.fromkeys(item.strip() for item in values if item.strip()))
    if len(items) > limit:
        raise AnalyticsQueryError(field, f"at most {limit} {field} values", limit=limit)
    for item in items:
        if not _ID.match(item):
            raise AnalyticsQueryError(
                field, f"{field} values must be 1..{MAX_SCOPE_ID_CHARS} plain identifier characters"
            )
    return tuple(items)


def _instant(raw: str, *, field: str) -> datetime | None:
    text = raw.strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text += "T00:00:00Z"
    parsed = parse_timestamp(text)
    if parsed is None:
        raise AnalyticsQueryError(field, f"{field} must be an ISO-8601 date or UTC instant")
    return parsed


def parse_scope(
    values: Callable[[str], Sequence[str]],
    *,
    now: datetime,
    teams_supported: bool,
) -> AnalyticsScope:
    """The request's scope from (repeated) query parameters; an error names the parameter.

    Parameters: ``since``/``until`` (ISO date or instant; or ``days``), ``org``,
    repeated ``repo``, ``team`` and ``path``.
    """

    def first(name: str) -> str:
        items = values(name)
        return items[0] if items else ""

    until = _instant(first("until"), field="until") or now
    since = _instant(first("since"), field="since")
    days_raw = first("days").strip()
    if since is None:
        days = DEFAULT_PERIOD_DAYS
        if days_raw:
            try:
                days = int(days_raw)
            except ValueError as exc:
                raise AnalyticsQueryError("days", "days must be an integer") from exc
            if days < 1 or days > MAX_PERIOD_DAYS:
                raise AnalyticsQueryError(
                    "days", f"days must be between 1 and {MAX_PERIOD_DAYS}", limit=MAX_PERIOD_DAYS
                )
        since = until - timedelta(days=days)
    elif days_raw:
        raise AnalyticsQueryError("days", "give either since or days, not both")
    if since >= until:
        raise AnalyticsQueryError("since", "since must be before until")
    if until - since > timedelta(days=MAX_PERIOD_DAYS):
        raise AnalyticsQueryError("since", f"the period is at most {MAX_PERIOD_DAYS} days", limit=MAX_PERIOD_DAYS)
    org = first("org").strip()
    if org and not _ID.match(org):
        raise AnalyticsQueryError("org", "org must be a plain organization identifier")
    teams = _ids(values("team"), field="team", limit=MAX_SCOPE_TEAMS)
    if teams and not teams_supported:
        raise AnalyticsQueryError("team", "teams are not available here: the local server has one user and no teams")
    raw_paths = [item for item in values("path") if item.strip()]
    if len(raw_paths) > MAX_SCOPE_PATHS:
        raise AnalyticsQueryError("path", f"at most {MAX_SCOPE_PATHS} path filters", limit=MAX_SCOPE_PATHS)
    return AnalyticsScope(
        since=since.astimezone(UTC),
        until=until.astimezone(UTC),
        org=org,
        repos=_ids(values("repo"), field="repo", limit=MAX_SCOPE_REPOS),
        teams=teams,
        paths=tuple(dict.fromkeys(_safe_pattern(item) for item in raw_paths)),
    )


def parse_drill_key(raw: str) -> str:
    key = raw.strip()
    if not key:
        raise AnalyticsQueryError("key", "key names the aggregate to drill into")
    if len(key) > MAX_DRILL_KEY_CHARS:
        raise AnalyticsQueryError("key", f"key is at most {MAX_DRILL_KEY_CHARS} characters", limit=MAX_DRILL_KEY_CHARS)
    return key


__all__ = [
    "DEFAULT_PERIOD_DAYS",
    "MAX_PERIOD_DAYS",
    "MAX_REVIEWS",
    "AnalyticsQueryError",
    "AnalyticsScope",
    "parse_drill_key",
    "parse_scope",
]

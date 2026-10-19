"""Deployment linkage for merged Reviews (Phase 15, P15-DEPLOY item 1) -- the pure model.

LemonCrow consumes deployment facts; it never deploys. A connector turns
provider/CD facts into :class:`DeploymentObservation` rows; this module decides
which of them a merged Review's commit is part of and what that means per
environment (``Deployed to staging · production canary 10%``).

Commit matching (the exact rule)
--------------------------------
* The **merged commit** is the provider's merge result -- GitHub's
  ``merge_commit_sha`` of the merged pull request: the merge commit (merge),
  the squash commit (squash) or the last rebased commit (rebase). Never the
  pull-request head: a squashed or rebased head is not an ancestor of what
  landed.
* ``exact`` -- the deployed commit *is* the merged commit.
* ``descendant`` -- the provider (GitHub compare ``merged...deployed`` is
  ``ahead`` or ``identical``) or local Git proved the merged commit is an
  ancestor of the deployed commit, so the deployment contains the change.
* ``not_contained`` -- proven otherwise: compare ``behind``/``diverged``, or
  the deployment was created before the merge existed.
* ``unknown`` -- not proven either way (the provider could not answer, or
  cannot compare at all). Conservative: an ``unknown`` deployment is never
  shown as deployed and never raises attention. A lookup the per-event budget
  did not allow is *deferred* (left out until a later event), never recorded
  as unknown.

Only deployments created within the Review's link horizon after its merge
count. Each deployment has its own observation window (``created_at`` plus
the configured hours); after it closes the deployment is summarized and its
raw signal expires (see ``deployment_outcomes``).

Environment outcome
-------------------
A Review's environment appears only once a deployment provably contains its
change. Over its deployments in creation order, with ``C`` the latest
containing one:

* a later deployment proven **not** to contain it that succeeded after a
  containing deployment succeeded -> ``rolled_back``;
* else a later deployment of unproven ancestry that succeeded -> ``unknown``
  (shown as "not proven");
* else by ``C``: success -> ``deployed`` (an in-progress redeploy keeps an
  earlier successful containing deployment ``deployed``), queued/in progress
  -> ``deploying``, failure/error -> ``failed``, inactive -> ``superseded``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, Protocol

from lemoncrow.pro.capabilities.review.freshness import parse_timestamp

DeploymentState = Literal["queued", "in_progress", "success", "failure", "error", "inactive"]
DEPLOYMENT_STATES: Final[tuple[str, ...]] = ("queued", "in_progress", "success", "failure", "error", "inactive")
_STATE_ALIASES: Final[Mapping[str, str]] = {
    "pending": "queued",
    "waiting": "queued",
    "queued": "queued",
    "in_progress": "in_progress",
    "success": "success",
    "failure": "failure",
    "error": "error",
    "inactive": "inactive",
}

Relation = Literal["exact", "descendant", "not_contained", "unknown"]
RELATIONS: Final[tuple[str, ...]] = ("exact", "descendant", "not_contained", "unknown")
CONTAINING: Final[frozenset[str]] = frozenset({"exact", "descendant"})

AncestryState = Literal["contains", "not_contained"]
"""A proven, immutable answer to "is the merged commit an ancestor of the deployed one"."""

EnvironmentState = Literal["deployed", "deploying", "failed", "rolled_back", "superseded", "unknown"]
ENVIRONMENT_STATES: Final[tuple[str, ...]] = (
    "deployed",
    "deploying",
    "failed",
    "rolled_back",
    "superseded",
    "unknown",
)
ATTENTION_STATES: Final[frozenset[str]] = frozenset({"failed", "rolled_back"})
"""Production states that raise ReviewAttention for the author and reviewers (item 7)."""

ConnectorKind = Literal["github_deployments", "gitlab_deployments", "argocd", "flux"]
CONNECTOR_KINDS: Final[tuple[str, ...]] = ("github_deployments", "gitlab_deployments", "argocd", "flux")
IMPLEMENTED_CONNECTORS: Final[frozenset[str]] = frozenset({"github_deployments"})
CONNECTOR_PROVIDER: Final[Mapping[str, str]] = {
    "github_deployments": "github",
    "gitlab_deployments": "gitlab",
    "argocd": "argocd",
    "flux": "flux",
}
ConnectorLocus = Literal["provider_webhook", "customer_worker", "local"]
_CONNECTOR_LOCUS: Final[Mapping[str, ConnectorLocus]] = {
    # Arrives on the existing provider app/webhook path; no new network reach.
    "github_deployments": "provider_webhook",
    "gitlab_deployments": "provider_webhook",
    # Need network access to customer CD systems: customer workers by default.
    "argocd": "customer_worker",
    "flux": "customer_worker",
}

DEFAULT_OBSERVATION_WINDOW_HOURS: Final[int] = 72
MAX_OBSERVATION_WINDOW_HOURS: Final[int] = 90 * 24
DEFAULT_LINK_HORIZON_DAYS: Final[int] = 14
MAX_LINK_HORIZON_DAYS: Final[int] = 90
DEFAULT_PRODUCTION_ENVIRONMENTS: Final[tuple[str, ...]] = ("production",)
MAX_PRODUCTION_ENVIRONMENTS: Final[int] = 20

# lc-debt: fixed per-environment entry ceiling (oldest non-containing dropped first, counted in
# ``entries_dropped``); upgrade path: page entries into their own rows if one environment
# deploys more than this inside one link horizon.
MAX_ENTRIES_PER_ENVIRONMENT: Final[int] = 50
# lc-debt: fixed per-Review environment ceiling (counted in the projection's
# ``environments_dropped``); upgrade path: paginate the Status section.
MAX_ENVIRONMENTS_PER_REVIEW: Final[int] = 20
MAX_TRANSITIONS_PER_ENTRY: Final[int] = 20
_MAX_ENVIRONMENT_NAME: Final[int] = 100
_SHA = re.compile(r"^[0-9a-f]{7,64}$")


def sortable_time(raw: object) -> str:
    """ISO-8601 instant -> fixed-width sortable UTC (``""`` when absent, naive or invalid)."""

    parsed = raw if isinstance(raw, datetime) else parse_timestamp(raw)
    if parsed is None or parsed.tzinfo is None:
        return ""
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def shifted(instant: str, *, hours: float = 0.0, days: float = 0.0) -> str:
    """``instant`` moved by a duration, in the same sortable form (``""`` stays ``""``)."""

    parsed = parse_timestamp(instant)
    if parsed is None:
        return ""
    return sortable_time(parsed + timedelta(hours=hours, days=days))


def normalize_state(raw: object) -> str:
    """A provider deployment/status state in the closed vocabulary, ``""`` when unknown."""

    return _STATE_ALIASES.get(str(raw or "").strip().lower(), "")


def environment_key(name: str) -> str:
    """Environments are matched case-insensitively (GitHub treats ``Production`` as ``production``)."""

    return name.strip().lower()[:_MAX_ENVIRONMENT_NAME]


def _commit(raw: object) -> str:
    text = str(raw or "").strip().lower()
    return text if _SHA.match(text) else ""


# --------------------------------------------------------------------------- #
# Settings and the connector seam
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DeploymentSettings:
    """Deployment connector configuration for one repository; ``connector=""`` means not configured.

    Configuring nothing is the default: then nothing from Phase 15 is stored,
    projected or shown.
    """

    connector: str = ""
    observation_window_hours: int = DEFAULT_OBSERVATION_WINDOW_HOURS
    link_horizon_days: int = DEFAULT_LINK_HORIZON_DAYS
    production_environments: tuple[str, ...] = DEFAULT_PRODUCTION_ENVIRONMENTS
    source: str = ""
    """Where the settings were read (operator policy, local settings file) -- display only."""
    error: str = ""
    """Why a settings document was refused (then nothing is configured); kept visible."""

    @property
    def configured(self) -> bool:
        return self.connector in IMPLEMENTED_CONNECTORS

    @property
    def provider(self) -> str:
        return CONNECTOR_PROVIDER.get(self.connector, "")

    def is_production(self, environment: str, *, provider_flag: bool = False) -> bool:
        """The provider's own production flag, or a configured production environment name."""

        key = environment_key(environment)
        return provider_flag or any(key == environment_key(name) for name in self.production_environments)

    def to_wire(self) -> dict[str, Any]:
        return {
            "connector": self.connector,
            "configured": self.configured,
            "observation_window_hours": self.observation_window_hours,
            "link_horizon_days": self.link_horizon_days,
            "production_environments": list(self.production_environments),
            "source": self.source,
            "error": self.error,
        }


NOT_CONFIGURED: Final[DeploymentSettings] = DeploymentSettings()
_SETTING_KEYS: Final[frozenset[str]] = frozenset(
    {"connector", "observation_window_hours", "link_horizon_days", "production_environments"}
)


def _bounded_int(raw: object, *, field: str, low: int, high: int) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or not low <= raw <= high:
        raise ValueError(f"deployments.{field} must be an integer between {low} and {high}")
    return raw


def parse_deployment_settings(raw: object, *, source: str) -> DeploymentSettings:
    """Validate one ``deployments`` settings document; ``None`` is "not configured".

    Raises ``ValueError`` on anything unknown or out of bounds (fail closed: an
    invalid document configures nothing).
    """

    if raw is None:
        return DeploymentSettings(source=source)
    if not isinstance(raw, Mapping):
        raise ValueError("deployments must be an object")
    unknown = sorted(set(raw) - _SETTING_KEYS)
    if unknown:
        raise ValueError(f"deployments has unknown field {unknown[0]!r}")
    connector = raw.get("connector", "")
    if not isinstance(connector, str) or (connector and connector not in CONNECTOR_KINDS):
        raise ValueError(f"deployments.connector must be one of {', '.join(CONNECTOR_KINDS)} (or empty)")
    window = DEFAULT_OBSERVATION_WINDOW_HOURS
    if "observation_window_hours" in raw:
        window = _bounded_int(
            raw["observation_window_hours"], field="observation_window_hours", low=1, high=MAX_OBSERVATION_WINDOW_HOURS
        )
    horizon = DEFAULT_LINK_HORIZON_DAYS
    if "link_horizon_days" in raw:
        horizon = _bounded_int(raw["link_horizon_days"], field="link_horizon_days", low=1, high=MAX_LINK_HORIZON_DAYS)
    production: tuple[str, ...] = DEFAULT_PRODUCTION_ENVIRONMENTS
    if "production_environments" in raw:
        values = raw["production_environments"]
        if not isinstance(values, list) or len(values) > MAX_PRODUCTION_ENVIRONMENTS:
            raise ValueError(
                f"deployments.production_environments must be an array of at most {MAX_PRODUCTION_ENVIRONMENTS} names"
            )
        names: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value.strip() or len(value) > _MAX_ENVIRONMENT_NAME:
                raise ValueError("deployments.production_environments entries must be non-empty names")
            if environment_key(value) not in {environment_key(item) for item in names}:
                names.append(value.strip())
        production = tuple(names)
    return DeploymentSettings(
        connector=connector,
        observation_window_hours=window,
        link_horizon_days=horizon,
        production_environments=production,
        source=source,
    )


@dataclass(frozen=True, slots=True)
class ConnectorStatus:
    """Whether one connector observes anything, where it runs, and why not."""

    connector: str
    state: Literal["configured", "not_configured"]
    locus: str = ""
    reason: str = ""

    def to_wire(self) -> dict[str, str]:
        return {"connector": self.connector, "state": self.state, "locus": self.locus, "reason": self.reason}


def connector_status(settings: DeploymentSettings, *, locus: ConnectorLocus | None = None) -> ConnectorStatus:
    """Honest status: only implemented connectors are ``configured``; the rest say why not."""

    if settings.error:
        return ConnectorStatus(settings.connector, "not_configured", reason=settings.error)
    if not settings.connector:
        return ConnectorStatus("", "not_configured", reason="No deployment connector is configured.")
    where = locus or _CONNECTOR_LOCUS.get(settings.connector, "customer_worker")
    if settings.connector not in IMPLEMENTED_CONNECTORS:
        return ConnectorStatus(
            settings.connector,
            "not_configured",
            locus=where,
            reason=f"The {settings.connector} connector is not available yet; nothing is observed.",
        )
    return ConnectorStatus(settings.connector, "configured", locus=where)


class DeploymentConnector(Protocol):
    """One deployment/CD fact source (GitHub Deployments, GitLab, Argo CD, Flux).

    GitHub Deployments arrive on the provider webhook/app path (hosted) or the
    local ``gh`` watch; connectors that need network access to customer CD
    systems run on customer workers by default. A connector returns summaries
    (state, environment, commit, time), never raw payloads.
    """

    @property
    def kind(self) -> str: ...

    def status(self) -> ConnectorStatus: ...

    def observations(self, repository: str, *, since: str, limit: int) -> Sequence[DeploymentObservation]: ...

    def relation(self, repository: str, merged_commit: str, deployed_commit: str) -> AncestryState | None: ...


@dataclass(frozen=True, slots=True)
class UnavailableConnector:
    """A typed placeholder for a connector that is not implemented: observes nothing, says so."""

    kind: str

    def status(self) -> ConnectorStatus:
        return connector_status(DeploymentSettings(connector=self.kind))

    def observations(self, repository: str, *, since: str, limit: int) -> Sequence[DeploymentObservation]:
        del repository, since, limit
        return ()

    def relation(self, repository: str, merged_commit: str, deployed_commit: str) -> AncestryState | None:
        del repository, merged_commit, deployed_commit
        return None


UNAVAILABLE_CONNECTORS: Final[Mapping[str, UnavailableConnector]] = {
    kind: UnavailableConnector(kind) for kind in CONNECTOR_KINDS if kind not in IMPLEMENTED_CONNECTORS
}


# --------------------------------------------------------------------------- #
# Observations and the matching rule
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MergedCommit:
    """The provider's merge result for one Review (never inferred from local Git)."""

    provider: str
    sha: str
    merged_at: str
    """Sortable UTC (:func:`sortable_time`)."""
    ref: str = ""

    def to_wire(self) -> dict[str, str]:
        return {"provider": self.provider, "sha": self.sha, "merged_at": self.merged_at, "ref": self.ref}

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> MergedCommit | None:
        sha = _commit(raw.get("sha"))
        merged_at = sortable_time(raw.get("merged_at"))
        if not sha or not merged_at:
            return None
        return cls(provider=str(raw.get("provider") or ""), sha=sha, merged_at=merged_at, ref=str(raw.get("ref") or ""))

    def horizon_end(self, settings: DeploymentSettings) -> str:
        return shifted(self.merged_at, days=settings.link_horizon_days)


@dataclass(frozen=True, slots=True)
class DeploymentObservation:
    """One deployment's latest provider-reported state (a summary, never the raw payload)."""

    provider: str
    deployment_id: str
    environment: str
    commit: str
    state: str
    created_at: str
    status_at: str
    version: str
    """Provider ordering key: an observation applies only when strictly newer."""
    production: bool = False
    """The provider's own production flag (GitHub ``production_environment``)."""
    traffic_percent: int | None = None
    url: str = ""
    connector: str = "github_deployments"

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.deployment_id}"

    @property
    def environment_key(self) -> str:
        return environment_key(self.environment)


def relation_for(merged: MergedCommit, observation: DeploymentObservation, ancestry: Mapping[str, str]) -> Relation:
    """The commit-matching rule (module docstring); ``ancestry`` maps deployed commit -> :data:`AncestryState`."""

    if observation.commit == merged.sha:
        return "exact"
    if observation.created_at and merged.merged_at and observation.created_at < merged.merged_at:
        # A deployment created before the merge existed cannot contain it.
        return "not_contained"
    proven = ancestry.get(observation.commit, "")
    if proven == "contains":
        return "descendant"
    if proven == "not_contained":
        return "not_contained"
    return "unknown"


def needs_ancestry(merged: MergedCommit, observation: DeploymentObservation, ancestry: Mapping[str, str]) -> bool:
    """Whether a provider/Git ancestry lookup could change this observation's relation."""

    return relation_for(merged, observation, ancestry) == "unknown"


# lc-debt: at most this many provider ancestry lookups (one compare each) per event or watch pass,
# shared by every merged Review it touches; observations past it are deferred (left out of the
# pass, not recorded as unknown) until a later event retries them.
# Upgrade path: one compare per deployment against the environment's previous deployment.
MAX_ANCESTRY_LOOKUPS: Final[int] = 24

AncestryLookup = Callable[[str], "AncestryState | None"]
"""``deployed_commit -> contains | not_contained | None`` (``None``: not provable now)."""


class LookupDeferred(RuntimeError):
    """Raised by a lookup that cannot run now (provider rate limit): defer, never record unknown."""


@dataclass
class LookupBudget:
    """Provider ancestry lookups one event or watch pass may still spend (shared across Reviews)."""

    remaining: int = MAX_ANCESTRY_LOOKUPS
    deferred: int = 0

    def take(self) -> bool:
        if self.remaining <= 0:
            self.deferred += 1
            return False
        self.remaining -= 1
        return True


def resolve_relations(
    merged: MergedCommit,
    observations: Iterable[DeploymentObservation],
    ancestry: Mapping[str, str],
    lookup: AncestryLookup | None,
    *,
    budget: LookupBudget | None = None,
) -> tuple[dict[str, Relation], dict[str, AncestryState], frozenset[str]]:
    """Relations per observation key, newly proven ancestry to cache, and keys deferred by the budget.

    Each distinct deployed commit is looked up at most once. A lookup that ran
    but proved nothing (``None`` or an exception) leaves the observation
    ``unknown``; one the budget did not allow is *deferred* -- not looked at
    yet -- and callers leave it out of this pass instead of recording it as
    unknown. Without any ``lookup`` (the provider cannot compare) an unproven
    observation is ``unknown``.
    """

    spend = budget if budget is not None else LookupBudget()
    known: dict[str, str] = dict(ancestry)
    learned: dict[str, AncestryState] = {}
    relations: dict[str, Relation] = {}
    tried: set[str] = set()
    postponed: set[str] = set()
    deferred: set[str] = set()
    for observation in observations:
        commit = observation.commit
        if lookup is not None and needs_ancestry(merged, observation, known):
            if commit not in tried and commit not in postponed:
                if spend.take():
                    tried.add(commit)
                    try:
                        answer = lookup(commit)
                    except LookupDeferred:
                        # Throttled: nothing more is looked up in this pass.
                        tried.discard(commit)
                        postponed.add(commit)
                        spend.remaining = 0
                        answer = None
                    except Exception:
                        answer = None
                    if answer == "contains" or answer == "not_contained":
                        known[commit] = answer
                        learned[commit] = answer
                else:
                    postponed.add(commit)
            if commit in postponed:
                deferred.add(observation.key)
        relations[observation.key] = relation_for(merged, observation, known)
    return relations, learned, frozenset(deferred)


def in_horizon(merged: MergedCommit, observation: DeploymentObservation, settings: DeploymentSettings) -> bool:
    """Deployments created after the Review's link horizon are not this Review's concern."""

    end = merged.horizon_end(settings)
    return not (observation.created_at and end and observation.created_at > end)


# --------------------------------------------------------------------------- #
# Per-Review entries and environment outcome
# --------------------------------------------------------------------------- #


def _order(created_at: str, deployment_id: str) -> tuple[str, str]:
    return (created_at, deployment_id.rjust(20, "0") if deployment_id.isdigit() else deployment_id)


@dataclass(frozen=True)
class DeploymentEntry:
    """One deployment of one environment as one merged Review sees it.

    ``transitions`` is the raw signal (state, provider time) kept only while
    the observation window is open; ``closed`` entries are summaries.
    """

    provider: str
    deployment_id: str
    environment: str
    commit: str
    relation: Relation
    state: str
    created_at: str
    status_at: str
    version: str
    window_closes_at: str
    production: bool = False
    traffic_percent: int | None = None
    url: str = ""
    succeeded: bool = False
    closed: bool = False
    transitions: tuple[tuple[str, str], ...] = ()
    transitions_dropped: int = 0

    @property
    def order(self) -> tuple[str, str]:
        return _order(self.created_at, self.deployment_id)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.deployment_id}"

    def to_wire(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "deployment_id": self.deployment_id,
            "environment": self.environment,
            "commit": self.commit,
            "relation": self.relation,
            "state": self.state,
            "created_at": self.created_at,
            "status_at": self.status_at,
            "version": self.version,
            "window_closes_at": self.window_closes_at,
            "production": self.production,
            "traffic_percent": self.traffic_percent,
            "url": self.url,
            "succeeded": self.succeeded,
            "closed": self.closed,
            "transitions": [list(item) for item in self.transitions],
            "transitions_dropped": self.transitions_dropped,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> DeploymentEntry:
        relation = str(raw.get("relation") or "unknown")
        traffic = raw.get("traffic_percent")
        transitions = raw.get("transitions") or ()
        return cls(
            provider=str(raw["provider"]),
            deployment_id=str(raw["deployment_id"]),
            environment=str(raw.get("environment") or ""),
            commit=str(raw.get("commit") or ""),
            relation=relation if relation in RELATIONS else "unknown",  # type: ignore[arg-type]
            state=normalize_state(raw.get("state")),
            created_at=str(raw.get("created_at") or ""),
            status_at=str(raw.get("status_at") or ""),
            version=str(raw.get("version") or ""),
            window_closes_at=str(raw.get("window_closes_at") or ""),
            production=bool(raw.get("production")),
            traffic_percent=traffic if isinstance(traffic, int) and not isinstance(traffic, bool) else None,
            url=str(raw.get("url") or ""),
            succeeded=bool(raw.get("succeeded")),
            closed=bool(raw.get("closed")),
            transitions=tuple(
                (str(item[0]), str(item[1]))
                for item in transitions
                if isinstance(item, list | tuple) and len(item) == 2
            ),
            transitions_dropped=int(raw.get("transitions_dropped") or 0),
        )


def _entry_from(
    observation: DeploymentObservation, relation: Relation, settings: DeploymentSettings
) -> DeploymentEntry:
    return DeploymentEntry(
        provider=observation.provider,
        deployment_id=observation.deployment_id,
        environment=observation.environment,
        commit=observation.commit,
        relation=relation,
        state=observation.state,
        created_at=observation.created_at,
        status_at=observation.status_at,
        version=observation.version,
        window_closes_at=shifted(observation.created_at, hours=settings.observation_window_hours),
        production=settings.is_production(observation.environment, provider_flag=observation.production),
        traffic_percent=observation.traffic_percent,
        url=observation.url,
        succeeded=observation.state == "success",
        transitions=((observation.state, observation.status_at or observation.created_at),),
    )


def _advance(entry: DeploymentEntry, observation: DeploymentObservation, relation: Relation) -> DeploymentEntry:
    """Apply a strictly newer observation to an open entry (older/replayed ones are no-ops)."""

    known = entry.relation if entry.relation != "unknown" else relation
    if not observation.version or observation.version <= entry.version:
        return replace(entry, relation=known) if known != entry.relation else entry
    transitions = entry.transitions
    dropped = entry.transitions_dropped
    if observation.state != entry.state:
        transitions = (*transitions, (observation.state, observation.status_at or observation.created_at))
        if len(transitions) > MAX_TRANSITIONS_PER_ENTRY:
            dropped += len(transitions) - MAX_TRANSITIONS_PER_ENTRY
            transitions = transitions[-MAX_TRANSITIONS_PER_ENTRY:]
    return replace(
        entry,
        relation=known,
        state=observation.state,
        status_at=observation.status_at,
        version=observation.version,
        traffic_percent=observation.traffic_percent,
        url=observation.url or entry.url,
        succeeded=entry.succeeded or observation.state == "success",
        production=entry.production or observation.production,
        transitions=transitions,
        transitions_dropped=dropped,
    )


def update_entries(
    prior: Sequence[DeploymentEntry],
    observations: Iterable[DeploymentObservation],
    relations: Mapping[str, Relation],
    settings: DeploymentSettings,
    *,
    now: str,
    prior_dropped: int = 0,
) -> tuple[tuple[DeploymentEntry, ...], int]:
    """Merge one environment's observations into a Review's stored entries.

    ``relations`` maps deployment key -> relation (from :func:`relation_for`).
    Closed entries are frozen summaries; an observation whose window already
    closed when it arrived opens nothing. A Review's environment exists only
    once a deployment provably contains its change: entries are the containing
    deployments plus every deployment created after the first of them
    (unproven or non-containing ones decide ``unknown``/``rolled_back``).
    Returns ``(entries, dropped)`` where ``dropped`` counts entries removed by
    the ceiling.
    """

    by_key: dict[str, DeploymentEntry] = {entry.key: entry for entry in prior}
    for observation in observations:
        if not observation.state:
            continue
        relation = relations.get(observation.key, "unknown")
        held = by_key.get(observation.key)
        if held is None:
            closes = shifted(observation.created_at, hours=settings.observation_window_hours)
            if closes and closes <= now:
                continue  # observed after its own window: raw signal is not retained
            by_key[observation.key] = _entry_from(observation, relation, settings)
        elif not held.closed:
            by_key[observation.key] = _advance(held, observation, relation)
    ordered = sorted(by_key.values(), key=lambda item: item.order)
    containing = [entry for entry in ordered if entry.relation in CONTAINING]
    first = containing[0].order if containing else None
    kept = [entry for entry in ordered if entry.relation in CONTAINING or (first is not None and entry.order > first)]
    dropped = prior_dropped
    while len(kept) > MAX_ENTRIES_PER_ENVIRONMENT:
        victim = next((entry for entry in kept if entry.relation not in CONTAINING), kept[0])
        kept.remove(victim)
        dropped += 1
    return tuple(kept), dropped


@dataclass(frozen=True)
class EnvironmentOutcome:
    """Where one merged Review's commit stands in one environment."""

    environment: str
    state: EnvironmentState
    deciding_id: str
    commit: str
    relation: Relation
    since: str
    open_until: str
    """Observation window of the deciding deployment: attention holds only while it is open."""
    production: bool
    merged: MergedCommit
    traffic_percent: int | None = None
    url: str = ""
    provider: str = ""
    entries: tuple[DeploymentEntry, ...] = ()
    entries_dropped: int = 0

    @property
    def key(self) -> str:
        return environment_key(self.environment)

    @property
    def label(self) -> str:
        return phrase(self)

    def to_wire(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "key": self.key,
            "state": self.state,
            "label": self.label,
            "deciding_id": self.deciding_id,
            "commit": self.commit,
            "relation": self.relation,
            "since": self.since,
            "open_until": self.open_until,
            "production": self.production,
            "traffic_percent": self.traffic_percent,
            "url": self.url,
            "provider": self.provider,
            "merged": self.merged.to_wire(),
            "entries": [item.to_wire() for item in self.entries],
            "entries_dropped": self.entries_dropped,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> EnvironmentOutcome | None:
        merged_raw = raw.get("merged")
        merged = MergedCommit.from_wire(merged_raw) if isinstance(merged_raw, Mapping) else None
        state = str(raw.get("state") or "")
        if merged is None or state not in ENVIRONMENT_STATES:
            return None
        relation = str(raw.get("relation") or "unknown")
        traffic = raw.get("traffic_percent")
        entries = tuple(
            DeploymentEntry.from_wire(item) for item in raw.get("entries") or () if isinstance(item, Mapping)
        )
        return cls(
            environment=str(raw.get("environment") or ""),
            state=state,  # type: ignore[arg-type]
            deciding_id=str(raw.get("deciding_id") or ""),
            commit=str(raw.get("commit") or ""),
            relation=relation if relation in RELATIONS else "unknown",  # type: ignore[arg-type]
            since=str(raw.get("since") or ""),
            open_until=str(raw.get("open_until") or ""),
            production=bool(raw.get("production")),
            merged=merged,
            traffic_percent=traffic if isinstance(traffic, int) and not isinstance(traffic, bool) else None,
            url=str(raw.get("url") or ""),
            provider=str(raw.get("provider") or ""),
            entries=entries,
            entries_dropped=int(raw.get("entries_dropped") or 0),
        )


_STATE_FROM_LATEST: Final[Mapping[str, EnvironmentState]] = {
    "success": "deployed",
    "queued": "deploying",
    "in_progress": "deploying",
    "failure": "failed",
    "error": "failed",
    "inactive": "superseded",
}


def derive_outcome(
    environment: str, entries: Sequence[DeploymentEntry], merged: MergedCommit, *, dropped: int = 0
) -> EnvironmentOutcome | None:
    """The environment outcome over a Review's entries (module docstring); ``None`` when nothing is relevant."""

    ordered = sorted(entries, key=lambda item: item.order)
    containing = [entry for entry in ordered if entry.relation in CONTAINING]
    state: EnvironmentState
    if not containing:
        return None
    else:
        latest = containing[-1]
        after = [entry for entry in ordered if entry.order > latest.order]
        replacer = (
            next((entry for entry in after if entry.relation == "not_contained" and entry.succeeded), None)
            if any(entry.succeeded for entry in containing)
            else None
        )
        unproven_success = next((entry for entry in after if entry.relation == "unknown" and entry.succeeded), None)
        if replacer is not None:
            deciding, state = replacer, "rolled_back"
        elif unproven_success is not None:
            deciding, state = unproven_success, "unknown"
        else:
            deciding, state = latest, _STATE_FROM_LATEST.get(latest.state, "unknown")
            if state == "deploying":
                live = next((entry for entry in reversed(containing[:-1]) if entry.state == "success"), None)
                if live is not None:
                    deciding, state = live, "deployed"
    return EnvironmentOutcome(
        environment=deciding.environment or environment,
        state=state,
        deciding_id=deciding.deployment_id,
        commit=deciding.commit,
        relation=deciding.relation,
        since=deciding.status_at or deciding.created_at,
        open_until=deciding.window_closes_at,
        production=any(entry.production for entry in ordered),
        merged=merged,
        traffic_percent=deciding.traffic_percent if state in {"deployed", "deploying"} else None,
        url=deciding.url,
        provider=deciding.provider,
        entries=tuple(ordered),
        entries_dropped=dropped,
    )


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #


def _target(outcome: EnvironmentOutcome) -> str:
    name = outcome.environment or outcome.key
    percent = outcome.traffic_percent
    if outcome.state in {"deployed", "deploying"} and percent is not None and 0 <= percent < 100:
        return f"{name} canary {percent}%"
    return name


def phrase(outcome: EnvironmentOutcome) -> str:
    """One environment in words (lower-case; :func:`status_line` composes them)."""

    name = outcome.environment or outcome.key
    if outcome.state == "deployed":
        return f"deployed to {_target(outcome)}"
    if outcome.state == "deploying":
        return f"deploying to {_target(outcome)}"
    if outcome.state == "failed":
        return f"{name} deployment failed"
    if outcome.state == "rolled_back":
        return f"rolled back from {name}"
    if outcome.state == "superseded":
        return f"{name} superseded"
    return f"{name} not proven"


def _display_order(outcome: EnvironmentOutcome) -> tuple[int, str]:
    return (1 if outcome.production else 0, outcome.key)


def status_line(outcomes: Iterable[EnvironmentOutcome]) -> str:
    """``Deployed to staging · production canary 10%`` -- deployed targets first, then the rest."""

    ordered = sorted(outcomes, key=_display_order)
    deployed = [_target(item) for item in ordered if item.state == "deployed"]
    parts: list[str] = []
    if deployed:
        parts.append("Deployed to " + " · ".join(deployed))
    parts.extend(phrase(item) for item in ordered if item.state != "deployed")
    if not parts:
        return ""
    first = parts[0]
    parts[0] = first[:1].upper() + first[1:]
    return " · ".join(parts)


def sentence(outcome: EnvironmentOutcome) -> str:
    """Activity/evidence wording for one environment outcome, naming the commit and why it counts."""

    text = phrase(outcome)
    commit = outcome.commit[:12]
    if outcome.state == "rolled_back":
        why = f"{commit} does not contain this change" if commit else "a later deployment does not contain this change"
    elif outcome.relation == "exact":
        why = f"{commit} is the merged commit"
    elif outcome.relation == "descendant":
        why = f"{commit} contains this change"
    else:
        why = f"{commit}: containment not proven" if commit else "containment not proven"
    return f"{text[:1].upper()}{text[1:]} ({why})."


__all__ = [
    "ATTENTION_STATES",
    "CONNECTOR_KINDS",
    "CONTAINING",
    "DEFAULT_LINK_HORIZON_DAYS",
    "DEFAULT_OBSERVATION_WINDOW_HOURS",
    "DEPLOYMENT_STATES",
    "ENVIRONMENT_STATES",
    "IMPLEMENTED_CONNECTORS",
    "MAX_ANCESTRY_LOOKUPS",
    "MAX_ENTRIES_PER_ENVIRONMENT",
    "MAX_ENVIRONMENTS_PER_REVIEW",
    "MAX_TRANSITIONS_PER_ENTRY",
    "NOT_CONFIGURED",
    "RELATIONS",
    "UNAVAILABLE_CONNECTORS",
    "AncestryLookup",
    "AncestryState",
    "ConnectorStatus",
    "DeploymentConnector",
    "DeploymentEntry",
    "DeploymentObservation",
    "DeploymentSettings",
    "EnvironmentOutcome",
    "EnvironmentState",
    "LookupBudget",
    "LookupDeferred",
    "MergedCommit",
    "Relation",
    "UnavailableConnector",
    "connector_status",
    "derive_outcome",
    "environment_key",
    "in_horizon",
    "needs_ancestry",
    "normalize_state",
    "parse_deployment_settings",
    "phrase",
    "relation_for",
    "resolve_relations",
    "sentence",
    "shifted",
    "sortable_time",
    "status_line",
    "update_entries",
]

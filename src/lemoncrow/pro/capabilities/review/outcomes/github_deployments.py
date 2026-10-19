"""GitHub Deployments -> deployment summaries (shared by the hosted webhook path and the local ``gh`` watch).

Only a summary is kept: id, commit, ref, environment, state, provider times,
the provider's production flag, an optional traffic percentage and an https
environment URL. Descriptions, log URLs, creators and the free-form
``payload`` document are never stored (a traffic percentage is the one
integer read from ``payload``, when it names one).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from lemoncrow.pro.capabilities.review.outcomes.deployments import (
    AncestryState,
    DeploymentObservation,
    normalize_state,
    sortable_time,
)

DEPLOYMENT_EVENTS: Final[frozenset[str]] = frozenset({"deployment", "deployment_status"})
_TRAFFIC_KEYS: Final[tuple[str, ...]] = ("traffic_percent", "canary_percent", "rollout_percent")
_SHA = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_ID = re.compile(r"^[0-9]{1,20}$")
_MAX_TEXT: Final[int] = 200


@dataclass(frozen=True, slots=True)
class DeploymentSummary:
    """One GitHub deployment and its newest status, as a closed summary."""

    deployment_id: str
    commit: str
    ref: str
    task: str
    environment: str
    state: str
    created_at: str
    status_at: str
    status_id: str
    production: bool
    transient: bool
    traffic_percent: int | None
    url: str

    def observation(self, *, version: str, provider: str = "github") -> DeploymentObservation:
        return DeploymentObservation(
            provider=provider,
            deployment_id=self.deployment_id,
            environment=self.environment,
            commit=self.commit,
            state=self.state,
            created_at=self.created_at,
            status_at=self.status_at,
            version=version,
            production=self.production,
            traffic_percent=self.traffic_percent,
            url=self.url,
        )

    @property
    def sequence_time(self) -> str:
        return self.status_at or self.created_at


def _text(raw: object) -> str:
    return str(raw or "").strip()[:_MAX_TEXT]


def _traffic(payload: object) -> int | None:
    document: object = payload
    if isinstance(payload, str) and payload.strip().startswith("{"):
        try:
            document = json.loads(payload)
        except ValueError:
            return None
    if not isinstance(document, Mapping):
        return None
    for key in _TRAFFIC_KEYS:
        value = document.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100:
            return value
    return None


def _https(raw: object) -> str:
    text = _text(raw) if isinstance(raw, str) and len(raw) <= 2048 else ""
    return text if text.startswith("https://") else ""


def deployment_summary(deployment: Mapping[str, Any], status: Mapping[str, Any] | None) -> DeploymentSummary | None:
    """Summary of a GitHub deployment object plus its newest status (``None`` when unusable)."""

    deployment_id = _text(deployment.get("id"))
    commit = _text(deployment.get("sha")).lower()
    created_at = sortable_time(deployment.get("created_at"))
    if not _ID.match(deployment_id) or not _SHA.match(commit) or not created_at:
        return None
    if status is not None:
        state = normalize_state(status.get("state"))
        status_at = sortable_time(status.get("created_at") or status.get("updated_at"))
        status_id = _text(status.get("id"))
        environment = _text(status.get("environment")) or _text(deployment.get("environment"))
        url = _https(status.get("environment_url"))
        if not state or not status_at or not _ID.match(status_id):
            return None
    else:
        state, status_at, status_id = "queued", "", ""
        environment = _text(deployment.get("environment"))
        url = ""
    if not environment:
        return None
    return DeploymentSummary(
        deployment_id=deployment_id,
        commit=commit,
        ref=_text(deployment.get("ref")),
        task=_text(deployment.get("task")),
        environment=environment,
        state=state,
        created_at=created_at,
        status_at=status_at,
        status_id=status_id,
        production=deployment.get("production_environment") is True,
        transient=deployment.get("transient_environment") is True,
        traffic_percent=_traffic(deployment.get("payload")),
        url=url,
    )


def compare_ancestry(payload: object) -> AncestryState | None:
    """GitHub compare ``merged...deployed`` -> whether the deployed commit contains the merged one.

    ``ahead``/``identical``: the merged commit is an ancestor (contains);
    ``behind``/``diverged``: it is not. Anything else proves nothing.
    """

    if not isinstance(payload, Mapping):
        return None
    state = str(payload.get("status") or "").strip().lower()
    if state in {"ahead", "identical"}:
        return "contains"
    if state in {"behind", "diverged"}:
        return "not_contained"
    return None


__all__ = ["DEPLOYMENT_EVENTS", "DeploymentSummary", "compare_ancestry", "deployment_summary"]

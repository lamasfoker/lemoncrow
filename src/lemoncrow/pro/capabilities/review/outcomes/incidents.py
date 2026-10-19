"""Incident linkage (Phase 15, P15-OBS item 5): advisory correlation, never blame.

An incident signal (an alert or a new error group from a connector's incident
scan) links to candidate merged Reviews whose change was running in production
when it started. Time alone never links: a candidate also needs one concrete
reason -- the incident is on a service the deployment runs, on a route the
change touched, or on an outbound host in the change's side-effect inventory.
Every link carries its reasons, at most :data:`MAX_CANDIDATES` Reviews link to
one incident, and the wording is always "may be related".

Links are ``review_derived`` rows (kind ``incident_link``, scope
``incident:<source>:<id>``) under each candidate's merged Snapshot, written
once (insert-if-absent); they hold typed labels and times only and are
removed with the Snapshot. They never edit readiness, a judgment or the
signed record.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from lemoncrow.pro.capabilities.review.freshness import parse_timestamp
from lemoncrow.pro.capabilities.review.outcomes.deployments import shifted
from lemoncrow.pro.capabilities.review.outcomes.observability import IncidentSignal, ObservationWindow

INCIDENT_ROW_KIND: Final[str] = "incident_link"
INCIDENT_ACTIVITY: Final[str] = "incident.linked"
INCIDENT_STAMP: Final[str] = "incident-v1"
MAX_CANDIDATES: Final[int] = 5
# lc-debt: at most 50 incident links per Review (oldest kept); upgrade path: roll up older links into a
# per-Review count once a service produces more incidents than this inside one observation window.
MAX_LINKS_PER_REVIEW: Final[int] = 50
ATTENTION_HOLD_HOURS: Final[int] = 72

REASON_LABELS: Final[Mapping[str, str]] = {
    "time_window": "started while this change was running in production",
    "touched_route": "is on a route this change touched",
    "inventory_match": "involves an outbound host in this change's side-effect inventory",
    "touched_service": "is on a service this deployment runs",
}
_STRONG: Final[tuple[str, ...]] = ("touched_route", "inventory_match")


@dataclass(frozen=True, slots=True)
class Candidate:
    """One merged Review whose change was observed running in production."""

    review_id: str
    revision_id: str
    environment: str
    deployment_id: str
    window: ObservationWindow
    services: tuple[str, ...] = ()
    routes: frozenset[str] = frozenset()
    hosts: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class IncidentLink:
    incident: IncidentSignal
    review_id: str
    revision_id: str
    environment: str
    deployment_id: str
    reasons: tuple[str, ...]
    summary: str
    candidates: int
    linked_at: str = ""

    @property
    def subject_id(self) -> str:
        return f"incident:{self.incident.key}"

    def to_wire(self) -> dict[str, Any]:
        return {
            "incident": self.incident.to_wire(),
            "review_id": self.review_id,
            "revision_id": self.revision_id,
            "environment": self.environment,
            "deployment_id": self.deployment_id,
            "reasons": list(self.reasons),
            "reason_labels": [REASON_LABELS[item] for item in self.reasons if item in REASON_LABELS],
            "summary": self.summary,
            "candidates": self.candidates,
            "linked_at": self.linked_at,
            "advisory": True,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> IncidentLink | None:
        incident_raw = raw.get("incident")
        incident = IncidentSignal.from_wire(incident_raw) if isinstance(incident_raw, Mapping) else None
        reasons = tuple(str(item) for item in raw.get("reasons") or () if str(item) in REASON_LABELS)
        if incident is None or "time_window" not in reasons or len(reasons) < 2:
            return None
        return cls(
            incident=incident,
            review_id=str(raw.get("review_id") or ""),
            revision_id=str(raw.get("revision_id") or ""),
            environment=str(raw.get("environment") or ""),
            deployment_id=str(raw.get("deployment_id") or ""),
            reasons=reasons,
            summary=str(raw.get("summary") or ""),
            candidates=int(raw.get("candidates") or 0),
            linked_at=str(raw.get("linked_at") or ""),
        )


def _after(start: str, instant: str) -> str:
    begin, end = parse_timestamp(start), parse_timestamp(instant)
    if begin is None or end is None:
        return ""
    minutes = max(0, int((end - begin).total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _what(incident: IncidentSignal) -> str:
    if incident.source == "alert":
        return f"Alert {incident.name or incident.id}"
    return f"New error group {incident.fingerprint[:12] or incident.id}"


def link_summary(incident: IncidentSignal, candidate: Candidate, reasons: Sequence[str]) -> str:
    """Advisory wording that names every reason; never a cause."""

    where = []
    if "touched_route" in reasons:
        where.append(f"on {incident.route}, a route this change touched")
    if "inventory_match" in reasons:
        where.append(f"calling {incident.host}, an outbound host this change declared")
    if "touched_service" in reasons:
        where.append(f"on service {incident.service}")
    after = _after(candidate.window.start, incident.started_at)
    when = f"started {after} after this change reached {candidate.environment}" if after else "started in the window"
    return f"{_what(incident)} may be related: {when}; {'; '.join(where)}. Correlation, not a cause."


def _negated(instant: str) -> str:
    # A later start sorts first: invert each digit of the fixed-width timestamp.
    return "".join(chr(ord("9") - (ord(char) - ord("0"))) if char.isdigit() else char for char in instant)


def correlate(incident: IncidentSignal, candidates: Iterable[Candidate]) -> tuple[IncidentLink, ...]:
    """Candidate links with reasons: a time-window match plus one concrete reason; at most five."""

    scored: list[tuple[tuple[int, str, str], Candidate, tuple[str, ...]]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.review_id in seen or not candidate.window.contains(incident.started_at):
            continue
        reasons = ["time_window"]
        if incident.route and incident.route in candidate.routes:
            reasons.append("touched_route")
        if incident.host and incident.host in candidate.hosts:
            reasons.append("inventory_match")
        if incident.service and incident.service in candidate.services:
            reasons.append("touched_service")
        if len(reasons) < 2:
            continue  # time alone never links
        seen.add(candidate.review_id)
        strong = sum(1 for item in reasons if item in _STRONG)
        # Stronger reasons first, then the change that reached production closest before the incident.
        scored.append(((-strong, _negated(candidate.window.start), candidate.review_id), candidate, tuple(reasons)))
    scored.sort(key=lambda item: item[0])
    chosen = scored[:MAX_CANDIDATES]
    return tuple(
        IncidentLink(
            incident=incident,
            review_id=candidate.review_id,
            revision_id=candidate.revision_id,
            environment=candidate.environment,
            deployment_id=candidate.deployment_id,
            reasons=reasons,
            summary=link_summary(incident, candidate, reasons),
            candidates=len(chosen),
        )
        for _key, candidate, reasons in chosen
    )


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


def _scope(incident: IncidentSignal) -> str:
    return f"incident:{incident.key}"


def review_links(store: Any, review_id: str) -> tuple[IncidentLink, ...]:
    """Every incident linked to one Review (oldest first)."""

    revision_ids = [revision.id for revision in store.list_revisions(review_id)]
    if not revision_ids:
        return ()
    links = [
        link
        for (_revision, _scope_key), (stamp, payload) in store.derived_results(INCIDENT_ROW_KIND, revision_ids).items()
        if str(stamp) == INCIDENT_STAMP and (link := IncidentLink.from_wire(payload)) is not None
    ]
    links.sort(key=lambda item: (item.linked_at, item.incident.key))
    return tuple(links)


def _fields(link: IncidentLink) -> dict[str, Any]:
    return {name: getattr(link, name) for name in IncidentLink.__slots__}


def store_links(store: Any, links: Sequence[IncidentLink], *, now: str, actor_id: str) -> tuple[IncidentLink, ...]:
    """Insert each link once, with one Activity entry each; returns the links created now."""

    from lemoncrow.pro.capabilities.review.session_models import ReviewActivityEvent
    from lemoncrow.pro.capabilities.review.store import DerivedResult

    created: list[IncidentLink] = []
    for link in links:
        scope = _scope(link.incident)
        existing = [row for row in store.derived_rows_by_scope(INCIDENT_ROW_KIND, scope) if row.stamp == INCIDENT_STAMP]
        if any(row.review_id == link.review_id for row in existing) or len(existing) >= MAX_CANDIDATES:
            continue
        if len(review_links(store, link.review_id)) >= MAX_LINKS_PER_REVIEW:
            continue
        stored = IncidentLink(**{**_fields(link), "linked_at": now})
        inserted, _stamp, _payload = store.insert_derived_result(
            DerivedResult(
                review_id=link.review_id,
                revision_id=link.revision_id,
                kind=INCIDENT_ROW_KIND,
                scope_key=scope,
                stamp=INCIDENT_STAMP,
                payload=stored.to_wire(),
            )
        )
        if not inserted:
            continue
        store.record_activity(
            ReviewActivityEvent(
                id="",
                review_id=link.review_id,
                revision_id=link.revision_id,
                kind=INCIDENT_ACTIVITY,  # type: ignore[arg-type]
                actor_id=actor_id,
                actor_type="agent",
                subject_type="incident",
                subject_id=stored.subject_id,
                summary=stored.summary[:500],
                detail_json=json.dumps(
                    {
                        "incident": stored.incident.to_wire(),
                        "reasons": list(stored.reasons),
                        "environment": stored.environment,
                        "deployment_id": stored.deployment_id,
                        "advisory": True,
                    },
                    sort_keys=True,
                ),
            )
        )
        created.append(stored)
    return tuple(created)


def incident_attention_triggers(store: Any, session: Any, now: str) -> tuple[Any, ...]:
    """An incident linked to this Review, held for ATTENTION_HOLD_HOURS after the link."""

    from lemoncrow.pro.capabilities.review.outcomes.deployment_outcomes import OutcomeAttentionTrigger

    out: list[OutcomeAttentionTrigger] = []
    for link in review_links(store, session.id):
        trigger = OutcomeAttentionTrigger(
            kind="incident_linked",
            subject_type="incident",
            subject_id=link.subject_id,
            fact=link.summary,
            fact_ref=f"incident:{link.incident.key}@{link.linked_at}",
            revision_id=link.revision_id,
            observed_at=link.incident.started_at,
            open_until=shifted(link.linked_at, hours=ATTENTION_HOLD_HOURS),
        )
        if trigger.active(now):
            out.append(trigger)
    return tuple(out)


__all__ = [
    "ATTENTION_HOLD_HOURS",
    "INCIDENT_ACTIVITY",
    "INCIDENT_ROW_KIND",
    "MAX_CANDIDATES",
    "MAX_LINKS_PER_REVIEW",
    "REASON_LABELS",
    "Candidate",
    "IncidentLink",
    "correlate",
    "incident_attention_triggers",
    "link_summary",
    "review_links",
    "store_links",
]

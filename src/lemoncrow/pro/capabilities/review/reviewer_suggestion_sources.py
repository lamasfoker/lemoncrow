"""Signal gathering and composition for reviewer suggestions (Phase 9 item 7).

The pure ranking lives in :mod:`reviewer_suggestions`; this module turns one
Review's durable state into evidence and composes the advisory
:class:`SuggestionSet`. It is shared by the local Reader and hosted Review:

* identity resolution is injected (:class:`IdentityResolver`), so local Reviews
  key people by CODEOWNERS handle / git email while hosted Reviews key them by
  bound principal or provider durable id;
* ownership is read through the generic ``ReviewStore`` API (both stores);
* git history is passed in only where a trusted local checkout exists.

Nothing here writes.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from lemoncrow.pro.capabilities.review.reviewer_history import HistoryResult
from lemoncrow.pro.capabilities.review.reviewer_suggestions import (
    DEFAULT_SUGGESTION_LIMIT,
    AccessState,
    CodeOwnersAuthority,
    ExclusionReason,
    ReviewerEvidence,
    ReviewerIdentity,
    ReviewerRouting,
    SignalStatus,
    SuggestionSet,
    codeowners_evidence,
    codeowners_head_changes,
    owner_kind,
    rank_suggestions,
    rule_evidence,
)

#: Earlier Reviews consulted for LemonCrow ownership, newest first.
OWNERSHIP_REVIEW_LIMIT = 40
_JUDGED_OUTCOMES = frozenset({"lgtm", "changes_requested"})


class IdentityResolver(Protocol):
    def owner(self, token: str) -> ReviewerIdentity | None:
        """A CODEOWNERS owner: ``@login``, ``@org/team`` or an email."""

    def user(self, name: str) -> ReviewerIdentity | None:
        """A reviewer-rule user entry."""

    def team(self, handle: str) -> ReviewerIdentity | None:
        """A reviewer-rule team entry (``@org/team``)."""

    def principal(self, principal_id: str) -> ReviewerIdentity | None:
        """A LemonCrow principal (earlier Review outcomes/comments)."""

    def git(self, email: str, name: str) -> ReviewerIdentity | None:
        """A git author/reviewer identity."""


@dataclass(frozen=True, slots=True)
class LocalIdentityResolver:
    """Local keys: CODEOWNERS handles, git emails, and non-sentinel principals.

    A local Review has no provider directory, so ``@login`` and a git email stay
    separate candidates unless CODEOWNERS names the owner by that same email.
    """

    local_principals: frozenset[str] = frozenset({"", "local"})

    def owner(self, token: str) -> ReviewerIdentity | None:
        kind = owner_kind(token)
        if kind == "team":
            return ReviewerIdentity("team", f"team:{token.lower()}", token, handle=token.lstrip("@"))
        if kind == "user":
            return ReviewerIdentity("user", f"handle:{token.lower()}", token, handle=token.lstrip("@"))
        if kind == "email":
            return ReviewerIdentity("user", f"email:{token.lower()}", token, handle=token.lower())
        return None

    def user(self, name: str) -> ReviewerIdentity | None:
        if name.startswith("@") or owner_kind(name) == "email":
            return self.owner(name)
        return self.principal(name)

    def team(self, handle: str) -> ReviewerIdentity | None:
        return self.owner(handle if handle.startswith("@") else f"@{handle}")

    def principal(self, principal_id: str) -> ReviewerIdentity | None:
        if principal_id.strip() in self.local_principals:
            return None  # the single local reviewer is not a routable candidate
        return ReviewerIdentity("user", f"principal:{principal_id}", principal_id, principal_id=principal_id)

    def git(self, email: str, name: str) -> ReviewerIdentity | None:
        address = email.strip().lower()
        if not address:
            return None
        return ReviewerIdentity("user", f"email:{address}", name.strip() or address, handle=address)


def history_evidence(history: HistoryResult | None, resolver: IdentityResolver) -> tuple[ReviewerEvidence, ...]:
    if history is None or history.state != "available":
        return ()
    out: list[ReviewerEvidence] = []
    for row in history.contributors:
        identity = resolver.git(row.email, row.name)
        if identity is None or not row.paths:
            continue
        role = "reviewed" if row.role == "reviewer" else "authored"
        out.append(
            ReviewerEvidence(
                signal="history",
                identity=identity,
                paths=row.paths,
                source=f"git history at {history.base_sha[:12]}",
                detail=f"{role} {row.commits} commit{'s' if row.commits != 1 else ''}",
                count=row.commits,
            )
        )
    return tuple(out)


def history_status(history: HistoryResult | None) -> SignalStatus:
    if history is None:
        return SignalStatus("history", "unavailable", detail="git history is not mined for this Review")
    if history.state != "available":
        return SignalStatus("history", "unavailable", detail=history.detail)
    detail = f"{history.commits_scanned} commit(s) scanned"
    if history.commits_bounded:
        detail += " (the bound; older history was not read)"
    if history.paths_unmined:
        # Partial coverage is stated, never presented as the whole Change's history.
        detail += f"; {history.paths_unmined} changed path(s) past the path bound were not mined"
    return SignalStatus(
        "history",
        "provisional" if history.paths_unmined else "available",
        source=f"git history at {history.base_sha[:12]}",
        detail=detail,
    )


def ownership_evidence(
    store: Any,
    review_id: str,
    paths: Sequence[str],
    resolver: IdentityResolver,
    *,
    limit_reviews: int = OWNERSHIP_REVIEW_LIMIT,
    same_repository: Callable[[Any], bool] | None = None,
) -> tuple[tuple[ReviewerEvidence, ...], SignalStatus]:
    """Who judged (LGTM / changes requested) or commented on these paths in earlier Reviews.

    Reads only the generic ``ReviewStore`` API. Bounded to the most recently
    updated *limit_reviews* other Reviews in the same store; when older Reviews
    exist past that bound the status says so (``provisional``).
    """

    wanted = set(paths)
    if not wanted:
        return (), SignalStatus("ownership", "available", detail="no changed paths")
    found: dict[str, tuple[ReviewerIdentity, list[str], set[str]]] = {}

    def add(principal_id: str, touched: Collection[str], other_id: str) -> None:
        identity = resolver.principal(principal_id)
        if identity is None:
            return
        _identity, held, reviews = found.setdefault(identity.key, (identity, [], set()))
        held.extend(path for path in sorted(touched) if path not in held)
        reviews.add(other_id)

    # lc-debt: ownership consults the 40 most recently updated Reviews per request and reports the bound; a per-path ownership index if repositories need deeper recall.
    limit_reviews = max(1, limit_reviews)
    # One row past the bound (plus this Review itself) tells whether older Reviews were left unread.
    others = [row for row in store.list_sessions(status="", limit=limit_reviews + 2) if row.id != review_id]
    bounded = len(others) > limit_reviews
    consulted = 0
    for other in others[:limit_reviews]:
        if same_repository is not None and not same_repository(other):
            continue
        consulted += 1
        latest = store.latest_revision(other.id)
        if latest is None:
            continue
        touched = {unit.path for unit in store.list_units(latest.id)} & wanted
        if touched:
            for outcome in store.latest_outcomes(other.id):
                if outcome.outcome in _JUDGED_OUTCOMES:
                    add(outcome.reviewer_id, touched, other.id)
            lister = getattr(store, "list_review_requests", None)
            if callable(lister):
                for request in lister(other.id, state="completed"):
                    add(str(request.reviewer_id), touched, other.id)
        for annotation in store.list_annotations(other.id):
            path = str(getattr(annotation.anchor, "path", "") or "")
            if annotation.source != "human" or annotation.created_by_actor != "human" or path not in wanted:
                continue
            add(annotation.created_by, (path,), other.id)
    evidence = tuple(
        ReviewerEvidence(
            signal="ownership",
            identity=identity,
            paths=tuple(held),
            source="earlier LemonCrow Reviews",
            detail=f"{len(reviews)} Review{'s' if len(reviews) != 1 else ''}",
            count=len(reviews),
        )
        for identity, held, reviews in found.values()
    )
    detail = f"{consulted} Review(s) consulted"
    if bounded:
        detail += f"; only the {limit_reviews} most recently updated Reviews were read"
    return evidence, SignalStatus(
        "ownership", "provisional" if bounded else "available", source="earlier LemonCrow Reviews", detail=detail
    )


def compose_suggestions(
    *,
    revision_id: str,
    changed_files: Sequence[Mapping[str, Any]],
    criticality: Callable[[str], str],
    codeowners: CodeOwnersAuthority,
    routing: ReviewerRouting,
    resolver: IdentityResolver,
    exclude: Callable[[ReviewerIdentity], ExclusionReason | None],
    access: Callable[[ReviewerIdentity], AccessState],
    requested: Collection[str] = (),
    ownership: tuple[tuple[ReviewerEvidence, ...], SignalStatus] | None = None,
    history: HistoryResult | None = None,
    limit: int = DEFAULT_SUGGESTION_LIMIT,
    errors: Sequence[str] = (),
    extra_evidence: Sequence[ReviewerEvidence] = (),
) -> SuggestionSet:
    """Every signal -> one deterministic, explained, advisory suggestion list.

    ``extra_evidence`` carries evidence a host resolved itself (hosted
    CODEOWNERS owners already keyed by provider durable identity); it is
    clipped to the changed paths like every other signal.
    """

    paths = tuple(dict.fromkeys(str(item.get("path") or "") for item in changed_files if item.get("path")))
    changed = set(paths)
    evidence: list[ReviewerEvidence] = [
        *rule_evidence(routing, paths, criticality=criticality, resolve_user=resolver.user, resolve_team=resolver.team),
        *codeowners_evidence(codeowners, paths, resolve_owner=resolver.owner),
        *history_evidence(history, resolver),
    ]
    for row in extra_evidence:
        clipped = tuple(path for path in row.paths if path in changed)
        if clipped:
            evidence.append(replace(row, paths=clipped))
    statuses = [
        SignalStatus(
            "rule",
            "invalid" if routing.errors else "available" if routing.rules else "absent",
            detail=f"{len(routing.rules)} reviewer rule(s)",
        ),
        codeowners.status(),
    ]
    if ownership is None:
        statuses.append(SignalStatus("ownership", "unavailable", detail="not consulted"))
    else:
        evidence.extend(ownership[0])
        statuses.append(ownership[1])
    statuses.append(history_status(history))
    suggestions, excluded, truncated = rank_suggestions(
        evidence, criticality=criticality, exclude=exclude, access=access, requested=requested, limit=limit
    )
    return SuggestionSet(
        suggestions=suggestions,
        excluded=excluded,
        truncated=truncated,
        signals=tuple(statuses),
        head_changes=codeowners_head_changes(changed_files),
        revision_id=revision_id,
        automatic_routing=routing.automatic,
        errors=tuple(dict.fromkeys((*routing.errors, *errors))),
    )


__all__ = [
    "OWNERSHIP_REVIEW_LIMIT",
    "IdentityResolver",
    "LocalIdentityResolver",
    "compose_suggestions",
    "history_evidence",
    "history_status",
    "ownership_evidence",
]

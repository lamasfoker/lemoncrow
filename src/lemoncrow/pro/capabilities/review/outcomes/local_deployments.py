"""The local path of P15-DEPLOY: GitHub deployment facts through the reviewer's own ``gh``.

Where provider facts are available locally (a GitHub pull-request Review the
local landing watch saw merged), the same watch pass reads the merge result
and the repository's GitHub Deployments, links them by the exact commit rule
of :mod:`.deployments` and runs the observation-window closer. Settings live
in the user-level operator file ``review-deployments.json`` (never in a
repository); no file configures nothing.

Interpretation (recorded): only merges the local server observed (a merged
landing) are linked; a Review merged while no local server ran is linked on
the next pass only if its landing reached ``merged``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from lemoncrow.pro.capabilities.review.outcomes.deployment_outcomes import (
    apply_observations,
    close_observation_windows,
    deployment_projection,
    merged_revision_id,
    utc_now,
)
from lemoncrow.pro.capabilities.review.outcomes.deployments import (
    AncestryState,
    DeploymentObservation,
    DeploymentSettings,
    LookupBudget,
    MergedCommit,
    parse_deployment_settings,
    resolve_relations,
    shifted,
    sortable_time,
)
from lemoncrow.pro.capabilities.review.outcomes.github_deployments import compare_ancestry, deployment_summary

_LOG = logging.getLogger(__name__)

LOCAL_SETTINGS_FILE: Final[str] = "review-deployments.json"
LOCAL_SOURCE: Final[str] = "local_operator_policy"
MERGE_ROW_KIND: Final[str] = "deployment_merge"
_MERGE_SCOPE: Final[str] = "merge"
_MERGE_STAMP: Final[str] = "deployment-merge-v1"
# lc-debt: per repository per watch pass the local path lists only the newest 30 deployments,
# reads at most 30 deployment statuses and captures at most 10 merge results; anything beyond
# waits for the next pass (or is missed when more than 30 deployments happen between passes).
# Upgrade path: paginate deployments since the oldest open window.
MAX_LOCAL_DEPLOYMENTS: Final[int] = 30
MAX_LOCAL_STATUS_READS: Final[int] = 30
MAX_LOCAL_MERGE_CAPTURES: Final[int] = 10
# lc-debt: proven ancestry cached per merged Review is capped (oldest entries dropped first).
MAX_ANCESTRY_CACHE: Final[int] = 200

GhRunner = Callable[[Sequence[str], "str | None"], str]


def load_local_deployment_settings(root: Path | None = None) -> DeploymentSettings:
    """The operator's local deployment settings; missing file -> not configured, invalid -> not configured + error."""

    if root is None:
        from lemoncrow.pro.capabilities.review.fix_agent import settings_root

        root = settings_root()
    path = Path(root) / LOCAL_SETTINGS_FILE
    if not path.is_file():
        return DeploymentSettings(source=LOCAL_SOURCE)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return DeploymentSettings(source=LOCAL_SOURCE, error=f"{LOCAL_SETTINGS_FILE} could not be read: {exc}")
    if not isinstance(document, Mapping) or document.get("version") != 1:
        return DeploymentSettings(source=LOCAL_SOURCE, error=f"{LOCAL_SETTINGS_FILE} must declare version 1")
    unknown = sorted(set(document) - {"version", "deployments"})
    if unknown:
        return DeploymentSettings(source=LOCAL_SOURCE, error=f"{LOCAL_SETTINGS_FILE} has unknown field {unknown[0]!r}")
    try:
        return parse_deployment_settings(document.get("deployments"), source=LOCAL_SOURCE)
    except ValueError as exc:
        return DeploymentSettings(source=LOCAL_SOURCE, error=str(exc))


# --------------------------------------------------------------------------- #
# The merge result of one locally watched pull request
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LocalMerge:
    review_id: str
    revision_id: str
    merged: MergedCommit
    slug: str
    ancestry: Mapping[str, str] = field(default_factory=dict)
    version: int = 0

    @property
    def stamp(self) -> str:
        return f"{_MERGE_STAMP}:{self.version}"

    def to_wire(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "revision_id": self.revision_id,
            "merged": self.merged.to_wire(),
            "slug": self.slug,
            "ancestry": dict(self.ancestry),
            "version": self.version,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> LocalMerge | None:
        merged_raw = raw.get("merged")
        merged = MergedCommit.from_wire(merged_raw) if isinstance(merged_raw, Mapping) else None
        ancestry = raw.get("ancestry")
        if merged is None or not str(raw.get("slug") or ""):
            return None
        return cls(
            review_id=str(raw.get("review_id") or ""),
            revision_id=str(raw.get("revision_id") or ""),
            merged=merged,
            slug=str(raw["slug"]),
            ancestry=(
                {str(k): str(v) for k, v in ancestry.items() if v in {"contains", "not_contained"}}
                if isinstance(ancestry, Mapping)
                else {}
            ),
            version=int(raw.get("version") or 0),
        )


def _merge_row(merge: LocalMerge) -> Any:
    from lemoncrow.pro.capabilities.review.store import DerivedResult

    return DerivedResult(
        review_id=merge.review_id,
        revision_id=merge.revision_id,
        kind=MERGE_ROW_KIND,
        scope_key=_MERGE_SCOPE,
        stamp=merge.stamp,
        payload=merge.to_wire(),
    )


def read_local_merge(store: Any, review_id: str) -> LocalMerge | None:
    revision_ids = [revision.id for revision in store.list_revisions(review_id)]
    if not revision_ids:
        return None
    for (_revision, scope), (stamp, payload) in store.derived_results(MERGE_ROW_KIND, revision_ids).items():
        if scope == _MERGE_SCOPE and str(stamp).startswith(_MERGE_STAMP + ":"):
            merge = LocalMerge.from_wire(payload)
            if merge is not None and merge.review_id == review_id:
                return merge
    return None


def capture_local_merge(store: Any, session: Any, runner: GhRunner) -> LocalMerge | None:
    """Read GitHub's merge result (``mergeCommit``) of a merged pull-request Review once."""

    from lemoncrow.pro.capabilities.review.sources.github import gh_json, parse_pr_source_ref

    ref = parse_pr_source_ref(str(session.source_ref or ""))
    if ref is None:
        return None
    slug = f"{ref.owner}/{ref.repo}"
    view = gh_json(
        runner, ["pr", "view", str(ref.number), "--repo", slug, "--json", "state,mergeCommit,mergedAt,baseRefName"]
    )
    if not isinstance(view, Mapping) or str(view.get("state") or "").upper() != "MERGED":
        return None
    commit = view.get("mergeCommit")
    merged = MergedCommit.from_wire(
        {
            "provider": "github",
            "sha": commit.get("oid") if isinstance(commit, Mapping) else "",
            "merged_at": view.get("mergedAt"),
            "ref": view.get("baseRefName"),
        }
    )
    revision_id = merged_revision_id(store, session)
    if merged is None or not revision_id:
        return None
    merge = LocalMerge(review_id=session.id, revision_id=revision_id, merged=merged, slug=slug)
    created, _stamp, payload = store.insert_derived_result(_merge_row(merge))
    return merge if created else LocalMerge.from_wire(payload)


def _remember(store: Any, merge: LocalMerge, learned: Mapping[str, AncestryState]) -> None:
    if not learned:
        return
    ancestry = {**merge.ancestry, **learned}
    while len(ancestry) > MAX_ANCESTRY_CACHE:
        ancestry.pop(next(iter(ancestry)))
    updated = replace(merge, ancestry=ancestry, version=merge.version + 1)
    # A lost race only loses a cache entry; the next pass looks it up again.
    store.compare_and_set_derived_result(_merge_row(updated), expected_stamp=merge.stamp)


# --------------------------------------------------------------------------- #
# The watch pass
# --------------------------------------------------------------------------- #


@dataclass
class _Pass:
    runner: GhRunner
    listings: dict[str, list[Mapping[str, Any]]] = field(default_factory=dict)
    statuses: dict[tuple[str, str], Mapping[str, Any] | None] = field(default_factory=dict)
    status_reads: int = 0
    captures: int = 0
    budget: LookupBudget = field(default_factory=LookupBudget)

    def listing(self, slug: str) -> list[Mapping[str, Any]]:
        from lemoncrow.pro.capabilities.review.sources.github import gh_json

        if slug not in self.listings:
            rows = gh_json(self.runner, ["api", f"repos/{slug}/deployments?per_page={MAX_LOCAL_DEPLOYMENTS}"])
            self.listings[slug] = [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []
        return self.listings[slug]

    def status(self, slug: str, deployment_id: str) -> tuple[bool, Mapping[str, Any] | None]:
        """``(read, newest status)``; ``read`` is false once the per-pass bound is spent."""

        from lemoncrow.pro.capabilities.review.sources.github import gh_json

        key = (slug, deployment_id)
        if key not in self.statuses:
            if self.status_reads >= MAX_LOCAL_STATUS_READS:
                return False, None
            self.status_reads += 1
            rows = gh_json(self.runner, ["api", f"repos/{slug}/deployments/{deployment_id}/statuses?per_page=1"])
            first = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], Mapping) else None
            self.statuses[key] = first
        return True, self.statuses[key]

    def compare(self, slug: str, merged: str) -> Callable[[str], AncestryState | None]:
        from lemoncrow.pro.capabilities.review.sources.github import gh_json

        def lookup(deployed: str) -> AncestryState | None:
            return compare_ancestry(
                gh_json(self.runner, ["api", f"repos/{slug}/compare/{merged}...{deployed}?per_page=1"])
            )

        return lookup


def _observations(
    work: _Pass, merge: LocalMerge, settings: DeploymentSettings, now: str
) -> list[DeploymentObservation]:
    horizon_end = merge.merged.horizon_end(settings)
    out: list[DeploymentObservation] = []
    for row in work.listing(merge.slug):
        created = sortable_time(row.get("created_at"))
        if not created or created < merge.merged.merged_at or (horizon_end and created > horizon_end):
            continue
        if shifted(created, hours=settings.observation_window_hours) <= now:
            continue  # its observation window closed: nothing new is recorded about it
        read, status = work.status(merge.slug, str(row.get("id") or ""))
        if not read:
            continue
        summary = deployment_summary(row, status)
        if summary is None:
            continue
        version = f"{summary.sequence_time}|{summary.status_id.rjust(20, '0') if summary.status_id else ''}"
        out.append(summary.observation(version=version))
    return out


def watch_local_deployments(
    store: Any,
    *,
    settings: DeploymentSettings | None = None,
    runner: GhRunner | None = None,
    now: str | None = None,
) -> tuple[str, ...]:
    """One bounded pass over this store's merged GitHub Reviews; returns the Review ids whose outcome rows changed.

    Not configured -> nothing is read, written or closed. Each Review is
    isolated: one failing ``gh`` call never stops the others.
    """

    effective = settings if settings is not None else load_local_deployment_settings()
    instant = now or utc_now()
    if not effective.configured:
        # Nothing is read or written; windows recorded while configured still close.
        _close(store, instant)
        return ()
    if runner is None:
        from lemoncrow.pro.capabilities.review.sources.github import run_gh

        def runner(args: Sequence[str], stdin: str | None) -> str:
            return run_gh(args, stdin)

    work = _Pass(runner=runner)
    touched: list[str] = []
    for landing in store.list_relationships_by_kind("landing"):
        if str(landing.status) != "merged":
            continue
        try:
            session = store.get_session(landing.review_id)
            if session is None or not str(session.source_ref or "").startswith("github:"):
                continue
            merge = read_local_merge(store, session.id)
            if merge is None:
                if work.captures >= MAX_LOCAL_MERGE_CAPTURES:
                    continue
                work.captures += 1
                merge = capture_local_merge(store, session, runner)
            if merge is None or instant > merge.merged.horizon_end(effective):
                continue
            observations = _observations(work, merge, effective, instant)
            if not observations:
                continue
            relations, learned, deferred = resolve_relations(
                merge.merged,
                observations,
                merge.ancestry,
                work.compare(merge.slug, merge.merged.sha),
                budget=work.budget,
            )
            _remember(store, merge, learned)
            result = apply_observations(
                store,
                session,
                merge.merged,
                [item for item in observations if item.key not in deferred],
                relations,
                effective,
                actor_id="provider_github",
                now=instant,
            )
            if result.touched:
                touched.append(session.id)
        except Exception:
            _LOG.warning("deployment watch failed for Change %s", landing.review_id, exc_info=True)
    _close(store, instant)
    return tuple(touched)


def _close(store: Any, instant: str) -> None:
    try:
        close_observation_windows(store, now=instant)
    except Exception:
        _LOG.warning("deployment observation-window closer failed", exc_info=True)


def local_deployment_projection(store: Any, session: Any, *, root: Path | None = None) -> dict[str, Any]:
    """The Review Status deployment line of the local server."""

    settings = load_local_deployment_settings(root)
    merge = read_local_merge(store, session.id) if settings.configured else None
    return deployment_projection(
        store, session, settings, merged=None if merge is None else merge.merged, locus="local"
    )


__all__ = [
    "LOCAL_SETTINGS_FILE",
    "MAX_LOCAL_DEPLOYMENTS",
    "MAX_LOCAL_MERGE_CAPTURES",
    "MAX_LOCAL_STATUS_READS",
    "MERGE_ROW_KIND",
    "LocalMerge",
    "capture_local_merge",
    "load_local_deployment_settings",
    "local_deployment_projection",
    "read_local_merge",
    "watch_local_deployments",
]

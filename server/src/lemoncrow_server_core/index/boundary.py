"""Generic content-scope boundary below a View.

Layer 1 is content-addressed and may deduplicate bytes inside its storage
partition. A manifest declaration is still only a claim: a View may address a
digest only when :class:`ContentProvenance` records that its opaque content
scope has demonstrated possession of those bytes.

The public engine intentionally does not decide how content scopes are chosen
or shared. Local mode uses one scope. Other compositions may choose narrower
or wider scopes before a View is opened.

Declaration never writes provenance, and inaccessible content is reported in
the same way as absent content so this layer does not expose storage membership.
"""

from __future__ import annotations

from collections.abc import Sequence

from .contracts import ContentProvenance, ViewState, ViewStore

__all__ = ["addressable", "addressable_in"]


def addressable_in(
    provenance: ContentProvenance,
    state: ViewState,
    digests: Sequence[str],
) -> frozenset[str]:
    """Of ``digests``, those ``state`` may actually read content for.

    Takes the view state rather than an identifier because every caller already
    holds one: the query bound its answer to a revision and the materializer
    proved its tree is revision-exact, so re-reading the view here would be a
    second read of something that must not have moved in between.
    """
    if not digests:
        return frozenset()
    return provenance.held(state.org_id, state.repo_id, state.content_subject, digests)


def addressable(
    views: ViewStore,
    provenance: ContentProvenance,
    org_id: str,
    view_id: str,
    digests: Sequence[str],
) -> frozenset[str]:
    """The same answer for a caller that holds only the view's identifier.

    ``views.get`` refuses another organization's view exactly as it refuses one
    that never existed, so this cannot be used to probe for a workspace either.
    """
    if not digests:
        return frozenset()
    return addressable_in(provenance, views.get(org_id, view_id), digests)

"""Informational D6 note for base movement across mutable Review Snapshots.

Pure projection over the existing ReviewSession/ReviewRevision authorities:
no new state, no ancestry claim, no change to judgment validity or readiness.
"""

from __future__ import annotations

from collections.abc import Sequence

from lemoncrow.pro.capabilities.review.session_models import ReviewRevision


def base_movement_notes(
    recorded_base_sha: str, current: ReviewRevision, history: Sequence[ReviewRevision]
) -> tuple[str, ...]:
    """Report a changed base only when both recorded and previous bases differ.

    Older clients and old Reviews can have no recorded base at all. In that
    case the previous Snapshot is sufficient; on the first Snapshot no
    movement can be inferred. The server cannot verify Git ancestry.
    """
    if current.range_mode not in {"working_tree", "staged"} or not current.base_sha:
        return ()
    previous = max(
        (
            item
            for item in history
            if item.review_id == current.review_id and item.revision_number < current.revision_number
        ),
        key=lambda item: item.revision_number,
        default=None,
    )
    if previous is None or not previous.base_sha:
        return ()
    if current.base_sha in {previous.base_sha, recorded_base_sha}:
        return ()
    return (
        "Review base changed since the previous capture "
        f"({previous.base_sha[:12]} → {current.base_sha[:12]}). "
        "Git ancestry was not verified; a rebase or missing base commit may explain the change.",
    )

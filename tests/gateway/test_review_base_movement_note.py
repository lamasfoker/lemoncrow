"""D6 base-movement notice is a projection, not a Review verdict or state."""

from __future__ import annotations

from lemoncrow.pro.capabilities.review.base_movement import base_movement_notes
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision


def _rev(number: int, base: str, mode: str = "working_tree") -> ReviewRevision:
    return ReviewRevision(
        id=str(number),
        review_id="review",
        revision_number=number,
        range_mode=mode,
        tree_fingerprint=str(number),
        packet_schema_version=1,
        base_sha=base,
    )


def test_no_note_on_initial_capture_or_stable_base() -> None:
    first, stable = _rev(1, "a" * 40), _rev(2, "a" * 40)
    assert base_movement_notes(first.base_sha, first, [first]) == ()
    assert base_movement_notes(first.base_sha, stable, [first, stable]) == ()


def test_integrated_base_movement_is_informational_and_idempotent() -> None:
    first, advanced = _rev(1, "a" * 40), _rev(2, "b" * 40)
    notes = base_movement_notes(first.base_sha, advanced, [first, advanced])
    assert len(notes) == 1
    assert "base changed" in notes[0]
    assert "Git ancestry was not verified" in notes[0]
    assert notes == base_movement_notes(first.base_sha, advanced, [first, advanced])


def test_unbound_legacy_review_reports_changed_head_without_frozen_base() -> None:
    first, moved = _rev(1, "a" * 40), _rev(2, "c" * 40)
    assert len(base_movement_notes("", moved, [first, moved])) == 1


def test_return_to_recorded_base_is_not_unexplained_movement() -> None:
    first, middle, returned = _rev(1, "a" * 40), _rev(2, "b" * 40), _rev(3, "a" * 40)
    assert base_movement_notes(first.base_sha, returned, [first, middle, returned]) == ()


def test_no_false_note_when_previous_base_is_missing_or_mode_not_mutable() -> None:
    blank, current = _rev(1, ""), _rev(2, "d" * 40)
    assert base_movement_notes("", current, [blank, current]) == ()
    first, second = _rev(1, "a" * 40, "commit_range"), _rev(2, "b" * 40, "commit_range")
    assert base_movement_notes("", second, [first, second]) == ()


def test_compares_against_immediately_previous_revision_not_earlier_one() -> None:
    first, second, third = _rev(1, "a" * 40), _rev(2, "b" * 40), _rev(3, "b" * 40)
    assert base_movement_notes(first.base_sha, third, [third, first, second]) == ()

"""Product wishes ("Make a wish"): validation, delivery, and the fact that a
wish is not telemetry."""

from __future__ import annotations

from typing import Any

import pytest

from lemoncrow.core.service import feedback as feedback_mod
from lemoncrow.core.service.feedback import FeedbackError, submit_feedback


@pytest.fixture(autouse=True)
def no_development_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(feedback_mod.OVERRIDE_ENV, raising=False)


@pytest.fixture()
def posted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture what was sent instead of opening a socket."""
    posts: list[dict[str, Any]] = []

    def fake_post_json(
        endpoint: str,
        payload: dict[str, Any],
        *,
        timeout_s: float,
        attempts: int,
        headers: dict[str, str] | None = None,
    ) -> int:
        posts.append({"endpoint": endpoint, "payload": payload})
        return 200

    monkeypatch.setattr(feedback_mod, "post_json", fake_post_json)
    return posts


def test_sends_a_wish_to_the_sink(posted: list[dict[str, Any]]) -> None:
    result = submit_feedback(message="  Teach me the map view  ", email="  Me@Example.COM ", source="review")

    assert result.delivered == "remote"
    assert posted == [
        {
            "endpoint": "https://lemoncrow.com/api/feedback",
            "payload": {
                "message": "Teach me the map view",
                "email": "me@example.com",
                "source": "review",
                "lemoncrow_version": feedback_mod._service_version(),
            },
        }
    ]


def test_omits_the_email_when_none_was_typed(posted: list[dict[str, Any]]) -> None:
    submit_feedback(message="Jump to the next hunk")
    assert posted[0]["payload"]["email"] is None


def test_an_unlisted_source_is_normalised_rather_than_sent(posted: list[dict[str, Any]]) -> None:
    submit_feedback(message="Show blame for this line", source="../../etc/passwd")
    assert posted[0]["payload"]["source"] == "unknown"


@pytest.mark.parametrize("kill_switch", ["DO_NOT_TRACK", "LEMONCROW_TELEMETRY"])
def test_a_wish_ignores_the_telemetry_switch(
    posted: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, kill_switch: str
) -> None:
    # A wish is a person asking to be sent something. It is not telemetry, so
    # the telemetry opt-out must not swallow it.
    monkeypatch.setenv(kill_switch, "1" if kill_switch == "DO_NOT_TRACK" else "off")

    result = submit_feedback(message="Show blame for this line")

    assert result.delivered == "remote"
    assert [post["endpoint"] for post in posted] == ["https://lemoncrow.com/api/feedback"]


def test_reports_a_sink_failure_instead_of_claiming_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feedback_mod, "post_json", lambda *a, **k: 503)

    result = submit_feedback(message="Show blame for this line")

    assert (result.delivered, result.status, result.detail) == ("failed", 503, "sink_status_503")


def test_rejects_a_wish_the_sender_can_fix(posted: list[dict[str, Any]]) -> None:
    with pytest.raises(FeedbackError):
        submit_feedback(message="   ")
    with pytest.raises(FeedbackError):
        submit_feedback(message="x" * (feedback_mod.MAX_MESSAGE_CHARS + 1))
    with pytest.raises(FeedbackError):
        submit_feedback(message="fine", email="not-an-email")
    with pytest.raises(FeedbackError):
        submit_feedback(message=42)
    assert posted == []


def test_the_development_override_receives_the_wish(
    posted: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(feedback_mod.OVERRIDE_ENV, "http://127.0.0.1:8787/api/feedback")

    result = submit_feedback(message="Try the override")

    assert result.delivered == "remote"
    assert posted[0]["endpoint"] == "http://127.0.0.1:8787/api/feedback"

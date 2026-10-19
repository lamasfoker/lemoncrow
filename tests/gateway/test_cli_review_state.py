"""Terminal Review summaries fetch independent projections concurrently."""

import threading
from types import SimpleNamespace

import click
import pytest
from lemoncrow_client.errors import ClientError, ErrorCode

from lemoncrow.gateway.cli.commands.review import _server_terminal_state
from lemoncrow.pro.capabilities.review import hosted


def _summary(**overrides: object) -> dict:
    arguments = dict(
        mark_targets=(),
        mark_state="reviewed",
        show_units=False,
        show_marks=False,
        comment_body=None,
        comment_target=None,
        comment_kind="comment",
        reply_to=None,
        show_comments=False,
        since_my_review=False,
        send_feedback=False,
        finish_review=False,
        reopen_review=False,
        discard_review=False,
    )
    arguments.update(overrides)
    return _server_terminal_state(SimpleNamespace(), "change", **arguments)


@pytest.mark.parametrize("since_my_review", [False, True])
def test_summary_fetches_only_used_projections_concurrently(
    monkeypatch: pytest.MonkeyPatch, since_my_review: bool
) -> None:
    calls: list[str] = []
    events: list[tuple[str, str]] = []
    barrier = threading.Barrier(3)
    owner = threading.get_ident()
    base = "/api/reviews/change"
    responses = {
        base: {
            "session": {"id": "change"},
            "revision": {"id": "snapshot", "revision_number": 4},
            "progress": {"target_count": 7},
            "target_count": 7,
            "discarded": [{"unit_key": "old", "state": "reviewed"}],
        },
        base + "/units": {"units": [{"kind": "file", "state": "reviewed", "unit_key": "file:a"}]},
        base + "/annotations": {"annotations": [], "counts": {"open": 0}},
        base + "/frontier": {"discarded_verdicts": [{"unit_key": "old", "state": "reviewed"}]},
    }

    def request(_config: object, method: str, path: str, *, body: object = None) -> dict:
        assert method == "GET"
        calls.append(path)
        events.append(("request", path))
        if path != base + "/frontier":
            # None may return until all three independent requests have started.
            barrier.wait(timeout=5)
        return responses[path]

    def progress(stage: str, detail: str) -> None:
        assert threading.get_ident() == owner
        events.append((stage, detail))

    monkeypatch.setattr(hosted, "server_review_request", request)
    state = _summary(since_my_review=since_my_review, progress=progress)
    assert sorted(calls) == sorted(
        [base, base + "/units", base + "/annotations", *([base + "/frontier"] if since_my_review else [])]
    )
    for key, suffix in (("state_overview", ""), ("state_units", "/units"), ("state_annotations", "/annotations")):
        stages = [stage for stage, _detail in events]
        assert stages.index(key) < events.index(("request", base + suffix)) < stages.index(key + "_done")
    assert state["target_count"] == 7
    assert state["mark_counts"] == {"reviewed": 1}
    assert state["discarded_verdicts"] == [{"unit_key": "old", "state": "reviewed"}]
    assert ("frontier" in state) == since_my_review


def test_actions_finish_before_parallel_summary_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    base = "/api/reviews/change"
    marked = threading.Event()
    barrier = threading.Barrier(3)
    calls: list[tuple[str, str]] = []

    def request(_config: object, method: str, path: str, *, body: object = None) -> dict:
        calls.append((method, path))
        if method == "POST":
            assert calls[:2] == [("GET", base), ("GET", base + "/frontier")]
            assert body["expected_revision_id"] == "snapshot"
            marked.set()
            return {"marks": []}
        if marked.is_set():
            barrier.wait(timeout=5)
        elif path in {base + "/units", base + "/annotations"}:
            pytest.fail("summary fetched before the mark was committed")
        if path == base:
            return {"session": {"id": "change"}, "revision": {"id": "snapshot"}}
        if path == base + "/units":
            return {"units": [{"kind": "file", "state": "reviewed", "unit_key": "file:a"}]}
        return {}

    monkeypatch.setattr(hosted, "server_review_request", request)
    state = _summary(mark_targets=("file:a",))
    assert state["mark_counts"] == {"reviewed": 1}
    assert calls[2] == ("POST", base + "/marks")
    assert sorted(calls[3:]) == sorted([("GET", base), ("GET", base + "/units"), ("GET", base + "/annotations")])


_ATTACHED = {
    "session": {"id": "h1", "hosted": {"reference": "r/h1", "url": "https://hosted.example/r/h1"}},
    "revision": {"id": "snapshot"},
}


@pytest.mark.parametrize(
    "action",
    [
        {"mark_targets": ("file:a",)},
        {"comment_body": "why?", "comment_target": "a.py:L2"},
        {"comment_body": "agreed", "reply_to": "ann-hosted-thread"},
    ],
)
def test_terminal_writes_on_an_attached_review_are_refused_never_queued(
    monkeypatch: pytest.MonkeyPatch, action: dict
) -> None:
    calls: list[tuple[str, str]] = []

    def request(_config: object, method: str, path: str, *, body: object = None) -> dict:
        calls.append((method, path))
        return _ATTACHED if path == "/api/reviews/change" else {}

    monkeypatch.setattr(hosted, "server_review_request", request)
    with pytest.raises(click.ClickException) as refused:
        _summary(**action)
    message = refused.value.message
    assert "attached to hosted r/h1" in message and "https://hosted.example/r/h1" in message
    assert "not queued" in message
    # Nothing was written: the human's offline queue never sees an agent's write.
    assert [method for method, _path in calls] == ["GET"]


@pytest.mark.parametrize("discard_fails", [False, True])
@pytest.mark.parametrize(
    ("suffix", "action"),
    [("/annotations", {"comment_body": "x", "comment_target": "a.py:L2"}), ("/marks", {"mark_targets": ("file:a",)})],
)
def test_a_write_queued_by_a_race_with_attach_is_withdrawn_and_reported(
    monkeypatch: pytest.MonkeyPatch, discard_fails: bool, suffix: str, action: dict
) -> None:
    base = "/api/reviews/change"
    calls: list[tuple[str, str, object]] = []

    def request(_config: object, method: str, path: str, *, body: object = None) -> dict:
        calls.append((method, path, body))
        if path == base:
            return {"session": {"id": "change"}, "revision": {"id": "snapshot"}}
        if path == base + suffix and method == "POST":
            # Attached between the read and the write: the server queued it (HTTP 202).
            return {"queued": True, "reference": "r/h1", "actions": [{"id": "rha-1"}, {"id": "rha-2"}]}
        if path.startswith(base + "/hosted/actions/"):
            if discard_fails and path.endswith("rha-2"):
                raise ClientError(ErrorCode.REVIEW_STATE_CONFLICT, "already applied")
            return {}
        return {}

    monkeypatch.setattr(hosted, "server_review_request", request)
    with pytest.raises(click.ClickException) as refused:
        _summary(**action)
    discards = [(path, body) for method, path, body in calls if "/hosted/actions/" in path]
    assert discards == [
        (base + "/hosted/actions/rha-1", {"op": "discard"}),
        (base + "/hosted/actions/rha-2", {"op": "discard"}),
    ]
    message = refused.value.message
    assert "attached to hosted r/h1" in message
    if discard_fails:
        assert "could not withdraw queued action(s) rha-2" in message
    else:
        assert message.endswith("the write it queued was withdrawn")
    # Nothing after the refused write runs: no summary is shown as if it had applied.
    assert calls[-1][0] == "POST" and "/hosted/actions/" in calls[-1][1]

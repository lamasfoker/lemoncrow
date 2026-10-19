"""Preview recordings are human observations that never leak to a disallowed model route."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.ai_review import _evidence_label
from lemoncrow.pro.capabilities.review.interactive_preview import RECORDING_TITLE, recording_for_model
from lemoncrow.pro.capabilities.review.preview_sessions import PreviewSessionStore
from tests.gateway.test_review_interactive_preview_api import (
    HEADERS,
    RECIPE,
    _browse,
    _enter,
    _fetch,
    _review,
    _start,
    _url,
)


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


DENIED = RECIPE + "ai_content_access:\n  - mode: ai_denied\n    paths: ['site/**']\n"


def _recorded(
    tmp_path: Path, *, sink_payload: bytes = b"", base_config: str = RECIPE
) -> tuple[Any, dict[str, Any], Any]:
    review = _review(tmp_path, base_config=base_config)
    if sink_payload:
        review.driver.sink_rows["new"] = [
            {
                "kind": "http",
                "destination": "http://hooks.example.com/notify",
                "method": "POST",
                "summary": "POST /notify",
                "payload_b64": base64.b64encode(sink_payload).decode(),
                "bytes": len(sink_payload),
            }
        ]
    payload = _start(review).json()
    sid, token = payload["session"]["id"], payload["token"]
    origin, cookie = _browse(_enter(review, sid, token, side="new", path="/orders/42").json()["url"])
    _fetch(origin, "/orders/42", cookie)
    record = review.client.post(_url(review, f"/sessions/{sid}/stop"), headers=HEADERS, json={}).json()["session"]
    evidence = next(
        row for row in review.store.list_evidence(review.review_id) if row.id == record["recording_evidence_id"]
    )
    document = json.loads((review.store.root / evidence.artifact_path).read_text())
    return review, document, evidence


def test_recording_is_a_human_observation_never_a_sandbox_pass(tmp_path: Path) -> None:
    _review_ns, document, evidence = _recorded(tmp_path)
    assert evidence.title == RECORDING_TITLE
    assert evidence.kind == "document"
    assert evidence.attestation_class == "human_observation"
    assert evidence.verification_status == ""
    assert evidence.stable_key == ""
    assert document["verdict"] is None
    assert "never a sandbox result" in document["note"]
    verification = json.loads(evidence.execution_verification)
    assert verification["executor_class"] == "author_machine"
    assert "pass" not in json.dumps(verification).lower()
    assert [row["path"] for row in document["events"]] == ["/orders/42"]


def test_model_routes_see_only_title_and_executor(tmp_path: Path) -> None:
    _review_ns, _document, evidence = _recorded(tmp_path)
    label = _evidence_label(evidence)
    assert label == f"{RECORDING_TITLE} · executor=author_machine"
    assert "/orders" not in label and "requests" not in label


def test_ai_denied_recording_content_never_reaches_any_model_route(tmp_path: Path) -> None:
    _review_ns, document, _evidence = _recorded(
        tmp_path, sink_payload=b"order shipped to someone@example.com", base_config=DENIED
    )
    # Executed code can read any file of the side: the most restrictive mode in force applies.
    assert document["content_access"] == "ai_denied"
    for route in ("external", "customer_local"):
        projected = recording_for_model(document, route_trust=route)
        assert projected["verdict"] is None
        assert str(projected["events"]).startswith("withheld")
        (capture,) = projected["sink_captures"]["new"]
        assert str(capture["payload"]).startswith("withheld")
        assert str(capture["destination"]).startswith("withheld")
        assert capture["delivered"] is False
        assert "someone@example.com" not in json.dumps(projected)
        assert "/orders" not in json.dumps(projected)


def test_allowed_content_access_exposes_structural_events(tmp_path: Path) -> None:
    _review_ns, document, _evidence = _recorded(tmp_path)
    allowed = recording_for_model({**document, "content_access": "customer_local_only"}, route_trust="customer_local")
    assert [row["path"] for row in allowed["events"]] == ["/orders/42"]
    external = recording_for_model({**document, "content_access": "customer_local_only"}, route_trust="external")
    assert str(external["events"]).startswith("withheld")


def test_sink_payloads_are_redacted_before_storage(tmp_path: Path) -> None:
    secret = "gh" + "p_" + "Q" * 36
    review, document, evidence = _recorded(tmp_path, sink_payload=f"token={secret}".encode())
    text = (review.store.root / evidence.artifact_path).read_text()
    assert secret not in text
    (capture,) = document["sink_captures"]["new"]
    assert capture["kind"] == "http"
    assert "1 outbound attempt captured, none delivered" in evidence.detail


def test_revoked_viewer_is_refused_per_request(tmp_path: Path) -> None:
    allowed = {"ok": True}
    review = _review(tmp_path)
    review.manager._viewer_authorized = lambda _review_id, _viewer_id: allowed["ok"]
    payload = _start(review).json()
    origin, cookie = _browse(_enter(review, payload["session"]["id"], payload["token"]).json()["url"])
    assert _fetch(origin, "/", cookie)[0] == 200
    allowed["ok"] = False
    assert _fetch(origin, "/", cookie)[0] == 403
    assert _enter(review, payload["session"]["id"], payload["token"]).status_code == 403


def test_concurrent_viewers_share_one_budget(tmp_path: Path) -> None:
    viewer = {"id": "alice"}
    review = _review(
        tmp_path,
        viewer=viewer,
        base_config=(
            "interactive_preview:\n  command: [python3, app.py]\n  port: 8080\n"
            "review:\n  budget:\n    per_review_minutes: 14\n"
        ),
    )
    first = _start(review, ttl_seconds=300).json()["session"]
    assert first["ttl_seconds"] == 300  # 10 of 14 compute minutes reserved
    viewer["id"] = "bob"
    second = _start(review, ttl_seconds=300)
    assert second.status_code == 200, second.text
    # Only 4 unreserved minutes remain (less the metered setup): two sides for two minutes.
    assert 110 <= second.json()["session"]["ttl_seconds"] <= 120
    assert second.json()["session"]["limited_by_budget"] is True
    viewer["id"] = "carol"
    third = _start(review, ttl_seconds=300)
    assert third.status_code == 409
    assert third.json()["detail"].startswith("Limited by budget")
    states = {row.viewer_id: row.state for row in PreviewSessionStore(review.store).for_review(review.review_id)}
    assert states["alice"] == "running" and states["bob"] == "running"

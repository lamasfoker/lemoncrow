"""P13-ANALYTICS over the local server: one user, no teams, read-only, every aggregate drillable."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from test_local_review_inbox_views import REPO, REPO_B, _Local, _ok, _refused, _review, _run

from lemoncrow.pro.capabilities.review.analytics.scope import MAX_PERIOD_DAYS
from lemoncrow.pro.capabilities.review.anchors import build_anchor
from lemoncrow.pro.capabilities.review.session_models import Annotation

ANALYTICS = "/api/review-analytics"
RULE = "rule:repository:no-raw-sql@sha256:" + "b" * 64


def _ai_finding(local: _Local, repo_id: str, review_id: str, revision_id: str, disposition: str, **extra: Any) -> str:
    created = local.store(repo_id).add_annotation(
        Annotation(
            id="",
            review_id=review_id,
            revision_id=revision_id,
            anchor=build_anchor(path="src/billing/charge.py", text="a\nb\n", start_line=1, end_line=1),
            body="finding",
            title=f"finding {disposition}",
            created_by="LemonCrow AI Review",
            created_by_actor="agent",
            source="ai_review",
            source_id="f",
            finding_disposition=disposition,  # type: ignore[arg-type]
            rule_refs=(RULE,),
            **extra,
        )
    )
    return created.id


def test_local_analytics_scope_drill_and_refusals(tmp_path: Path) -> None:
    async def body(local: _Local) -> None:
        first, first_rev = _review(local.store(), "billing")
        _review(local.store(), "waiting")
        other, other_rev = _review(local.store(REPO_B), "other-repo", status="finished")
        _ai_finding(local, REPO, first, first_rev, "confirmed_by_reviewer")
        _ai_finding(local, REPO, first, first_rev, "superseded_stale")
        _ai_finding(local, REPO_B, other, other_rev, "open")

        report = await _ok(await local.get(ANALYTICS))
        assert report["gating"] == "never" and report["individuals"] == "not_reported"
        assert report["teams_supported"] is False
        assert report["volume"]["reviews"]["count"] == 3
        assert {row["key"] for row in report["volume"]["by_repository"]} == {REPO, REPO_B}
        assert report["findings"]["current"]["count"] == 2  # the stale finding is not current
        assert [row["rule"] for row in report["rules"]] == [RULE]
        assert report["waiting_by_owner"] and report["reviewer_load"]["individuals"] == "not_reported"

        # Repository filter and drill-down to the exact source records.
        scoped = await _ok(await local.get(ANALYTICS, repo=REPO))
        assert scoped["volume"]["reviews"]["count"] == 2 and scoped["findings"]["current"]["count"] == 1
        window = {"since": scoped["scope"]["since"], "until": scoped["scope"]["until"], "repo": REPO}
        drill = await _ok(await local.get(f"{ANALYTICS}/drill", key="findings.current", **window))
        assert drill["count"] == 1 and drill["records"][0]["review_id"] == first
        assert (
            drill["records"][0]["review_path"] == f"/r/{first}" and drill["records"][0]["record_kind"] == "ai_finding"
        )
        limited = await _ok(await local.get(f"{ANALYTICS}/drill", key="volume.reviews", limit="1", **window))
        assert limited["count"] == 2 and len(limited["records"]) == 1 and limited["truncated"] is True

        # Another organization selects nothing; it never widens access.
        foreign = await _ok(await local.get(ANALYTICS, org="someone-else"))
        assert foreign["volume"]["reviews"]["count"] == 0 and foreign["bounds"]["candidates"] == 0

        # Teams do not exist locally: refused, never ignored. Bounds are refused past their cap.
        await _refused(await local.get(ANALYTICS, team="payments"), 400, "team")
        await _ok(await local.get(ANALYTICS, days=str(MAX_PERIOD_DAYS)))
        await _refused(await local.get(ANALYTICS, days=str(MAX_PERIOD_DAYS + 1)), 400, "days")
        assert local.client is not None
        unsafe = await local.client.get(f"{local.url}{ANALYTICS}", params={"path": "../etc"})
        await _refused(unsafe, 400, "path")
        await _refused(await local.get(f"{ANALYTICS}/drill"), 400, "key")
        await _refused(await local.get(f"{ANALYTICS}/drill", key="volume.reviews", limit="501"), 400, "limit")
        unknown = await _ok(await local.get(f"{ANALYTICS}/drill", key="no.such.aggregate"))
        assert unknown["known"] is False and unknown["records"] == []

    _run(tmp_path, body)


def test_local_analytics_never_write_review_state(tmp_path: Path) -> None:
    async def body(local: _Local) -> None:
        review_id, revision_id = _review(local.store(), "billing")
        _ai_finding(local, REPO, review_id, revision_id, "open")
        store = local.store()
        before = (store.list_annotations(review_id), store.list_outcomes(review_id), store.get_session(review_id))
        await _ok(await local.get(ANALYTICS))
        await _ok(await local.get(f"{ANALYTICS}/drill", key="findings.current"))
        after = (store.list_annotations(review_id), store.list_outcomes(review_id), store.get_session(review_id))
        assert after == before

    _run(tmp_path, body)

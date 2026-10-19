from __future__ import annotations

import json

from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence, ReviewRevision
from lemoncrow.pro.capabilities.review.verification import build_verification_projection


def _revision(identifier: str, number: int) -> ReviewRevision:
    return ReviewRevision(
        id=identifier,
        review_id="r-review",
        revision_number=number,
        range_mode="working_tree",
        tree_fingerprint=f"tree-{number}",
        packet_schema_version=1,
        created_at=f"2026-09-25T00:0{number}:00+00:00",
    )


def test_old_pass_is_stale_and_does_not_pass_current_snapshot() -> None:
    first = _revision("rr-one", 1)
    second = _revision("rr-two", 2)
    evidence = (
        ReviewEvidence(
            id="ev-old",
            review_id="r-review",
            revision_id=first.id,
            kind="verification",
            title="unit tests",
            source="ci",
            source_ref="run-1",
            verification_status="PASS",
            created_at="2026-09-25T00:01:30+00:00",
        ),
        ReviewEvidence(
            id="ev-current",
            review_id="r-review",
            revision_id=second.id,
            kind="verification",
            title="browser",
            source="test",
            verification_status="NOT_RUN",
            detail="browser runtime unavailable",
            created_at="2026-09-25T00:02:30+00:00",
        ),
    )

    projection = build_verification_projection(
        packet={"evidence": [{"name": "lint", "status": "PASS", "source": "local"}]},
        evidence=evidence,
        revision=second,
        revisions=(first, second),
    )

    # The packet's lint row is author-session evidence, not an observation of
    # this Snapshot, so it is shown but never counted.
    assert projection["summary"] == {"pass": 0, "fail": 0, "not_run": 1, "unknown": 0, "stale": 1}
    lint = next(row for row in projection["checks"] if row["name"] == "lint")
    assert lint["state"] == "session"
    old = next(row for row in projection["checks"] if row["name"] == "unit tests")
    assert old["status"] == "PASS"
    assert old["state"] == "stale"
    assert old["snapshot_id"] == first.id
    current = next(row for row in projection["checks"] if row["name"] == "browser")
    assert current["state"] == "current"
    assert current["status"] == "NOT_RUN"


def test_supply_chain_artifact_exposes_subject_digest_and_observation_source() -> None:
    revision = _revision("rr-one", 1)
    digest = "sha256:" + "b" * 64
    evidence = (
        ReviewEvidence(
            id="ev-sbom",
            review_id="r-review",
            revision_id=revision.id,
            kind="sbom",
            title="candidate image SBOM",
            content_hash="sbom-sha256",
            source="ci",
            source_ref="buildkit",
            detail=json.dumps(
                {
                    "artifact_digest": digest,
                    "standard": "SPDX",
                    "format_version": "SPDX-2.3",
                    "observation_source": "BuildKit",
                }
            ),
            created_at="2026-09-25T00:01:30+00:00",
        ),
    )

    projection = build_verification_projection(
        packet=None,
        evidence=evidence,
        revision=revision,
        revisions=(revision,),
    )

    artifact = projection["artifacts"][0]
    assert artifact["state"] == "current"
    assert artifact["artifact_digest"] == digest
    assert artifact["standard"] == "SPDX"
    assert artifact["format_version"] == "SPDX-2.3"
    assert artifact["observation_source"] == "BuildKit"


def test_newer_observation_outranks_packet_row_regardless_of_packet_source() -> None:
    revision = _revision("rr-one", 1)
    rerun = ReviewEvidence(
        id="ev-rerun",
        review_id="r-review",
        revision_id=revision.id,
        kind="verification",
        title="Focused tests",
        source="ci",
        verification_status="FAIL",
        created_at="2026-09-25T00:01:30+00:00",
    )

    projection = build_verification_projection(
        # Packet evidence carries its own provenance label (run_ledger /
        # session:<id>), not the literal "packet"; it must still yield to a
        # newer exact-Snapshot observation of the same check.
        packet={"evidence": [{"name": "Focused tests", "status": "PASS", "source": "run_ledger"}]},
        evidence=(rerun,),
        revision=revision,
        revisions=(revision,),
    )

    row = next(item for item in projection["checks"] if item["name"] == "Focused tests")
    assert row["status"] == "FAIL"
    assert projection["summary"]["pass"] == 0
    assert projection["summary"]["fail"] == 1


def test_author_session_evidence_is_shown_but_never_counts_as_snapshot_verification() -> None:
    revision = _revision("rr-one", 1)

    projection = build_verification_projection(
        # What provenance records from the authoring session: any command run
        # at any time in the session, plus placeholders for checks never run.
        # None of it is bound to the captured source.
        packet={
            "evidence": [
                {"name": "Full suite", "status": "PASS", "detail": "pytest -q (exit 0)", "source": "session:abc"},
                {"name": "Typecheck", "status": "NOT_RUN", "detail": "", "source": "none"},
                {"name": "Migration check", "status": "UNKNOWN", "detail": "", "source": "none"},
            ]
        },
        evidence=(),
        revision=revision,
        revisions=(revision,),
    )

    assert {row["name"]: row["state"] for row in projection["checks"]} == {
        "Full suite": "session",
        "Typecheck": "session",
        "Migration check": "session",
    }
    assert projection["summary"] == {"pass": 0, "fail": 0, "not_run": 0, "unknown": 0, "stale": 0}


def test_historical_projection_never_lists_a_newer_snapshots_artifact() -> None:
    first = _revision("rr-one", 1)
    second = _revision("rr-two", 2)
    newer_sbom = ReviewEvidence(
        id="ev-newer",
        review_id="r-review",
        revision_id=second.id,
        kind="sbom",
        title="newer image SBOM",
        content_hash="sbom-newer",
        source="ci",
        detail=json.dumps({"artifact_digest": "sha256:" + "c" * 64}),
        created_at="2026-09-25T00:02:30+00:00",
    )

    historical = build_verification_projection(
        packet=None, evidence=(newer_sbom,), revision=first, revisions=(first, second)
    )
    assert historical["artifacts"] == []

    latest = build_verification_projection(
        packet=None, evidence=(newer_sbom,), revision=second, revisions=(first, second)
    )
    assert [item["id"] for item in latest["artifacts"]] == ["ev-newer"]
    assert latest["artifacts"][0]["state"] == "current"

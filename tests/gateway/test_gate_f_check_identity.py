"""Gate F property 6 (local): a posted verification can never claim typed check identity.

``checks/ingest`` spoofing is pinned in ``test_review_check_ingest``. The
``/verification`` route reads a JSON body too: every authority field in it
(class, provider, integration, stable key, provider revision) is ignored, and
the server assigns ``human_observation`` or nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.gateway.test_review_check_ingest import _EQUIVALENTS, _condition, _latest, _post, _review, _typed


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


def test_a_posted_verification_claiming_provider_ci_identity_never_satisfies_the_required_check(
    tmp_path: Path,
) -> None:
    review = _review(tmp_path, base_config=_EQUIVALENTS)
    revision = _latest(review)
    _typed(review, revision, status="FAIL")
    assert _condition(review, "verification")["state"] == "fail"

    for source in ("ci", "test", "external", "human"):
        posted = _post(
            review,
            "verification",
            {
                "revision_id": revision.id,
                "name": "CI / unit tests",
                "status": "PASS",
                "source": source,
                "source_ref": "github:actions:run-2",
                # Display and identity claims the server must never take from a caller.
                "attestation_class": "provider_ci",
                "stable_key": "unit-tests",
                "provider": "github",
                "integration_id": "actions",
                "provider_revision": revision.head_sha,
                "attested_by": "github:actions",
                "executor_class": "provider_hosted",
                "remote_identity": "run-2",
            },
        )
        assert posted.status_code == 201, posted.text
        evidence = posted.json()["evidence"]
        assert evidence["attestation_class"] == ("human_observation" if source == "human" else "")
        assert evidence.get("provider", "") == "" and evidence.get("integration_id", "") == ""
        assert _condition(review, "verification")["state"] == "fail"

from __future__ import annotations

import hashlib
from pathlib import Path

from lemoncrow.core.capabilities.verified_work.models import Diagnostic, VerifiedWorkOpportunity
from lemoncrow.pro.capabilities.verified_work.promote import promote_candidate
from lemoncrow.pro.capabilities.verified_work.repairs import RepairJournal, RepairMeta, list_repairs


def _journal(tmp_path: Path, live: Path) -> RepairJournal:
    meta = RepairMeta(str(live), "repo", str(live), "s", "claude", "m", "typecheck", "check")
    return RepairJournal(meta, root=tmp_path / "lc")


def _opp(live: Path) -> VerifiedWorkOpportunity:
    digest = hashlib.sha256((live / "a.txt").read_bytes()).hexdigest()
    return VerifiedWorkOpportunity(
        id="x",
        session_id="s",
        workspace=str(live),
        command="check",
        cwd=str(live),
        kind="typecheck",
        exit_code=1,
        diagnostics=(Diagnostic("a.txt", 1, 1, "X", "bad"),),
        diagnostic_digest="d",
        diagnostic_excerpt="bad",
        implicated_paths=("a.txt",),
        base_file_hashes=(("a.txt", digest),),
        original_result_digest="r",
    )


def test_promotion_rolls_back_when_post_verify_fails(tmp_path: Path) -> None:
    live = tmp_path / "live"
    candidate = tmp_path / "candidate"
    live.mkdir()
    candidate.mkdir()
    (live / "a.txt").write_text("bad\n")
    opp = _opp(live)
    (candidate / "a.txt").write_text("good\n")

    result = promote_candidate(
        opp,
        candidate_workspace=candidate,
        changed_paths=("a.txt",),
        post_verify=lambda: False,
        journal=_journal(tmp_path, live),
    )

    assert result.promoted is False
    assert result.reason == "post_promotion_verifier_failed"
    assert (live / "a.txt").read_text() == "bad\n"
    assert list_repairs(tmp_path / "lc") == []


def test_promotion_refuses_stale_base(tmp_path: Path) -> None:
    live = tmp_path / "live"
    candidate = tmp_path / "candidate"
    live.mkdir()
    candidate.mkdir()
    (live / "a.txt").write_text("bad\n")
    opp = _opp(live)
    (live / "a.txt").write_text("new host edit\n")
    (candidate / "a.txt").write_text("good\n")

    result = promote_candidate(
        opp, candidate_workspace=candidate, changed_paths=("a.txt",), journal=_journal(tmp_path, live)
    )

    assert result.promoted is False
    assert result.reason == "base_hash_conflict"
    assert (live / "a.txt").read_text() == "new host edit\n"

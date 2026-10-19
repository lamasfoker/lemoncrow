"""Undo records for promoted Verified Local Work repairs (G4-VLW)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lemoncrow.core.capabilities.verified_work.models import Diagnostic, VerifiedWorkOpportunity
from lemoncrow.pro.capabilities.verified_work import repairs
from lemoncrow.pro.capabilities.verified_work.promote import promote_candidate
from lemoncrow.pro.capabilities.verified_work.repairs import (
    RepairJournal,
    RepairMeta,
    list_repairs,
    load_repair,
    promotion_notice,
    prune_repairs,
    repairs_dir,
    undo_repair,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _opp(live: Path, rels: tuple[str, ...]) -> VerifiedWorkOpportunity:
    return VerifiedWorkOpportunity(
        id="x",
        session_id="s",
        workspace=str(live),
        command="check",
        cwd=str(live),
        kind="typecheck",
        exit_code=1,
        diagnostics=tuple(Diagnostic(rel, 1, 1, "X", "bad") for rel in rels),
        diagnostic_digest="d",
        diagnostic_excerpt="bad",
        implicated_paths=rels,
        base_file_hashes=tuple((rel, _sha((live / rel).read_bytes())) for rel in rels),
        original_result_digest="r",
    )


def _meta(live: Path, *, repo_key: str = "repo-1") -> RepairMeta:
    return RepairMeta(
        workspace=str(live),
        repo_key=repo_key,
        repo_root=str(live),
        session_id="s",
        host="claude",
        model="custom/local/fake",
        kind="typecheck",
        verifier_command="check",
    )


def _setup(tmp_path: Path, files: dict[str, tuple[bytes, bytes]]) -> tuple[Path, Path]:
    live = tmp_path / "live"
    candidate = tmp_path / "candidate"
    for rel, (before, after) in files.items():
        (live / rel).parent.mkdir(parents=True, exist_ok=True)
        (candidate / rel).parent.mkdir(parents=True, exist_ok=True)
        (live / rel).write_bytes(before)
        (candidate / rel).write_bytes(after)
    return live, candidate


def _promote(live: Path, candidate: Path, rels: tuple[str, ...], root: Path, **kwargs):  # type: ignore[no-untyped-def]
    return promote_candidate(
        _opp(live, rels),
        candidate_workspace=candidate,
        changed_paths=rels,
        journal=RepairJournal(_meta(live, repo_key=kwargs.pop("repo_key", "repo-1")), root=root),
        **kwargs,
    )


def test_bounds_are_the_documented_contract() -> None:
    assert repairs.MAX_RECORDS == 50
    assert repairs.MAX_AGE_S == 30 * 24 * 60 * 60
    assert repairs.MAX_RECORD_BYTES == 4 * 1024 * 1024


def test_promotion_without_a_repair_journal_is_refused(tmp_path: Path) -> None:
    live, candidate = _setup(tmp_path, {"a.txt": (b"bad\n", b"good\n")})

    result = promote_candidate(_opp(live, ("a.txt",)), candidate_workspace=candidate, changed_paths=("a.txt",))

    assert result.promoted is False
    assert result.reason == "repair_record_required"
    assert (live / "a.txt").read_bytes() == b"bad\n"


def test_promotion_writes_record_with_pre_bytes_post_hashes_and_identity(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    live, candidate = _setup(tmp_path, {"a.txt": (b"bad\n", b"good\n"), "pkg/b.txt": (b"old\n", b"new\n")})

    result = _promote(live, candidate, ("a.txt", "pkg/b.txt"), root)

    assert result.promoted is True
    assert result.record_id
    record = load_repair(result.record_id, root=root)
    assert record is not None
    assert record.status == "applied"
    assert record.model == "custom/local/fake"
    assert record.kind == "typecheck"
    assert record.verifier_command == "check"
    assert record.host == "claude"
    assert record.created_at > 0
    by_path = {row.path: row for row in record.files}
    assert by_path["a.txt"].pre_sha256 == _sha(b"bad\n")
    assert by_path["a.txt"].post_sha256 == _sha(b"good\n")
    assert by_path["pkg/b.txt"].pre_sha256 == _sha(b"old\n")
    assert by_path["pkg/b.txt"].post_sha256 == _sha(b"new\n")
    # Pre-repair bytes are stored verbatim, not reconstructed from git.
    blob = repairs_dir(root) / record.id / by_path["a.txt"].blob
    assert blob.read_bytes() == b"bad\n"


def test_undo_restores_exact_bytes_and_mode(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    live, candidate = _setup(tmp_path, {"a.txt": (b"bad\r\nno newline", b"good\n")})
    os.chmod(live / "a.txt", 0o640)

    result = _promote(live, candidate, ("a.txt",), root)
    assert result.promoted is True
    assert (live / "a.txt").read_bytes() == b"good\n"

    undone = undo_repair(result.record_id, root=root)

    assert undone.ok is True
    assert undone.reason == "restored"
    assert undone.restored_paths == ("a.txt",)
    assert (live / "a.txt").read_bytes() == b"bad\r\nno newline"
    assert stat.S_IMODE((live / "a.txt").stat().st_mode) == 0o640
    record = load_repair(result.record_id, root=root)
    assert record is not None and record.status == "undone"

    again = undo_repair(result.record_id, root=root)
    assert again.ok is False
    assert again.reason == "already_undone"
    assert (live / "a.txt").read_bytes() == b"bad\r\nno newline"


def test_undo_refuses_on_drift_names_conflicts_and_changes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    live, candidate = _setup(tmp_path, {"a.txt": (b"bad a\n", b"good a\n"), "b.txt": (b"bad b\n", b"good b\n")})
    result = _promote(live, candidate, ("a.txt", "b.txt"), root)
    assert result.promoted is True
    (live / "b.txt").write_bytes(b"host edited after repair\n")

    undone = undo_repair(result.record_id, root=root)

    assert undone.ok is False
    assert undone.reason == "drift"
    assert undone.conflicts == ("b.txt",)
    assert (live / "a.txt").read_bytes() == b"good a\n"
    assert (live / "b.txt").read_bytes() == b"host edited after repair\n"
    record = load_repair(result.record_id, root=root)
    assert record is not None and record.status == "applied"


def test_undo_refuses_when_a_repaired_file_was_deleted(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    live, candidate = _setup(tmp_path, {"a.txt": (b"bad\n", b"good\n")})
    result = _promote(live, candidate, ("a.txt",), root)
    (live / "a.txt").unlink()

    undone = undo_repair(result.record_id, root=root)

    assert undone.ok is False
    assert undone.conflicts == ("a.txt",)
    assert not (live / "a.txt").exists()


def test_undo_restores_dirty_and_untracked_live_state_not_head(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    repo = tmp_path / "live"
    repo.mkdir()
    (repo / "tracked.py").write_text("committed\n", encoding="utf-8")
    for args in (("init",), ("add", "."), ("-c", "user.email=t@e", "-c", "user.name=t", "commit", "-m", "base")):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "tracked.py").write_text("dirty uncommitted\n", encoding="utf-8")
    (repo / "untracked.py").write_text("untracked work\n", encoding="utf-8")
    status_before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "tracked.py").write_text("repaired\n", encoding="utf-8")
    (candidate / "untracked.py").write_text("repaired untracked\n", encoding="utf-8")

    result = _promote(repo, candidate, ("tracked.py", "untracked.py"), root)
    assert result.promoted is True
    undone = undo_repair(result.record_id, root=root)

    assert undone.ok is True
    assert (repo / "tracked.py").read_text(encoding="utf-8") == "dirty uncommitted\n"
    assert (repo / "untracked.py").read_text(encoding="utf-8") == "untracked work\n"
    status_after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout
    assert status_after == status_before


def test_failed_post_verify_rolls_back_and_leaves_no_record(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    live, candidate = _setup(tmp_path, {"a.txt": (b"bad\n", b"good\n")})

    result = _promote(live, candidate, ("a.txt",), root, post_verify=lambda: False)

    assert result.promoted is False
    assert result.reason == "post_promotion_verifier_failed"
    assert (live / "a.txt").read_bytes() == b"bad\n"
    assert list_repairs(root=root) == []


def test_record_write_failure_blocks_promotion(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "lc"
    live, candidate = _setup(tmp_path, {"a.txt": (b"bad\n", b"good\n")})

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("disk full")

    monkeypatch.setattr(repairs, "_write_blob", boom)

    result = _promote(live, candidate, ("a.txt",), root)

    assert result.promoted is False
    assert result.reason == "repair_record_failed"
    assert (live / "a.txt").read_bytes() == b"bad\n"


def test_commit_failure_rolls_back_promotion(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "lc"
    live, candidate = _setup(tmp_path, {"a.txt": (b"bad\n", b"good\n")})
    monkeypatch.setattr(RepairJournal, "commit", lambda self, record_id, post_hashes: False)

    result = _promote(live, candidate, ("a.txt",), root)

    assert result.promoted is False
    assert result.reason == "repair_record_failed"
    assert (live / "a.txt").read_bytes() == b"bad\n"
    assert list_repairs(root=root) == []


@pytest.mark.parametrize(("size", "promoted"), [(16, True), (17, False)])
def test_record_size_bound_at_and_past_limit(tmp_path: Path, monkeypatch, size: int, promoted: bool) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "lc"
    monkeypatch.setattr(repairs, "MAX_RECORD_BYTES", 16)
    before = b"x" * size
    live, candidate = _setup(tmp_path, {"a.txt": (before, b"good\n")})

    result = _promote(live, candidate, ("a.txt",), root)

    assert result.promoted is promoted
    if promoted:
        assert (live / "a.txt").read_bytes() == b"good\n"
    else:
        assert result.reason == "repair_record_too_large"
        assert (live / "a.txt").read_bytes() == before
        assert list_repairs(root=root) == []


def _journal_record(root: Path, live: Path, *, created_at: float, repo_key: str = "repo-1") -> str:
    journal = RepairJournal(_meta(live, repo_key=repo_key), root=root, clock=lambda: created_at)
    record_id, reason = journal.begin({"a.txt": (b"x", 0o644)}, {"a.txt": b"y"})
    assert record_id is not None, reason
    assert journal.commit(record_id, {"a.txt": _sha(b"y")}, prune=False)
    return record_id


@pytest.mark.parametrize(("extra", "expected"), [(0, 50), (1, 50), (3, 50)])
def test_prune_count_bound_at_and_past_limit(tmp_path: Path, extra: int, expected: int) -> None:
    root = tmp_path / "lc"
    live = tmp_path / "live"
    live.mkdir()
    now = 1_000_000.0
    ids = [_journal_record(root, live, created_at=now + i) for i in range(repairs.MAX_RECORDS + extra)]

    removed = prune_repairs(root=root, now=now + 1000)

    assert removed == extra
    kept = {record.id for record in list_repairs(root=root, prune=False)}
    assert len(kept) == expected
    # Oldest records go first; the newest MAX_RECORDS survive.
    assert kept == set(ids[extra:])


@pytest.mark.parametrize(("age_offset", "kept"), [(-1, True), (0, True), (1, False)])
def test_prune_age_bound_at_and_past_limit(tmp_path: Path, age_offset: int, kept: bool) -> None:
    root = tmp_path / "lc"
    live = tmp_path / "live"
    live.mkdir()
    now = 10_000_000.0
    record_id = _journal_record(root, live, created_at=now - repairs.MAX_AGE_S - age_offset)

    prune_repairs(root=root, now=now)

    assert (load_repair(record_id, root=root) is not None) is kept


def test_new_record_prunes_past_count_bound(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "lc"
    monkeypatch.setattr(repairs, "MAX_RECORDS", 3)
    live, candidate = _setup(tmp_path, {"a.txt": (b"v0\n", b"v1\n")})
    ids: list[str] = []
    for index in range(4):
        (live / "a.txt").write_bytes(f"v{index}\n".encode())
        (candidate / "a.txt").write_bytes(f"v{index + 1}\n".encode())
        result = _promote(live, candidate, ("a.txt",), root)
        assert result.promoted is True
        ids.append(result.record_id)

    remaining = [record.id for record in list_repairs(root=root)]
    assert len(remaining) == 3
    assert ids[0] not in remaining


def test_undo_last_picks_newest_applied_record_in_repository(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    live = tmp_path / "live"
    live.mkdir()
    (live / "a.txt").write_bytes(b"y")
    now = time.time()
    older = _journal_record(root, live, created_at=now - 300, repo_key="repo-1")
    newer = _journal_record(root, live, created_at=now - 200, repo_key="repo-1")
    other = _journal_record(root, live, created_at=now - 100, repo_key="repo-2")

    undone = undo_repair(repo_key="repo-1", root=root)

    assert undone.ok is True
    assert undone.record_id == newer
    assert (live / "a.txt").read_bytes() == b"x"
    assert load_repair(older, root=root).status == "applied"  # type: ignore[union-attr]
    assert load_repair(other, root=root).status == "applied"  # type: ignore[union-attr]


def test_undo_last_without_records_reports_not_found(tmp_path: Path) -> None:
    undone = undo_repair(repo_key="repo-1", root=tmp_path / "lc")
    assert undone.ok is False
    assert undone.reason == "not_found"


def test_pending_record_after_crash_is_undoable(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    live = tmp_path / "live"
    live.mkdir()
    (live / "a.txt").write_bytes(b"before")
    journal = RepairJournal(_meta(live), root=root)
    record_id, _ = journal.begin({"a.txt": (b"before", 0o644)}, {"a.txt": b"after"})
    assert record_id is not None
    # Simulate a crash after the live write but before commit.
    (live / "a.txt").write_bytes(b"after")

    undone = undo_repair(record_id, root=root)

    assert undone.ok is True
    assert (live / "a.txt").read_bytes() == b"before"


def test_pending_record_after_partial_write_restores_only_written_files(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    live = tmp_path / "live"
    live.mkdir()
    (live / "a.txt").write_bytes(b"a-before")
    (live / "b.txt").write_bytes(b"b-before")
    journal = RepairJournal(_meta(live), root=root)
    record_id, _ = journal.begin(
        {"a.txt": (b"a-before", 0o644), "b.txt": (b"b-before", 0o644)},
        {"a.txt": b"a-after", "b.txt": b"b-after"},
    )
    assert record_id is not None
    # Crash after the first file was written: b.txt still holds its pre bytes.
    (live / "a.txt").write_bytes(b"a-after")

    undone = undo_repair(record_id, root=root)

    assert undone.ok is True
    assert undone.restored_paths == ("a.txt",)
    assert (live / "a.txt").read_bytes() == b"a-before"
    assert (live / "b.txt").read_bytes() == b"b-before"


def test_applied_record_treats_a_file_back_at_pre_bytes_as_drift(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    live, candidate = _setup(tmp_path, {"a.txt": (b"a-bad", b"a-good"), "b.txt": (b"b-bad", b"b-good")})
    result = _promote(live, candidate, ("a.txt", "b.txt"), root)
    (live / "b.txt").write_bytes(b"b-bad")

    undone = undo_repair(result.record_id, root=root)

    assert undone.ok is False
    assert undone.conflicts == ("b.txt",)
    assert (live / "a.txt").read_bytes() == b"a-good"


def test_pending_record_without_live_write_is_already_restored(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    live = tmp_path / "live"
    live.mkdir()
    (live / "a.txt").write_bytes(b"before")
    journal = RepairJournal(_meta(live), root=root)
    record_id, _ = journal.begin({"a.txt": (b"before", 0o644)}, {"a.txt": b"after"})

    undone = undo_repair(record_id, root=root)

    assert undone.ok is True
    assert undone.reason == "already_restored"
    assert (live / "a.txt").read_bytes() == b"before"


def test_undo_refuses_corrupt_pre_repair_blob(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    live, candidate = _setup(tmp_path, {"a.txt": (b"bad\n", b"good\n")})
    result = _promote(live, candidate, ("a.txt",), root)
    record = load_repair(result.record_id, root=root)
    assert record is not None
    (repairs_dir(root) / record.id / record.files[0].blob).write_bytes(b"tampered")

    undone = undo_repair(result.record_id, root=root)

    assert undone.ok is False
    assert undone.reason == "record_corrupt"
    assert (live / "a.txt").read_bytes() == b"good\n"


def test_undo_refuses_record_paths_that_escape_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    live, candidate = _setup(tmp_path, {"a.txt": (b"bad\n", b"good\n")})
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"good\n")
    result = _promote(live, candidate, ("a.txt",), root)
    record_path = repairs_dir(root) / result.record_id / "record.json"
    raw = json.loads(record_path.read_text(encoding="utf-8"))
    raw["files"][0]["path"] = "../outside.txt"
    record_path.write_text(json.dumps(raw), encoding="utf-8")

    undone = undo_repair(result.record_id, root=root)

    assert undone.ok is False
    assert undone.reason == "path_escape"
    assert outside.read_bytes() == b"good\n"


def test_promotion_notice_names_files_and_undo_command() -> None:
    text = promotion_notice(("src/a.ts", "src/b.ts"), attempts=1, record_id="abc123def456")

    assert "src/a.ts" in text
    assert "src/b.ts" in text
    assert "lc rt savings undo abc123def456" in text


def _formatting_verifier(path: Path, formatted: bytes):  # type: ignore[no-untyped-def]
    def verify() -> bool:
        # The live verifier (e.g. a formatter-backed check) rewrites the file.
        path.write_bytes(formatted)
        return True

    return verify


def test_commit_records_the_hash_observed_after_the_live_verifier(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    live, candidate = _setup(tmp_path, {"a.txt": (b"bad\n", b"good  \n")})

    result = _promote(live, candidate, ("a.txt",), root, post_verify=_formatting_verifier(live / "a.txt", b"good\n"))

    assert result.promoted is True
    record = load_repair(result.record_id, root=root)
    assert record is not None
    assert record.files[0].post_sha256 == _sha(b"good\n")
    undone = undo_repair(result.record_id, root=root)
    assert undone.ok is True
    assert undone.reason == "restored"
    assert (live / "a.txt").read_bytes() == b"bad\n"


def test_undo_refuses_bytes_matching_only_the_unverified_candidate(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    live, candidate = _setup(tmp_path, {"a.txt": (b"bad\n", b"good  \n")})
    result = _promote(live, candidate, ("a.txt",), root, post_verify=_formatting_verifier(live / "a.txt", b"good\n"))
    (live / "a.txt").write_bytes(b"good  \n")

    undone = undo_repair(result.record_id, root=root)

    assert undone.ok is False
    assert undone.reason == "drift"
    assert undone.conflicts == ("a.txt",)
    assert (live / "a.txt").read_bytes() == b"good  \n"


def test_prune_removes_crashed_record_folders_only(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    live, candidate = _setup(tmp_path, {"a.txt": (b"bad\n", b"good\n")})
    kept = _promote(live, candidate, ("a.txt",), root)
    base = repairs_dir(root)
    empty = base / "0123456789ab"
    empty.mkdir()
    garbled = base / "ba9876543210"
    garbled.mkdir()
    (garbled / "record.json").write_text("{not json", encoding="utf-8")
    foreign = base / "notes"
    foreign.mkdir()

    prune_repairs(root)

    assert not empty.exists()
    assert not garbled.exists()
    assert foreign.exists()
    assert load_repair(kept.record_id, root=root) is not None


def _flaky_replace(monkeypatch, fail_on: set[int]) -> list[str]:  # type: ignore[no-untyped-def]
    real = repairs._atomic_replace
    calls: list[str] = []

    def flaky(path: Path, data: bytes, mode: int) -> None:
        calls.append(path.name)
        if len(calls) in fail_on:
            raise OSError("disk full")
        real(path, data, mode)

    monkeypatch.setattr(repairs, "_atomic_replace", flaky)
    return calls


def test_undo_puts_restored_files_back_when_a_later_write_fails(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "lc"
    live, candidate = _setup(tmp_path, {"a.txt": (b"a-bad", b"a-good"), "b.txt": (b"b-bad", b"b-good")})
    result = _promote(live, candidate, ("a.txt", "b.txt"), root)
    _flaky_replace(monkeypatch, {2})

    undone = undo_repair(result.record_id, root=root)

    assert undone.ok is False
    assert undone.reason == "restore_failed"
    assert (live / "a.txt").read_bytes() == b"a-good"
    assert (live / "b.txt").read_bytes() == b"b-good"
    record = load_repair(result.record_id, root=root)
    assert record is not None and record.status == "applied"
    from lemoncrow.pro.capabilities.verified_work.promote import _atomic_replace

    monkeypatch.setattr(repairs, "_atomic_replace", _atomic_replace)
    assert undo_repair(result.record_id, root=root).reason == "restored"


def test_undo_reports_files_it_could_not_put_back(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "lc"
    live, candidate = _setup(tmp_path, {"a.txt": (b"a-bad", b"a-good"), "b.txt": (b"b-bad", b"b-good")})
    result = _promote(live, candidate, ("a.txt", "b.txt"), root)
    _flaky_replace(monkeypatch, {2, 3})

    undone = undo_repair(result.record_id, root=root)

    assert undone.ok is False
    assert undone.reason == "restore_partial"
    assert undone.restored_paths == ("a.txt",)
    assert (live / "a.txt").read_bytes() == b"a-bad"
    assert (live / "b.txt").read_bytes() == b"b-good"
    record = load_repair(result.record_id, root=root)
    assert record is not None and record.status == "applied"


# --------------------------------------------------------------------------- #
# RC12-A: promotion and Undo share path locks across processes                #
# --------------------------------------------------------------------------- #

#: Another LemonCrow process: takes the store's path locks the way promotion and Undo do,
#: signals ``ready``, optionally waits for ``go`` and writes ``target``, then holds until
#: ``release`` exists (or releases at once when ``release`` is empty).
_HOLDER = """
import sys, time
from pathlib import Path
from lemoncrow.core.capabilities.workspace_locks import cross_process_path_locks

store, ready, go, target, payload, release, *paths = sys.argv[1:]


def wait(flag):
    deadline = time.monotonic() + 30
    while not Path(flag).exists():
        if time.monotonic() > deadline:
            sys.exit(3)
        time.sleep(0.01)


with cross_process_path_locks([Path(p) for p in paths], store_root=Path(store), timeout_s=30):
    Path(ready).write_text("held")
    if go:
        wait(go)
    if target:
        Path(target).write_bytes(bytes.fromhex(payload))
    if release:
        wait(release)
"""

#: Lock the same paths over and over in a seeded random order (deadlock probe).
_CHURN = """
import random, sys
from pathlib import Path
from lemoncrow.core.capabilities.workspace_locks import cross_process_path_locks

store, seed, rounds, *paths = sys.argv[1:]
rng = random.Random(int(seed))
for _ in range(int(rounds)):
    chosen = rng.sample(paths, k=rng.randint(1, len(paths)))
    with cross_process_path_locks([Path(p) for p in chosen], store_root=Path(store), timeout_s=30):
        pass
"""


class _OtherProcess:
    def __init__(self, tmp_path: Path, root: Path, paths: list[Path], *, target: Path | None = None) -> None:
        self.ready = tmp_path / "holder-ready"
        self.go = tmp_path / "holder-go"
        self.release = tmp_path / "holder-release"
        args = [str(root), str(self.ready)]
        args += [str(self.go), str(target), b"edited in another process\n".hex(), ""] if target else ["", "", "", ""]
        if target is None:
            args[-1] = str(self.release)
        self.process = subprocess.Popen([sys.executable, "-c", _HOLDER, *args, *map(str, paths)])
        deadline = time.monotonic() + 30
        while not self.ready.exists():
            assert self.process.poll() is None, "the other process exited before taking the locks"
            assert time.monotonic() < deadline, "the other process never took the locks"
            time.sleep(0.01)

    def finish(self) -> None:
        self.go.write_text("go")
        self.release.write_text("release")
        try:
            assert self.process.wait(timeout=30) == 0
        finally:
            if self.process.poll() is None:
                self.process.kill()


def test_undo_refuses_while_another_process_holds_the_path_locks(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    live, candidate = _setup(tmp_path, {"a.txt": (b"bad\n", b"good\n")})
    result = _promote(live, candidate, ("a.txt",), root)
    assert result.promoted is True

    other = _OtherProcess(tmp_path, root, [live / "a.txt"])
    try:
        held = undo_repair(result.record_id, root=root, lock_timeout_s=0.3)
    finally:
        other.finish()

    # Fail closed: nothing restored, the record still undoable.
    assert (held.ok, held.reason) == (False, "path_locked")
    assert (live / "a.txt").read_bytes() == b"good\n"
    record = load_repair(result.record_id, root=root)
    assert record is not None and record.status == "applied"
    assert undo_repair(result.record_id, root=root).reason == "restored"
    assert (live / "a.txt").read_bytes() == b"bad\n"


def test_promotion_refuses_while_another_process_holds_the_path_locks(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    live, candidate = _setup(tmp_path, {"a.txt": (b"bad\n", b"good\n")})

    other = _OtherProcess(tmp_path, root, [live / "a.txt"])
    try:
        held = _promote(live, candidate, ("a.txt",), root, lock_timeout_s=0.3)
    finally:
        other.finish()

    assert (held.promoted, held.reason) == (False, "path_locked")
    assert (live / "a.txt").read_bytes() == b"bad\n"
    assert list_repairs(root) == []


def test_undo_waits_out_another_process_and_reports_its_write_as_drift(tmp_path: Path) -> None:
    """The race the process-local locks let through: another process writes the
    repaired file while Undo is between its hash check and its restore."""

    import threading

    root = tmp_path / "lc"
    live, candidate = _setup(tmp_path, {"a.txt": (b"bad\n", b"good\n")})
    result = _promote(live, candidate, ("a.txt",), root)
    other = _OtherProcess(tmp_path, root, [live / "a.txt"], target=live / "a.txt")
    outcome: list[repairs.UndoResult] = []
    try:
        undo = threading.Thread(
            target=lambda: outcome.append(undo_repair(result.record_id, root=root, lock_timeout_s=20))
        )
        undo.start()
        time.sleep(0.5)  # Undo is now waiting on the other process's locks
        assert not outcome, outcome
    finally:
        other.finish()  # it writes its bytes, then releases
    undo.join(timeout=30)

    [undone] = outcome
    assert (undone.ok, undone.reason, undone.conflicts) == (False, "drift", ("a.txt",))
    assert (live / "a.txt").read_bytes() == b"edited in another process\n"
    record = load_repair(result.record_id, root=root)
    assert record is not None and record.status == "applied"


def _between_read_and_lock(monkeypatch, action) -> None:  # type: ignore[no-untyped-def]
    """Run *action* after Undo read the record and before it holds the path locks."""
    original = repairs.cross_process_path_locks

    def locks(*args, **kwargs):  # type: ignore[no-untyped-def]
        action()
        return original(*args, **kwargs)

    monkeypatch.setattr(repairs, "cross_process_path_locks", locks)


def test_undo_decides_from_the_record_as_committed_under_the_locks(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "lc"
    live = tmp_path / "live"
    live.mkdir()
    (live / "a.txt").write_bytes(b"before")
    journal = RepairJournal(_meta(live), root=root)
    record_id, _ = journal.begin({"a.txt": (b"before", 0o644)}, {"a.txt": b"after"})
    assert record_id is not None
    # The live verifier reformatted the file; commit records the observed hash.
    (live / "a.txt").write_bytes(b"after, formatted")
    _between_read_and_lock(monkeypatch, lambda: journal.commit(record_id, {"a.txt": _sha(b"after, formatted")}))

    undone = undo_repair(record_id, root=root)

    # The pending record's expected hash would call the formatted bytes drift.
    assert (undone.ok, undone.reason) == (True, "restored")
    assert (live / "a.txt").read_bytes() == b"before"


def test_undo_reports_an_undo_that_finished_while_it_waited(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "lc"
    live, candidate = _setup(tmp_path, {"a.txt": (b"bad\n", b"good\n")})
    result = _promote(live, candidate, ("a.txt",), root)

    def other_undo() -> None:
        (live / "a.txt").write_bytes(b"bad\n")
        repairs._mark_undone(root, result.record_id, "other")

    _between_read_and_lock(monkeypatch, other_undo)

    undone = undo_repair(result.record_id, root=root)

    assert (undone.ok, undone.reason) == (False, "already_undone")
    record = load_repair(result.record_id, root=root)
    assert record is not None and record.undone_by == "other"


def test_windows_lock_contention_waits_and_any_other_lock_error_fails_closed(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """``msvcrt.locking`` raises OSError both for a held byte and for real failures;
    only EACCES/EDEADLK mean "held" (Windows simulated: msvcrt and os.name patched)."""
    import errno
    import types

    from lemoncrow.core.capabilities import workspace_locks
    from lemoncrow.core.capabilities.workspace_locks import PathLockUnavailable, cross_process_path_locks

    raised: list[int | None] = [None]

    def locking(_fd: int, _mode: int, _nbytes: int) -> None:
        if raised[0] is not None:
            raise OSError(raised[0], os.strerror(raised[0]))

    monkeypatch.setitem(sys.modules, "msvcrt", types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking))
    original = workspace_locks._try_os_lock

    def as_windows(handle):  # type: ignore[no-untyped-def]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(workspace_locks.os, "name", "nt")
            return original(handle)

    with (tmp_path / "stripe.lock").open("a+b") as handle:
        assert as_windows(handle) is True
        for contended in (errno.EACCES, errno.EDEADLK):
            raised[0] = contended
            assert as_windows(handle) is False
        for failure in (errno.EBADF, errno.ENOLCK, errno.EIO):
            raised[0] = failure
            with pytest.raises(OSError):
                as_windows(handle)

    monkeypatch.setattr(workspace_locks, "_try_os_lock", as_windows)
    target = tmp_path / "a.txt"
    raised[0] = errno.EACCES
    with pytest.raises(PathLockUnavailable) as held:
        with cross_process_path_locks([target], store_root=tmp_path / "lc", timeout_s=0.05):
            pytest.fail("entered a held lock")
    assert held.value.reason == "path_locked"
    raised[0] = errno.EIO
    with pytest.raises(PathLockUnavailable) as failed:
        with cross_process_path_locks([target], store_root=tmp_path / "lc", timeout_s=5):
            pytest.fail("entered without a lock")
    assert failed.value.reason == "path_lock_failed"


def test_cross_process_path_locks_never_deadlock_across_processes(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    paths = [str(tmp_path / f"f{index}.txt") for index in range(12)]
    workers = [
        subprocess.Popen([sys.executable, "-c", _CHURN, str(root), str(seed), "150", *paths]) for seed in (1, 2, 3)
    ]
    try:
        assert [worker.wait(timeout=120) for worker in workers] == [0, 0, 0]
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()


def test_cross_process_path_lock_files_stay_bounded_and_fail_closed(tmp_path: Path) -> None:
    import threading

    from lemoncrow.core.capabilities.workspace_locks import (
        LOCK_DIR_NAME,
        LOCK_STRIPES,
        PathLockUnavailable,
        cross_process_path_locks,
        path_lock_stripe,
    )

    root = tmp_path / "lc"
    paths = [tmp_path / f"file-{index}.txt" for index in range(1000)]
    with cross_process_path_locks(paths, store_root=root):
        pass
    lock_files = list((root / LOCK_DIR_NAME).iterdir())
    assert len(lock_files) == len({path_lock_stripe(path) for path in paths}) <= LOCK_STRIPES

    # Another thread of this process past the deadline: refused, not waited on forever.
    taken = threading.Event()
    done = threading.Event()

    def hold() -> None:
        with cross_process_path_locks([paths[0]], store_root=root):
            taken.set()
            done.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert taken.wait(10)
        with pytest.raises(PathLockUnavailable) as locked:
            with cross_process_path_locks([paths[0]], store_root=root, timeout_s=0.1):
                pytest.fail("entered a held lock")
        assert locked.value.reason == "path_locked"
    finally:
        done.set()
        holder.join(10)

    # A store whose lock directory cannot be created refuses rather than writing unlocked.
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("")
    with pytest.raises(PathLockUnavailable) as failed:
        with cross_process_path_locks([paths[0]], store_root=blocked):
            pytest.fail("entered without a lock file")
    assert failed.value.reason == "path_lock_failed"

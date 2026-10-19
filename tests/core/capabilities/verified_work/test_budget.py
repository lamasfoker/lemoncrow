from __future__ import annotations

from pathlib import Path

from lemoncrow.core.capabilities.verified_work.settings import VerifiedWorkSettings
from lemoncrow.pro.capabilities.verified_work.budget import release_work, reserve_work


def test_budget_enforces_global_parallel_slot(tmp_path: Path) -> None:
    settings = VerifiedWorkSettings(max_parallel=1, max_repairs_session=5, max_seconds_session=300)
    first, reason = reserve_work("s1", settings, root=tmp_path)
    assert first is not None
    assert reason == "reserved"

    second, reason = reserve_work("s2", settings, root=tmp_path)
    assert second is None
    assert reason == "parallel_budget_exhausted"

    release_work(first, root=tmp_path)
    third, reason = reserve_work("s2", settings, root=tmp_path)
    assert third is not None
    assert reason == "reserved"
    release_work(third, root=tmp_path)


def test_budget_counts_repairs_per_session_across_reservations(tmp_path: Path) -> None:
    settings = VerifiedWorkSettings(max_parallel=1, max_repairs_session=2, max_seconds_session=300)
    for _ in range(2):
        reservation, reason = reserve_work("same", settings, root=tmp_path)
        assert reservation is not None, reason
        release_work(reservation, root=tmp_path)

    denied, reason = reserve_work("same", settings, root=tmp_path)
    assert denied is None
    assert reason == "session_repair_budget_exhausted"

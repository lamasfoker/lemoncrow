from __future__ import annotations

import os
from functools import partial
from pathlib import Path

import pytest
from lemoncrow_server_core import cli
from lemoncrow_server_core.state_lock import StateRootBusy, claim_state_root


def test_a_second_server_cannot_claim_a_served_state_directory(tmp_path: Path) -> None:
    # Two servers on one directory ran two index workers over the same SQLite
    # files; a restart of either then failed with "database is locked".
    first = claim_state_root(tmp_path, port=7420)
    try:
        with pytest.raises(StateRootBusy, match=f"pid={os.getpid()} port=7420"):
            claim_state_root(tmp_path, port=7430, wait_s=0.3)
    finally:
        first.close()
    # Released, as process exit releases it, the directory is claimable again.
    claim_state_root(tmp_path, port=7430, wait_s=0.3).close()


def test_up_refuses_a_state_directory_another_server_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "claim_state_root", partial(claim_state_root, wait_s=0.3))
    held = claim_state_root(tmp_path, port=7420)
    try:
        code = cli.main(["up", "--directory", str(tmp_path), "--port", "0", "--local-no-auth"])
    finally:
        held.close()
    assert code == 1
    assert "another LemonCrow server is serving" in capsys.readouterr().err

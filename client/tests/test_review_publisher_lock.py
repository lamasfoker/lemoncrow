"""P2b: exclusive foreground publication, crash recovery, source-currentness fences."""

from __future__ import annotations

import os
import subprocess
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from lemoncrow_client.errors import ClientError
from lemoncrow_client.review_delta import PublisherLock
from lemoncrow_client.review_watch import watch_main


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "src.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
    return root


def test_same_worktree_exclusive_and_reusable_lock(repo: Path, tmp_path: Path) -> None:
    first = PublisherLock(repo, tmp_path / "state")
    second = PublisherLock(repo, tmp_path / "state")
    with first:
        assert first.path.is_file()
        assert first.path.stat().st_mode & 0o077 == 0
        assert first.path.parent.stat().st_mode & 0o077 == 0
        with pytest.raises(ClientError, match="another Review publisher"):
            second.acquire()
    with second:
        assert second.path == first.path
    assert second.path.is_file()  # never unlink: inode-level flock identity


def test_cross_process_lock_enforced_then_crash_released(repo: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    script = (
        "from pathlib import Path\n"
        "import sys\n"
        "from lemoncrow_client.review_delta import PublisherLock\n"
        "with PublisherLock(Path(sys.argv[1]), Path(sys.argv[2])):\n"
        "    print('acquired', flush=True)\n"
    )
    lock = PublisherLock(repo, state)
    with lock:
        result = subprocess.run(
            [sys.executable, "-c", script, str(repo), str(state)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert result.returncode != 0
        assert "another Review publisher" in result.stderr
    successful = subprocess.run(
        [sys.executable, "-c", script, str(repo), str(state)],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert successful.stdout.strip() == "acquired"


@pytest.mark.parametrize("target", ["state", "lock"])
def test_untrusted_symlink_state_is_denied(repo: Path, tmp_path: Path, target: str) -> None:
    state = tmp_path / "state"
    real = tmp_path / "private"
    real.mkdir()
    if target == "state":
        os.symlink(real, state)
    else:
        lock = PublisherLock(repo, state)
        lock.path.parent.mkdir(mode=0o700, parents=True)
        os.symlink(real / "bogus", lock.path)
    with pytest.raises((ClientError, OSError)):
        PublisherLock(repo, state).acquire()


def test_watch_once_denies_second_publisher_before_capture(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lemoncrow_client.review_watch as module

    state = tmp_path / "state"
    config = SimpleNamespace(repo_root=repo, review_state_dir=state, hosted=False)
    monkeypatch.setattr(module, "load_config", lambda **_kwargs: config)
    calls: list[str] = []

    def capture(*_args: object, **_kwargs: object) -> tuple[dict[str, object], str]:
        calls.append("sent")
        return {}, "https://app.lemoncrow.com/r/one"

    monkeypatch.setattr(module, "_capture", capture)
    (repo / "src.py").write_text("x = 2\n", encoding="utf-8")
    with PublisherLock(repo, state):
        output = StringIO()
        assert watch_main(["--once"], sink=output) == 1
        assert "another Review publisher" in output.getvalue()
        assert calls == []
    assert watch_main(["--once"], sink=StringIO()) == 0
    assert calls == ["sent"]


def test_changed_during_http_capture_never_claims_current(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lemoncrow_client.review_watch as module

    state = tmp_path / "state"
    config = SimpleNamespace(repo_root=repo, review_state_dir=state, hosted=False)
    monkeypatch.setattr(module, "load_config", lambda **_kwargs: config)
    (repo / "src.py").write_text("x = 2\n", encoding="utf-8")

    def capture(*_args: object, **_kwargs: object) -> tuple[dict[str, object], str]:
        (repo / "src.py").write_text("x = 3\n", encoding="utf-8")
        return {}, "https://app.lemoncrow.com/r/stale"

    monkeypatch.setattr(module, "_capture", capture)
    output = StringIO()
    assert watch_main(["--once", "--json"], sink=output) == 1
    assert "worktree changed during Review publish" in output.getvalue()
    assert '"event": "captured"' not in output.getvalue()
    # A terminated writer releases its lock so the newer source can retry.
    with PublisherLock(repo, state):
        pass


def test_non_posix_lock_mode_refuses_capture_without_unsafe_fallback(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lemoncrow_client.review_delta as delta

    monkeypatch.setattr(delta, "fcntl", None)
    with pytest.raises(ClientError, match="POSIX publisher lock"):
        PublisherLock(repo, tmp_path / "state").acquire()

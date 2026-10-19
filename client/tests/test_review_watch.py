"""Standalone Review watch: content identity and state-machine qualification."""

from __future__ import annotations

import json
import os
import subprocess
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from lemoncrow_client.cli import main
from lemoncrow_client.errors import ClientError, ErrorCode
from lemoncrow_client.review import review_main
from lemoncrow_client.review_watch import ReviewWatch, SourceState, source_state, watch_main


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "review"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "<redacted-email>")
    git(root, "config", "user.name", "Watch Test")
    (root / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    (root / "source.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    return root


def test_identity_includes_content_changes_with_same_git_status(repo: Path) -> None:
    baseline = source_state(repo)
    assert not baseline.changed
    source = repo / "source.py"
    source.write_text("x = 2\n", encoding="utf-8")
    first = source_state(repo)
    assert first.changed and first != baseline
    source.write_text("x = 3\n", encoding="utf-8")
    second = source_state(repo)
    assert second.changed and second.fingerprint != first.fingerprint
    assert second == source_state(repo)


def test_untracked_and_ignored_and_internal_paths(repo: Path) -> None:
    baseline = source_state(repo)
    (repo / "ignored.log").write_text("secret\n", encoding="utf-8")
    (repo / ".lemoncrow").mkdir()
    (repo / ".lemoncrow" / "runtime.cache").write_text("cache\n", encoding="utf-8")
    assert source_state(repo) == baseline
    source = repo / "new.py"
    source.write_text("one\n", encoding="utf-8")
    first = source_state(repo)
    source.write_text("two\n", encoding="utf-8")
    assert source_state(repo).fingerprint != first.fingerprint
    source.unlink()
    assert source_state(repo) == baseline


def test_staged_identity_is_independent_of_unstaged_changes(repo: Path) -> None:
    source = repo / "source.py"
    source.write_text("x = 2\n", encoding="utf-8")
    git(repo, "add", "source.py")
    staged = source_state(repo, staged=True)
    assert staged.changed
    source.write_text("x = 3\n", encoding="utf-8")
    assert source_state(repo, staged=True) == staged
    assert source_state(repo).changed
    git(repo, "add", "source.py")
    assert source_state(repo, staged=True) != staged
    git(repo, "reset", "-q", "HEAD", "--", "source.py")
    assert not source_state(repo, staged=True).changed


def test_commits_and_branch_identity_advance_even_when_clean(repo: Path) -> None:
    first = source_state(repo)
    git(repo, "checkout", "-qb", "feature")
    branch = source_state(repo)
    assert not branch.changed and branch != first
    (repo / "source.py").write_text("x = 7\n", encoding="utf-8")
    git(repo, "add", "source.py")
    git(repo, "commit", "-qm", "change")
    commit = source_state(repo)
    assert not commit.changed and commit != branch


def test_deleted_file_and_symlink_do_not_follow_outside_content(repo: Path, tmp_path: Path) -> None:
    (repo / "source.py").unlink()
    removed = source_state(repo)
    assert removed.changed
    outside = tmp_path / "outside.txt"
    outside.write_text("secret one", encoding="utf-8")
    os.symlink(outside, repo / "link")
    state = source_state(repo)
    outside.write_text("secret two", encoding="utf-8")
    assert source_state(repo) == state  # symlink target content never read
    (repo / "link").unlink()
    assert source_state(repo) == removed


def test_settle_dedupe_and_revisions() -> None:
    watch = ReviewWatch(settle_s=2.0)
    calls: list[str] = []

    def capture() -> str:
        calls.append("capture")
        return "https://app.lemoncrow.com/r/example"

    clean = SourceState("headA", False)
    first = SourceState("changedA", True)
    second = SourceState("changedB", True)
    assert watch.consider(clean, 0, capture)[0] == "idle"
    assert watch.consider(first, 1, capture)[0] == "settling"
    assert watch.consider(first, 2.99, capture)[0] == "settling"
    assert watch.consider(first, 3.0, capture)[0] == "captured"
    assert watch.consider(first, 5.0, capture)[0] == "unchanged"
    assert watch.consider(second, 5.1, capture)[0] == "settling"
    assert watch.consider(second, 7.2, capture)[0] == "captured"
    assert watch.consider(clean, 8.0, capture)[0] == "settling"
    assert watch.consider(clean, 10.0, capture)[0] == "captured"  # post-commit clean
    assert len(calls) == 3


def test_retry_backoff_and_new_revision_bypasses_old_backoff() -> None:
    watch = ReviewWatch(settle_s=0)
    calls = 0

    def capture() -> str:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise ClientError(ErrorCode.PAYLOAD_INVALID, "temporary")
        return "https://app.lemoncrow.com/r/example"

    a = SourceState("a", True)
    b = SourceState("b", True)
    assert watch.consider(a, 1, capture)[0] == "failed"
    assert watch.consider(a, 2, capture)[0] == "backoff"
    assert watch.consider(b, 2, capture)[0] == "failed"
    assert watch.consider(b, 3, capture)[0] == "backoff"
    assert watch.consider(b, 6, capture)[0] == "captured"
    assert calls == 3


def test_one_shot_uses_existing_capture_without_runtime(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import lemoncrow_client.review_watch as watch_module

    config = SimpleNamespace(repo_root=repo, hosted=False)
    monkeypatch.setattr(watch_module, "load_config", lambda **kwargs: config)
    captures: list[object] = []

    def capture(*args: object, **kwargs: object) -> tuple[dict[str, object], str]:
        captures.append((args, kwargs))
        return {}, "https://app.lemoncrow.com/r/once"

    monkeypatch.setattr(watch_module, "_capture", capture)
    output = StringIO()
    assert review_main(["watch", "--once"], sink=output) == 0
    assert "No working-tree changes" in output.getvalue()
    (repo / "source.py").write_text("x = 5\n", encoding="utf-8")
    output = StringIO()
    assert review_main(["watch", "--once"], sink=output) == 0
    assert len(captures) == 1
    assert "https://app.lemoncrow.com/r/once" in output.getvalue()
    output = StringIO()
    assert review_main(["watch", "--once", "--json"], sink=output) == 0
    assert json.loads(output.getvalue())["event"] == "captured"
    assert len(captures) == 2
    assert main(["review", "watch", "--help"], stdout=StringIO()) == 0


def test_watch_options_and_hosted_signin(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import lemoncrow_client.review_watch as watch_module

    output = StringIO()
    assert watch_main(["--interval", "nan"], sink=output) == 2
    assert "between" in output.getvalue()
    output = StringIO()
    assert watch_main(["--unknown"], sink=output) == 2
    monkeypatch.setattr(
        watch_module, "load_config", lambda **kwargs: SimpleNamespace(repo_root=repo, hosted=True, authenticated=False)
    )
    output = StringIO()
    assert watch_main(["--once"], sink=output) == 1
    assert "lc auth login" in output.getvalue()


def test_one_shot_capture_over_real_loopback_http(
    config: object, worktree: Path, stub: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Authenticated View + Review capture, not an in-process fake _capture."""
    import lemoncrow_client.review_watch as watch_module

    # The stub has no branch-base lookup route. Tell the client the truth
    # instead of simulating an impossible full server capability.
    stub.state.review_manifest["capture_options"] = [
        option for option in stub.state.review_manifest["capture_options"] if option != "workspace_branch"
    ]
    monkeypatch.setattr(watch_module, "load_config", lambda **kwargs: config)
    (worktree / "pkg" / "alpha.py").write_text("def alpha():\n    return 'updated'\n", encoding="utf-8")
    output = StringIO()
    assert watch_main(["--once", "--json"], sink=output) == 0, output.getvalue()
    result = json.loads(output.getvalue())
    assert result["event"] == "captured"
    assert result["review_url"].startswith(config.url)
    assert len(stub.state.review_captures) == 1
    assert stub.state.review_captures[0]["derive_packet"] is True


@pytest.mark.parametrize("mode", ["local", "hosted"])
def test_one_watch_command_preserves_local_oss_and_unpaired_hosted_capture(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """A local Review server needs no account/pairing; hosted capture needs no machine pair."""
    import lemoncrow_client.review_companion_cli as companion
    import lemoncrow_client.review_watch as watch_module
    from lemoncrow_client.config import load_config

    config = load_config(
        env={
            "HOME": str(tmp_path),
            "LEMONCROW_INSTALL_MODE": mode,
            "LEMONCROW_URL": "http://127.0.0.1:7420" if mode == "local" else "https://app.lemoncrow.com",
            **({"LEMONCROW_TOKEN": "ordinary-hosted-account-token-12345"} if mode == "hosted" else {}),
        },
        cwd=repo,
        product="review",
    )
    assert config.hosted is (mode == "hosted")
    assert config.authenticated is (mode == "hosted")
    assert config.product == "review"
    monkeypatch.setattr(watch_module, "load_config", lambda **_kwargs: config)
    monkeypatch.setattr(
        companion,
        "HttpTransport",
        lambda *_args, **_kwargs: pytest.fail("unpaired/local watch must not use machine HTTP"),
    )
    captures: list[str] = []

    def capture(_config: object, _range: object, **_options: object) -> tuple[dict[str, object], str]:
        captures.append("captured")
        return {}, config.url + "/r/one"

    monkeypatch.setattr(watch_module, "_capture", capture)
    clean = StringIO()
    assert review_main(["watch", "--once"], sink=clean) == 0
    assert not captures and "No working-tree changes" in clean.getvalue()
    (repo / "source.py").write_text("x = 2\n", encoding="utf-8")
    dirty = StringIO()
    assert review_main(["watch", "--once"], sink=dirty) == 0
    assert captures == ["captured"]
    assert config.url + "/r/one" in dirty.getvalue()


def test_foreground_watch_publishes_json_events_and_stops_on_ctrl_c(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lemoncrow_client.review_watch as watch_module

    monkeypatch.setattr(watch_module, "load_config", lambda **kwargs: SimpleNamespace(repo_root=repo, hosted=False))
    # Each successful publish rechecks source identity. The same existing
    # three poll states are returned, plus stable post-publish observations.
    snapshots = iter(
        [
            SourceState("original", True),
            SourceState("original", True),
            SourceState("original", True),
            SourceState("next", True),
            SourceState("next", True),
        ]
    )
    monkeypatch.setattr(watch_module, "source_state", lambda *args, **kwargs: next(snapshots))
    tick = iter([0.0, 1.0, 2.0])
    # Replace only this module's clock: mutating time.monotonic globally lets
    # unrelated test/server machinery exhaust the deterministic tick iterator.
    clock = SimpleNamespace(monotonic=lambda: next(tick))
    calls: list[str] = []

    def captured(*args: object, **kwargs: object) -> tuple[dict[str, object], str]:
        calls.append("capture")
        return {}, f"https://app.lemoncrow.com/r/{len(calls)}"

    monkeypatch.setattr(watch_module, "_capture", captured)
    count = 0

    def stop_after_three(_interval: float) -> None:
        nonlocal count
        count += 1
        if count == 3:
            raise KeyboardInterrupt

    clock.sleep = stop_after_three
    monkeypatch.setattr(watch_module, "time", clock)
    output = StringIO()
    assert watch_main(["--json", "--settle", "0"], sink=output) == 0
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [event["event"] for event in events] == ["watch_started", "captured", "captured", "watch_stopped"], (
        events,
        calls,
    )
    assert len(calls) == 2
    assert events[1]["review_url"].endswith("/1")
    assert events[2]["review_url"].endswith("/2")

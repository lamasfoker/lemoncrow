from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from lemoncrow.gateway.cli.commands.admin import _install_attribution_hook

SCRIPT = Path("integrations/claude/plugin/scripts/install_attribution_hook.sh").resolve()
EMAIL = "302591943+lemoncrow-agent[bot]@users.noreply.github.com"
# Every variable the hook reads to find the committing session; scrubbed so the
# session running this test suite never leaks into a hook under test.
SESSION_ENVS = (
    "LEMONCROW_AGENT",
    "CLAUDE_CODE_SESSION_ID",
    "ANTIGRAVITY_SESSION_ID",
    "AGY_SESSION_ID",
    "CODEX_SESSION_ID",
    "LEMONCODE_SESSION_ID",
    "OPENCODE_SESSION_ID",
    "CURSOR_SESSION_ID",
    "HERMES_SESSION_ID",
    "GITHUB_COPILOT_SESSION_ID",
)


def _trailer(name: str) -> str:
    return f"Co-Authored-By: {name} <{EMAIL}>"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


def _env(tmp_path: Path, **session: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SESSION_ENVS}
    env["LEMONCROW_ROOT"] = str(tmp_path / "lc-root")
    env.update(session)
    return env


def _own_session(tmp_path: Path, host: str, session_id: str, **stats: str) -> None:
    path = tmp_path / "lc-root" / "sessions" / "2026" / "09" / "29" / host / session_id / "stats.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(stats, indent=2), encoding="utf-8")


def _commit(repo: Path, fname: str, env: dict[str, str], message: str = "msg") -> str:
    (repo / fname).write_text("x", encoding="utf-8")
    _git(repo, "add", "-A")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", message],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return _git(repo, "log", "-1", "--pretty=%B").stdout


def _install_script(repo: Path) -> None:
    result = subprocess.run(["bash", str(SCRIPT), str(repo)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _install_admin(repo: Path) -> None:
    assert _install_attribution_hook(repo) is not None


def _new_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


installers = pytest.mark.parametrize("install", [_install_script, _install_admin], ids=["script", "admin"])


def test_installer_adds_trailer_idempotently(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)

    # Installing twice must be idempotent (single managed block).
    for _ in range(2):
        _install_script(repo)
    hook_text = (repo / ".git" / "hooks" / "prepare-commit-msg").read_text(encoding="utf-8")
    assert hook_text.count("# >>> lemoncrow attribution >>>") == 1

    msg = _commit(repo, "a.txt", _env(tmp_path))
    assert msg.count(_trailer("lemoncrow")) == 1

    # A second commit also gets exactly one trailer (no duplication).
    msg2 = _commit(repo, "b.txt", _env(tmp_path))
    assert msg2.count(_trailer("lemoncrow")) == 1


@installers
def test_own_sessions_model_is_folded_into_the_co_author_name(tmp_path: Path, install) -> None:  # type: ignore[no-untyped-def]
    repo = _new_repo(tmp_path)
    _own_session(tmp_path, "claude", "sess-123", last_model="claude-sonnet-5")
    install(repo)

    msg = _commit(repo, "a.txt", _env(tmp_path, CLAUDE_CODE_SESSION_ID="sess-123"))

    assert msg.count(_trailer("lemoncrow+claude-sonnet-5")) == 1
    assert msg.count("Co-Authored-By:") == 1
    assert "sess-123" not in msg


@installers
def test_other_sessions_models_are_never_used(tmp_path: Path, install) -> None:  # type: ignore[no-untyped-def]
    repo = _new_repo(tmp_path)
    # The workspace-shared state holds whichever session wrote last, and another
    # live session has its own stats -- neither is the committing session.
    shared = repo / ".lemoncrow" / "workspace" / "session_state.json"
    shared.parent.mkdir(parents=True)
    shared.write_text(json.dumps({"session_id": "sess-123", "host": "claude", "model": "gpt-5.5"}), encoding="utf-8")
    _own_session(tmp_path, "claude", "sess-other", last_model="claude-opus-5")
    _own_session(tmp_path, "opencode", "sess-123", last_model="claude-haiku-5")
    install(repo)

    in_session = _commit(repo, "a.txt", _env(tmp_path, CLAUDE_CODE_SESSION_ID="sess-123"))
    no_session = _commit(repo, "b.txt", _env(tmp_path))

    assert in_session.count(_trailer("lemoncrow")) == 1
    assert no_session.count(_trailer("lemoncrow")) == 1


def test_agent_label_picks_the_session_it_names(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    # OpenCode launched from a Claude Code terminal inherits the outer window's id.
    _own_session(tmp_path, "claude", "outer", last_model="claude-opus-5")
    _own_session(tmp_path, "opencode", "inner", last_model="gpt-5.5")
    _install_script(repo)

    env = _env(tmp_path, CLAUDE_CODE_SESSION_ID="outer", OPENCODE_SESSION_ID="inner", LEMONCROW_AGENT="opencode")

    assert _commit(repo, "a.txt", env).count(_trailer("lemoncrow+gpt-5.5")) == 1


@installers
def test_unknown_model_falls_back_to_plain_name(tmp_path: Path, install) -> None:  # type: ignore[no-untyped-def]
    repo = _new_repo(tmp_path)
    _own_session(tmp_path, "claude", "sess-123")
    install(repo)

    assert _commit(repo, "a.txt", _env(tmp_path, CLAUDE_CODE_SESSION_ID="sess-123")).count(_trailer("lemoncrow")) == 1


@installers
def test_model_name_is_sanitised(tmp_path: Path, install) -> None:  # type: ignore[no-untyped-def]
    repo = _new_repo(tmp_path)
    _own_session(tmp_path, "claude", "sess-123", last_model="claude-opus-4-5[1m] <x>")
    install(repo)

    msg = _commit(repo, "a.txt", _env(tmp_path, CLAUDE_CODE_SESSION_ID="sess-123"))

    assert msg.count(_trailer("lemoncrow+claude-opus-4-5-1m---x")) == 1


@installers
def test_existing_bot_trailer_is_not_duplicated(tmp_path: Path, install) -> None:  # type: ignore[no-untyped-def]
    repo = _new_repo(tmp_path)
    _own_session(tmp_path, "claude", "sess-123", last_model="claude-sonnet-5")
    install(repo)

    env = _env(tmp_path, CLAUDE_CODE_SESSION_ID="sess-123")
    msg = _commit(repo, "a.txt", env, message=f"msg\n\n{_trailer('lemoncrow')}")

    assert msg.count(f"<{EMAIL}>") == 1


def test_installer_rejects_non_repo(tmp_path: Path) -> None:
    result = subprocess.run(["bash", str(SCRIPT), str(tmp_path)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "not a git repository" in result.stderr

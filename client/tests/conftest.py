"""Shared fixtures.

Everything runs against a real loopback socket -- the stub in ``_stub.py`` for
the unit tests and the real ``enterprise/server`` for the end-to-end suite --
because a mocked transport would not exercise the one thing this package is
mostly made of: bounded HTTP against exactly one origin.

No fixture reaches the network. The stub binds ``127.0.0.1:0``; nothing here
resolves a hostname or opens an outbound connection.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from _stub import TOKEN, StubServer, StubState
from lemoncrow_client.config import ClientConfig, load_config
from lemoncrow_client.session import RemoteSession

SAMPLE_FILES: Mapping[str, bytes] = {
    "pkg/__init__.py": b'"""Package root."""\n',
    "pkg/alpha.py": b"def alpha():\n    return 'alpha'\n",
    "pkg/beta.py": b"from pkg.alpha import alpha\n\n\ndef beta():\n    return alpha()\n",
    "docs/notes.md": b"Notes about the widget label.\n",
    "README.md": b"# sample\n",
}


def write_tree(root: Path, files: Mapping[str, bytes]) -> None:
    for relative, data in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


@pytest.fixture(autouse=True)
def _no_real_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never launch a desktop browser from tests; print the URL instead."""
    import webbrowser

    monkeypatch.setenv("LEMONCROW_NO_BROWSER", "1")

    def _print_url(url: str, *args: object, **kwargs: object) -> bool:
        print(f"[test] browser open suppressed: {url}")
        return True

    monkeypatch.setattr(webbrowser, "open", _print_url)


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A small repository-shaped directory, with a ``.git`` marker and ignores."""
    # Parametrized test names can exceed the per-environment-entry exec limit.
    monkeypatch_env = {key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"}
    root = tmp_path / "worktree"
    root.mkdir()
    write_tree(root, SAMPLE_FILES)
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=monkeypatch_env)
    (root / ".gitignore").write_text("build/\n*.log\n", encoding="utf-8")
    (root / "build").mkdir()
    (root / "build" / "artifact.o").write_bytes(b"\x00binary\x00")
    (root / "noisy.log").write_text("ignored\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, env=monkeypatch_env)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
        env=monkeypatch_env,
    )
    return root


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    target = tmp_path / "lemoncrow-home"
    target.mkdir()
    return target


@pytest.fixture
def stub() -> Iterator[StubServer]:
    with StubServer(StubState()) as server:
        yield server


def make_config(
    *,
    url: str,
    worktree: Path,
    state_dir: Path,
    token: str = TOKEN,
    **overrides: Any,
) -> ClientConfig:
    """Build a config directly, so a test never depends on the real environment."""
    environment = {
        "LEMONCROW_URL": url,
        # The unit-test stub intentionally models the credentialed hosted
        # protocol even though it binds loopback. Local no-auth is exercised by
        # the real-server tests instead of weakening this stub's auth checks.
        "LEMONCROW_INSTALL_MODE": "hosted",
        "LEMONCROW_TOKEN": token,
        "LEMONCROW_HOME": str(state_dir),
        "HOME": str(state_dir.parent),
        **{key: str(value) for key, value in overrides.items()},
    }
    if not token:
        environment.pop("LEMONCROW_TOKEN")
    return load_config(environment, cwd=worktree)


@pytest.fixture
def config(stub: StubServer, worktree: Path, state_dir: Path) -> ClientConfig:
    return make_config(url=stub.url, worktree=worktree, state_dir=state_dir)


@pytest.fixture
def session(config: ClientConfig) -> RemoteSession:
    return RemoteSession(config)


@pytest.fixture
def bootstrapped(session: RemoteSession) -> RemoteSession:
    report = session.bootstrap(deadline_s=10.0)
    assert report.ok, report.reason
    return session


@pytest.fixture(autouse=True)
def _no_ambient_lemoncrow_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop a developer's own shell from leaking into a test.

    A machine with ``LEMONCROW_URL`` exported would otherwise silently change
    which endpoint a test resolves -- and the whole suite is about which
    endpoint the client talks to.
    """
    for name in list(os.environ):
        if name.startswith("LEMONCROW_"):
            monkeypatch.delenv(name, raising=False)
    # No background sync threads or sockets unless a test asks for them.
    monkeypatch.setenv("LEMONCROW_AUTOSYNC", "0")

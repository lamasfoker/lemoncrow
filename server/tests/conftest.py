from __future__ import annotations

import os

import pytest

# Knobs a host session (Claude Code, the LemonCrow plugin) injects into every
# child process. Inherited by pytest they narrow the tool surface or change
# runtime behaviour for whoever launched the suite; tests of them set them.
_HOST_SESSION_KNOBS = (
    "CLAUDE_PROJECT_DIR",
    "LEMONCROW_MCP_TOOL_PROFILE",
    "LEMONCROW_TELEGRAPHIC",
    "LEMONCROW_HEADROOM_MCP_TAIL_MODE",
    "LEMONCROW_HEADROOM_TAIL_STATS",
    "LEMONCROW_VERIFY_SKIP_PATHS",
    "LEMONCROW_AUTO_UPDATE",
    "LEMONCROW_UPDATE_CHECK",
)


@pytest.fixture(autouse=True)
def _public_semantic_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the public server: never resolve the private semantic engine here.

    The public suite must mean the same thing whether or not the private
    distribution happens to be importable. Private semantic behaviour is
    tested beside its implementation (enterprise/server/tests/semantic).
    """
    from lemoncrow_server_core.index import semantic

    monkeypatch.setattr(semantic, "_private_semantic_module", lambda: None)


@pytest.fixture(autouse=True)
def _isolated_runtime_traces(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Never write runtime traces into the developer's ~/.lemoncrow."""
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path_factory.mktemp("lemoncrow-root")))
    for env_var in list(os.environ):
        if env_var.startswith("CLAUDE_CODE_") or env_var in _HOST_SESSION_KNOBS:
            monkeypatch.delenv(env_var, raising=False)

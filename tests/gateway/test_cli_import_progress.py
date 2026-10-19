"""Internal session-import diagnostics stay on stderr."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from lemoncrow.core.service.session_import import (
    _IMPORT_PROGRESS_LOGGER,
    _ensure_import_progress_logging,
    import_agent_sessions,
    main,
)
from lemoncrow.gateway.hosts.session_parsers.claude import ClaudeImporter
from tests.helpers import init_store_at


def _make_claude_fixture(tmp_path: Path) -> Path:
    """Create a minimal ~/.claude/projects-style dir with one session file."""
    projects = tmp_path / "projects"
    project_dir = projects / "-home-user-Projects-demo"
    project_dir.mkdir(parents=True)
    # A single (intentionally trivial) jsonl session — enough to trigger the
    # "discovering sessions (found N)" progress line on import_all.
    (project_dir / "00000000-0000-0000-0000-000000000001.jsonl").write_text(
        '{"type": "user", "message": {"role": "user", "content": "hi"}}\n',
        encoding="utf-8",
    )
    return projects


def test_import_progress_lands_on_stderr_not_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "store"
    init_store_at(str(root))
    sessions = _make_claude_fixture(tmp_path)
    monkeypatch.setattr(
        "lemoncrow.gateway.hosts.session_parsers.registry.iter_importer_classes",
        lambda: [
            ("claude", lambda store: SimpleNamespace(import_all=lambda: ClaudeImporter(store).import_all(sessions)))
        ],
    )
    _ensure_import_progress_logging()
    counts = import_agent_sessions(root)
    captured = capsys.readouterr()
    assert "claude: discovered sessions" in captured.err
    assert captured.out == ""
    assert "claude" in counts


def test_worker_prints_machine_readable_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["session_import", "--root", str(tmp_path)])
    monkeypatch.setattr(
        "lemoncrow.core.service.session_import.import_agent_sessions", lambda root: {"claude": 3, "codex": 1}
    )
    main()
    assert json.loads(capsys.readouterr().out) == {"claude": 3, "codex": 1}


def test_import_progress_handler_is_idempotent() -> None:
    progress_logger = logging.getLogger(_IMPORT_PROGRESS_LOGGER)

    def _flagged_handler_count() -> int:
        return sum(1 for h in progress_logger.handlers if getattr(h, "_lemoncrow_import_progress_handler", False))

    _ensure_import_progress_logging()
    after_first = _flagged_handler_count()
    assert after_first == 1

    # Repeat invocations must not accumulate duplicate handlers.
    _ensure_import_progress_logging()
    _ensure_import_progress_logging()
    assert _flagged_handler_count() == 1

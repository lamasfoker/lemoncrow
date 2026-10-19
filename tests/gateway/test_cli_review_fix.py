from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli

_REVIEW = "a" * 32
_REVISION = "b" * 32
_FIX = "c" * 32
_LINE = {
    "id": _FIX,
    "snapshot_number": 3,
    "status": "fixing",
    "worktree_path": "/work/repo/.lemoncrow/fixes/k7q2-s3",
    "branch": "lc-fix/k7q2-s3",
    "applied_files": ["app.py"],
}


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, Any]]:
    calls: list[tuple[str, str, Any]] = []
    config = SimpleNamespace(
        hosted=False,
        authenticated=True,
        url="http://127.0.0.1:9",
    )
    monkeypatch.setattr("lemoncrow_client.config.load_config", lambda **_kw: config)

    def request(
        _config: Any,
        method: str,
        path: str,
        *,
        body: Any = None,
    ) -> dict[str, Any]:
        calls.append((method, path, body))
        if path == "/api/reviews/k7q2":
            return {"session": {"id": _REVIEW}}
        if path == f"/api/reviews/{_REVIEW}/revisions":
            return {"revisions": [{"id": _REVISION, "revision_number": 3}]}
        if path.endswith("/fix-line"):
            return {"fix_line": _LINE, "created": True}
        if path.endswith("/fix-lines"):
            return {"fix_lines": [_LINE], "fix_of": None}
        if path.endswith("/apply"):
            markers = bool((body or {}).get("with_conflict_markers"))
            return {
                "applied": markers,
                "fix_line": {
                    **_LINE,
                    "status": ("applied_with_conflicts" if markers else "fixing"),
                },
                "plan": {
                    "conflicts": ["app.py"],
                    "files": [{"path": "app.py", "outcome": "conflict"}],
                    "warnings": [],
                },
            }
        raise AssertionError(path)

    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.review.hosted.server_review_request",
        request,
    )
    return calls


def _review(tmp_path: Path, *args: str):
    return CliRunner().invoke(cli, ["--root", str(tmp_path / "store"), "review", *args])


def test_fix_prints_worktree_path(tmp_path: Path, server: list[tuple[str, str, Any]]) -> None:
    result = _review(tmp_path, "--fix", "k7q2.3")

    assert result.exit_code == 0, result.output
    assert _LINE["worktree_path"] in result.output
    assert (
        "POST",
        f"/api/reviews/{_REVIEW}/snapshots/{_REVISION}/fix-line",
        {},
    ) in server


def test_apply_fix_stops_on_conflicts_without_markers(tmp_path: Path, server: list[tuple[str, str, Any]]) -> None:
    result = _review(tmp_path, "--apply-fix", "k7q2.3")

    assert result.exit_code == 1
    assert "nothing was written" in result.output
    assert "app.py" in result.output


def test_apply_fix_can_write_conflict_markers(tmp_path: Path, server: list[tuple[str, str, Any]]) -> None:
    result = _review(
        tmp_path,
        "--apply-fix",
        "k7q2.3",
        "--with-conflict-markers",
    )

    assert result.exit_code == 0, result.output
    assert "Applied fix to main" in result.output
    assert (
        "POST",
        f"/api/reviews/{_REVIEW}/fix-lines/{_FIX}/apply",
        {"with_conflict_markers": True},
    ) in server


def test_hosted_fix_fails_with_local_only_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = SimpleNamespace(hosted=True, authenticated=True)
    monkeypatch.setattr("lemoncrow_client.config.load_config", lambda **_kw: config)

    result = _review(tmp_path, "--fix", "k7q2.3")

    assert result.exit_code == 1
    assert "available for local Reviews only" in result.output

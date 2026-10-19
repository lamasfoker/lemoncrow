from __future__ import annotations

from pathlib import Path

from lemoncrow.gateway.tools import workspace


def test_workspace_path_preserves_absolute_paths(tmp_path: Path) -> None:
    absolute = tmp_path / "a.py"
    assert workspace.workspace_path(str(absolute)) == absolute

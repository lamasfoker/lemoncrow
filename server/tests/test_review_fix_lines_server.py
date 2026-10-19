from __future__ import annotations

import asyncio
import subprocess
from dataclasses import replace
from pathlib import Path

from lemoncrow_client.config import ClientConfig
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig

from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.hosted import (
    capture_server_review,
    server_review_request,
    server_review_rows,
)

_APP = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "app.py").write_text(_APP, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / "app.py").write_text(_APP.replace("return 1", "return 10"), encoding="utf-8")
    return repo


def _config(url: str, repo: Path, tmp_path: Path) -> ClientConfig:
    return ClientConfig(
        url=url,
        token="",
        repo_root=repo,
        state_dir=tmp_path / "client",
        install_mode="local",
        startup_budget_s=10.0,
        request_timeout_s=30.0,
        tool_timeout_s=60.0,
        resolution_cache_bytes=0,
        offer_local_fs=True,
        token_source="test",
    )


def _capture(config: ClientConfig, root: Path, tmp_path: Path) -> str:
    return capture_server_review(
        replace(config, repo_root=root),
        root,
        resolve_rev_range(root, working_tree=True),
        store_root=tmp_path / "client-store",
        session_id=None,
        limit=200,
        with_impact=False,
        with_provenance=False,
    ).review_id


def test_fix_line_end_to_end_on_local_server(tmp_path: Path) -> None:
    async def exercise() -> None:
        repo = _repo(tmp_path)
        server, _state = build_local_server(
            LocalServerConfig(
                listen_port=0,
                state_root=tmp_path / "state",
                local_no_auth=True,
                allow_local_fs=True,
            )
        )
        url = await server.start()
        config = _config(url, repo, tmp_path)

        def flow() -> None:
            parent_id = _capture(config, repo, tmp_path)
            history = server_review_request(config, "GET", f"/api/reviews/{parent_id}/revisions")
            latest = max(
                history["revisions"],
                key=lambda row: int(row["revision_number"]),
            )
            started = server_review_request(
                config,
                "POST",
                f"/api/reviews/{parent_id}/snapshots/{latest['id']}/fix-line",
                body={},
            )
            line = started["fix_line"]
            fix = Path(line["worktree_path"])

            (fix / "app.py").write_text(
                _APP.replace("return 1", "return 10").replace("return 2", "return 20"),
                encoding="utf-8",
            )
            _git(fix, "commit", "-qam", "fix b")
            (fix / "extra.txt").write_text("extra\n", encoding="utf-8")

            from lemoncrow.pro.capabilities.review.hosted import (
                _branch_bound_review,
                active_local_review_base,
            )

            assert _branch_bound_review(fix, line["branch"]) == line["child_review_id"]
            assert active_local_review_base(
                replace(config, repo_root=fix),
                repo_root=fix,
                store_root=tmp_path / "client-store",
                range_mode="working_tree",
            ) == (line["child_review_id"], line["base_commit"])

            child_id = _capture(config, fix, tmp_path)
            assert child_id == line["child_review_id"]
            child_history = server_review_request(config, "GET", f"/api/reviews/{child_id}/revisions")
            child_latest = max(
                child_history["revisions"],
                key=lambda row: int(row["revision_number"]),
            )
            assert child_latest["base_sha"] == line["base_commit"]

            rows = server_review_rows(config)
            assert all(row["id"] != child_id for row in rows)

            listed = server_review_request(config, "GET", f"/api/reviews/{parent_id}/fix-lines")
            [shown] = listed["fix_lines"]
            assert shown["child_review_id"] == child_id
            assert shown["snapshots"]

            result = server_review_request(
                config,
                "POST",
                f"/api/reviews/{parent_id}/fix-lines/{line['id']}/apply",
                body={},
            )
            assert result["applied"] is True
            assert result["fix_line"]["status"] == "applied"
            assert "return 20" in (repo / "app.py").read_text(encoding="utf-8")
            assert (repo / "extra.txt").read_text(encoding="utf-8") == "extra\n"
            assert not fix.exists()

        try:
            await asyncio.to_thread(flow)
        finally:
            await server.stop()

    asyncio.run(exercise())

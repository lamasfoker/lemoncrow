"""One agent session's files in a shared worktree review as their own Change."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from lemoncrow_client.config import ClientConfig
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig

from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.hosted import capture_server_review
from lemoncrow.pro.capabilities.review.live_runtime import load_live_review_binding


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_scoped_review_is_its_own_change_and_never_takes_over_the_branch(tmp_path: Path) -> None:
    asyncio.run(_exercise(tmp_path))


async def _exercise(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    for name in ("a.py", "b.py"):
        (repo / name).write_text(f"def {name[0]}():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    (repo / "a.py").write_text("def a():\n    return 2\n", encoding="utf-8")
    (repo / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")

    server, state = build_local_server(
        LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=True)
    )
    url = await server.start()
    config = ClientConfig(
        url=url,
        token="",
        repo_root=repo,
        state_dir=tmp_path / "client",
        install_mode="local",
        startup_budget_s=10.0,
        request_timeout_s=10.0,
        tool_timeout_s=30.0,
        resolution_cache_bytes=0,
        offer_local_fs=True,
        token_source="test",
    )

    def capture(**scope: object) -> tuple[str, int, list[str]]:
        captured = capture_server_review(
            config,
            repo,
            resolve_rev_range(repo, working_tree=True),
            store_root=tmp_path / "client-store",
            session_id=None,
            limit=50,
            with_impact=False,
            with_provenance=False,
            **scope,  # type: ignore[arg-type]
        )
        revision = captured.response["revision"]
        return (
            captured.review_id,
            int(revision["revision_number"]),
            sorted(item.path for item in captured.build.packet.files),
        )

    try:
        branch_review, branch_revision, branch_files = await asyncio.to_thread(capture)
        assert branch_files == ["a.py", "b.py"]

        scope = {"paths": ("a.py",), "review_scope": "session:claude:s1"}
        scoped_review, scoped_revision, scoped_files = await asyncio.to_thread(capture, **scope)
        assert scoped_review != branch_review
        assert scoped_files == ["a.py"]
        # Nor does it become the worktree's warm pointer, which plain `lc review` opens.
        live = load_live_review_binding(tmp_path / "client-store", repo, server_url=config.url)
        assert live is not None and live.review_id == branch_review

        # The branch capture still continues the branch's Change.
        (repo / "b.py").write_text("def b():\n    return 3\n", encoding="utf-8")
        again_review, again_revision, again_files = await asyncio.to_thread(capture)
        assert again_review == branch_review
        assert again_revision == branch_revision + 1
        assert again_files == ["a.py", "b.py"]

        # Later edits by the same session advance its own Change.
        (repo / "a.py").write_text("def a():\n    return 4\n", encoding="utf-8")
        next_review, next_revision, next_files = await asyncio.to_thread(capture, **scope)
        assert next_review == scoped_review
        assert next_revision == scoped_revision + 1
        assert next_files == ["a.py"]

        # `/code` reads relationships from the View this worktree keeps in sync.
        provider = state._code_intelligence_for_root(repo)
        assert provider is not None
        assert provider.readiness().backend == "native"
        assert state._code_intelligence_for_root(tmp_path / "elsewhere") is None
    finally:
        await server.stop()

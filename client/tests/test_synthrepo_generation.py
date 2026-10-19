from __future__ import annotations

from _synthrepo import generate_repository
from lemoncrow_client.generation import plan_repository_generation
from lemoncrow_client.manifest import walk_worktree


def test_synthetic_scale_repository_is_a_real_clean_git_generation(tmp_path) -> None:
    synthetic = generate_repository(tmp_path / "repo", files=12)
    walk = walk_worktree(synthetic.root, size_cap=1024 * 1024, max_files=100)
    generation = plan_repository_generation(
        synthetic.root,
        walk.entries(),
        content_size_cap=1024 * 1024,
    )

    assert generation.base_revision != "0" * 40
    assert len(generation.base_entries) == synthetic.files
    assert generation.overlay_entries == ()
    assert generation.removed_paths == ()

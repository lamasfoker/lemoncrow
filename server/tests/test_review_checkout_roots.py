from __future__ import annotations

from pathlib import Path

from lemoncrow_server_core.local_reviews import LocalReviewReader, LocalReviewRepository

from lemoncrow.pro.capabilities.review.session_models import ReviewSession

_REPO = "0123456789abcdef0123456789abcdef"


def _review(repository: LocalReviewRepository) -> ReviewSession:
    return repository.store(_REPO).create_session(
        ReviewSession(
            id="",
            subject_type="local_change",
            repo_root=f"local:{_REPO}",
            range_mode="working_tree",
        )
    )


def test_each_review_reads_its_own_checkout(tmp_path: Path) -> None:
    repository = LocalReviewRepository(tmp_path / "state")
    main = tmp_path / "main"
    fix = main / ".lemoncrow" / "fixes" / "k7q2-s3"
    fix.mkdir(parents=True)
    parent, child = _review(repository), _review(repository)
    repository.set_checkout_root(_REPO, main)
    repository.set_review_checkout_root(_REPO, parent.id, main)
    repository.set_review_checkout_root(_REPO, child.id, fix)

    assert repository.source_root_for(_REPO, parent.id) == main.resolve()
    assert repository.source_root_for(_REPO, child.id) == fix.resolve()
    unmarked = _review(repository)
    assert repository.source_root_for(_REPO, unmarked.id) == main.resolve()


def test_fix_worktree_never_replaces_repository_checkout(tmp_path: Path) -> None:
    repository = LocalReviewRepository(tmp_path / "state")
    main = tmp_path / "main"
    main.mkdir()
    fix = main / ".lemoncrow" / "fixes" / "k7q2-s3"
    fix.mkdir(parents=True)
    repository.set_checkout_root(_REPO, main)

    repository.set_checkout_root(_REPO, fix)

    assert repository.checkout_root(_REPO) == main.resolve()


def test_removed_review_checkout_never_falls_back_to_another(tmp_path: Path) -> None:
    repository = LocalReviewRepository(tmp_path / "state")
    main = tmp_path / "main"
    main.mkdir()
    fix = main / ".lemoncrow" / "fixes" / "k7q2-s3"
    fix.mkdir(parents=True)
    child = _review(repository)
    repository.set_checkout_root(_REPO, main)
    repository.set_review_checkout_root(_REPO, child.id, fix)
    fix.rmdir()

    assert repository.source_root_for(_REPO, child.id) is None


def test_resolution_names_the_review(tmp_path: Path) -> None:
    repository = LocalReviewRepository(tmp_path / "state")
    review = _review(repository)
    reader = LocalReviewReader(repository, listen_port=7420)

    assert reader.resolve(f"/api/reviews/{review.id}").review_id == review.id

from __future__ import annotations

from pathlib import Path

from lemoncrow_server_core.local_reviews import LocalReviewReader, LocalReviewRepository, _ResolvedReader


def test_reader_app_is_route_cache_ready_before_it_leaves_the_app_lock(tmp_path: Path) -> None:
    """The first parallel Reader requests must never build FastAPI route caches."""

    repository = LocalReviewRepository(tmp_path / "reviews")
    store = repository.store("repo_fixture")
    reader = LocalReviewReader(repository, listen_port=7420)

    app = reader._app(_ResolvedReader("repo_fixture", store))
    included = [route for route in app.routes if callable(getattr(route, "effective_candidates", None))]
    assert included, "FastAPI included-router cache shape changed; revisit the Reader compatibility guard"

    for route in included:
        assert route._effective_candidates_version == route.original_router._get_routes_version()
        assert route._effective_low_priority_routes_version == route.original_router._get_routes_version()

    paths = {
        str(getattr(getattr(candidate, "original_route", None), "path", ""))
        for route in included
        for candidate in route.effective_candidates()
    }
    assert "/api/reviews/{review_id}/revisions/{revision_number}/publish" in paths

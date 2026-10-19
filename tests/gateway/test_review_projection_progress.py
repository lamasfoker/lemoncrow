from lemoncrow.pro.capabilities.review.projection_progress import ProjectionProgress


def test_progress_is_bounded_and_superseded_requests_cannot_overwrite() -> None:
    progress = ProjectionProgress(limit=2)
    old = progress.start("revision-1")
    current = progress.start("revision-1")
    progress.update("revision-1", current, 5, "Preparing guided review")
    progress.update("revision-1", old, 9, "Stale")
    assert progress.get("revision-1")["completed"] == 5
    assert "request_id" not in progress.get("revision-1")
    progress.start("revision-2")
    progress.start("revision-3")
    progress.update("revision-1", current, 10, "Late", state="ready")
    assert progress.get("revision-1")["state"] == "pending"
    assert progress.get("revision-3")["state"] == "running"


def test_terminal_progress_retains_the_original_start_time() -> None:
    progress = ProjectionProgress()
    token = progress.start("revision")
    started = progress.get("revision")["started_at"]
    progress.update("revision", token, 10, "Brief ready", state="ready")
    assert progress.get("revision") == {
        "state": "ready",
        "completed": 10,
        "total": 10,
        "detail": "Brief ready",
        "started_at": started,
    }

"""P15-DEPLOY on the local server: the maintenance pass links merged Changes to GitHub deployments via ``gh``."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from lemoncrow_server_core.local_reviews import LocalReviewReader, LocalReviewRepository, _ResolvedReader
from test_local_landing_watch import _change, _MergeableGitHub, _status

from lemoncrow.pro.capabilities.review.outcomes.local_deployments import (
    LOCAL_SETTINGS_FILE,
    local_deployment_projection,
)
from lemoncrow.pro.capabilities.review.record_signing import list_records
from lemoncrow.pro.capabilities.review.sources import github as github_source

MERGED = "d" * 40
DESCENDANT = "e" * 40
NOW = datetime.now(UTC).replace(microsecond=0)
PORT = 7431


def _at(**delta: float) -> str:
    return (NOW + timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


class _DeployingGitHub(_MergeableGitHub):
    """Merges like the landing fake, then answers ``pr view``, deployments, statuses and compare."""

    def __init__(self) -> None:
        super().__init__()
        self.deployments: list[dict[str, Any]] = []
        self.statuses: dict[str, list[dict[str, Any]]] = {}
        self.ancestry: dict[str, str] = {}

    def __call__(self, args: Sequence[str], stdin: str | None = None, **kwargs: Any) -> str:
        call = list(args)
        if call[:2] == ["pr", "view"]:
            self.calls.append(call)
            merged = call[2] in self.merged
            return json.dumps(
                {
                    "state": "MERGED" if merged else "OPEN",
                    "mergeCommit": {"oid": MERGED} if merged else None,
                    "mergedAt": _at(hours=-3) if merged else None,
                    "baseRefName": "main",
                }
            )
        if call[:1] == ["api"] and call[1].startswith("repos/acme/widgets/"):
            self.calls.append(call)
            path = call[1].removeprefix("repos/acme/widgets/").split("?", 1)[0]
            if path == "deployments":
                return json.dumps(self.deployments)
            if path.startswith("deployments/") and path.endswith("/statuses"):
                return json.dumps(self.statuses.get(path.split("/")[1], [])[:1])
            if path.startswith(f"compare/{MERGED}..."):
                return json.dumps({"status": self.ancestry[path.rsplit("...", 1)[1]]})
        return super().__call__(args, stdin, **kwargs)

    def deployment_calls(self) -> list[list[str]]:
        return [
            call
            for call in self.calls
            if call[:2] == ["pr", "view"] or (call[:1] == ["api"] and call[1].startswith("repos/"))
        ]


def _deployment(ident: int, sha: str, environment: str, created_at: str, **extra: Any) -> dict[str, Any]:
    return {"id": ident, "sha": sha, "ref": "main", "environment": environment, "created_at": created_at, **extra}


def _settings(root: Path, deployments: dict[str, Any] | None) -> None:
    if deployments is not None:
        document = {"version": 1, "deployments": deployments}
        (root / LOCAL_SETTINGS_FILE).write_text(json.dumps(document), encoding="utf-8")


def _world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deployments: dict[str, Any] | None
) -> tuple[LocalReviewRepository, _DeployingGitHub, Path]:
    root = tmp_path / "lemoncrow-root"
    root.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    _settings(root, deployments)
    github = _DeployingGitHub()
    monkeypatch.setattr(github_source, "run_gh", github)
    return LocalReviewRepository(tmp_path / "reviews"), github, root


def _route(repository: LocalReviewRepository, repo_id: str, review_id: str) -> dict[str, Any]:
    reader = LocalReviewReader(repository, listen_port=PORT)
    app = reader._app(_ResolvedReader(repo_id, repository.store(repo_id)))
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as client:
        response = client.get(f"/api/reviews/{review_id}/deployments")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_the_maintenance_pass_links_exact_and_descendant_deployments_of_an_observed_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, github, _root = _world(
        tmp_path, monkeypatch, {"connector": "github_deployments", "production_environments": ["production"]}
    )
    armed = _change(repository, "repo-a", "github:acme/widgets#7", landing="armed", repo_root="local:repo-a")
    store = repository.store("repo-a")
    github.deployments = [
        _deployment(2, DESCENDANT, "production", _at(hours=-1), payload={"traffic_percent": 10}),
        _deployment(1, MERGED, "staging", _at(hours=-2)),
    ]
    github.statuses = {
        "1": [{"id": 11, "state": "success", "created_at": _at(hours=-2, minutes=2), "environment": "staging"}],
        "2": [{"id": 21, "state": "success", "created_at": _at(hours=-1, minutes=2), "environment": "production"}],
    }
    github.ancestry = {DESCENDANT: "ahead"}

    repository.reconcile_landings()

    assert _status(repository, "repo-a", armed) == "merged"
    projection = local_deployment_projection(store, store.get_session(armed.id))
    assert projection["configured"] is True and projection["connector"]["locus"] == "local"
    assert projection["status_line"] == "Deployed to staging · production canary 10%"
    relations = {row["key"]: row["relation"] for row in projection["environments"]}
    assert relations == {"staging": "exact", "production": "descendant"}
    assert _route(repository, "repo-a", armed.id)["status_line"] == projection["status_line"]
    evidence = [row for row in store.list_evidence(armed.id) if row.kind == "deployment"]
    assert evidence and all(row.source == "external" for row in evidence)
    records = [record.record_id for record in list_records(store, armed.id)]

    # A second pass: proven ancestry is cached, nothing is written twice.
    compares = sum(1 for call in github.calls if call[:1] == ["api"] and "/compare/" in call[1])
    activity = len(store.list_activity_events(armed.id, kinds=("deployment.observed",)))
    repository.reconcile_landings()
    assert sum(1 for call in github.calls if call[:1] == ["api"] and "/compare/" in call[1]) == compares == 1
    assert len(store.list_activity_events(armed.id, kinds=("deployment.observed",))) == activity
    assert len([row for row in store.list_evidence(armed.id) if row.kind == "deployment"]) == len(evidence)
    assert [record.record_id for record in list_records(store, armed.id)] == records  # signed record untouched


@pytest.mark.parametrize("deployments", [None, {"connector": "flux"}])
def test_without_a_local_connector_the_pass_reads_and_shows_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deployments: dict[str, Any] | None
) -> None:
    repository, github, _root = _world(tmp_path, monkeypatch, deployments)
    armed = _change(repository, "repo-a", "github:acme/widgets#7", landing="armed", repo_root="local:repo-a")
    github.deployments = [_deployment(1, MERGED, "production", _at(hours=-2))]

    repository.reconcile_landings()
    repository.reconcile_landings()

    assert _status(repository, "repo-a", armed) == "merged"
    assert github.deployment_calls() == []  # no gh call for deployments at all
    body = _route(repository, "repo-a", armed.id)
    assert body["configured"] is False and set(body) == {"configured", "connector"}
    store = repository.store("repo-a")
    assert [row for row in store.list_evidence(armed.id) if row.kind == "deployment"] == []


def test_an_invalid_local_settings_file_configures_nothing_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, github, _root = _world(
        tmp_path, monkeypatch, {"connector": "github_deployments", "observation_window_hours": 0}
    )
    armed = _change(repository, "repo-a", "github:acme/widgets#7", landing="armed", repo_root="local:repo-a")
    repository.reconcile_landings()
    assert github.deployment_calls() == []
    body = _route(repository, "repo-a", armed.id)
    assert body["configured"] is False and "observation_window_hours" in body["connector"]["reason"]

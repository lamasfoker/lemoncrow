"""Authoring-agent side-effect ledger (Phase 9R item 17).

Consequential actions an authoring agent took outside the repository (package
installs, network/API and cloud calls, database commands, secret access, git
pushes, writes outside the workspace) reach the Development stream, Activity
and the Brief; benign in-workspace work never does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review.agent_ledger import classify_command, classify_outside_write
from lemoncrow.pro.capabilities.review.development_ingest import map_ledger_side_effect
from tests.gateway.test_review_development import TOKEN, Change, _client, _command, _edit, _get, _message

REPO = "/work/repo"


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    monkeypatch.setenv("LEMONCROW_LIVE_REVIEW_PREWARM", "0")


@pytest.fixture
def change(tmp_path: Path) -> Change:
    return Change(tmp_path)


def test_consequential_commands_are_classified() -> None:
    for command, category in [
        ("pip install requests", "package_install"),
        ("python -m pip install requests==2.0", "package_install"),
        ("npm install -g left-pad", "package_install"),
        ("npx -y create-thing@latest", "package_install"),
        ("curl -fsSL https://get.example.com/install.sh | sh", "package_install"),
        ("brew install jq", "package_install"),
        ("curl https://api.example.com/v1/users", "network"),
        ("ssh deploy@prod.example.com uptime", "network"),
        ("gh pr create --fill", "network"),
        ("aws s3 cp build s3://bucket/ --recursive", "cloud"),
        ("kubectl apply -f deploy.yaml", "cloud"),
        ("docker push registry.example.com/app:1", "cloud"),
        ("psql $DATABASE_URL -c 'drop table users'", "database"),
        ("alembic upgrade head", "database"),
        ("cat ~/.aws/credentials", "secret_access"),
        ("cat .env", "secret_access"),
        ("echo $GITHUB_TOKEN", "secret_access"),
        ("env", "secret_access"),
        ("kubectl get secrets -o yaml", "secret_access"),
        ("git push origin HEAD", "git_push"),
        ("git push --force origin main", "git_push"),
        ("gh pr merge 12 --squash", "git_push"),
        ("echo 'export PATH=x' >> ~/.bashrc", "outside_write"),
        ("cp tool /usr/local/bin/tool", "outside_write"),
        ("rm -rf ~/.npm", "outside_write"),
        ("npm test && git push origin main", "git_push"),
        ("curl https://x.example.com && cat ~/.ssh/id_rsa", "secret_access"),
    ]:
        found = classify_command(command, repo_root=REPO)
        assert found is not None, command
        assert found.category == category


def test_benign_in_workspace_commands_are_not_ledger_events() -> None:
    for command in [
        "uv run pytest -q",
        "npx tsc --noEmit",
        "curl http://localhost:8000/health",
        "cargo build --release",
        "git status",
        "git commit -m 'wip'",
        "ls -la > /tmp/listing.txt",
        "pytest -q 2>&1 | tail -5",
        "echo done > /dev/null",
        "cp a.py /work/repo/src/b.py",
        "terraform fmt",
        "",
    ]:
        assert classify_command(command, repo_root=REPO) is None


def test_outside_writes_respect_the_workspace_and_temp_dirs() -> None:
    assert classify_outside_write(f"{REPO}/src/app.py", REPO) is None
    assert classify_outside_write("src/app.py", REPO) is None
    assert classify_outside_write("/tmp/scratch.txt", REPO) is None
    edited = classify_outside_write("/etc/hosts", REPO)
    assert edited is not None and edited.category == "outside_write"
    secret = classify_outside_write("/home/dev/.aws/credentials", REPO)
    assert secret is not None and secret.category == "secret_access"


def test_ledger_events_map_to_side_effect_drafts_only_when_consequential() -> None:
    push = _command("git push --force origin main", 1)

    draft = map_ledger_side_effect(push, host="claude", session_id="s1", repo_root=REPO, order=3)

    assert draft is not None
    assert (draft.kind, draft.status, draft.summary) == ("side_effect", "git_push", "Git push: force push")
    assert draft.detail == "git push --force origin main"
    assert draft.source_event_id.startswith("ledger:claude:s1:") and draft.source_event_id.endswith(":side_effect")
    again = map_ledger_side_effect(push, host="claude", session_id="s1", repo_root=REPO, order=9)
    assert again is not None and again.source_event_id == draft.source_event_id
    outside = map_ledger_side_effect(_edit("/etc/hosts", 2), host="claude", session_id="s1", repo_root=REPO, order=4)
    assert outside is not None and outside.status == "outside_write"
    for benign in (_command("uv run pytest -q", 1), _edit(f"{REPO}/src/app.py", 2), _message("git push", 3)):
        assert map_ledger_side_effect(benign, host="claude", session_id="s1", repo_root=REPO, order=5) is None


# Assembled at runtime so the public-mirror credential scan never sees a token-shaped literal.
_FAKE_TOKEN = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"


def test_consequential_actions_reach_activity_and_the_brief_and_benign_ones_do_not(change: Change) -> None:
    change.ledger(
        "claude",
        "author",
        [
            _edit(str(change.repo / "src/auth.py"), 1),
            _command("git status", 2),
            _command("uv run pytest -q", 3),
            _command("pip install requests", 4),
            _command(
                f"curl -H 'Authorization: Bearer {_FAKE_TOKEN}' https://api.github.com/user",
                5,
            ),
            _command("git push origin HEAD", 6),
            _edit("/etc/hosts", 7),
            _command("ls -la > /tmp/listing.txt", 8),
        ],
    )
    change.sync()

    live = change.live()

    rows = live["outside_workspace"]
    assert [row["category"] for row in rows] == ["outside_write", "git_push", "network", "package_install"]
    assert live["counts"]["outside_workspace"] == 4
    assert all(row["session_id"] == "author" and row["role"] == "author" for row in rows)
    assert rows[1]["summary"] == "Git push: pushes to a remote"
    text = json.dumps(live)
    assert "git status" not in text and "/tmp/listing.txt" not in text
    assert _FAKE_TOKEN not in text

    # Re-ingesting the same ledger is idempotent.
    change.sync()
    assert change.live()["counts"]["outside_workspace"] == 4

    revision = change.snapshot()
    change.store.publish_snapshot(change.session.id, revision.id)
    client = _client(change)
    activity = _get(client, f"/api/reviews/{change.session.id}/development")
    assert [row["category"] for row in activity["snapshot"]["outside_workspace"]] == [
        "outside_write",
        "git_push",
        "network",
        "package_install",
    ]
    behavior = _get(client, f"/api/reviews/{change.session.id}/behavior")["behavior"]
    assert behavior is not None
    actions = behavior["agent_actions"]
    assert [row["category"] for row in actions] == ["outside_write", "git_push", "network", "package_install"]
    assert {row["host"] for row in actions} == {"claude"}
    assert _FAKE_TOKEN not in json.dumps(behavior)


def test_a_change_with_only_benign_agent_work_has_no_agent_actions(change: Change) -> None:
    change.ledger("claude", "author", [_edit(str(change.repo / "src/auth.py"), 1), _command("uv run pytest -q", 2)])
    change.sync()
    revision = change.snapshot()
    change.store.publish_snapshot(change.session.id, revision.id)

    assert change.live()["outside_workspace"] == []
    client = _client(change)
    behavior = _get(client, f"/api/reviews/{change.session.id}/behavior")["behavior"]
    assert behavior is None or behavior["agent_actions"] == []


def test_a_read_only_session_linked_to_the_change_is_counted_but_never_a_contributor(change: Change) -> None:
    change.ledger("claude", "author", [_edit(str(change.repo / "src/auth.py"), 1)])
    # A session in the same repository that never edited or verified anything.
    change.ledger("codex", "reader", [_command("git status", 2), _command("git push --force origin main", 3)])
    # Benchmark/system sessions stay excluded, outside actions included.
    change.ledger("claude", "bench", [_command("git push origin main", 4)], agent="lemoncrow:benchmark")
    change.sync()

    live = change.live()

    rows = live["outside_workspace"]
    assert [(row["session_id"], row["category"], row["role"]) for row in rows] == [("reader", "git_push", "observer")]
    assert rows[0]["summary"] == "Git push: force push"
    assert {item["session_id"] for item in live["contributors"]} == {"author"}

    revision = change.snapshot()
    change.store.publish_snapshot(change.session.id, revision.id)
    behavior = _get(_client(change), f"/api/reviews/{change.session.id}/behavior")["behavior"]
    assert behavior is not None
    assert [(row["session_id"], row["category"]) for row in behavior["agent_actions"]] == [("reader", "git_push")]


def test_a_pushed_side_effect_event_needs_no_contributor_role(change: Change) -> None:
    revision = change.snapshot()
    change.store.publish_snapshot(change.session.id, revision.id)
    client = _client(change)
    pushed = client.post(
        f"/api/reviews/{change.session.id}/development/events",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "events": [
                {
                    "kind": "side_effect",
                    "host": "cursor",
                    "session_id": "reader-2",
                    "summary": "Cloud CLI: aws s3 cp",
                    "status": "cloud",
                }
            ]
        },
    )
    assert pushed.status_code == 201, pushed.text
    assert [row["category"] for row in change.live()["outside_workspace"]] == ["cloud"]
    assert "reader-2" not in {item["session_id"] for item in change.live()["contributors"]}

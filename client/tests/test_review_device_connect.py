"""One-use standalone Review Companion pairing and owner-only local persistence."""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from lemoncrow_client.review import review_main
from lemoncrow_client.review_device_connect import connect_main, load_pairing, save_pairing
from lemoncrow_client.transport import Response


def response() -> dict[str, Any]:
    return {
        "org_id": "org-1",
        "repo_id": "repo-1",
        "worker_id": "machine-1",
        "machine_token": "lcrm.auth.scoped.secret-at-least-20-chars-12345",
        "kind": "laptop",
        "execution_classes": ["runtime_validation"],
        "expires_at": time.time() + 1800,
        "worker_signing_keys": [
            {
                "key_id": "key-1",
                "algorithm": "Ed25519",
                "status": "active",
                "public_key_hex": "ab" * 32,
            }
        ],
    }


def test_owner_only_local_device_record_and_status(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state" / "review"
    document = response()
    path = save_pairing(repo, state, document, "https://app.lemoncrow.com")
    assert path.is_file() and path.stat().st_mode & 0o077 == 0
    assert path.parent.stat().st_mode & 0o077 == 0
    stored = load_pairing(repo, state)
    assert stored is not None
    assert stored["worker_id"] == document["worker_id"]
    assert stored["machine_token"] == document["machine_token"]
    with pytest.raises(ValueError, match="different hosted origin"):
        load_pairing(repo, state, expected_origin="https://foreign.example")
    assert path.read_text(encoding="utf-8").startswith('{"execution_classes"')
    # Distinct worktrees never read each other's machine tokens.
    other = tmp_path / "other"
    other.mkdir()
    assert load_pairing(other, state) is None
    # Refresh overwrites atomically, preserving owner-only mode.
    old = path
    updated = response()
    updated["worker_id"] = "machine-2"
    save_pairing(repo, state, updated, "https://app.lemoncrow.com")
    assert path == old
    assert load_pairing(repo, state)["worker_id"] == "machine-2"


@pytest.mark.parametrize(
    "mutation",
    [
        {"machine_token": "short"},
        {"worker_signing_keys": []},
        {
            "worker_signing_keys": [
                {"key_id": "bad", "algorithm": "RSA", "status": "active", "public_key_hex": "ab" * 32}
            ]
        },
        {"execution_classes": ["production_observation"]},
        {"execution_classes": ["runtime_validation", "runtime_validation"]},
        {"expires_at": 1},
        {"kind": "browser"},
    ],
)
def test_corrupt_untrusted_pair_response_never_persists(
    tmp_path: Path,
    mutation: dict[str, Any],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    raw = {**response(), **mutation}
    with pytest.raises(ValueError):
        save_pairing(repo, state, raw, "https://app.lemoncrow.com")
    assert load_pairing(repo, state) is None


def test_review_root_symlink_cannot_redirect_machine_credentials(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    state = tmp_path / "review"
    os.symlink(outside, state)
    with pytest.raises(ValueError, match="symlink"):
        save_pairing(repo, state, response(), "https://app.lemoncrow.com")
    assert not list(outside.iterdir())


@pytest.mark.parametrize("part", ["parent", "destination"])
def test_symlink_destination_never_overwrites_foreign_state(tmp_path: Path, part: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    from lemoncrow_client.review_device_connect import _path

    path = _path(repo, state)
    path.parent.parent.mkdir(parents=True)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    marker = foreign / "marker.txt"
    marker.write_text("safe", encoding="utf-8")
    if part == "parent":
        os.symlink(foreign, path.parent)
    else:
        path.parent.mkdir(mode=0o700)
        os.symlink(marker, path)
    with pytest.raises(ValueError, match="symlink"):
        save_pairing(repo, state, response(), "https://app.lemoncrow.com")
    assert marker.read_text(encoding="utf-8") == "safe"


def test_refuse_world_readable_machine_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    path = save_pairing(repo, state, response(), "https://app.lemoncrow.com")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        load_pairing(repo, state)


def test_real_cli_connect_uses_stdin_and_bounded_single_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lemoncrow_client.review_device_connect as module

    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state" / "review"
    fake = SimpleNamespace(
        repo_root=repo,
        review_state_dir=state,
        url="https://app.lemoncrow.com",
        request_timeout_s=15.0,
    )
    monkeypatch.setattr(module, "load_config", lambda **kwargs: fake)
    requests: list[tuple[str, dict[str, str]]] = []
    closures: list[bool] = []

    class Transport:
        def __init__(self, url: str, *, timeout_s: float) -> None:
            assert url == fake.url and timeout_s == 15

        def post(self, route: str, *, body: dict[str, str]) -> Response:
            requests.append((route, body))
            return Response(200, response())

        def close(self) -> None:
            closures.append(True)

    monkeypatch.setattr(module, "HttpTransport", Transport)
    output = io.StringIO()
    code = "lcrp.explicit-code-from-user"
    assert connect_main([], sink=output, input_source=io.StringIO(code + "\n")) == 0
    assert requests == [("/v1/review-devices/redeem", {"pairing_code": code})]
    assert closures == [True]
    assert code not in output.getvalue()
    assert response()["machine_token"] not in output.getvalue()
    output = io.StringIO()
    assert connect_main(["--status"], sink=output) == 0
    assert "machine-1" in output.getvalue()
    assert "secret" not in output.getvalue()
    output = io.StringIO()
    assert review_main(["connect", "--help"], sink=output) == 0
    assert "one-time" in output.getvalue()


def test_invalid_pairing_code_never_uses_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import lemoncrow_client.review_device_connect as module

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        module,
        "load_config",
        lambda **_kwargs: SimpleNamespace(
            repo_root=repo,
            review_state_dir=tmp_path / "state",
            url="https://app.lemoncrow.com",
            request_timeout_s=15.0,
        ),
    )
    monkeypatch.setattr(module, "HttpTransport", lambda *_args, **_kwargs: pytest.fail("network used"))
    out = io.StringIO()
    assert connect_main([], sink=out, input_source=io.StringIO("not-a-code\n")) == 1
    assert "malformed" in out.getvalue()


def test_expired_local_machine_never_falls_back_to_human_token(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    path = save_pairing(repo, state, response(), "https://app.lemoncrow.com")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["expires_at"] = time.time() - 1
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="expired"):
        load_pairing(repo, state)

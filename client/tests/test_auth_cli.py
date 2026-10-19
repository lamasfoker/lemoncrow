from __future__ import annotations

from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import lemoncrow_client.auth_cli as auth_cli
import pytest
from lemoncrow_client.cli import main
from lemoncrow_client.errors import AgentAction, ClientError, ErrorCode


class _Authward:
    def __init__(self, *, authorization: dict[str, Any] | None = None, polls: list[object] | None = None) -> None:
        self.authorization = authorization or {}
        self.polls = list(polls or [])
        self.calls: list[tuple[str, object]] = []

    def start_device(self) -> dict[str, Any]:
        self.calls.append(("start_device", None))
        return self.authorization

    def poll_device(self, device_code: str) -> dict[str, Any]:
        self.calls.append(("poll_device", device_code))
        answer = self.polls.pop(0)
        if isinstance(answer, Exception):
            raise answer
        assert isinstance(answer, dict)
        return answer

    def revoke_refresh(self, refresh_token: str) -> bool:
        self.calls.append(("revoke_refresh", refresh_token))
        return True


def test_auth_help_is_on_the_thin_client() -> None:
    sink = StringIO()
    assert main(["auth", "--help"], stdout=sink) == 0
    assert "login" in sink.getvalue()
    assert "logout" in sink.getvalue()
    assert "status" in sink.getvalue()


@pytest.mark.parametrize("verb", ["login", "status", "logout"])
def test_auth_verb_help_prints_usage_without_loading_config(verb: str, monkeypatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("help must not load configuration")

    monkeypatch.setattr(auth_cli, "_config", refuse)
    sink = StringIO()
    assert main(["auth", verb, "--help"], stdout=sink) == 0
    assert sink.getvalue().startswith(f"usage: lemoncrow-client auth {verb}")


def test_auth_status_reports_local_mode(monkeypatch) -> None:
    monkeypatch.setattr(auth_cli, "_config", lambda *_args, **_kwargs: SimpleNamespace(hosted=False))
    sink = StringIO()
    assert main(["auth", "status"], stdout=sink) == 0
    assert sink.getvalue() == "local: no login required\n"


def test_device_login_persists_managed_tokens_without_full_runtime(tmp_path: Path, monkeypatch) -> None:
    config = SimpleNamespace(
        hosted=True,
        url="https://api.lemoncrow.com",
        state_dir=tmp_path,
        account_state_dir=tmp_path / "account",
    )
    pending = ClientError(
        ErrorCode.UNAUTHENTICATED,
        "pending",
        details={"reason": "authorization_pending"},
        retryable=True,
        action=AgentAction.RETRY_LATER,
        server_code="authorization_pending",
    )
    authward = _Authward(
        authorization={
            "device_code": "device-1",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://auth.example/device",
            "verification_uri_complete": "https://auth.example/device?user_code=ABCD-EFGH",
            "expires_in": 60,
            "interval": 1,
        },
        polls=[pending, {"access_token": "access-1", "refresh_token": "refresh-1"}],
    )
    persisted: list[tuple[Path, str, str]] = []
    opened: list[str] = []
    monkeypatch.setattr(auth_cli.webbrowser, "open", opened.append)
    monkeypatch.setattr(auth_cli, "_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(auth_cli, "_authward", lambda _config: authward)
    monkeypatch.setattr(auth_cli.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        auth_cli,
        "write_managed_credentials",
        lambda state_dir, *, access_token, refresh_token: persisted.append((state_dir, access_token, refresh_token)),
    )

    sink = StringIO()
    assert main(["auth", "login"], stdout=sink) == 0
    assert "Open https://auth.example/device?user_code=ABCD-EFGH" in sink.getvalue()
    assert opened == ["https://auth.example/device?user_code=ABCD-EFGH"]
    assert "Code: ABCD-EFGH" in sink.getvalue()
    assert "Signed in to https://api.lemoncrow.com" in sink.getvalue()
    assert persisted == [(config.account_state_dir, "access-1", "refresh-1")]
    assert authward.calls == [
        ("start_device", None),
        ("poll_device", "device-1"),
        ("poll_device", "device-1"),
    ]


@pytest.mark.parametrize(
    "complete",
    [
        "https://evil.example/device?user_code=ABCD-EFGH",
        "https://auth.example/other?user_code=ABCD-EFGH",
        "https://user:password@auth.example/device?user_code=ABCD-EFGH",
        "javascript:alert(1)",
        "https://auth.example/device?user_code=WRONG",
        "https://auth.example/device?user_code=ABCD-EFGH&user_code=OTHER",
        "https://auth.example/device?user_code=ABCD-EFGH#fragment",
    ],
)
def test_device_link_does_not_change_origin_or_terminal_code(complete: str) -> None:
    base = "https://auth.example/device"
    assert auth_cli._device_browser_uri({"verification_uri_complete": complete}, base, "ABCD-EFGH") == base


def test_logout_revokes_authward_then_clears_managed_credentials(tmp_path: Path, monkeypatch) -> None:
    config = SimpleNamespace(
        hosted=True,
        authenticated=True,
        url="https://api.lemoncrow.com",
        state_dir=tmp_path,
        account_state_dir=tmp_path / "account",
        token="access-current",
        refresh_token="refresh-current",
    )
    authward = _Authward()
    cleared: list[Path] = []
    monkeypatch.setattr(auth_cli, "_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(auth_cli, "_managed", lambda _config: True)
    monkeypatch.setattr(auth_cli, "_authward", lambda _config: authward)
    monkeypatch.setattr(auth_cli, "clear_managed_credentials", lambda state_dir: cleared.append(state_dir))

    sink = StringIO()
    assert main(["auth", "logout"], stdout=sink) == 0
    assert sink.getvalue() == "Signed out.\n"
    assert cleared == [config.account_state_dir, tmp_path]
    assert authward.calls == [("revoke_refresh", "refresh-current")]


def test_logout_never_deletes_operator_credentials(tmp_path: Path, monkeypatch) -> None:
    config = SimpleNamespace(
        hosted=True,
        authenticated=True,
        url="https://api.lemoncrow.com",
        state_dir=tmp_path,
        account_state_dir=tmp_path / "account",
        token="operator-token",
        refresh_token="",
    )
    monkeypatch.setattr(auth_cli, "_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(auth_cli, "_managed", lambda _config: False)

    sink = StringIO()
    assert main(["auth", "logout"], stdout=sink) == 1
    assert "operator-provided" in sink.getvalue()

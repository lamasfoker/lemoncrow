"""Where remote telemetry goes: default, user choice, administrator policy."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.service.telemetry import config as config_mod
from lemoncrow.core.service.telemetry import event_rollup as event_rollup_mod
from lemoncrow.core.service.telemetry import public_rollup as public_rollup_mod
from lemoncrow.core.service.telemetry.config import (
    TelemetryConfigError,
    TelemetryPolicyLocked,
    auth_headers,
    lemoncrow_cloud_enabled,
    resolve_destination,
    save_telemetry_config,
    telemetry_status,
)
from lemoncrow.core.service.telemetry.event_rollup import event_rollup_endpoint, flush_daily_event_rollup
from lemoncrow.core.service.telemetry.local_store import LocalTelemetryStore
from lemoncrow.core.service.telemetry.public_rollup import public_rollup_endpoint, publish_public_savings_rollup

_SERVER_TOKEN = "srv-token-0123456789abcdef"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Remote telemetry allowed, no policy, no configured server, no overrides."""
    monkeypatch.setenv("LEMONCROW_TELEMETRY_DB", str(tmp_path / "telemetry.db"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_CONFIG", str(tmp_path / "telemetry.toml"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ID_PATH", str(tmp_path / "telemetry_id"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ALLOW_IN_TESTS", "1")
    monkeypatch.setenv("LEMONCROW_TELEMETRY_POLICY", str(tmp_path / "policy.toml"))
    monkeypatch.setattr(config_mod, "_system_policy_path", lambda: tmp_path / "system-policy.toml")
    client_home = tmp_path / "client-home"
    client_home.mkdir()
    monkeypatch.setenv("LEMONCROW_HOME", str(client_home))
    for name in (
        "LEMONCROW_TELEMETRY",
        "DO_NOT_TRACK",
        "LEMONCROW_URL",
        "LEMONCROW_INSTALL_MODE",
        "LEMONCROW_TOKEN",
        "LEMONCROW_TOKEN_FILE",
        "LEMONCROW_PUBLIC_TELEMETRY_ENDPOINT",
        "LEMONCROW_EVENT_TELEMETRY_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _policy(home: Path, body: str) -> Path:
    path = home / "policy.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_default_follows_a_hosted_server_and_is_lemoncrow_otherwise(home: Path) -> None:
    dest = resolve_destination()
    assert (dest.kind, dest.base_url, dest.source, dest.managed) == (
        "lemoncrow",
        "https://lemoncrow.com",
        "default",
        False,
    )
    assert event_rollup_endpoint() == "https://lemoncrow.com/api/telemetry/events"
    assert public_rollup_endpoint() == "https://lemoncrow.com/api/telemetry/rollup"
    assert lemoncrow_cloud_enabled() is True

    # `lemoncrow-client target` persists the server in ~/.lemoncrow/env.
    (home / "client-home" / "env").write_text("LEMONCROW_URL=https://lc.corp.test\n", encoding="utf-8")
    dest = resolve_destination()
    assert (dest.kind, dest.base_url) == ("server", "https://lc.corp.test")
    assert event_rollup_endpoint() == "https://lc.corp.test/api/telemetry/events"
    assert public_rollup_endpoint() == "https://lc.corp.test/api/telemetry/rollup"
    # Never LemonCrow's own services for an enterprise install.
    assert lemoncrow_cloud_enabled() is False


def test_explicit_local_mode_keeps_the_lemoncrow_default(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_URL", "https://lc.corp.test")
    monkeypatch.setenv("LEMONCROW_INSTALL_MODE", "local")
    assert resolve_destination().kind == "lemoncrow"


def test_server_token_is_sent_only_to_the_server_origin(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_URL", "https://lc.corp.test")
    monkeypatch.setenv("LEMONCROW_TOKEN", _SERVER_TOKEN)

    assert resolve_destination().token == ""  # only resolved for uploads
    dest = resolve_destination(with_token=True)
    assert auth_headers(dest, event_rollup_endpoint(dest)) == {"Authorization": f"Bearer {_SERVER_TOKEN}"}

    monkeypatch.setenv("LEMONCROW_EVENT_TELEMETRY_ENDPOINT", "https://dev.example.test/events")
    assert event_rollup_endpoint(dest) == "https://dev.example.test/events"
    assert auth_headers(dest, event_rollup_endpoint(dest)) == {}


def test_custom_destination_is_validated_and_saved_owner_only(home: Path) -> None:
    for bad in ("http://telemetry.corp.test", "https://user:pw@telemetry.corp.test", "ftp://x", "https://x?q=1"):
        with pytest.raises(TelemetryConfigError):
            save_telemetry_config(destination="custom", endpoint=bad)
    with pytest.raises(TelemetryConfigError, match="needs an endpoint"):
        save_telemetry_config(destination="custom")
    with pytest.raises(TelemetryConfigError, match="token"):
        save_telemetry_config(destination="custom", endpoint="https://t.corp.test", token="has space")
    with pytest.raises(TelemetryConfigError):
        save_telemetry_config(destination="everywhere")

    save_telemetry_config(destination="custom", endpoint="https://telemetry.corp.test/lc/", token="ingest-secret")

    mode = stat.S_IMODE((home / "telemetry.toml").stat().st_mode)
    assert mode == 0o600
    dest = resolve_destination(with_token=True)
    assert (dest.kind, dest.base_url, dest.source) == ("custom", "https://telemetry.corp.test/lc", "user")
    assert event_rollup_endpoint(dest) == "https://telemetry.corp.test/lc/api/telemetry/events"
    assert auth_headers(dest, event_rollup_endpoint(dest)) == {"Authorization": "Bearer ingest-secret"}
    assert lemoncrow_cloud_enabled() is False
    status = telemetry_status()
    assert status["token_set"] is True
    assert "ingest-secret" not in json.dumps(status)

    # Loopback may use plain HTTP (a collector on this machine).
    save_telemetry_config(destination="custom", endpoint="http://127.0.0.1:9000")
    assert resolve_destination().base_url == "http://127.0.0.1:9000"


def test_remote_off_and_the_legacy_opt_out_mean_local_only(home: Path) -> None:
    (home / "telemetry.toml").write_text(
        '[telemetry]\nremote_enabled = false\ndestination = "lemoncrow"\n', encoding="utf-8"
    )
    assert resolve_destination().kind == "off"
    assert event_rollup_endpoint() == ""

    save_telemetry_config(remote_enabled=True)
    assert telemetry_status()["destination_setting"] == "auto"
    assert resolve_destination().kind == "lemoncrow"

    save_telemetry_config(destination="custom", endpoint="https://telemetry.corp.test")
    save_telemetry_config(remote_enabled=False)
    assert resolve_destination().kind == "off"
    assert (
        publish_public_savings_rollup(
            session_id="s", saved_usd=1.0, tokens_saved=1, calls_avoided=1, turn_count=1, source="claude"
        )
        is False
    )


def test_policy_fixes_the_destination_and_refuses_user_changes(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    save_telemetry_config(destination="lemoncrow")
    path = _policy(
        home,
        '[telemetry]\ndestination = "custom"\nendpoint = "https://telemetry.corp.test"\ntoken = "org-token"\n',
    )
    monkeypatch.setenv("LEMONCROW_EVENT_TELEMETRY_ENDPOINT", "https://elsewhere.example.test/events")

    dest = resolve_destination(with_token=True)
    assert (dest.kind, dest.base_url, dest.managed, dest.source) == (
        "custom",
        "https://telemetry.corp.test",
        True,
        "policy",
    )
    # A development override cannot route around the policy.
    assert event_rollup_endpoint(dest) == "https://telemetry.corp.test/api/telemetry/events"
    assert auth_headers(dest, event_rollup_endpoint(dest)) == {"Authorization": "Bearer org-token"}
    assert lemoncrow_cloud_enabled() is False

    for change in ({"destination": "lemoncrow"}, {"remote_enabled": False}, {"endpoint": "https://x.test"}):
        with pytest.raises(TelemetryPolicyLocked, match=str(path)):
            save_telemetry_config(**change)  # type: ignore[arg-type]
    # Unrelated settings stay editable.
    assert save_telemetry_config(lexical_frustration_enabled=True).lexical_frustration_enabled is True

    status = telemetry_status()
    assert (status["managed"], status["policy_path"], status["destination"]) == (True, str(path), "custom")
    assert "org-token" not in json.dumps(status)

    # The user's own opt-out still wins.
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    assert resolve_destination().kind == "off"
    assert telemetry_status()["managed"] is True


@pytest.mark.parametrize(
    "body",
    [
        "not [valid toml",
        '[telemetry]\ndestination = "everywhere"\n',
        '[telemetry]\ndestination = "custom"\nendpoint = "http://telemetry.corp.test"\n',
        '[telemetry]\ndestination = "custom"\n',
    ],
)
def test_an_unusable_policy_fails_closed(home: Path, body: str) -> None:
    _policy(home, body)
    dest = resolve_destination()
    assert (dest.kind, dest.managed) == ("off", True)
    assert dest.error
    assert event_rollup_endpoint() == ""


def test_a_policy_without_a_destination_is_not_a_lock(home: Path) -> None:
    _policy(home, "# nothing managed yet\n[telemetry]\n")
    assert resolve_destination().managed is False
    save_telemetry_config(destination="off")


def test_the_system_policy_wins_over_the_env_override(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    system = home / "system-policy.toml"
    system.write_text('[telemetry]\ndestination = "off"\n', encoding="utf-8")
    _policy(home, '[telemetry]\ndestination = "lemoncrow"\n')
    dest = resolve_destination()
    assert (dest.kind, dest.managed) == ("off", True)
    assert telemetry_status()["policy_path"] == str(system)


def test_both_rollups_post_to_the_destination_with_its_token(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    save_telemetry_config(destination="custom", endpoint="https://telemetry.corp.test", token="ingest-secret")

    rollups: list[tuple[str, dict[str, str] | None]] = []

    def fake_post_json(
        endpoint: str,
        payload: dict[str, Any],
        *,
        timeout_s: float,
        attempts: int = 1,
        headers: dict[str, str] | None = None,
    ) -> bool:
        rollups.append((endpoint, headers))
        return True

    monkeypatch.setattr(public_rollup_mod, "_post_json", fake_post_json)
    assert publish_public_savings_rollup(
        session_id="s", saved_usd=1.0, tokens_saved=1, calls_avoided=1, turn_count=1, source="claude"
    )
    assert rollups == [("https://telemetry.corp.test/api/telemetry/rollup", {"Authorization": "Bearer ingest-secret"})]

    day = datetime.now(UTC).date() - timedelta(days=2)
    LocalTelemetryStore(home / "telemetry.db").write_event(
        event="session_start",
        props={"agent_host": "claude"},
        exported=False,
        ts=datetime(day.year, day.month, day.day, 12, tzinfo=UTC).timestamp(),
    )
    events: list[tuple[str, dict[str, str] | None]] = []

    def fake_post(
        endpoint: str,
        payload: dict[str, Any],
        *,
        timeout_s: float,
        attempts: int,
        headers: dict[str, str] | None = None,
    ) -> int:
        events.append((endpoint, headers))
        return 200

    monkeypatch.setattr(event_rollup_mod, "post_json", fake_post)
    result, _checkpoint = flush_daily_event_rollup(home, checkpoint_day=(day - timedelta(days=1)).isoformat())
    assert result["posted_days"] == 1
    assert events == [("https://telemetry.corp.test/api/telemetry/events", {"Authorization": "Bearer ingest-secret"})]

"""`lc telemetry destination` and /telemetry/config honor the destination and
an administrator policy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from lemoncrow.core.service.telemetry import config as config_mod
from lemoncrow.gateway.cli import cli
from tests.helpers import init_store_at


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LEMONCROW_TELEMETRY_DB", str(tmp_path / "telemetry.db"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_CONFIG", str(tmp_path / "telemetry.toml"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ID_PATH", str(tmp_path / "telemetry_id"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ACK", str(tmp_path / "telemetry_ack"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ALLOW_IN_TESTS", "1")
    monkeypatch.setenv("LEMONCROW_TELEMETRY_POLICY", str(tmp_path / "policy.toml"))
    monkeypatch.setattr(config_mod, "_system_policy_path", lambda: tmp_path / "system-policy.toml")
    (tmp_path / "client-home").mkdir()
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "client-home"))
    for name in ("LEMONCROW_TELEMETRY", "DO_NOT_TRACK", "LEMONCROW_URL", "LEMONCROW_INSTALL_MODE"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _invoke(root: Path, *args: str) -> Result:
    argv = ["runtime", *args] if args and args[0] in ["telemetry"] else list(args)
    return CliRunner().invoke(cli, ["--root", str(root), *argv])


def test_cli_sets_shows_and_is_refused_under_policy(home: Path) -> None:
    root = home / "store"
    init_store_at(str(root))

    shown = _invoke(root, "telemetry", "destination")
    assert shown.exit_code == 0, shown.output
    assert shown.output.strip() == "telemetry destination: lemoncrow.com (automatic)"

    bad = _invoke(root, "telemetry", "destination", "custom", "http://telemetry.corp.test")
    assert bad.exit_code != 0
    assert "HTTPS" in bad.output

    wrong = _invoke(root, "telemetry", "destination", "off", "https://telemetry.corp.test")
    assert wrong.exit_code != 0

    ok = _invoke(root, "telemetry", "destination", "custom", "https://telemetry.corp.test", "--token", "ingest-secret")
    assert ok.exit_code == 0, ok.output
    assert ok.output.strip() == "telemetry destination: custom https://telemetry.corp.test"

    status = _invoke(root, "telemetry", "status", "--json")
    payload = json.loads(status.output)
    assert (payload["destination"], payload["token_set"], payload["managed"]) == ("custom", True, False)
    assert "ingest-secret" not in status.output

    (home / "policy.toml").write_text('[telemetry]\ndestination = "off"\n', encoding="utf-8")
    for args in (("destination", "lemoncrow"), ("remote", "on")):
        refused = _invoke(root, "telemetry", *args)
        assert refused.exit_code != 0
        assert "managed by your organization" in refused.output
    overview = _invoke(root, "telemetry")
    assert "local only (managed by policy" in overview.output


def test_api_reports_and_updates_the_destination(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fastapi", reason="FastAPI API tests require the api extra")
    from fastapi.testclient import TestClient

    from lemoncrow.core.service.api import create_app

    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    client = TestClient(create_app(store_root=home / ".lemoncrow"))

    body = client.get("/telemetry/config").json()
    assert (body["destination"], body["destination_setting"], body["managed"]) == ("lemoncrow", "auto", False)
    assert body["destination_url"] == "https://lemoncrow.com"

    rejected = client.post("/telemetry/config", json={"destination": "custom", "endpoint": "http://x.corp.test"})
    assert rejected.status_code == 400
    assert "HTTPS" in rejected.json()["detail"]
    assert client.post("/telemetry/config", json={"destination": 3}).status_code == 400

    saved = client.post(
        "/telemetry/config",
        json={"destination": "custom", "endpoint": "https://telemetry.corp.test", "token": "ingest-secret"},
    )
    assert saved.status_code == 200
    assert (saved.json()["destination"], saved.json()["token_set"]) == ("custom", True)
    assert "ingest-secret" not in saved.text

    (home / "policy.toml").write_text('[telemetry]\ndestination = "off"\n', encoding="utf-8")
    locked = client.post("/telemetry/config", json={"destination": "lemoncrow"})
    assert locked.status_code == 409
    assert client.post("/telemetry/config", json={"lexical_frustration_enabled": True}).status_code == 200
    body = client.get("/telemetry/config").json()
    assert (body["destination"], body["managed"], body["lexical_frustration_enabled"]) == ("off", True, True)

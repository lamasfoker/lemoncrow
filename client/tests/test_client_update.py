from __future__ import annotations

import io

import pytest
from lemoncrow_client import cli


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_RELEASE_BASE_URL", "https://example.test/rel")
    return tmp_path


def _review(home, *, engine=False):
    (home / "review" / "client").mkdir(parents=True)
    if engine:
        (home / "review" / "engine").mkdir(parents=True)


def _runtime(home, mode=None):
    exe = home / "runtime" / "client" / "bin" / "lemoncrow-client"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    if mode:
        (home / "runtime" / "env").write_text(f"LEMONCROW_INSTALL_MODE={mode}\n")


def _capture(monkeypatch, code=0):
    calls = []

    def fake(args, sink, *, dry_run=False):
        calls.append((list(args), dry_run))
        return code

    monkeypatch.setattr(cli, "_run_release_installer", fake)
    return calls


def test_nothing_installed_returns_2(home, monkeypatch):
    calls = _capture(monkeypatch)
    sink = io.StringIO()
    assert cli.main(["update"], sink) == 2
    assert calls == []
    assert "--product review" in sink.getvalue()


def test_leftover_full_engine_does_not_block_split_update(home, monkeypatch):
    (home / "uv-tools" / "lemoncrow").mkdir(parents=True)
    _review(home)
    calls = _capture(monkeypatch)
    sink = io.StringIO()
    assert cli.main(["update"], sink) == 0
    assert calls == [(["--product", "review", "--cloud"], False), (["--product", "runtime", "--local"], False)]


def test_legacy_only_install_migrates_runtime(home, monkeypatch):
    (home / "uv-tools" / "lemoncrow").mkdir(parents=True)
    calls = _capture(monkeypatch)
    assert cli.main(["update"], io.StringIO()) == 0
    assert calls == [(["--product", "runtime", "--local"], False)]


def test_review_cloud_passes_an_explicit_hosted_flag(home, monkeypatch):
    # The bare installer is Local Review; a hosted update must say so.
    _review(home)
    calls = _capture(monkeypatch)
    assert cli.main(["update"], io.StringIO()) == 0
    assert calls == [(["--product", "review", "--cloud"], False)]


def test_review_with_engine_adds_local(home, monkeypatch):
    _review(home, engine=True)
    calls = _capture(monkeypatch)
    assert cli.main(["update"], io.StringIO()) == 0
    assert calls == [(["--product", "review", "--local"], False)]


def test_both_products_runtime_lite_adds_local(home, monkeypatch):
    _review(home)
    _runtime(home, mode="lite")
    calls = _capture(monkeypatch)
    assert cli.main(["update"], io.StringIO()) == 0
    assert calls == [(["--product", "review", "--cloud"], False), (["--product", "runtime", "--local"], False)]


def test_runtime_hosted_passes_an_explicit_hosted_flag(home, monkeypatch):
    _runtime(home, mode="hosted")
    calls = _capture(monkeypatch)
    assert cli.main(["update"], io.StringIO()) == 0
    assert calls == [(["--product", "runtime", "--cloud"], False)]


def test_dry_run_prints_commands_without_running(home, monkeypatch):
    _review(home, engine=True)
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: ran.append(a))
    sink = io.StringIO()
    assert cli.main(["update", "--dry-run"], sink) == 0
    assert ran == []
    assert (
        "curl -fLsS --retry 3 --connect-timeout 15 https://example.test/rel/install.sh"
        " | bash -s -- --product review --local"
    ) in sink.getvalue()


def test_failure_code_is_propagated(home, monkeypatch):
    _review(home)
    _capture(monkeypatch, code=7)
    sink = io.StringIO()
    assert cli.main(["update"], sink) == 7
    assert "update failed" in sink.getvalue()


def test_update_rejects_unknown_option(home, monkeypatch):
    calls = _capture(monkeypatch)
    assert cli.main(["update", "--bogus"], io.StringIO()) == 2
    assert calls == []


def test_uninstall_without_product_prints_usage_even_on_tty(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    sink = io.StringIO()
    assert cli.main(["uninstall"], sink) == 2
    assert "usage: lc uninstall review|runtime|all [--dry-run]" in sink.getvalue()


def test_root_host_verb_is_gone_but_rt_host_remains(monkeypatch):
    sink = io.StringIO()
    assert cli.main(["host", "status"], sink) == 2
    assert "unknown command: host" in sink.getvalue()
    assert "It is a Runtime command: lc rt host" in sink.getvalue()
    assert "  lemoncrow-client host " not in sink.getvalue()
    monkeypatch.setattr(cli, "_host", lambda args, sink: 0)
    assert cli.main(["rt", "host", "status"], io.StringIO()) == 0


def test_full_engine_verbs_name_the_source_checkout():
    sink = io.StringIO()
    assert cli.main(["doctor"], sink) == 2
    out = sink.getvalue()
    assert "unknown command: doctor" in out
    assert "`uv run lc doctor`" in out
    assert "`lc status` and `lc rt status`" in out
    sink = io.StringIO()
    assert cli.main(["nosuchverb"], sink) == 2
    assert sink.getvalue() == "unknown command: nosuchverb\nRun `lc --help` for the commands this client supports.\n"


def test_rt_telemetry_without_the_full_cli_says_the_client_sends_none(monkeypatch, tmp_path):
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_FULL_RUNTIME_CLI", raising=False)
    monkeypatch.setattr(cli, "_full_runtime_executable", lambda: None)
    sink = io.StringIO()
    assert cli.main(["rt", "telemetry", "remote", "off"], sink) == 2
    out = sink.getvalue()
    assert "This client sends no product telemetry" in out
    assert "LEMONCROW_FULL_RUNTIME_CLI" in out


def test_not_installed_messages_give_install_command(monkeypatch, tmp_path):
    monkeypatch.setenv("LEMONCROW_NO_LAZY_INSTALL", "1")
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_RUNTIME_CLIENT", raising=False)
    sink = io.StringIO()
    assert cli._delegate_external_runtime(["x"], sink) == 2
    out = sink.getvalue()
    assert "LemonCrow Runtime is not installed." in out
    assert "curl -fsSL https://lemoncrow.com/install.sh | bash -s -- --product runtime" in out
    assert "choose" not in out


def test_split_install_with_persisted_uninstall_suite_is_updated(home, monkeypatch):
    # Split installs keep uninstall scripts under install/; that is not the full engine.
    (home / "install" / "scripts").mkdir(parents=True)
    _review(home, engine=True)
    calls = _capture(monkeypatch)
    assert cli.main(["update"], io.StringIO()) == 0
    assert calls == [(["--product", "review", "--local"], False)]


def test_hosted_server_url_survives_update_and_lazy_fetch(home, monkeypatch):
    # A non-default hosted server must not fall back to the public cloud URL.
    _review(home)
    (home / "review" / "env").write_text(
        "LEMONCROW_URL=https://lemoncrow.acme.test\nLEMONCROW_INSTALL_MODE=hosted\n", encoding="utf-8"
    )
    calls = _capture(monkeypatch)
    assert cli.main(["update"], io.StringIO()) == 0
    monkeypatch.delenv("LEMONCROW_NO_LAZY_INSTALL", raising=False)
    assert cli._lazy_install_product("runtime", io.StringIO()) is True
    cloud = ["--cloud", "--url", "https://lemoncrow.acme.test"]
    assert calls == [(["--product", "review", *cloud], False), (["--product", "runtime", *cloud], False)]


@pytest.mark.parametrize(
    ("product", "setup", "expected"),
    [
        ("runtime", lambda h: _review(h, engine=True), ["--product", "runtime", "--local"]),
        ("runtime", lambda h: _review(h), ["--product", "runtime", "--cloud"]),
        ("review", lambda h: _runtime(h, "lite"), ["--product", "review", "--local"]),
        ("review", lambda h: _runtime(h, "hosted"), ["--product", "review", "--cloud"]),
    ],
)
def test_lazy_install_matches_installed_deployment(home, monkeypatch, product, setup, expected):
    setup(home)
    monkeypatch.delenv("LEMONCROW_NO_LAZY_INSTALL", raising=False)
    calls = _capture(monkeypatch)
    assert cli._lazy_install_product(product, io.StringIO()) is True
    assert calls == [(expected, False)]


def test_local_review_update_preserves_recorded_port(home, monkeypatch):
    _review(home, engine=True)
    (home / "review" / "env").write_text("LEMONCROW_INSTALL_MODE=local\nLEMONCROW_URL=http://127.0.0.1:7428\n")
    calls = _capture(monkeypatch)
    assert cli.main(["update"], io.StringIO()) == 0
    assert calls == [(["--product", "review", "--local", "--port", "7428"], False)]


def test_recorded_hosted_deployment_wins_over_leftover_engine(home, monkeypatch):
    _review(home, engine=True)
    (home / "review" / "env").write_text("LEMONCROW_INSTALL_MODE=hosted\nLEMONCROW_URL=https://lc.example\n")
    calls = _capture(monkeypatch)
    assert cli.main(["update"], io.StringIO()) == 0
    assert calls == [(["--product", "review", "--cloud", "--url", "https://lc.example"], False)]


def test_failed_installer_download_is_not_success(home, monkeypatch):
    monkeypatch.setenv("LEMONCROW_RELEASE_BASE_URL", (home / "missing-release").as_uri())
    assert cli._run_release_installer(["--product", "review"], io.StringIO()) != 0


def test_installer_url_is_literal_shell_data(home, monkeypatch):
    marker = home / "injected"
    release = home / f"release$(touch {marker})"
    monkeypatch.setenv("LEMONCROW_RELEASE_BASE_URL", release.as_uri())
    # URI escaping itself is not the protection: use a literal substitution.
    monkeypatch.setenv("LEMONCROW_RELEASE_BASE_URL", f"file://{release}")
    assert cli._run_release_installer([], io.StringIO()) != 0
    assert not marker.exists()


def test_client_initiated_install_cannot_inherit_unverified_download_bypass(home, monkeypatch):
    """Even an explicit shell bypass cannot relax client-managed artifact checks."""
    monkeypatch.setenv("LEMONCROW_ALLOW_UNVERIFIED", "1")
    captured = []

    def execute(args, **kwargs):
        captured.append((args, kwargs))
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(cli.subprocess, "run", execute)
    assert cli._run_release_installer(["--product", "review"], io.StringIO()) == 0
    assert captured[0][1]["env"]["LEMONCROW_ALLOW_UNVERIFIED"] == "0"
    assert captured[0][0][:4] == ["bash", "-o", "pipefail", "-c"]


def test_client_install_preview_does_not_execute_any_downloader(home, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("dry-run must never download or install")

    monkeypatch.setattr(cli.subprocess, "run", forbidden)
    assert cli._run_release_installer(["--product", "runtime"], io.StringIO(), dry_run=True) == 0

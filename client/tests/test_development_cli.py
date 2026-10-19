"""The hidden public dispatcher requires an explicit private checkout."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from lemoncrow_client import cli, development


def test_dev_is_hidden_in_help_and_revealed_with_existing_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = io.StringIO()
    monkeypatch.delenv("LEMONCROW_SHOW_ALL", raising=False)
    assert cli.main(["--help"], stdout=sink) == 0
    assert "lc dev" not in sink.getvalue()
    monkeypatch.setenv("LEMONCROW_SHOW_ALL", "1")
    sink = io.StringIO()
    assert cli.main(["--help"], stdout=sink) == 0
    assert "lc dev" in sink.getvalue()


def test_dev_without_private_checkout_fails_without_installing_anything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(development, "__file__", str(tmp_path / "installed/lib/client/development.py"))
    monkeypatch.delenv("LEMONCROW_DEV_CHECKOUT", raising=False)
    monkeypatch.setattr(development.os, "execv", lambda *_: pytest.fail("must not launch/install"))
    sink = io.StringIO()
    assert cli.main(["dev", "use", "hosted"], stdout=sink) == 2
    assert "requires the private LemonCrow checkout" in capsys.readouterr().err
    assert sink.getvalue() == ""


def test_dev_forwards_mcp_and_home_from_another_repository_to_locked_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    marker = checkout / "enterprise/server/dev.py"
    marker.parent.mkdir(parents=True)
    marker.touch()
    (checkout / ".lemoncrow-dev.json").write_text(
        '{"schema_version": 1, "project": "enterprise/server", "entrypoint": "enterprise/server/dev.py"}'
    )
    repo = tmp_path / "agent-repository"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setattr(development.shutil, "which", lambda _: "/usr/bin/uv")

    class ReplacedProcess(BaseException):
        pass

    def replace(binary: str, arguments: list[str]) -> None:
        assert binary == "/usr/bin/uv"
        assert arguments[:5] == [
            "/usr/bin/uv",
            "run",
            "--project",
            str(checkout / "enterprise/server"),
            "--locked",
        ]
        assert arguments[5:] == [
            "python",
            str(checkout / "enterprise/server/dev.py"),
            "--home",
            str(tmp_path / "profiles"),
            "mcp",
            "--host",
            "codex",
        ]
        assert Path.cwd() == repo
        raise ReplacedProcess

    monkeypatch.setattr(development.os, "execv", replace)
    with pytest.raises(ReplacedProcess):
        cli.main(["dev", "--checkout", str(checkout), "--home", str(tmp_path / "profiles"), "mcp", "--host", "codex"])


def test_dev_manifest_cannot_select_an_entrypoint_outside_the_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    external = tmp_path / "outside.py"
    external.write_text("raise RuntimeError('must not execute')", encoding="utf-8")
    (checkout / ".lemoncrow-dev.json").write_text(
        '{"schema_version": 1, "project": ".", "entrypoint": "../outside.py"}'
    )
    monkeypatch.setattr(development.shutil, "which", lambda _: "/usr/bin/uv")
    monkeypatch.setattr(development.os, "execv", lambda *_: pytest.fail("must not execute"))
    assert development.dev_main(["--checkout", str(checkout), "mcp"]) == 2
    assert "escapes the checkout" in capsys.readouterr().err


@pytest.mark.parametrize("tool", ["eval", "perf", "defaults"])
@pytest.mark.parametrize("with_root", [False, True])
def test_engineering_tools_use_root_checkout_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, with_root: bool
) -> None:
    checkout = tmp_path / "checkout"
    marker = checkout / "enterprise/server/dev.py"
    marker.parent.mkdir(parents=True)
    marker.touch()
    (checkout / ".lemoncrow-dev.json").write_text(
        '{"schema_version": 1, "project": "enterprise/server", "entrypoint": "enterprise/server/dev.py"}'
    )
    monkeypatch.setattr(development.shutil, "which", lambda _: "/usr/bin/uv")
    root_args = ["--root", str(tmp_path / "state")] if with_root else []

    class ReplacedProcess(BaseException):
        pass

    def replace(binary: str, arguments: list[str]) -> None:
        assert binary == "/usr/bin/uv"
        assert arguments == [
            "/usr/bin/uv",
            "run",
            "--project",
            str(checkout),
            "--locked",
            "python",
            "-m",
            "lemoncrow.gateway.cli.developer_tools",
            *root_args,
            tool,
            "--help",
        ]
        raise ReplacedProcess

    monkeypatch.setattr(development.os, "execv", replace)
    with pytest.raises(ReplacedProcess):
        cli.main(["dev", "--checkout", str(checkout), *root_args, tool, "--help"])

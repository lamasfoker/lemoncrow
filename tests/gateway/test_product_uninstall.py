from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNINSTALL_REVIEW = ROOT / "scripts" / "uninstall_review.sh"
UNINSTALL_RUNTIME = ROOT / "scripts" / "uninstall_runtime.sh"


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def _layout(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    state = tmp_path / ".lemoncrow"
    bin_dir = state / "bin"
    review = state / "review" / "client" / "bin"
    runtime = state / "runtime" / "client" / "bin"
    account = state / "account"
    bin_dir.mkdir(parents=True)
    account.mkdir(parents=True)
    (account / "sentinel").write_text("keep", encoding="utf-8")
    _make_executable(review / "lc")
    _make_executable(review / "lcr")
    _make_executable(review / "lemoncrow-review-client")
    _make_executable(runtime / "lemoncrow-client")
    _make_executable(runtime / "lemoncrow-runtime-client")
    (state / "review" / "state-sentinel").write_text("review", encoding="utf-8")
    (state / "runtime" / "state-sentinel").write_text("runtime", encoding="utf-8")
    return state, bin_dir, review, runtime


def _env(tmp_path: Path, state: Path, bin_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path),
            "LEMONCROW_HOME": str(state),
            "LEMONCROW_BIN_DIR": str(bin_dir),
        }
    )
    return env


def test_uninstall_review_preserves_runtime_and_account_and_hands_off_lc(tmp_path: Path) -> None:
    state, bin_dir, review, runtime = _layout(tmp_path)
    (bin_dir / "lc").symlink_to(review / "lc")
    (bin_dir / "lcr").symlink_to(review / "lcr")
    (bin_dir / "lemoncrow-review-client").symlink_to(review / "lemoncrow-review-client")
    (bin_dir / "lemoncrow-runtime-client").symlink_to(runtime / "lemoncrow-runtime-client")

    result = subprocess.run(
        ["bash", str(UNINSTALL_REVIEW)],
        env=_env(tmp_path, state, bin_dir),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not (state / "review").exists()
    assert (state / "runtime" / "state-sentinel").read_text() == "runtime"
    assert (state / "account" / "sentinel").read_text() == "keep"
    assert not (bin_dir / "lcr").exists()
    assert (bin_dir / "lemoncrow-runtime-client").exists()
    assert (bin_dir / "lc").resolve() == (runtime / "lemoncrow-client").resolve()


def test_uninstall_runtime_preserves_review_and_account_and_hands_off_lc(tmp_path: Path) -> None:
    state, bin_dir, review, runtime = _layout(tmp_path)
    (bin_dir / "lc").symlink_to(runtime / "lemoncrow-client")
    (bin_dir / "lcr").symlink_to(review / "lcr")
    (bin_dir / "lemoncrow-review-client").symlink_to(review / "lemoncrow-review-client")
    (bin_dir / "lemoncrow-runtime-client").symlink_to(runtime / "lemoncrow-runtime-client")

    result = subprocess.run(
        ["bash", str(UNINSTALL_RUNTIME), "--keep-hosts"],
        env=_env(tmp_path, state, bin_dir),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not (state / "runtime").exists()
    assert (state / "review" / "state-sentinel").read_text() == "review"
    assert (state / "account" / "sentinel").read_text() == "keep"
    assert not (bin_dir / "lemoncrow-runtime-client").exists()
    assert (bin_dir / "lcr").exists()
    assert (bin_dir / "lc").resolve() == (review / "lc").resolve()


def test_uninstall_runtime_removes_pi_host_integration_before_deleting_client(tmp_path: Path) -> None:
    state, bin_dir, _review, runtime = _layout(tmp_path)
    calls = tmp_path / "runtime-client.log"
    (runtime / "lemoncrow-client").write_text(f'#!/bin/sh\necho "$*" >> "{calls}"\n', encoding="utf-8")
    result = subprocess.run(
        ["bash", str(UNINSTALL_RUNTIME)], env=_env(tmp_path, state, bin_dir), capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "runtime host remove --engine pi --yes" in calls.read_text().splitlines()
    assert not (state / "runtime").exists()


def test_uninstall_runtime_removes_remote_mcp_units_but_keeps_connector_state(tmp_path: Path) -> None:
    state, bin_dir, _review, runtime = _layout(tmp_path)
    calls = tmp_path / "runtime-client.log"
    (runtime / "lemoncrow-client").write_text(f'#!/bin/sh\necho "$*" >> "{calls}"\n', encoding="utf-8")
    connector = state / "mcp" / "connectors" / "mcp-example-com.json"
    connector.parent.mkdir(parents=True)
    connector.write_text("{}", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(UNINSTALL_RUNTIME), "--keep-hosts"],
        env=_env(tmp_path, state, bin_dir),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    # The units run this Runtime's Python, which is about to be deleted.
    assert calls.read_text(encoding="utf-8").splitlines() == ["runtime mcp service uninstall"]
    assert connector.exists()


def test_uninstall_review_only_removes_owned_lc_when_runtime_absent(tmp_path: Path) -> None:
    state, bin_dir, review, _runtime = _layout(tmp_path)
    (state / "runtime").rename(state / "runtime-away")
    (bin_dir / "lc").symlink_to(review / "lc")
    (bin_dir / "lcr").symlink_to(review / "lcr")

    result = subprocess.run(
        ["bash", str(UNINSTALL_REVIEW)],
        env=_env(tmp_path, state, bin_dir),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not (bin_dir / "lc").exists()
    assert (state / "account" / "sentinel").exists()


def test_uninstallers_do_not_remove_foreign_lc_symlink(tmp_path: Path) -> None:
    state, bin_dir, review, _runtime = _layout(tmp_path)
    foreign = tmp_path / "foreign-lc"
    _make_executable(foreign)
    (bin_dir / "lc").symlink_to(foreign)
    (bin_dir / "lcr").symlink_to(review / "lcr")

    result = subprocess.run(
        ["bash", str(UNINSTALL_REVIEW)],
        env=_env(tmp_path, state, bin_dir),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (bin_dir / "lc").resolve() == foreign.resolve()


def _fake_server_script(tmp_path: Path) -> tuple[Path, Path]:
    script = tmp_path / "local_server.sh"
    log = tmp_path / "server.log"
    script.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$LC_TEST_SERVER_LOG"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, log


def test_uninstall_last_local_review_stops_shared_server(tmp_path: Path) -> None:
    state, bin_dir, review, _runtime = _layout(tmp_path)
    (state / "runtime").rename(state / "runtime-away")
    (bin_dir / "lc").symlink_to(review / "lc")
    (bin_dir / "lcr").symlink_to(review / "lcr")
    target = state / "server" / "target-product"
    target.parent.mkdir(parents=True)
    target.write_text("review\n", encoding="utf-8")
    script, log = _fake_server_script(tmp_path)
    env = _env(tmp_path, state, bin_dir)
    env.update({"LEMONCROW_LOCAL_SERVER_SCRIPT": str(script), "LC_TEST_SERVER_LOG": str(log)})

    result = subprocess.run(
        ["bash", str(UNINSTALL_REVIEW)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert target.read_text().strip() == "none"
    assert log.read_text().splitlines() == ["stop"]


def test_uninstall_review_from_both_transfers_server_to_runtime(tmp_path: Path) -> None:
    state, bin_dir, review, runtime = _layout(tmp_path)
    (bin_dir / "lc").symlink_to(review / "lc")
    (bin_dir / "lcr").symlink_to(review / "lcr")
    target = state / "server" / "target-product"
    target.parent.mkdir(parents=True)
    target.write_text("both\n", encoding="utf-8")
    script, log = _fake_server_script(tmp_path)
    env = _env(tmp_path, state, bin_dir)
    env.update({"LEMONCROW_LOCAL_SERVER_SCRIPT": str(script), "LC_TEST_SERVER_LOG": str(log)})

    result = subprocess.run(
        ["bash", str(UNINSTALL_REVIEW)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert target.read_text().strip() == "runtime"
    assert log.read_text().splitlines() == ["stop", "start"]
    assert (bin_dir / "lc").resolve() == (runtime / "lemoncrow-client").resolve()


def test_uninstall_last_local_runtime_stops_shared_server(tmp_path: Path) -> None:
    state, bin_dir, _review, runtime = _layout(tmp_path)
    (state / "review").rename(state / "review-away")
    (bin_dir / "lc").symlink_to(runtime / "lemoncrow-client")
    target = state / "server" / "target-product"
    target.parent.mkdir(parents=True)
    target.write_text("runtime\n", encoding="utf-8")
    script, log = _fake_server_script(tmp_path)
    env = _env(tmp_path, state, bin_dir)
    env.update({"LEMONCROW_LOCAL_SERVER_SCRIPT": str(script), "LC_TEST_SERVER_LOG": str(log)})

    result = subprocess.run(
        ["bash", str(UNINSTALL_RUNTIME), "--keep-hosts"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert target.read_text().strip() == "none"
    assert log.read_text().splitlines() == ["stop"]


def test_uninstall_runtime_from_both_transfers_server_to_review(tmp_path: Path) -> None:
    state, bin_dir, review, runtime = _layout(tmp_path)
    (bin_dir / "lc").symlink_to(runtime / "lemoncrow-client")
    target = state / "server" / "target-product"
    target.parent.mkdir(parents=True)
    target.write_text("both\n", encoding="utf-8")
    script, log = _fake_server_script(tmp_path)
    env = _env(tmp_path, state, bin_dir)
    env.update({"LEMONCROW_LOCAL_SERVER_SCRIPT": str(script), "LC_TEST_SERVER_LOG": str(log)})

    result = subprocess.run(
        ["bash", str(UNINSTALL_RUNTIME), "--keep-hosts"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert target.read_text().strip() == "review"
    assert log.read_text().splitlines() == ["stop", "start"]
    assert (bin_dir / "lc").resolve() == (review / "lc").resolve()


def test_uninstall_local_runtime_removes_full_engine_and_preserves_review_engine(tmp_path: Path) -> None:
    state, bin_dir, review, runtime = _layout(tmp_path)
    (bin_dir / "lc").symlink_to(runtime / "lemoncrow-client")
    (bin_dir / "lcr").symlink_to(review / "lcr")
    (state / "runtime/env").write_text("LEMONCROW_INSTALL_MODE=local\n", encoding="utf-8")
    review_engine = state / "review/engine/bin/python"
    runtime_engine = state / "uv-tools/lemoncrow/bin/python"
    _make_executable(review_engine)
    _make_executable(runtime_engine)
    (state / "review/engine/sentinel").write_text("review-engine", encoding="utf-8")
    (state / "uv-tools/lemoncrow/sentinel").write_text("runtime-engine", encoding="utf-8")

    target = state / "server/target-product"
    target.parent.mkdir(parents=True)
    target.write_text("both\n", encoding="utf-8")
    script, log = _fake_server_script(tmp_path)
    env = _env(tmp_path, state, bin_dir)
    env.update(
        {
            "LEMONCROW_LOCAL_SERVER_SCRIPT": str(script),
            "LEMONCROW_TOOL_DIR": str(state / "uv-tools"),
            "LC_TEST_SERVER_LOG": str(log),
        }
    )

    result = subprocess.run(
        ["bash", str(UNINSTALL_RUNTIME), "--keep-hosts"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert target.read_text().strip() == "review"
    assert log.read_text().splitlines() == ["stop", "start"]
    assert not (state / "runtime").exists()
    assert not (state / "uv-tools/lemoncrow").exists()
    assert (state / "review/engine/sentinel").read_text() == "review-engine"
    assert (bin_dir / "lc").resolve() == (review / "lc").resolve()


def _isolated_uninstall_all(tmp_path: Path, *host_scripts: str) -> tuple[Path, Path]:
    scripts = tmp_path / "suite"
    scripts.mkdir()
    (scripts / "uninstall.sh").write_text((ROOT / "scripts" / "uninstall.sh").read_text(), encoding="utf-8")
    for host in host_scripts:
        _make_executable(scripts / f"uninstall_{host}.sh")
    return scripts / "uninstall.sh", tmp_path / "runtime-client.log"


def test_uninstall_all_removes_split_runtime_host_registrations(tmp_path: Path) -> None:
    state, bin_dir, _review, runtime = _layout(tmp_path)
    script, calls = _isolated_uninstall_all(tmp_path)
    (runtime / "lemoncrow-client").write_text(f'#!/bin/sh\necho "$*" >> "{calls}"\n', encoding="utf-8")
    env = {**_env(tmp_path, state, bin_dir), "PATH": "/usr/bin:/bin"}

    result = subprocess.run(["bash", str(script)], env=env, cwd=tmp_path, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == [
        "runtime disable claude",
        "runtime disable codex",
        "runtime disable opencode",
        "runtime host remove --engine pi --yes",
    ]
    # A split install ships no per-host uninstallers; that is not a warning.
    assert "No uninstaller for" not in result.stdout + result.stderr


def test_uninstall_all_keeps_hosts_and_warns_on_missing_engine_host_uninstaller(tmp_path: Path) -> None:
    state, bin_dir, _review, runtime = _layout(tmp_path)
    script, calls = _isolated_uninstall_all(tmp_path, "claude")
    (runtime / "lemoncrow-client").write_text(f'#!/bin/sh\necho "$*" >> "{calls}"\n', encoding="utf-8")
    env = {**_env(tmp_path, state, bin_dir), "PATH": "/usr/bin:/bin"}

    kept = subprocess.run(["bash", str(script), "--no-hosts"], env=env, cwd=tmp_path, capture_output=True, text=True)
    assert kept.returncode == 0, kept.stderr
    assert not calls.exists()

    result = subprocess.run(["bash", str(script)], env=env, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "No uninstaller for codex" in result.stdout
    assert "No uninstaller for claude" not in result.stdout

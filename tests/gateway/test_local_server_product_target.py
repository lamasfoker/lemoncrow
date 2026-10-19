from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCAL_SERVER = ROOT / "scripts" / "local_server.sh"


def _fake_python(tmp_path: Path) -> Path:
    target = tmp_path / "python"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o755)
    return target


def _prepare(tmp_path: Path, product: str | None) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    state = home / ".lemoncrow"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "LEMONCROW_HOME": str(state),
            "LEMONCROW_SERVER_PYTHON": str(_fake_python(tmp_path)),
            "LEMONCROW_LOCAL_SERVER_DIR": str(state / "server"),
            # Explicit path disables source-checkout frontend synchronization.
            "LEMONCROW_FRONTEND_DIR": str(state / "frontend"),
            "LEMONCROW_LOCAL_SERVER_SUPERVISOR": "none",
        }
    )
    if product is not None:
        env["LEMONCROW_LOCAL_SERVER_TARGET_PRODUCT"] = product
    else:
        env.pop("LEMONCROW_LOCAL_SERVER_TARGET_PRODUCT", None)
    return subprocess.run(
        ["bash", str(LOCAL_SERVER), "_prepare"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_local_server_can_target_review_only(tmp_path: Path) -> None:
    result = _prepare(tmp_path, "review")
    state = tmp_path / "home" / ".lemoncrow"
    assert result.returncode == 0, result.stderr
    assert (state / "review" / "env").is_file()
    assert not (state / "runtime" / "env").exists()
    assert not (state / "env").exists()
    content = (state / "review" / "env").read_text()
    assert "LEMONCROW_URL=http://127.0.0.1:7420" in content
    assert "LEMONCROW_INSTALL_MODE=local" in content
    assert (state / "server" / "target-product").read_text().strip() == "review"


def test_local_server_runtime_target_writes_no_runtime_endpoint(tmp_path: Path) -> None:
    # Runtime runs as Runtime Lite; the local server only serves its Dashboard.
    result = _prepare(tmp_path, "runtime")
    state = tmp_path / "home" / ".lemoncrow"
    assert result.returncode == 0, result.stderr
    assert not (state / "runtime" / "env").exists()
    assert not (state / "review" / "env").exists()
    assert not (state / "env").exists()


def test_local_server_can_target_both_products(tmp_path: Path) -> None:
    result = _prepare(tmp_path, "both")
    state = tmp_path / "home" / ".lemoncrow"
    assert result.returncode == 0, result.stderr
    assert (state / "review" / "env").is_file()
    assert not (state / "runtime" / "env").exists()
    assert not (state / "env").exists()


def test_local_server_target_choice_persists_across_prepare_calls(tmp_path: Path) -> None:
    first = _prepare(tmp_path, "review")
    assert first.returncode == 0, first.stderr
    second = _prepare(tmp_path, None)
    state = tmp_path / "home" / ".lemoncrow"
    assert second.returncode == 0, second.stderr
    assert (state / "server" / "target-product").read_text().strip() == "review"
    assert (state / "review" / "env").is_file()
    assert not (state / "env").exists()


def test_legacy_target_mode_migrates_to_product_owned_state(tmp_path: Path) -> None:
    result = _prepare(tmp_path, "legacy")
    state = tmp_path / "home" / ".lemoncrow"
    assert result.returncode == 0, result.stderr
    assert not (state / "env").exists()
    assert (state / "review" / "env").is_file()
    assert not (state / "runtime" / "env").exists()
    assert (state / "server" / "target-product").read_text().strip() == "both"


def test_default_target_mode_writes_the_review_endpoint_not_shared_env(tmp_path: Path) -> None:
    result = _prepare(tmp_path, None)
    state = tmp_path / "home" / ".lemoncrow"
    assert result.returncode == 0, result.stderr
    assert not (state / "env").exists()
    assert (state / "review" / "env").is_file()
    assert not (state / "runtime" / "env").exists()
    assert (state / "server" / "target-product").read_text().strip() == "both"


def test_none_target_mode_writes_no_client_endpoint(tmp_path: Path) -> None:
    result = _prepare(tmp_path, "none")
    state = tmp_path / "home" / ".lemoncrow"
    assert result.returncode == 0, result.stderr
    assert not (state / "env").exists()
    assert not (state / "review" / "env").exists()
    assert not (state / "runtime" / "env").exists()
    assert (state / "server" / "target-product").read_text().strip() == "none"


def test_invalid_target_product_is_refused(tmp_path: Path) -> None:
    result = _prepare(tmp_path, "banana")
    assert result.returncode == 2
    assert "must be review, runtime, both, or none" in result.stderr


def test_none_target_cannot_start_server(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = home / ".lemoncrow"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "LEMONCROW_HOME": str(state),
            "LEMONCROW_SERVER_PYTHON": str(_fake_python(tmp_path)),
            "LEMONCROW_LOCAL_SERVER_DIR": str(state / "server"),
            "LEMONCROW_FRONTEND_DIR": str(state / "frontend"),
            "LEMONCROW_LOCAL_SERVER_SUPERVISOR": "none",
            "LEMONCROW_LOCAL_SERVER_TARGET_PRODUCT": "none",
        }
    )
    result = subprocess.run(
        ["bash", str(LOCAL_SERVER), "start"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "no local LemonCrow product is enabled" in result.stderr


def _recording_python(path: Path, log: Path, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{label}' >> '{log}'\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def _prepare_with_owned_engines(tmp_path: Path, product: str, *, override: Path | None = None) -> list[str]:
    home = tmp_path / "home"
    state = home / ".lemoncrow"
    log = tmp_path / "python.log"
    _recording_python(state / "review/engine/bin/python", log, "review")
    _recording_python(state / "uv-tools/lemoncrow/bin/python", log, "runtime")
    if override is not None:
        _recording_python(override, log, "override")
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "LEMONCROW_HOME": str(state),
            "LEMONCROW_LOCAL_SERVER_DIR": str(state / "server"),
            "LEMONCROW_FRONTEND_DIR": str(state / "frontend"),
            "LEMONCROW_LOCAL_SERVER_SUPERVISOR": "none",
            "LEMONCROW_LOCAL_SERVER_TARGET_PRODUCT": product,
        }
    )
    if override is not None:
        env["LEMONCROW_SERVER_PYTHON"] = str(override)
    else:
        env.pop("LEMONCROW_SERVER_PYTHON", None)
    result = subprocess.run(
        ["bash", str(LOCAL_SERVER), "_prepare"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return log.read_text().splitlines()


def test_review_target_uses_review_engine_python_by_default(tmp_path: Path) -> None:
    calls = _prepare_with_owned_engines(tmp_path, "review")
    assert calls
    assert set(calls) == {"review"}


def test_runtime_and_both_targets_use_full_runtime_engine_python(tmp_path: Path) -> None:
    runtime_calls = _prepare_with_owned_engines(tmp_path / "runtime", "runtime")
    both_calls = _prepare_with_owned_engines(tmp_path / "both", "both")
    assert runtime_calls and set(runtime_calls) == {"runtime"}
    assert both_calls and set(both_calls) == {"runtime"}


def test_explicit_server_python_overrides_product_engine_selection(tmp_path: Path) -> None:
    calls = _prepare_with_owned_engines(tmp_path, "review", override=tmp_path / "custom/python")
    assert calls and set(calls) == {"override"}

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("include_client", [True, False])
def test_bundle_requires_exact_client_wheel(tmp_path: Path, include_client: bool) -> None:
    """A same-version registry client must never replace the bundled client."""
    bundle = tmp_path / "bundle"
    scripts = bundle / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "lib").mkdir()
    (scripts / "lib" / "common.sh").write_text(
        """
fail() { echo "$*" >&2; exit 1; }
verbose() { :; }
install_uv_if_needed() { :; }
stop_existing_lemoncrow_processes() { :; }
warn_on_foreign_cli_collision() { :; }
spin_tail() { shift; "$@"; }
ensure_lc_alias() { :; }
persist_install_record() { :; }
warn() { echo "$*" >&2; }
""",
        encoding="utf-8",
    )
    source = (ROOT / "scripts" / "bundle.sh").read_text(encoding="utf-8")
    # Run the real install function with only unrelated setup orchestration stubbed.
    (scripts / "bundle.sh").write_text(source.rsplit('main "$@"', 1)[0], encoding="utf-8")
    for folder, name in (
        ("bin", "lemoncrow-0.8.0-py3-none-any.whl"),
        ("server", "lemoncrow_server-0.1.0-py3-none-any.whl"),
        ("vendor", "babel-99.0.0-py3-none-any.whl"),
    ):
        (bundle / folder).mkdir(exist_ok=True)
        (bundle / folder / name).touch()
    client = bundle / "vendor" / "lemoncrow_client-0.1.0-py3-none-any.whl"
    if include_client:
        client.touch()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(
        '#!/usr/bin/env bash\nif [[ "$1 $2" == "tool install" ]]; then printf "%s\\n" "$@" > "$UV_ARGS_LOG"; fi\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    args_log = tmp_path / "uv-args"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "LEMONCROW_INSTALL_DIR": str(bundle),
            "LEMONCROW_BIN_DIR": str(tmp_path / "bin"),
            "LEMONCROW_TOOL_DIR": str(tmp_path / "tools"),
            "UV_ARGS_LOG": str(args_log),
        }
    )
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; install_lemoncrow_from_wheel', "test", str(scripts / "bundle.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if include_client:
        assert result.returncode == 0, result.stderr
        args = args_log.read_text(encoding="utf-8").splitlines()
        with_paths = [Path(args[index + 1]).resolve() for index, arg in enumerate(args) if arg == "--with"]
        assert client in with_paths
        assert str(bundle / "server" / "lemoncrow_server-0.1.0-py3-none-any.whl") in args
    else:
        assert result.returncode != 0
        assert "requires the bundled LemonCrow client wheel" in result.stderr
        assert not args_log.exists()

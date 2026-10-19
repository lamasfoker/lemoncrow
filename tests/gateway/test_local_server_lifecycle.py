from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "local_server.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.parametrize("supervisor", ["systemd", "none"])
def test_start_rejects_another_installations_listener_before_launch(tmp_path: Path, supervisor: str) -> None:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "systemctl.log"
    fake_python = tmp_path / "python"
    _write_executable(
        fake_python,
        f"""#!/bin/bash
if [[ "$1" == - && "${{2:-}}" =~ ^[0-9]+$ ]]; then exec {str(sys.executable)!r} "$@"; fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "systemctl",
        f"""#!/bin/bash
printf '%s\\n' "$*" >> {str(calls)!r}
if [[ "$*" == *is-active* ]]; then exit 1; fi
if [[ "$*" == *MainPID* ]]; then echo 0; fi
exit 0
""",
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        env = {
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "LEMONCROW_HOME": str(home / ".lemoncrow"),
            "LEMONCROW_SERVER_PYTHON": str(fake_python),
            "LEMONCROW_FRONTEND_DIR": str(tmp_path / "frontend"),
            "LEMONCROW_LOCAL_SERVER_SUPERVISOR": supervisor,
            "LEMONCROW_LOCAL_SERVER_PORT": str(port),
        }
        result = subprocess.run(["bash", str(SCRIPT), "start"], env=env, capture_output=True, text=True)
        assert result.returncode != 0
        assert f"Port {port} is already in use" in result.stderr
        assert "ready at" not in result.stdout
        assert listener.getsockname()[1] == port
    if calls.exists():
        assert "restart " not in calls.read_text()


def test_process_start_does_not_accept_health_from_an_unrelated_server(tmp_path: Path) -> None:
    """A stale 200 on the target port cannot bless a failed fallback child."""
    home = tmp_path / "home"
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("ok", encoding="utf-8")
    (frontend / "assets").mkdir()

    fake_python = tmp_path / "fake-python"
    _write_executable(
        fake_python,
        """#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  -c) exit 0 ;;
  -) exit 0 ;; # Simulate an unrelated healthy process already answering /healthz.
  -m) sleep 0.2; exit 1 ;;
  *) exit 0 ;;
esac
""",
    )
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "LEMONCROW_HOME": str(home / ".lemoncrow"),
            "LEMONCROW_SERVER_PYTHON": str(fake_python),
            "LEMONCROW_LOCAL_SERVER_SUPERVISOR": "none",
            "LEMONCROW_LOCAL_SERVER_START_TIMEOUT": "1",
            "LEMONCROW_LOCAL_SERVER_PORT": "17499",
            "LEMONCROW_FRONTEND_DIR": str(frontend),
        }
    )

    result = subprocess.run(
        ["bash", str(SCRIPT), "start"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "did not become healthy" in result.stderr


def test_stop_keeps_fallback_pid_until_process_cleanup(tmp_path: Path) -> None:
    """Supervisor cleanup must not erase the fallback PID before it is stopped."""
    home = tmp_path / "home"
    state = home / ".lemoncrow" / "server"
    state.mkdir(parents=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "systemctl", "#!/usr/bin/env bash\nexit 1\n")

    process = subprocess.Popen(["bash", "-c", "exec -a lemoncrow_server_core sleep 60"])
    try:
        (state / "server.pid").write_text(f"{process.pid}\n", encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "LEMONCROW_HOME": str(home / ".lemoncrow"),
                "LEMONCROW_LOCAL_SERVER_DIR": str(state),
                "LEMONCROW_SERVER_PYTHON": "/bin/true",
                "LEMONCROW_LOCAL_SERVER_SUPERVISOR": "none",
            }
        )
        result = subprocess.run(
            ["bash", str(SCRIPT), "stop"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        process.wait(timeout=2)
        assert not (state / "server.pid").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)


def test_systemd_restart_is_handed_off_without_stopping_own_unit(tmp_path: Path) -> None:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "calls.log"

    fake_python = tmp_path / "fake-python"
    _write_executable(
        fake_python,
        """#!/usr/bin/env bash
set -euo pipefail
exit 0
""",
    )
    _write_executable(
        fake_bin / "systemctl",
        f"""#!/usr/bin/env bash
printf 'systemctl %s\\n' "$*" >> {str(log)!r}
if [[ "$*" == *"show-environment"* ]]; then exit 0; fi
if [[ "$*" == *"DefaultLimitNOFILE"* ]]; then echo 65536; exit 0; fi
if [[ "$*" == *"MainPID"* ]]; then echo 4242; exit 0; fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "systemd-run",
        f"""#!/usr/bin/env bash
printf 'systemd-run %s\\n' "$*" >> {str(log)!r}
exit 0
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "LEMONCROW_HOME": str(home / ".lemoncrow"),
            "LEMONCROW_SERVER_PYTHON": str(fake_python),
            "LEMONCROW_LOCAL_SERVER_SUPERVISOR": "systemd",
            "LEMONCROW_LOCAL_SERVER_RESTART_HANDOFF": "0",
        }
    )

    result = subprocess.run(
        ["bash", str(SCRIPT), "restart"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text(encoding="utf-8")
    assert "systemd-run " in calls
    assert "_restart-worker systemd" in calls
    assert "systemctl --user stop " not in calls
    assert "systemctl --user disable " not in calls
    assert "restart handed off" in result.stdout

    status = home / ".lemoncrow" / "server" / "restart.status"
    assert status.read_text(encoding="utf-8").startswith("scheduled\t")


def test_limits_on_launchd_neither_fails_the_install_nor_gives_systemd_advice(tmp_path: Path) -> None:
    """The dev install reports limits under `set -e`: a systemd-only probe broke macOS."""
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "uname", "#!/usr/bin/env bash\nprintf 'Darwin\\n'\n")
    _write_executable(fake_bin / "systemctl", "#!/usr/bin/env bash\nexit 1\n")
    _write_executable(fake_bin / "launchctl", "#!/usr/bin/env bash\nexit 0\n")
    env = os.environ.copy()
    env.update({"HOME": str(home), "PATH": f"{fake_bin}:{env['PATH']}", "LEMONCROW_HOME": str(home / ".lemoncrow")})

    result = subprocess.run(
        ["bash", str(SCRIPT), "limits"], cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert "systemd" not in result.stdout + result.stderr


def test_configure_nofile_does_not_ask_for_sudo_again_before_the_reboot(tmp_path: Path) -> None:
    """The user manager applies the drop-ins only after a reboot; a reinstall must not re-prompt."""
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sudo_log = tmp_path / "sudo.log"
    _write_executable(fake_bin / "uname", "#!/usr/bin/env bash\nprintf 'Linux\\n'\n")
    _write_executable(fake_bin / "systemctl", "#!/usr/bin/env bash\nprintf '65536\\n'\n")
    _write_executable(fake_bin / "sudo", f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> {str(sudo_log)!r}\nexit 1\n")
    conf_root = tmp_path / "systemd"
    env = os.environ.copy()
    for name in ("LEMONCROW_NON_INTERACTIVE", "LEMONCROW_DRY_RUN"):
        env.pop(name, None)
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "LEMONCROW_HOME": str(home / ".lemoncrow"),
            "LEMONCROW_SYSTEMD_CONF_ROOT": str(conf_root),
        }
    )

    def configure() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), "configure-nofile"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    unconfigured = configure()
    assert unconfigured.returncode == 0, unconfigured.stderr
    assert "reboot to activate" not in unconfigured.stdout
    assert "needs a terminal" in unconfigured.stderr

    (conf_root / "system" / "user@.service.d").mkdir(parents=True)
    (conf_root / "system" / "user@.service.d" / "lemoncrow.conf").write_text("[Service]\nLimitNOFILE=1048576\n")
    (conf_root / "user.conf.d").mkdir(parents=True)
    (conf_root / "user.conf.d" / "lemoncrow.conf").write_text("[Manager]\nDefaultLimitNOFILE=1048576\n")
    configured = configure()

    assert configured.returncode == 0, configured.stderr
    assert "reboot to activate them (current: 65536)" in configured.stdout
    assert configured.stderr == ""
    assert not sudo_log.exists()


def test_supervised_launchd_plist_carries_agent_install_directories(tmp_path: Path) -> None:
    """launchd's default PATH is only the four base directories.

    Without an explicit PATH the server cannot see `claude`, `codex`, `opencode`
    or `lemoncode` installed under ~/.local/bin, ~/.opencode/bin and
    ~/.lemoncrow/bin, so every one of them reports as not installed.
    """
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    agents = home / ".local" / "bin"
    agents.mkdir(parents=True)
    _write_executable(agents / "claude", "#!/usr/bin/env bash\nexit 0\n")

    fake_python = tmp_path / "fake-python"
    _write_executable(
        fake_python,
        """#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  -c) exit 0 ;;
  -) exit 0 ;;
  *) exit 0 ;;
esac
""",
    )
    _write_executable(fake_bin / "uname", "#!/usr/bin/env bash\nprintf 'Darwin\\n'\n")
    _write_executable(fake_bin / "systemctl", "#!/usr/bin/env bash\nexit 1\n")
    _write_executable(
        fake_bin / "launchctl",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "list" ]]; then printf '{\\n    "PID" = 4242;\\n}\\n'; fi
exit 0
""",
    )

    spaced_bin = tmp_path / "agent tools"
    spaced_bin.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            # launchd's own default, which cannot see ~/.local/bin.
            "PATH": f"{fake_bin}:{spaced_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            # An executable shell returning no PATH must work under Bash 3.2 nounset.
            "SHELL": "/usr/bin/true",
            "LEMONCROW_HOME": str(home / ".lemoncrow"),
            "LEMONCROW_SERVER_PYTHON": str(fake_python),
            "LEMONCROW_LOCAL_SERVER_SUPERVISOR": "launchd",
            "LEMONCROW_LOCAL_SERVER_PORT": "7441",
        }
    )

    subprocess.run(
        ["bash", str(SCRIPT), "start"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    plist = home / "Library" / "LaunchAgents" / "com.lemoncrow.local-server.plist"
    content = plist.read_text(encoding="utf-8")
    assert "<key>EnvironmentVariables</key>" in content
    match = re.search(r"<key>PATH</key><string>([^<]+)</string>", content)
    assert match, content
    entries = match.group(1).split(":")
    assert str(agents) in entries, entries
    assert str(spaced_bin) in entries, entries
    # Every entry must be a real directory and appear exactly once.
    assert all(Path(entry).is_dir() for entry in entries), entries
    assert len(entries) == len(set(entries)), entries


def test_launchd_start_uses_machine_auth_and_leaves_remote_mcp_alone(tmp_path: Path) -> None:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    python_log = tmp_path / "python.log"

    fake_python = tmp_path / "fake-python"
    _write_executable(
        fake_python,
        f"""#!/usr/bin/env bash
set -euo pipefail
case "${{1:-}}" in
  -c) exit 0 ;;
  -)
    if [[ "$#" == "1" ]]; then
      printf 'repair url=%s mode=%s home=%s\\n' "${{LEMONCROW_URL:-}}" "${{LEMONCROW_INSTALL_MODE:-}}" "${{LEMONCROW_HOME:-}}" >> {str(python_log)!r}
    fi
    exit 0
    ;;
  *) exit 0 ;;
esac
""",
    )
    _write_executable(fake_bin / "uname", "#!/usr/bin/env bash\nprintf 'Darwin\\n'\n")
    _write_executable(fake_bin / "systemctl", "#!/usr/bin/env bash\nexit 1\n")
    _write_executable(
        fake_bin / "launchctl",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf 'launchctl %s\\n' "$*" >> {str(launchctl_log)!r}
if [[ "${{1:-}}" == "list" ]]; then
  printf '{{\\n    "PID" = 4242;\\n}}\\n'
fi
exit 0
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "LEMONCROW_HOME": str(home / ".lemoncrow"),
            "LEMONCROW_SERVER_PYTHON": str(fake_python),
            "LEMONCROW_LOCAL_SERVER_SUPERVISOR": "launchd",
            "LEMONCROW_LOCAL_SERVER_PORT": "7438",
            "LEMONCROW_URL": "http://127.0.0.1:7999",
        }
    )

    started = subprocess.run(
        ["bash", str(SCRIPT), "start"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "ready at http://127.0.0.1:7438" in started.stdout
    plist = home / "Library" / "LaunchAgents" / "com.lemoncrow.local-server.plist"
    content = plist.read_text(encoding="utf-8")
    assert "<string>--token-file</string>" in content
    assert f"<string>{home / '.lemoncrow' / 'token'}</string>" in content
    assert "<string>7438</string>" in content
    assert "$(seq " not in SCRIPT.read_text(encoding="utf-8")
    # Remote MCP is served by the client, independent of this server: the
    # server lifecycle never repairs or restarts its units.
    assert not python_log.exists()
    calls = launchctl_log.read_text(encoding="utf-8")
    assert f"launchctl load {plist}" in calls

    status = subprocess.run(
        ["bash", str(SCRIPT), "status"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "supervisor=launchd" in status.stdout
    assert "pid=4242" in status.stdout

    subprocess.run(
        ["bash", str(SCRIPT), "stop"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert not plist.exists()


def test_start_targets_the_backend_it_actually_started_and_leaves_remote_mcp_alone(tmp_path: Path) -> None:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "python.log"

    fake_python = tmp_path / "fake-python"
    _write_executable(
        fake_python,
        f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "${{1:-}}" == "-" && "$#" == "1" ]]; then
  printf 'repair url=%s mode=%s home=%s\\n' "${{LEMONCROW_URL:-}}" "${{LEMONCROW_INSTALL_MODE:-}}" "${{LEMONCROW_HOME:-}}" >> {str(calls)!r}
fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "systemctl",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *"show-environment"* ]]; then exit 0; fi
if [[ "$*" == *"DefaultLimitNOFILE"* ]]; then echo 65536; exit 0; fi
if [[ "$*" == *"MainPID"* ]]; then echo 4242; exit 0; fi
exit 0
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "LEMONCROW_HOME": str(home / ".lemoncrow"),
            "LEMONCROW_SERVER_PYTHON": str(fake_python),
            "LEMONCROW_LOCAL_SERVER_SUPERVISOR": "systemd",
            "LEMONCROW_LOCAL_SERVER_PORT": "7437",
            # This is the regression: a caller's stale target must not win over
            # the backend this lifecycle invocation just started.
            "LEMONCROW_URL": "http://127.0.0.1:7999",
        }
    )

    result = subprocess.run(
        ["bash", str(SCRIPT), "start"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not calls.exists()
    state = home / ".lemoncrow"
    assert not (state / "env").exists()
    assert not (state / "runtime" / "env").exists()
    target = (state / "review" / "env").read_text(encoding="utf-8")
    assert "LEMONCROW_URL=http://127.0.0.1:7437" in target
    assert "LEMONCROW_INSTALL_MODE=local" in target

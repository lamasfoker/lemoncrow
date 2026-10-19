"""Optional macOS per-user LaunchAgent for a single paired Review checkout.

No sudo, listener, Runtime/MCP, secrets in plist, or automatic model work.
Install only writes an owner-private plist. The user explicitly starts it;
stop unloads it and remove refuses to erase a still-loaded agent.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import TextIO


def _label(repo: Path) -> str:
    digest = hashlib.sha256(os.fsencode(str(repo.resolve()))).hexdigest()[:16]
    return f"com.lemoncrow.review.{digest}"


def _plist(*, executable: Path, repo: Path, state_dir: Path) -> bytes:
    if not all(path.is_absolute() for path in (executable, repo, state_dir)):
        raise ValueError("Review LaunchAgent paths must be absolute")
    return plistlib.dumps(
        {
            "Label": _label(repo),
            "ProgramArguments": [str(executable), "device", "serve"],
            "WorkingDirectory": str(repo),
            "EnvironmentVariables": {"LEMONCROW_HOME": str(state_dir), "GIT_OPTIONAL_LOCKS": "0"},
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 10,
        },
        fmt=plistlib.FMT_XML,
        sort_keys=True,
    )


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], text=True, capture_output=True, check=False, timeout=15)


def manage_launchd(
    action: str,
    *,
    binary: Path,
    repo: Path,
    state_dir: Path,
    sink: TextIO,
    dry_run: bool = False,
) -> int:
    """Run launchctl only after an explicit user command, never at import/startup."""
    if action not in {"install", "start", "stop", "status", "remove"}:
        raise ValueError("unsupported Review LaunchAgent operation")
    unit_dir = Path.home() / "Library" / "LaunchAgents"
    unit = unit_dir / (_label(repo) + ".plist")
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{_label(repo)}"
    if action == "install":
        content = _plist(executable=binary, repo=repo, state_dir=state_dir)
        if dry_run:
            sink.write(content.decode("utf-8"))
            return 0
        unit_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if unit_dir.is_symlink():
            raise ValueError("LaunchAgents directory may not be a symlink")
        if unit.exists() or unit.is_symlink():
            info = unit.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("LaunchAgent destination may not redirect or replace other owners' data")
        fd, temp = tempfile.mkstemp(prefix=".review-agent-", dir=unit_dir)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, unit)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        sink.write("Review LaunchAgent installed; start explicitly with lcr service start.\n")
        return 0
    if action != "status" and (not unit.is_file() or unit.is_symlink()):
        sink.write("No Review LaunchAgent is installed for this checkout.\n")
        return 1
    if action == "status":
        result = _launchctl("print", target)
        sink.write("Review LaunchAgent active.\n" if result.returncode == 0 else "Review LaunchAgent inactive.\n")
        return 0 if result.returncode == 0 else 1
    if action in {"stop", "remove"}:
        result = _launchctl("bootout", target)
        if result.returncode != 0 and _launchctl("print", target).returncode == 0:
            sink.write("Unable to stop Review LaunchAgent; leaving installation intact.\n")
            return 1
        if action == "stop":
            sink.write("Review LaunchAgent stopped.\n")
            return 0
        unit.unlink()
        sink.write("Review LaunchAgent removed; paired credential unchanged.\n")
        return 0
    result = _launchctl("bootstrap", domain, str(unit))
    sink.write("Review LaunchAgent started.\n" if result.returncode == 0 else "Unable to start Review LaunchAgent.\n")
    return 0 if result.returncode == 0 else 1

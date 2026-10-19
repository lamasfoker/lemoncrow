"""Persistent Runtime Lite extension in Pi's native agent directory."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def agent_dir() -> Path:
    configured = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".pi" / "agent"


def _settings() -> tuple[Path, dict[str, object], list[object]]:
    path = agent_dir() / "settings.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid Pi settings; refusing to overwrite {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Invalid Pi settings; refusing to overwrite {path}")
    extensions = value.get("extensions", [])
    if not isinstance(extensions, list):
        raise RuntimeError(f"Invalid Pi extensions list; refusing to overwrite {path}")
    return path, value, extensions


def _extension_path() -> Path:
    return agent_dir() / "lemoncrow" / "lite.mjs"


def _write(path: Path, content: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(content)
            output.close()
            temporary.chmod(mode)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def enabled() -> bool:
    try:
        _, _, extensions = _settings()
        return str(_extension_path()) in extensions and _extension_path().is_file()
    except (OSError, RuntimeError):
        return False


def _extension_source() -> str:
    from .host_trial import _pi_lite_extension_path, _runtime_mcp_invocation

    command, args = _runtime_mcp_invocation("pi")
    invocation = json.dumps({"command": command, "args": args})
    source = _pi_lite_extension_path().read_text(encoding="utf-8")
    marker = "const installedInvocation = null;"
    if marker not in source:
        raise RuntimeError("Pi extension is missing its installation marker")
    return source.replace(marker, f"const installedInvocation = {invocation};", 1)


def refresh() -> None:
    """Rewrite an installed extension that predates this client; settings stay as they are."""
    extension = _extension_path()
    expected = _extension_source()
    if extension.read_text(encoding="utf-8") != expected:
        _write(extension, expected)


def install() -> Path:
    settings, value, extensions = _settings()
    extension = _extension_path()
    _write(extension, _extension_source())
    value["extensions"] = [entry for entry in extensions if entry != str(extension)] + [str(extension)]
    _write(settings, json.dumps(value, indent=2) + "\n")
    return settings


def remove() -> bool:
    settings, value, extensions = _settings()
    extension = _extension_path()
    retained = [entry for entry in extensions if entry != str(extension)]
    changed = retained != extensions
    if changed:
        value["extensions"] = retained
        _write(settings, json.dumps(value, indent=2) + "\n")
    if extension.is_file():
        extension.unlink()
        changed = True
        try:
            extension.parent.rmdir()
        except OSError:
            pass
    return changed


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Remove LemonCrow's native Pi integration")
    parser.add_argument("--remove", action="store_true", required=True)
    parser.parse_args()
    remove()

"""Focused OpenCode V2 config migration for LemonCrow; preserve unrelated entries.

Called by source and packaged installers. No third-party parser dependencies.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def read_jsonc(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    # Remove comments only outside string literals, then trailing commas.
    tokens = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/')
    text = tokens.sub(lambda match: match.group() if match.group().startswith('"') else "", text)
    trailing = re.compile(r'"(?:\\.|[^"\\])*"|,\s*(?=[}\]])')
    text = trailing.sub(lambda match: match.group() if match.group().startswith('"') else "", text)
    result = json.loads(text) if text.strip() else {}
    if not isinstance(result, dict):
        raise ValueError(f"OpenCode config must be an object: {path}")
    return result


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if value:
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    elif path.exists():
        path.unlink()


def merge_v2(path: Path, entry: dict) -> None:
    config = read_jsonc(path)
    mcp = config.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        raise ValueError("mcp must be a mapping")
    mcp.pop("lc", None)  # migrated V1 entry
    mcp.setdefault("servers", {})["lc"] = entry["mcp"]["servers"]["lc"]
    legacy_permission = config.get("permission")
    if isinstance(legacy_permission, dict):
        legacy_permission.pop("lc_*", None)
        if not legacy_permission:
            config.pop("permission", None)
    rule = {"action": "lc_*", "resource": "*", "effect": "allow"}
    permissions = config.setdefault("permissions", [])
    if not isinstance(permissions, list):
        raise ValueError("permissions must be a list")
    if rule not in permissions:
        permissions.append(rule)
    write(path, config)


def remove_v2(path: Path) -> None:
    if not path.exists():
        return
    config = read_jsonc(path)
    mcp = config.get("mcp")
    if isinstance(mcp, dict):
        servers = mcp.get("servers")
        if isinstance(servers, dict):
            servers.pop("lc", None)
            if not servers:
                mcp.pop("servers", None)
        mcp.pop("lc", None)
        if not mcp:
            config.pop("mcp", None)
    legacy = config.get("permission")
    if isinstance(legacy, dict):
        legacy.pop("lc_*", None)
        if not legacy:
            config.pop("permission", None)
    permissions = config.get("permissions")
    if isinstance(permissions, list):
        config["permissions"] = [
            rule for rule in permissions if rule != {"action": "lc_*", "resource": "*", "effect": "allow"}
        ]
        if not config["permissions"]:
            config.pop("permissions", None)
    write(path, config)


def merge_cli(path: Path, plugin_dir: str) -> None:
    config = read_jsonc(path)
    plugins = config.setdefault("plugins", [])
    if not isinstance(plugins, list):
        raise ValueError("cli.json plugins must be a list")
    if plugin_dir not in plugins:
        plugins.append(plugin_dir)
    write(path, config)


def remove_cli(path: Path, plugin_dir: str) -> None:
    if not path.exists():
        return
    config = read_jsonc(path)
    plugins = config.get("plugins")
    if isinstance(plugins, list):
        config["plugins"] = [plugin for plugin in plugins if plugin != plugin_dir]
        if not config["plugins"]:
            config.pop("plugins", None)
    write(path, config)


def main() -> None:
    command, file, *args = sys.argv[1:]
    path = Path(file)
    if command == "merge":
        merge_v2(path, json.loads(args[0]))
    elif command == "remove":
        remove_v2(path)
    elif command == "cli-merge":
        merge_cli(path, args[0])
    elif command == "cli-remove":
        remove_cli(path, args[0])
    else:
        raise ValueError(command)


if __name__ == "__main__":
    main()

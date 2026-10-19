"""Hidden checkout development bootstrap; standard library only.

The public client ships this dispatcher, never the private server or another
executable. Explicit development commands use the checkout's locked environment.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

_PRIVATE_DEV_MANIFEST = ".lemoncrow-dev.json"


def development_checkout(explicit: str | None = None) -> Path:
    if explicit:
        candidates = [Path(explicit).expanduser().resolve()]
    else:
        cwd = Path.cwd().resolve()
        candidates = [cwd, *cwd.parents, Path(__file__).resolve().parents[3]]
    for candidate in candidates:
        if (candidate / _PRIVATE_DEV_MANIFEST).is_file():
            return candidate
    raise RuntimeError("lc dev requires the private LemonCrow checkout; run there or pass --checkout DIR")


def dev_main(argv: Sequence[str], sink: TextIO | None = None) -> int:
    arguments = list(argv)
    explicit = os.environ.get("LEMONCROW_DEV_CHECKOUT")
    if arguments[:1] == ["--checkout"]:
        if len(arguments) < 2:
            sys.stderr.write("lc dev --checkout requires a directory\n")
            return 2
        explicit = arguments[1]
        arguments = arguments[2:]
    try:
        checkout = development_checkout(explicit)
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError("lc dev requires uv for the checkout's locked development environment")
        tool_arguments = arguments[2:] if arguments[:1] == ["--root"] else arguments
        if tool_arguments[:1] and tool_arguments[0] in {"eval", "perf", "defaults"}:
            command = [
                uv,
                "run",
                "--project",
                str(checkout),
                "--locked",
                "python",
                "-m",
                "lemoncrow.gateway.cli.developer_tools",
                *arguments,
            ]
        else:
            # The private checkout declares its own developer entry point;
            # the public client never knows the private repository layout.
            document = json.loads((checkout / _PRIVATE_DEV_MANIFEST).read_text(encoding="utf-8"))
            if not isinstance(document, dict) or document.get("schema_version") != 1:
                raise RuntimeError("the checkout's private development manifest is invalid")
            project_name = document.get("project")
            entry_name = document.get("entrypoint")
            if not isinstance(project_name, str) or not isinstance(entry_name, str):
                raise RuntimeError("the checkout's private development manifest is incomplete")
            project = (checkout / project_name).resolve()
            entry = (checkout / entry_name).resolve()
            if not project.is_relative_to(checkout) or not entry.is_relative_to(checkout) or not entry.is_file():
                raise RuntimeError("the checkout's private development manifest escapes the checkout")
            command = [uv, "run", "--project", str(project), "--locked", "python", str(entry), *arguments]
        os.execv(uv, command)
    except (OSError, ValueError, RuntimeError) as exc:
        # Errors go to stderr even when the caller passes a stdout sink.
        sys.stderr.write(f"lc dev: {exc}\n")
        return 2
    return 0

"""Provider-neutral opt-in Review checkpoint hooks. Never execute input as instructions.

Agent hosts may invoke `lcr hook after-edit|after-checkpoint|on-exit` with
arbitrary event JSON on stdin. We deliberately ignore that untrusted body:
Git is the only snapshot authority, and `lcr watch --once` handles publication.
No Runtime/MCP, native plugin/session credentials or host config mutations.
"""

from __future__ import annotations

from typing import TextIO

from .review_watch import watch_main

_EVENTS = frozenset({"after-edit", "after-checkpoint", "on-exit"})


def hook_main(arguments: list[str], *, sink: TextIO) -> int:
    usage = (
        "Usage: lcr hook after-edit|after-checkpoint|on-exit [--staged]\n"
        "Configure any Claude Code, Codex, OpenCode, Cursor, Pi or custom agent "
        "hook to invoke this executable. Its stdin is ignored; Git controls the source.\n"
        "The hook is optional; a persistent `lcr device serve` also captures changes.\n"
    )
    if arguments in ([], ["--help"], ["help"], ["-h"]):
        sink.write(usage)
        return 0 if arguments else 2
    if arguments[0] not in _EVENTS or len(arguments) > 2 or (len(arguments) == 2 and arguments[1] != "--staged"):
        sink.write(usage)
        return 2
    # A transient SSH/sandbox agent is not permitted to push a Review without
    # explicit configured Hosted user credentials and/or a valid device pairing.
    return watch_main(
        [
            "--once",
            "--json",
            "--no-impact",
            *(
                ["--staged"]
                if len(arguments) == 2
                else ["--include-clean-head"] if arguments[0] != "after-edit" else []
            ),
        ],
        sink=sink,
    )

"""Remote MCP for chat apps, served by the client itself.

ChatGPT, Claude.ai and other remote MCP hosts cannot spawn a stdio process on
the developer's machine; they reach an HTTPS URL. ``lc runtime mcp serve``
therefore runs one opt-in loopback gateway behind a tunnel. It never needs a
LemonCrow server: in Lite mode each MCP session runs Runtime Lite in the gateway
process; in hosted mode each session runs the thin client, whose client-site
tools execute here and whose server-site tools go to the hosted URL.

Standard library only, like the rest of the package.
"""

from __future__ import annotations

__all__: list[str] = []

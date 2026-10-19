# LemonCrow OpenCode V2 sidebar

The V2 plugin is separate from the OpenCode V1 `@opencode-ai/plugin/tui` extension.
The installer copies this package into `~/.config/opencode/lemoncrow-status-v2`
and adds that directory to `cli.json` → `plugins`. V2 automatically loads
`./tui` from the package exports. It contributes to `sidebar.content` and
uses OpenCode's native `@opencode/plugin/tui` surface.

The panel never estimates savings without a LemonCrow snapshot. Set
`LEMONCROW_STATUS_FILE` to a gateway-managed JSON snapshot to enable
usage, token, tool-call, cost, and savings metrics.

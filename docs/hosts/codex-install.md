# Codex integration

LemonCrow integrates with Codex as a plugin with Codex-native lifecycle hooks and LemonCrow tools.

## Try it in the current repository

```bash
lc with codex
```

Codex keeps its normal authentication, model, and saved settings. LemonCrow prepares the repository index and adds its plugin to that launch.

## Keep LemonCrow enabled

```bash
lc rt enable codex
```

Check it:

```bash
lc rt status --host codex
```

Remove it:

```bash
lc rt disable codex
```

## Source-checkout install

```bash
bash scripts/install_codex.sh
```

The Codex integration can include:

- the LemonCrow plugin bundle;
- global/project agent instructions;
- LemonCrow custom agents;
- lifecycle hooks for recorded evidence;
- the Codex tool-connection configuration.

Restart Codex after the first install or after plugin changes. Some Codex builds can require plugin activation/trust through Codex's `/plugins` UI.

## Project-specific agent setup

The source/development installer can materialize project-local agent definitions and LemonCrow guidance under the project's Codex configuration. Keep project-specific model pins in project-local settings rather than modifying the shared global plugin.

## Verify

From the shell:

```bash
codex --version
lc rt status --host codex
lc rt status
```

Inside Codex, use:

```text
/plugins
```

when you need to inspect plugin activation.

## Implementation/debug files

Codex's plugin bundle contains its own tool-connection config plus generated skills/agents. Contributors may see `.mcp.json` and `mcp_servers.*` entries because those are Codex's underlying external-tool configuration format.

Normal product setup should use `lc with codex` or `lc rt enable codex`; you should not need to manually run a LemonCrow server.

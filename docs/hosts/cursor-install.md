# Cursor integration (IDE + `cursor-agent`)

Cursor uses an installed LemonCrow integration rather than the `lc with` temporary-launch path. The Cursor IDE and `cursor-agent` CLI share the same integration files, so one install covers both.

The public installer (`curl -fsSL https://lemoncrow.com/install.sh | bash`) and `lc rt enable` do not configure Cursor; `lc rt enable cursor` reports `unsupported agent 'cursor'`. Today the integration needs a clone of this repository.

## Install from a source checkout

```bash
bash scripts/install_cursor.sh
```

Workspace-specific rules/hooks:

```bash
bash scripts/install_cursor.sh --workspace /path/to/repo
```

Optional enforcement modes for development/benchmark work:

```bash
bash scripts/install_cursor.sh --enforce soft
bash scripts/install_cursor.sh --enforce hard
```

## What gets installed

Depending on scope, the installer can write:

- Cursor's LemonCrow external-tool connection (`~/.cursor/mcp.json`);
- lifecycle hooks (`sessionStart` and `stop`);
- project-local Cursor rules;
- `cursor-agent` approval for the LemonCrow tool integration.

The user-facing product is the LemonCrow Cursor integration. Cursor happens to store its external-tool connection in an `mcp.json` file; that protocol detail does not mean you need to operate a separate LemonCrow server.

## Verify

```bash
bash scripts/verify_cursor.sh
```

For a workspace install:

```bash
bash scripts/verify_cursor.sh --workspace /path/to/repo
```

Cursor's own debug commands can confirm the tool connection:

```bash
cursor-agent mcp list
cursor-agent mcp list-tools lemoncrow
```

## Lifecycle evidence

Cursor exposes `sessionStart` and `stop` hooks to both the IDE and CLI. LemonCrow uses those hooks to associate tool activity and usage evidence with the Cursor session where the host exposes enough information.

Cursor privacy/ghost modes can limit provider-usage data, so savings attribution is deliberately conservative when evidence is missing.

## Cost and usage

Use LemonCrow's normal usage surfaces:

```bash
lc usage
lc usage --help
```

Do not assume Cursor has the same cost-saving profile as the Claude/Codex benchmark path: Cursor keeps its own built-in tools and server-side behavior.

## Advanced implementation detail

The Cursor config entry identifies the host as `cursor` so LemonCrow labels traces/evidence correctly. The low-level protocol is documented in [Protocol / MCP reference](../sdk/mcp.md) for contributors who need it.

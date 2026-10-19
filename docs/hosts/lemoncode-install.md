# LemonCode managed host

LemonCode is LemonCrow's maintained OpenCode fork and a public coding-agent host. Like Pi, its binary lifecycle is managed independently from the Hosted `lc code` product.

## Manage LemonCode

```bash
lc rt host install --engine lemoncode
lc rt host status --engine lemoncode
lc rt host update --engine lemoncode
lc rt host remove --engine lemoncode --yes
```

Source builds are available with:

```bash
lc rt host build --engine lemoncode --source /path/to/lemoncode
```

The managed binary is `lemoncode-host`; LemonCrow no longer installs a `lemoncode` console alias that redirects into `lc code`.

## Relationship to Runtime and Hosted

Runtime Lite remains the open-source local repository-intelligence layer used by existing-agent integrations. LemonCode itself is public and can be configured with LemonCrow's local integration artifacts.

`lc code` has a different meaning: it is the Hosted LemonCrow-owned coding agent. A LemonCode host must never silently redirect into that Hosted command.

Hosted or Enterprise Runtime may also choose LemonCode as an implementation/frontend detail. That does not make the LemonCode host binary private.

## Why LemonCode still exists

LemonCode remains useful because LemonCrow controls its source and can integrate frontend UI directly. It also provides a managed alternative to stock OpenCode while keeping the host lifecycle auditable and independently replaceable.

The LemonCode config/data directories are isolated from a stock OpenCode install.

## Source-checkout development

Install LemonCode development integration artifacts with:

```bash
bash scripts/install_lemoncode.sh
```

or for a workspace:

```bash
bash scripts/install_lemoncode.sh --workspace /path/to/repo
```

## Protocol/config details

LemonCode inherits OpenCode-shaped configuration, including an `mcp` section for external tools. That is an implementation detail of the host integration, not the product meaning of `lc code`.

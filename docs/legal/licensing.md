# Licensing

The LemonCrow **public/local source mirror** is licensed under the Apache License, Version 2.0. The exact grant is defined by [`LICENSE`](../../LICENSE) and [`LICENSE-APACHE`](../../LICENSE-APACHE); this page explains the current product/source boundary in plain language and does not replace those license texts.

## Public Apache-2.0 source

The public mirror includes the local/open-source product code, including public `lemoncrow.pro` modules, `server/`, `client/`, `frontend/`, public integrations, Review code that is present in the public mirror, and public tooling.

Runtime Lite is the public local code-intelligence kernel. An OSS-only install can:

- use `lc with claude|codex|opencode|pi`;
- use the installed Cursor integration;
- build/use the Runtime Lite repository index with `lc runtime index`;
- manage the public Pi and LemonCode host binaries with `lc rt host`;
- use the public Review product surfaces that ship in the mirror;
- use public protocol/client contracts for hosted integrations.

No LemonCrow account or local license check is required for those public/local capabilities.

## Private / commercial source

The private `enterprise/` tree in the commercial development repository is a separate proprietary product and is not granted under the Apache-2.0 license merely because it shares the same monorepo.

That private tree includes Enterprise services and the extracted advanced code-intelligence implementation used by Hosted/Enterprise Runtime, including advanced semantic/hybrid retrieval, learned ranking/reranking assets, richer graph/lineage, cross-workspace routing/intelligence, and commercial control-plane code.

The source-level code-intelligence boundary is documented in [OSS / private code-intelligence boundary](../architecture/oss-private-code-intelligence.md) and enforced by `scripts/audit_oss_source_boundary.py`.

## Product boundary

| Public/local Apache-2.0 mirror | Private/commercial Enterprise tree |
| --- | --- |
| Runtime Lite local repository intelligence and coding tools | Hosted/Enterprise advanced code-intelligence provider and learned assets |
| Public CLI/client/server contracts and host integrations | Enterprise Runtime/controller/workspace implementation |
| Public Review code and single-developer/local workflows present in the mirror | Hosted multi-user authority, organization policy, private hosted persistence, commercial control plane |
| Public protocol/thin-client contracts | Entitlements, quotas, organization identity/governance, dedicated deployment code |

The public Runtime does not require the private provider. The old `lc code --pro` mode is removed; `lc code` is the Hosted owned-agent entry point and Pro is a commercial access plan, not an execution switch. A guarded local private-engine path remains only for LemonCrow engineering, qualification, and benchmarks.

Signing in to a hosted LemonCrow deployment does not unlock or silently replace local OSS capabilities.

## License files

- Full project grant/clarification: [`/LICENSE`](../../LICENSE)
- Apache 2.0 text: [`/LICENSE-APACHE`](../../LICENSE-APACHE)
- Attribution / third-party notices: [`/NOTICE`](../../NOTICE)

## Optional performance builds

Compiling public Python modules with mypyc is an optional performance/build choice. Compilation does not change the license grant or turn a private Enterprise module into public source.

## Network behavior

Local Runtime repository indexing and tools used by local agent integrations stay on the developer machine. Existing coding agents make their normal provider requests. `lc code` follows the Hosted/Enterprise network boundary because it is the LemonCrow-owned hosted agent.

Anonymous aggregate telemetry is on by default and can be disabled with:

```bash
lc rt telemetry remote off
```

or `DO_NOT_TRACK=1` / `LEMONCROW_TELEMETRY=off`.

See [Privacy & network behavior](../setup/privacy.md) for the full network/data-boundary description.

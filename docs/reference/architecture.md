# LemonCrow architecture

LemonCrow is infrastructure around coding agents. The architecture is split by product responsibility rather than one monolithic local/server runtime.

## Product layers

```text
                         LemonCrow
                            |
          +-----------------+-----------------+
          |                 |                 |
       Runtime            Review          Enterprise
          |                 |                 |
  agent context/tools   human review     shared policy,
  + optional Pro loop   + evidence       workspaces, audit
```

Runtime comes first in the coding flow; Review is the human inspection surface after or during that work. Enterprise adds shared infrastructure and organization controls without changing that basic model.

## Runtime: keep your agent

Claude Code, Codex, OpenCode, and Cursor can keep owning their normal agent/model UX while LemonCrow adds repository intelligence and tools around them.

```text
existing coding agent
        |
LemonCrow plugin/integration
        |
local repository intelligence
        |
     your repo
```

The local Runtime path:

- keeps the host's model/provider account and saved settings;
- builds/reuses an incremental repository index under `.lemoncrow/workspace/`;
- does not require a LemonCrow account;
- does not upload repository source to LemonCrow;
- does not require a resident Runtime daemon or listening Runtime port.

Claude Code, Codex, and OpenCode support `lc with <host>` for a temporary launch and `lc rt enable <host>` for persistent integration. Cursor uses its installed IDE/CLI integration.

See [Runtime Lite architecture](../architecture/runtime-lite.md).

## Runtime: LemonCrow owns the coding loop

`lc code` is the Hosted LemonCrow-owned coding agent. Runtime Lite stays local through existing-agent integrations such as `lc with`, persistent host integration, and `lc runtime index`.

```text
        Pi / LemonCode / Codex / Claude
                 |
        LemonCrow-owned loop
          /       |        \
      model      tools    evidence
                 |
              your repo
```

Hosted/Enterprise owned execution may use Pi, LemonCode, Codex, or Claude as implementation/frontends. Pi and LemonCode themselves remain public managed hosts; Pro is a Hosted access plan.

`lc code` always means the Hosted owned agent. Local Runtime Lite uses the agent integrations instead. Hosted Runtime is the remote/private path for deeper retrieval, richer relationships, cross-workspace context, model routing, cache policy, spend ceilings, and managed execution. Enterprise Runtime places that private capability in customer-controlled or dedicated infrastructure. Pro is a commercial plan, not an engine.

The owned loop can start short-lived local helper/gateway processes as implementation details. Those are not the same thing as requiring a persistent LemonCrow Runtime service.

## Review

Review is a separate product boundary.

```text
working tree / commit / PR
          |
       Snapshot
          |
  Review Reader + evidence
          |
   human judgment/state
```

A Review is durable across revisions of the same change. Each capture creates an immutable Snapshot. Human review state is reconciled against the next Snapshot: unchanged reviewed work can stay reviewed; changed or new work returns to the queue.

Review stores and renders:

- the actual diff;
- review targets and reading order;
- impact and provenance where available;
- recorded checks/evidence;
- comments/discussion;
- durable human review state;
- author/reviewer handoff state.

Local and hosted Review can use shared HTTP/application services because Review is a collaborative browser product. That service boundary should not be confused with the local Runtime plugin path.

See [Review reference](review.md).

## Enterprise

Enterprise adds shared infrastructure around Runtime and Review:

```text
developer clients / agents
          |
 enterprise Runtime + server plane
   /          |             \
identity    workspaces      policy/audit
   \          |             /
       model providers
```

Enterprise capabilities include:

- remote/shared code intelligence;
- identity and access control;
- organization policy and provider boundaries;
- managed workspaces;
- usage attribution and chargeback;
- audit/evidence retention;
- customer-controlled or dedicated deployment;
- Runtime controls around broader long-running agent workloads.

Coding is the shipped and measured workload for Runtime today. Broader agent workloads belong to the Enterprise scope rather than the core `/runtime` story.

## Public and private code-intelligence boundary

Runtime Lite is public/open-source and intentionally lightweight. It includes deterministic local lexical/indexed repository intelligence and local coding tools.

The private/Pro engine can add capabilities that are deliberately absent from Lite, including advanced semantic/hybrid retrieval, learned ranking, richer graph/lineage, and cross-workspace intelligence.

Public code must not silently import or depend on the private engine. See [OSS/private code-intelligence boundary](../architecture/oss-private-code-intelligence.md).

## Evidence, not hidden reasoning

LemonCrow records observable execution evidence: tool calls, provider usage where available, checks, diffs, provenance, and Review state. It does not expose or depend on hidden model chain-of-thought as a product feature.

Different hosts expose different evidence. Claude Code, Codex, OpenCode, Cursor, Pi, and other integrations therefore have different capture confidence. See the [host capability matrix](../hosts/host-capability-matrix.md).

## Network boundaries

There is no single network story for all LemonCrow products:

- **Local Runtime plugin:** repository intelligence and tool execution stay local; model traffic follows the external host/provider.
- **`lc code`:** Hosted LemonCrow-owned coding agent.
- **Hosted Runtime:** remote private Runtime; LemonCrow can own provider/tool execution and advanced intelligence.
- **Enterprise Runtime:** the private Runtime in customer-controlled/dedicated infrastructure.
- **Hosted Runtime:** uses the managed deployment explicitly selected by the user.
- **Review:** local or hosted depending on configured Review deployment.
- **Enterprise:** uses the customer-controlled or dedicated infrastructure chosen for that deployment.

See [Privacy & network behavior](../setup/privacy.md).

## Low-level protocol layer

Host integrations use different native surfaces: plugins, hooks, config files, session stores, and protocol adapters. Most users should not need to know which transport a host uses.

Integration authors can use [Protocol / MCP reference](../sdk/mcp.md) for the low-level transport/tool contract.

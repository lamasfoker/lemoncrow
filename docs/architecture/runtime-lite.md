# Runtime Lite architecture

Runtime Lite is LemonCrow's Apache-2.0 local repository-intelligence kernel. It runs on the developer machine, requires no LemonCrow account or application server, and does not upload repository source to LemonCrow.

## Product model

There are three customer-facing Runtime deployment concepts:

1. **Runtime — local / open source.** Runtime Lite powers `lc with claude|codex|opencode|pi`, Cursor's installed integration, `lc runtime index`, and persistent local integrations such as `lc rt enable claude`. Pi and LemonCode host lifecycle is also public under `lc rt host`.
2. **Hosted Runtime — private / LemonCrow-operated.** Managed deeper retrieval, richer code relationships, cross-workspace context, model/provider routing, budgets, prompt-cache orchestration, and managed execution belong here. A commercial Pro plan can grant access to Hosted Runtime.
3. **Enterprise Runtime — private / customer-controlled.** The same class of private Runtime capabilities can run in customer-controlled or dedicated infrastructure with identity, policy, managed workspaces, governance, and audit.

`Pro` is a plan name, not a Runtime engine, package, or execution location. There is no customer-facing Local Pro Runtime.

## Local repository state

Durable Runtime Lite lexical intelligence lives at:

```text
<repo>/.lemoncrow/workspace/runtime-lite-v3.sqlite3
```

When the optional semantic helper is enabled, its compact generation files live separately at .lemoncrow/workspace/runtime-lite-semantic/ inside the repository. The helper reuses unchanged quantized chunks across generations and atomically switches generation files. Stale generation files are pruned after the switch; already-mapped readers keep their open file mapping on platforms that permit unlinking mapped files.

The repository walker excludes `.lemoncrow`. LemonCrow adds `.lemoncrow/workspace/` to the clone-local `.git/info/exclude`, so the index stays out of commits without modifying the committed `.gitignore`.

SQLite uses WAL. The schema-versioned filename keeps older MCP sessions on their own schema: upgrading a newer runtime never rewrites an older session's legacy database. On first use of v3, a supported legacy v1/v2/v3 database can be copied through SQLite's online backup before the v3 copy is migrated; the source is preserved. Incremental generations allow readers to keep using the last committed generation while one writer updates changed files. Linked Git worktrees are separate Runtime Lite workspaces. A new worktree may seed unchanged rows from a fresh sibling index only when reuse is proven safe; divergent, dirty, new, deleted, or unproven paths go through normal incremental indexing.

There is no resident Runtime Lite daemon, no Runtime Lite listening port, and no watcher service. Indexed operations refresh stale local state when needed.

The stdio MCP server answers `initialize` immediately and brings the index current on one background thread owned by that session; EOF stops and joins it. A cold build of a large repository can take minutes, longer than a host's MCP connect timeout. `code_search`, `relations`, `graph` and `read symbol=` wait up to 20 seconds for the build, then return `[lc:index_building]` with files done/total and elapsed time. File reads, grep, edit and bash never wait. `lc runtime index` builds the same index in the foreground with a progress bar.

## Retrieval pipeline

```text
query
  -> identifier interpretation + code-syntax noise filtering
  -> exact and partial symbol rows
     + Porter-stemmed SQLite FTS5 lexical rows
     + path/name matches
     + parsed imports and local import-target resolution
  -> deterministic evidence fusion
     + multi-term coverage
     + bounded import-hop expansion
     + intent-aware source/test/docs/vendor path priors
  -> bounded source snippets + ranked candidate-path tail
```

Import rows come from statement readers for Python, JavaScript/TypeScript, Go, Rust and Java/Kotlin (`client/src/lemoncrow_client/lite/imports.py`): multi-line, relative, re-export, dynamic and `require` imports, Go import blocks, Rust `use` groups and `mod` declarations. Other files keep the original line patterns. `ImportResolver` maps a row to indexed files using only the repository's files: Python packages (relative imports, `src/` layouts, namespace packages), `tsconfig.json`/`jsconfig.json` `baseUrl`, `paths` and `extends`, `package.json` `imports`, `exports` and workspace package names, `go.mod` module paths, and Cargo crate names. An installed package stays external and is never reported missing. The local Code map and `relations kind=imports` read these rows. Index schema 3 introduced them. An older index is preserved for older MCP processes; the v3 copy re-reads its rows once, without mutating the legacy index.

The default Runtime Lite path deliberately contains no embeddings, ANN/vector retrieval, Torch, SentenceTransformers, learned reranking, private semantic/hybrid fusion, advanced graph/lineage, or cross-workspace intelligence.

An **optional local semantic recall mode** is available as a separate public helper through the lemoncrow lite-semantic extra and LEMONCROW_LITE_SEMANTIC=1. It uses the MIT-licensed minishlab/potion-code-16M-v2 static code model over syntax-aware roughly 750-character chunks, embedding each chunk together with its repository-relative file path and the nearest public Lite structural symbol name when available. Vectors are stored as signed 6-bit values in int8 cells and scanned exactly in bounded blocks; there is no ANN service, listening port, persistent daemon, hosted dependency, or private/enterprise intelligence in this path. The standard-library lemoncrow-client still has zero dependencies and communicates with the helper only over session-owned stdio. Missing, cold, or stale semantic state fails open to normal lexical Lite, and lexical rank one is never displaced by semantic fusion.

Install the optional extra with pip install 'lemoncrow[lite-semantic]'; enable a session with LEMONCROW_LITE_SEMANTIC=1 lc with claude. Runtime sessions are offline: the helper sets Hugging Face offline mode and never downloads model weights on the search path. Point LEMONCROW_LITE_SEMANTIC_MODEL at a local Model2Vec directory or pre-populate the model cache during an explicit installation/setup step.

This helper is bounded local recall, not the private Hosted/Enterprise semantic engine. Advanced semantic intelligence, organization context, cross-workspace retrieval, learned reranking, and managed indexing remain Hosted/Enterprise capabilities.

## Local integrations

Claude Code, Codex, and OpenCode support a temporary integration:

```bash
lc with claude
lc with codex
lc with opencode
```

For persistent local integration:

```bash
lc rt enable claude
lc rt enable codex
lc rt enable opencode
```

Cursor uses its installed integration rather than `lc with`. `lc code` is deliberately not another local launcher: it names LemonCrow's Hosted owned coding agent. Public Pi and LemonCode binaries are managed separately with `lc rt host`; Pi can also be launched for one Lite session with `lc with pi`.

`lc rt mcp` runs Runtime Lite in its own process unless Runtime targets a hosted server, in which case it runs the thin client. The opt-in remote MCP gateway (`lc rt mcp serve [--persistent]`) is served by the public client in the same way; neither path starts a local LemonCrow server. Persistence never changes a Local Lite trial into hosted behavior implicitly.

Each Runtime Lite tool call leaves one local usage record (the client trace schema, site `lite`, one session id per stdio process) under `<LEMONCROW_HOME>/runtime/traces/`, readable with `lc runtime trace`. Arguments are kept except bulk content (edit bodies, file text, diffs), which is stored only as size and digest. Retention is 14 days / 256 MB; `LEMONCROW_RUNTIME_TRACE=off` disables the record. Nothing is uploaded.

None of these local paths requires a LemonCrow account or uploads repository source to LemonCrow.

## Hosted Runtime

Hosted use is explicit, for example:

```bash
lc rt enable claude --hosted
```

That is a real remote/shared Runtime boundary using the configured LemonCrow deployment. It is not the old local private-engine process under a new name.

The hosted server already provides revision-bound remote code search, semantic retrieval where configured, relationship/graph queries, source/view synchronization, authentication, and tenant scoping. The CLI reserves `lc code` for the Hosted owned agent; current builds fail closed until the remote owned-loop transport is wired. Provider routing, spend controls, prompt-cache orchestration, and the owned execution loop remain Hosted/Enterprise implementation capabilities.

## Internal private-development path

LemonCrow engineering still needs to run the private engine locally for development, qualification, and benchmarks. That path is deliberately hidden from public help and guarded by `LEMONCROW_INTERNAL_PRIVATE_RUNTIME=1` plus `--internal-private`. It is not a supported customer install/update/entitlement flow.

The old `lc code --pro` mode has been removed. `--pro` is not a compatibility flag; Pro is a commercial plan for Hosted Runtime.

## Public tool placement

`lemoncrow_client.lite.policy.TOOL_PLACEMENT` is the executable ownership map. The model-visible Runtime Lite allowlist is exactly:

- `bash`
- `code_search`
- `edit`
- `read`
- `web_fetch`

Other local helper implementations may remain routable internally without being advertised to the model. Advanced semantic search and shared/organization capabilities belong to Hosted/Enterprise Runtime.

## Local to managed

| Deployment | Repository intelligence | Shared infrastructure | Cross-workspace | Customer boundary |
| --- | --- | --- | --- | --- |
| Runtime Lite | Lightweight deterministic local intelligence | No | No | Developer machine; Apache-2.0 |
| Hosted Runtime | Private advanced intelligence | Yes | Yes, subject to hosted policy | LemonCrow-operated |
| Enterprise Runtime | Private advanced intelligence | Dedicated/customer-controlled | Yes, subject to policy | Customer-controlled/dedicated |

Signing in never silently turns local Runtime Lite into Hosted Runtime.

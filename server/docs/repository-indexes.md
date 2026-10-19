# Repository indexes

The local server keeps one catalogue at
`<state>/index.repositories/catalog.sqlite3`. Each organization/repository
pair has its own `repositories/<opaque-hash>/index.sqlite3` beneath that directory.
The catalogue maps repository identities and view IDs to these databases. All
worktrees with the same repository identity share a database. Identical content
in different repositories is intentionally stored separately.

This layout is the local server's. Enterprise deployments keep the shared
`index.sqlite3` that their backup, purge and preflight tooling operate on.

Only git repositories and worktrees are indexed. A session started in a plain
directory, such as a home or a folder of projects, opens no view and uploads
nothing. There, `code_search`, `search`, `relations`, `graph` and `index` return
`not_indexed` (not retryable, action `use_client_tool`), and `read` reads from
disk.

Repository databases contain content, analysis, manifests, overlays, provenance,
and link graphs. Exact term and trigram membership uses contentless SQLite FTS5
indexes with integer document IDs, no positions, and no duplicated source text.
Tokens are encoded before FTS5 sees them; the existing analyzer and scoring
rules remain authoritative. Legacy term/gram tables remain empty compatibility
schema, not a second copy of the postings.

Garbage collection runs independently in each repository. A live reference in
one repository cannot pin historical content in another. Maintenance merges
bounded FTS segments, checkpoints WAL, and incrementally releases free pages
(up to 64 MiB per repository per pass at the default page size). It also retires
obsolete per-view graph copies once a clean view reads its shared generation
graph. Private graphs for views with overlays remain intact.
The storage report includes actual database, WAL, and shared-memory file bytes.

## Upgrade an existing shared database

Stop the server before migration, then run:

```sh
lemoncrow-server migrate-index --database ~/.lemoncrow/server/data/index.sqlite3
```

The command checkpoints and write-locks the source, preserves repository IDs,
view IDs and revisions, provenance, manifests, stored content and analysis,
and writes to `index.repositories.migrating`. It verifies row counts and
SQLite/FTS integrity before atomically publishing `index.repositories`.
Unattributed uploads and historical roots without a repository owner are retained
in a separate unassigned partition and remain subject to garbage collection.

Rerun the same command after interruption. Verified repositories are reused;
an incomplete repository is rebuilt only inside the migration-owned staging
directory. If the source changed, resume refuses without overwriting the staging
directory. Startup also refuses a legacy source changed after migration.

The source database is retained for rollback. Its bytes are not reclaimed by the
migration. Keep it until the new server has passed operational checks. The old
nested `index/index.sqlite3` is a separate deployment/namespace and is not merged
or deleted by this command.

Restart the server with the same state directory after a successful migration.
To roll back, stop it, retain the new repository directory, and run the previous
server version against the retained shared source. Writes made after migration
are not synchronized back to the old source.

## Disk limits and WAL backpressure

The defaults are a 64 GiB budget per repository, with 128 MiB reserved for WAL.
Set them on both migration and server startup with
`--max-repository-index-bytes` and `--max-index-wal-bytes`.

SQLite's page limit bounds the main database, including its indexes. An existing
database above the configured ceiling remains readable and accepts cleanup under
a no-growth ceiling; maintenance tightens that ceiling as pages are reclaimed.
It does not prevent the other repositories from starting. If a reader
pins an oversized WAL, the next write attempts a checkpoint and refuses with
`storage_limit` and a retry instruction until the reader releases it. A bounded
transaction may temporarily exceed the WAL reserve; it cannot trigger an
unbounded series of further writes. Local filesystem sync commits every 64
files or 4 MiB of input, whichever comes first, and still indexes all remaining
files. Uploads commit content and analysis together per blob.

These are per-repository limits, not a promise that all repositories together
fit in 64 GiB. Repository size and total disk usage should be monitored separately.

# GitHub Check publication (hosted)

Hosted Review can report a paired worker's signed Runtime Validation result on the pull request as a GitHub Check Run named `LemonCrow Review verification`. Every Check goes through a durable outbox: a result that should publish a Check is written down in the same transaction that stores its verification evidence, and a background worker delivers it to GitHub, retrying until GitHub has the Check or the entry is settled. The pull request and your branch protection stay the source of truth; nothing here changes Review state.

## When a Check is published

A result publishes a Check only when all of these hold:

- it is a Runtime Validation job for the current execution intent of its Review;
- the Review is linked to an open GitHub pull request with a numeric number;
- the Review's latest revision is the job's revision, and its head is the 40-character commit the PR head points at.

Anything else (a historical run, a closed or moved PR, a review on another provider) publishes nothing. The Check itself carries the same content as before: success or failure only for signed evidence bound to the exact Snapshot, otherwise neutral.

## Operator configuration

Publication is off by default. With it off the server enqueues nothing and the worker does nothing.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LEMONCROW_SERVER_GITHUB_REVIEW_CHECKS_ENABLED` | `false` | Turn Check publication on. Requires the GitHub Review installation (App id, installation id and private key, or a token file) with the `checks:write` permission, hosted Review storage and the durable job queue. |
| `LEMONCROW_SERVER_GITHUB_REVIEW_CHECK_MAX_ATTEMPTS` | `8` | Attempts before an entry is dead-lettered (1 to 50). |

The worker runs in the server's maintenance loop every `LEMONCROW_SERVER_MAINTENANCE_INTERVAL_S`, clamped to between 5 and 30 seconds. Nothing else needs configuring. Backoff (30 seconds doubling, capped at one hour, plus or minus 20% jitter), the delivery lease (120 seconds) and retention are fixed.

## How delivery works

**Enqueue.** A worker result stores its verification evidence and, in the same database transaction, inserts or re-arms the outbox entry for its Check. If the transaction rolls back, neither exists. The publication reconciler that finishes an interrupted worker result enqueues the same way. The worker-result request then attempts delivery once inline; whatever is left is the worker's.

**One entry per Check.** An entry is keyed by organization, repository, GitHub repository id, head commit, Check name, Review and revision. A second result for the same Check (a retry, a manual reconcile, a concurrent replica) re-arms the same entry with a fresh attempt budget instead of adding one. An attempt that is in flight when that happens keeps its lease; when it settles it sees the newer generation and leaves the entry pending, so the newest result is always delivered.

**Reconcile before writing.** Every attempt re-reads the live pull request and lists the Check Runs with this name on the head commit before it writes. If the run already exists it is updated (`PATCH`); only if none exists is one created (`POST`). An attempt whose `POST` reached GitHub but whose response was lost therefore updates the run it created on the next attempt instead of adding a second one.

**Leases.** Claims are serialized per repository (SQLite `BEGIN IMMEDIATE`, PostgreSQL advisory lock). A claim counts an attempt and leases the entry; the sender renews the lease with a compare-and-set on its token immediately before calling GitHub and skips an entry a peer has taken over. If a replica dies mid-attempt, the lease expires and another pass picks the entry up; on its last attempt the entry is dead-lettered with `lease_expired_final_attempt` instead.

**Fairness.** A pass serves organizations round-robin, at most 10 entries per organization per round, at most 200 entries or 20 seconds per pass, and the next pass starts after the last organization served.

### Outcomes

| Outcome | Entry becomes | Code examples |
| --- | --- | --- |
| GitHub has the Check | `delivered` (Check Run id and conclusion kept) | |
| The PR moved, closed, or the Review advanced | `superseded` (replayable) | `superseded` |
| GitHub rate limit: `429`, or `403` with an exhausted rate-limit budget | `pending` until the time `Retry-After` or `x-ratelimit-reset` names, plus up to 5 seconds of jitter. No attempt is spent. | `rate_limited_primary`, `rate_limited_secondary` |
| Transient: `408`, `425`, `5xx`, no HTTP response, timeout, connection error, incomplete Check listing or unconfirmed write, evidence not yet durable, unexpected adapter error | `pending` with backoff; `dead` after the maximum attempts | `github_http_502`, `timeout`, `connection_error`, `reconcile_incomplete`, `publication_pending`, `adapter_error` |
| Permanent: any other `4xx`; a refusal by the publisher (evidence that does not match the Snapshot, ambiguous duplicate runs); the job no longer exists | `dead` at once | `github_http_422`, `check_refused`, `job_not_found` |

Stored codes are short identifiers. GitHub response bodies, tokens and evidence are never stored, logged or returned.

## Admin routes

All three require an organization admin (`ADMINISTER`), are scoped to the caller's organization, and are listed in the route authorization table. An entry in another organization, or in a repository the caller cannot administer, is `404` (`REVIEW_UNKNOWN`) or `403`.

- `GET /v1/admin/github-checks?state=dead,superseded&repo_id=<repo>&limit=100` lists entries, newest first. `state` takes `pending`, `delivering`, `delivered`, `superseded` and `dead` (comma-separated, all by default); `repo_id` is optional; `limit` is 1 to 200. The response is `{org_id, enabled, states, entries, truncated}`.
- `GET /v1/admin/repos/{repo_id}/github-checks/{entry_id}` returns `{entry}`.
- `POST /v1/admin/repos/{repo_id}/github-checks/{entry_id}/replay` re-arms a `dead`, `superseded` or `pending` entry with a fresh attempt budget, due now, and attempts it inline through the same reconcile-then-write path. It returns `{entry, attempt: {state, error_code}}`. An entry that is in flight or already delivered is `409` (`REVIEW_STATE_CONFLICT`); with publication off it is `NOT_CONFIGURED`. Each replay emits the audit event `review.provider.check_replay`.

The existing manual control `POST /v1/repos/{repo_id}/reviews/{review_id}/provider/verification-check` (`{"job_id": ...}`) goes through the same outbox: it re-arms the entry for that job's Check and attempts it inline.

Organization > Background jobs (`GET /v1/organization/jobs`) also lists the entries that are still owed or replayable (`pending`, `delivering`, `superseded`, `dead`) under `github_checks`: `{available, reason, enabled, items, truncated}`, where each item is the entry shape below.

### Entry shape

```json
{
  "entry_id": "rgc-...",
  "repo_id": "...",
  "review_id": "...",
  "revision_id": "...",
  "snapshot_id": "...",
  "job_id": "...",
  "provider": "github",
  "provider_repo_id": "1234",
  "pull_number": "42",
  "head_sha": "<40 hex>",
  "check_name": "LemonCrow Review verification",
  "check_key": "<sha256 hex>",
  "state": "pending | delivering | delivered | superseded | dead",
  "generation": 1,
  "attempt_count": 0,
  "max_attempts": 8,
  "next_attempt_at": "ISO-8601 UTC, only while pending, else null",
  "error_code": "",
  "check_run_id": null,
  "conclusion": "",
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601",
  "replayable": false
}
```

Fields are only added, never renamed or removed, within this version.

## Metrics

The admin metrics endpoint adds, per state:

- `lemoncrow_review_github_check_outbox{state="..."}`: entries in that state;
- `lemoncrow_review_github_check_outbox_attempts{state="..."}`: attempts spent by entries in that state.

Alert on a growing `dead` count, or on `pending` that does not drain. The worker logs one line per pass that did any work, with counts only.

## Retention

The existing Review retention pass removes `delivered` and `superseded` entries 7 days after they settled, and `dead` and never-delivered `pending` entries after 30 days. An entry being delivered is never removed. Retention also runs with publication off, so entries left from an earlier enablement still age out. An entry goes with its Review when the Review is deleted.

## Failure modes

- **GitHub down or slow.** Entries back off and retry; after the maximum attempts they are dead-lettered. Replay them once GitHub recovers.
- **Rate limited.** Entries wait until the time GitHub names and do not spend attempts.
- **Installation lost `checks:write` or was removed.** GitHub answers `403` or `404` and entries are dead-lettered (`github_http_403`, `github_http_404`). Fix the installation, then replay.
- **PR moved before delivery.** The entry is `superseded`; the newer head gets its own Check when its own result arrives. Replaying a superseded entry only publishes if the PR is back on that head.
- **Replica crash mid-attempt.** The lease expires and another pass finishes the entry. If the crash came after GitHub created the run, the retry finds and updates it.
- **Duplicate runs on GitHub.** The publisher refuses to choose between two runs with the same identity and the entry dead-letters with `check_refused`. Delete the extra run on GitHub, then replay.

## Upgrade notes

- PostgreSQL deployments gain `review_github_check_outbox` in Review schema v41 (expand-only; the previous binary runs against it and never reads it). Creating its foreign key to `review_sessions` briefly takes a `SHARE ROW EXCLUSIVE` lock on that table while the empty table is created; there is no scan. Like the other Review tables it has forced row-level security with the repository-scoped and organization read and delete policies.
- SQLite deployments get the table on start. The previous release does not know it: its one-time legacy Review id migration would abort on a database whose outbox has rows. That migration only runs on databases that predate canonical Review ids; finish the rolling upgrade before running an old binary against a SQLite Review database that holds outbox rows.
- With publication already on before this release, the worker-result response's `github_check` now reports `queued` (with the outbox `entry_id`) instead of `retryable` when the inline attempt does not finish. The Check is still delivered: the worker retries it.

## Operator steps

1. Roll every replica to this release.
2. Confirm the GitHub Review installation has `checks:write`.
3. Set `LEMONCROW_SERVER_GITHUB_REVIEW_CHECKS_ENABLED=true` and, if you want a different budget, `LEMONCROW_SERVER_GITHUB_REVIEW_CHECK_MAX_ATTEMPTS`.
4. Watch `lemoncrow_review_github_check_outbox{state="dead"}` and the Background jobs page; replay dead entries after fixing their cause.

To stop publishing, set the flag back to `false`. Queued entries stay where they are and are delivered if you turn it on again before retention removes them.

## Not supported yet

- Replay from the web UI: use the admin route.
- Helm values for the flag: set it with `extraEnv`.
- Checks for providers other than GitHub.

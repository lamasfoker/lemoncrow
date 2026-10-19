# Review attention notifications (hosted)

Hosted Review can push a short notice to an engineer when a Review needs them. It is a delivery layer over ReviewAttention, not a second attention model: your code host (pull request comments and checks) stays the primary channel, and nothing here changes Review state.

## What can notify

Only a ReviewAttention transition that adds the *first* reason of one of these classes for a person, caused by someone else:

- `review_requested`: a review is requested from you
- `work_returned`: work comes back to you for another look
- `feedback_returned`: feedback is returned to you
- `mention_or_reply`: someone mentions or replies to you
- `check_failed`: a required check fails on a change you own
- `landing_attention`: a landing you armed needs attention
- `production_outcome`: a change you wrote or reviewed fails or is rolled back in production

AI progress and findings, successful checks, indexing, manual marks and scope reconciliation never push. A Snapshot capture creates no notice of its own, but a recapture that returns work to a reviewer who had completed an open request makes ReviewAttention derive `work_returned` for that reviewer, and that one default-class push is delivered (a capture that returns no work pushes nothing). The person who caused a change, including the author who captured the Snapshot, is never notified about it. Personal preferences (Settings > Notifications, or `GET`/`PUT /v1/reviews/notification-preferences`) can only turn these classes off or move a person to a digest; naming any other class is refused. A preference is read again when a notice is sent, so narrowing it takes effect for notices already queued.

## Operator configuration

Delivery is off until you configure a destination: the webhook below, an email relay (see "Email (SMTP)"), or both. With neither the server records nothing.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LEMONCROW_SERVER_REVIEW_NOTIFICATION_WEBHOOK_URL` | empty | The one destination. `https` unless private destinations are allowed. No credentials in the URL. Never taken from a request or tenant data. |
| `LEMONCROW_SERVER_REVIEW_NOTIFICATION_WEBHOOK_SECRET_FILE` | none | File holding the HMAC-SHA256 signing secret. Required with the URL. Read once at startup and never logged or returned. |
| `LEMONCROW_SERVER_REVIEW_NOTIFICATION_WEBHOOK_ALLOW_PRIVATE` | `false` | Allow `http` and a destination that resolves to a private, loopback or link-local address (an internal chat bridge). |
| `LEMONCROW_SERVER_REVIEW_NOTIFICATION_WEBHOOK_TIMEOUT_S` | `5` | One overall deadline per `POST`, covering connect, TLS, request and response (0.5 to 30). A receiver that answers a byte at a time cannot extend it. The delivery lease and the size of a claim are derived from it. |
| `LEMONCROW_SERVER_REVIEW_NOTIFICATION_MAX_ATTEMPTS` | `8` | Attempts before a notice is dead-lettered (1 to 50). |
| `LEMONCROW_SERVER_REVIEW_NOTIFICATION_DIGEST_INTERVAL_S` | `3600` | Window for people who chose digest delivery (60 to 604800). |
| `LEMONCROW_SERVER_REVIEW_NOTIFICATION_LINK_BASE_URL` | empty | Public base URL of this server; links become `<base>/r/<review_id>`. Empty gives a relative path. Required when email is configured. |

On Kubernetes, set these through the chart's `notifications.*` values instead of `extraEnv`: `notifications.webhook.url`, `existingSecret` (mounted at `/etc/lemoncrow/notifications/webhook-secret`), `allowPrivate`, `timeoutSeconds`, `notifications.linkBaseUrl`. The chart's default-deny `NetworkPolicy` blocks the destination unless you list it in `notifications.webhook.egressCidrs`. Each CIDR there generates an egress rule on the URL's port, or on `notifications.webhook.port` if that is set. See "Notifications (webhook and email)" in `enterprise/deploy/server/README.md`. On the hosted VM, set the `LEMONCROW_NOTIFICATION_*` variables in `.env` (`enterprise/deploy/gcp/vm/README.md`).

The transport refuses a destination whose DNS answers include any non-public address (unless private destinations are allowed), connects only to the address it validated, never follows a redirect, and bounds the request body (64 KiB), the response it reads (4 KiB, at most 16 KiB on the wire including headers) and the total time of the request. An IPv6 address that only wraps an IPv4 one (IPv4-mapped, NAT64 `64:ff9b::/96` and `64:ff9b:1::/48`, IPv4-compatible, 6to4) is judged by the IPv4 address it carries, so `64:ff9b::a9fe:a9fe` is refused like `169.254.169.254`.

## What the receiver gets

A `POST` of JSON, `schema` `lemoncrow.review.notification/v1`, carrying identifiers and a link only: `id`, `kind` (`review.attention.<class>`), `org_id`, `repo_id`, `review_id`, `recipient_id`, `actor_id`, `reason`, `subject_id`, `revision_id`, `fact_ref`, `link`, `created_at`. No source, comment, path, evidence or email address is included; resolve `recipient_id` to a person on your side. A digest has `kind` `review.attention.digest` and an `items` list of the same records (at most 50 per message).

Headers: `X-LemonCrow-Timestamp` (epoch seconds), `X-LemonCrow-Signature: sha256=<hex>` and `X-LemonCrow-Delivery` (the `id`). Verify the signature as `HMAC_SHA256(secret, timestamp + "." + raw_body)` and reject stale timestamps.

Delivery is **at-least-once, deduplicated by delivery id**. Every attempt for a notice carries the same `id` (and `X-LemonCrow-Delivery`). A duplicate can still reach the receiver in one case: the process dies, or loses its database connection, after the receiver accepted the `POST` but before the row was marked delivered. The row's lease then expires and another pass sends it again, so deduplicate on `id`. Two replicas cannot send the same row at once: a sender renews its lease with a compare-and-set on the lease token immediately before each send and skips a row a peer has taken over. One send lasts at most the webhook deadline and the lease is at least twice that, so the only remaining overlap is a sender stalled longer than its lease *during* a send.

## Reliability and retention

Each notice is a row in a durable outbox written in the same transaction that commits the attention change, so a crash cannot lose a notice *before delivery starts* and a replayed transition cannot enqueue it twice (the row is unique on its semantic key). Sending is separate and at-least-once (see above). A failed attempt backs off exponentially (30 seconds doubling, capped at one hour) and is dead-lettered after the maximum attempts. Delivery state is independent of Review state: a broken receiver never blocks, reorders or undoes a Review action. Settled rows are pruned after 7 days and dead letters after 30 days by the existing retention pass. Deleting a person (SCIM) removes their preferences and settles their unsent notices; a Review's notices go with the Review.

The background pass is fair and keeps draining. Each round gives every organization with due rows at most 25 notices, rounds repeat while anything is due, a pass stops after 1000 notices or 20 seconds, and the next pass starts with the organization after the last one served, so a busy organization cannot starve the ones that sort after it. Rows a pass did not reach in time are handed back without spending an attempt. Replicas compare lease times against their own clocks, so keep them within a few seconds of each other (NTP).

Postgres deployments gain the outbox and preference tables in Review schema v34 (expand-only; the previous binary keeps running against it). Roll every replica to the new binary before relying on delivery.

Upgrade notes:

- The v34 migration creates the outbox with a foreign key to `review_sessions`. Creating it briefly takes a `SHARE ROW EXCLUSIVE` lock on `review_sessions` (writes to Reviews wait, reads do not) while the empty table is created; there is no table scan. Run it in a quiet moment on a very busy deployment. A `NOT VALID` constraint would not avoid the lock (it is taken to install the constraint, not to validate it), so none is used.
- Mixed N and N-1 replicas. The previous release does not know the new tables. On PostgreSQL its organization purge (`delete_org`) leaves that organization's `review_notification_preferences` rows behind; the new release's retention pass removes preference rows of organizations that no longer exist in the repository registry (rows older than a day, and never when no organization exists at all). On SQLite the previous release's legacy Review identity migration (the one-time rewrite of old Review ids) does not rewrite `review_notification_outbox.review_id` and aborts on its foreign-key check if the outbox has rows. That migration only runs on a database that predates canonical Review ids, and the new release rewrites the outbox rows itself; do not point an old binary at a SQLite Review database that holds outbox rows, and finish the rolling upgrade before running one. Forward-only: there is no downgrade path for those rows.

## Email (SMTP)

The server can also email these notices, and invitations, through an SMTP relay you run or rent. Email is a second adapter on the same outbox, not a second queue: one notice is one row, retried, deduplicated and dead-lettered as above. With a webhook configured too, the webhook is sent first and the email last; if the email fails, the row is retried and the webhook is sent again with the same `id`, so receivers must deduplicate on it.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LEMONCROW_SERVER_NOTIFICATION_SMTP_HOST` | empty (off) | The relay's host name. Never taken from a request or tenant data. |
| `LEMONCROW_SERVER_NOTIFICATION_SMTP_PORT` | `587` | Usually `587` for `starttls` and `465` for `implicit`. |
| `LEMONCROW_SERVER_NOTIFICATION_SMTP_TLS` | `starttls` | `starttls` or `implicit`. There is no plaintext mode: a relay that does not offer STARTTLS is refused before any credential or message is sent, and the certificate is verified against the host name (TLS 1.2 or later). |
| `LEMONCROW_SERVER_NOTIFICATION_SMTP_FROM` | none | The sender, for example `LemonCrow <notifications@example.com>`. Required with the host. |
| `LEMONCROW_SERVER_NOTIFICATION_SMTP_USERNAME_FILE` / `_PASSWORD_FILE` | none | Private files (not readable by other users) holding the relay credentials, if it needs SMTP AUTH. These are LemonCrow's own sender credentials, separate from the identity service's mail credentials. |
| `LEMONCROW_SERVER_NOTIFICATION_SMTP_TIMEOUT_S` | `10` | Deadline for each socket operation of one send (1 to 60). |
| `LEMONCROW_SERVER_NOTIFICATION_SMTP_ALLOW_PRIVATE` | `false` | Allow a relay that resolves to a private address (an internal relay). |

Email needs `LEMONCROW_SERVER_REVIEW_NOTIFICATION_LINK_BASE_URL`: every link in a message points to that hosted origin, and the server refuses to start with a relay but no base URL. Publish SPF, DKIM and DMARC for the sender's domain before turning email on.

The relay is resolved once per send, refused if it resolves to a non-public address (unless allowed), and the connection is pinned to the validated address. A failure is stored as a short class (`smtp_4xx`, `smtp_5xx`, `smtp_auth_failed`, `tls_error`, `tls_unavailable`, `timeout`, `connection_error` and so on), never the relay's own response text.

What a message carries: a plain-text part and a minimal HTML part with the same content. A review notice names its class, the repository id, the Review id, the reason and a link to `<base>/r/<review_id>`; a digest lists up to 50 of them. No source, comment, path or evidence text is included, and every value is HTML-escaped. `Message-ID` is derived from the delivery `id`, so a re-sent notice is recognizable as the same message. Each message ends with a note that email can be turned off in Settings > Notifications; there is no unsubscribe link.

Who gets email: nobody by default. A person turns it on in Settings > Notifications (`email_enabled` on `PUT /v1/reviews/notification-preferences`). Turning it on stores the verified email address of their current sign-in, and is refused if the server has no relay (501) or the address cannot be verified. Turning it off clears the stored address. A notice for someone who has not turned email on is settled as `suppressed` (`email_not_enabled`) instead of being sent. The address goes with the person's preferences (SCIM deprovision) or the organization.

Where the address comes from: Authward's access tokens carry no `email` claim, so the server asks Authward's OIDC userinfo endpoint once, at opt-in, with the person's own access token. The endpoint is the `userinfo_endpoint` of the issuer's discovery document and follows the same `https` rule as the JWKS and token endpoints. The request follows no redirect, has a 5 second deadline and a 64 KiB answer cap. The answer counts only when its `sub` equals the token's `sub` and it says `email_verified: true`; an absent claim is not verified. An access token that itself carries `email` with `email_verified: true` is used without the call. Requirements: the sign-in must grant the `openid` and `email` scopes (LemonCrow Web requests both by default, and the LemonCrow resource and the `lemoncrow-web` client must allow them), and the identity service must be reachable from the server. A refusal carries `details.reason`: `email_scope_missing` (sign in again so the token has `email`), `email_missing`, `email_unverified`, `userinfo_subject_mismatch`, `email_source_unavailable` (a sign-in that is not an Authward access token, such as a static token), all 400; or `userinfo_unavailable` (503, retry), when the identity service is unreachable, slow, refuses the token or advertises no userinfo endpoint. A refused opt-in changes nothing that is stored.

Invitations: when an administrator creates an invitation with a recipient address (LemonCrow Web > Account), and the identity service did not email it itself, the invitation link is queued on a per-organization invitation outbox with the same retry, backoff and dead-letter rules, and sent by the same background pass. The link and the address are deleted from the row once it is sent, cancelled or dead-lettered. The invitations panel shows each invitation's email as queued, sent, failed (with the error class) or cancelled. `POST /v1/account/invitations/{id}/resend` (organization administrators only, same-origin, audited as `account.invitation.resend`) revokes the pending invitation, issues a new one with a fresh link and queues its email. It is limited to one resend per recipient every ten minutes and 20 per organization per hour, and refused for an invitation this organization did not record. Postgres deployments gain the opt-in columns and the invitation outbox in Review schema v40 (expand-only).

On Kubernetes, set `notifications.smtp.*` (and `notifications.smtp.egressCidrs` for the relay) in the `lemoncrow-enterprise-server` chart. On the hosted VM, set the `LEMONCROW_NOTIFICATION_SMTP_*` variables in `.env`.

## Not supported yet

- Slack and chat-specific adapters (put them behind the webhook).
- An unsubscribe link or `List-Unsubscribe` header; email is turned off in Settings > Notifications.
- Restoration-failure email (restore is not available yet).
- Replay of dead-lettered notices from the product; they stay inspectable for 30 days.
- One digest across organizations (a digest groups a person's notices within one organization).

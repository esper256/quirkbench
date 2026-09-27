# Device protocol (version 1)

The controller listens on HTTPS only. `make_server` binds to `127.0.0.1` by
default; a LAN address requires `allow_lan=True`, a configured certificate and
private key, and one random token of at least 32 characters per device. Device
clients verify the server certificate and hostname against a supplied CA file.
There is no insecure client mode. Tokens are sent in `Authorization: Bearer`
with `X-Device-ID`; request paths, headers, and tokens are not logged.

Every JSON request has `schema_version: 1`; unknown request fields and unknown
versions are rejected. JSON bodies are limited to 1,500,000 bytes. Evidence
upload chunks decode to at most 1,000,000 bytes. The upload route is bounded
and resumable by `upload_id`, byte `offset`, expected SHA-256 and total size.
Clients may replay an acknowledged chunk after losing the reply; the store must
recognize an identical replay. Responses have the envelope
`{"schema_version":1,"data":{"value":...}}`. Errors carry `data.error`.

| Method and path | Request fields | Purpose |
| --- | --- | --- |
| `POST /v1/register` | `report` | Report mode, boot identity, and true capabilities. |
| `POST /v1/reconcile` | `boot_id` | Reconcile changed boot identity before work. |
| `POST /v1/claim` | `boot_id`, `request_id` | Idempotently claim one queued experiment. |
| `POST /v1/start` | `attempt_id`, `token`, `boot_id` | Mark execution started under its lease. |
| `POST /v1/heartbeat` | `attempt_id`, `token`, `boot_id` | Renew a live execution lease. |
| `POST /v1/upload` | `attempt_id`, `token`, `boot_id`, `upload_id`, `offset`, `data_b64`, `expected_digest`, `total_size` | Upload evidence bytes. |
| `POST /v1/evidence` | `attempt_id`, `token`, `stream`, `sequence`, `sha256`, `size` | Attach uploaded evidence to the attempt. |
| `POST /v1/complete` | `result`, `token`, `boot_id` | Submit the final structured result. |
| `GET /v1/artifacts/{sha256}` | none | Fetch an artifact only when assigned to the authenticated device. |

Attempt operations check both the device token and attempt ownership. The
attempt token further scopes writes. Controller administration, campaign
creation, scheduling, and status have no HTTP route. The target accepts only
recipes installed in its local Python registry. The built-in `smoke` recipe is
available in simulation mode and returns `INCONCLUSIVE` with an explicit demo
observation; it does not claim to test a kernel. Kernel candidate boot requires
a commissioned `BootControl` implementation. The default target has none,
reports no boot capability, and never reboots or runs host stress commands.

The target writes each journal transition and evidence blob with a temporary
file, `fsync`, and atomic rename. It records execution intent before calling
a local recipe. After a restart at that point, it reports `NEEDS_HUMAN` rather
than running the recipe again. It uploads evidence bytes, attaches evidence
references, then submits the result; unacknowledged work remains in the outbox
for the next `step()` call. A changed boot ID is reconciled with the controller
before any claim.


## Progress visibility (v1)

`POST /v1/progress` accepts `schema_version`, `attempt_id`, `token`, and `report` (the Progress contract). Device authentication scopes the attempt; campaign ownership is checked separately. Local builder and coding-agent activities use `Controller.progress` without a device token. Activity IDs, producer sequence numbers and immutable deadlines make progress durable and replay-safe. Progress never renews the execution lease. Use a fresh activity ID for a retried operation; do not reset its counter or slide its deadline.

`quirkbench watch CAMPAIGN` exposes timestamped phase, measured progress, reporting age, advancement age, waits, suspected stalls and deadlines. `Controller.events` supports incremental retrieval. The controller automatically tracks attempt times and durable upload bytes; detailed recipe-specific progress is an explicit future adapter responsibility.

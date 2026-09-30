# Device protocol (version 1)

This is the existing target-facing protocol. The planned [local agent/operation
API](product-roadmap.md) uses the CLI and typed application services, not a new MCP
server or remote administrative route. Its operation IDs, JSON envelopes and
watchdog authorization records require explicit versioned implementation; examples
in the roadmap do not extend this protocol implicitly.

The [contract supplement](implementation-contracts.md) specifies local operation
fencing (C2), initial manual authenticated setup and later automated enrollment (C4),
and a later unattended watchdog grant route (C6). Enrollment automation and grants
are **not implemented** by the protocol tables
below. Registration authenticates an already provisioned target; it must not be reused
as unauthenticated pairing. New capabilities/routes need explicit negotiation.
The current physical runtime checks its private target binding before connecting;
missing or changed identity leaves recovery waiting for explicit setup.

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
observation; it does not claim to test a kernel. The generic target CLI has no
physical boot adapter and cannot reboot the computer running that command. The verified USB runtime
assembles the OSTree/BootControl adapters and the physical `system-observation`
recipe; it still needs actual hardware commissioning.

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

## OS deployment references

The v1 experiment envelope remains unchanged. Its `deployment` artifact role names an immutable versioned deployment manifest containing backend, exact revision, configured repository identifier, protection profile and provenance. The configured OSTree backend retrieves signed content from authenticated HTTPS; target bearer credentials and trust configuration are separate from agent credentials. The ordinary artifact endpoint carries the small manifest, not an ISO or an ad hoc kernel bundle.

Unsupported deployment backends and legacy kernel-only execution requests fail explicitly before a recipe starts. Candidate execution requires the configured physical adapters and exact controller handoff; successful manifest validation does not itself authorize a boot. Repository commits reachable from experiments and retained checkpoints are part of backup and retention obligations.

The read-only OSTree publication service is separate from `/v1` administration and evidence transport. It exposes `/<repository-alias>/<OSTree-path>` through mutual TLS: every client certificate must chain to the configured client CA, and clients verify the server CA. GET and HEAD support streamed objects and single byte ranges; directory listing, symlink traversal, private files and HTTP writes are rejected. Commit-signature verification remains required in addition to TLS. Keep signing keys outside published repositories. Repository download permission does not authorize experiment execution.

A controller accepts a new deployment only with its explicit `provenance.build_evidence` closure: `{"schema_version":1,"artifacts":{"role":"sha256"}}`. Required roles are build provenance, `vmlinux`, `system_map`, `kernel_source`, `userspace_source`, `config`, and `modules` (the provenance role is named `build_provenance`). All referenced blobs must already be durable in the controller store, and source/symbol identities must match the recorded build. This is submission validation rather than a change to the v1 experiment envelope. Read-only historical records remain available.

## Physical handoff and live evidence extension

Additive v1 routes preserve existing experiment/result envelopes:

| Route | Purpose |
| --- | --- |
| `POST /v1/handoff` | Commit exact revision and boot origin before arming |
| `POST /v1/candidate-started` | Adopt the authorized candidate's new boot identity |
| `POST /v1/recovery-returned` | Record recovery separately from result completion |
| `POST /v1/maintenance` | Read the operator-created library maintenance fence |
| `GET /v1/artifacts/HASH` with `Range` | Resume large immutable library content with final client hash verification |

A handoff has its own BOOT_PENDING state and bounded authorization. Repeated
acknowledgements converge; stale boots cannot authorize execution. Live sealed
chunks use the existing upload/evidence routes. Candidate finish has bounded
network work and requests recovery; pending data remains available across boots.
Library selections use the ordinary `library` artifact role and exact pack IDs;
backup retention follows their complete content closure. Maintenance is started
and finished through local controller administration, never by a candidate recipe.

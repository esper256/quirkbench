# Deliver Quirkbench’s installation-to-patch user journey

This is the implementation map for the
[aspirational manual](../README.md). The first general release must support a new
user from installation through an attended investigation and patch export, without
handwritten configuration, build manifests or knowledge of a previous investigation.
Deliver fresh-user setup first. External coding agents are the primary journey;
managed invocation and unattended target operation are separate optional milestones.

This checklist maps the desired commands to implementation status and acceptance.
[C8](product-interface.md) defines interface semantics, [C0–C7](implementation-contracts.md)
define safety and execution authority, the [roadmap](product-roadmap.md) sets delivery
order, and the [handoff](implementation-handoff.md) defines bounded packets.

## Starting point

Reuse the existing controller database, operation records, attempt state machine,
content store, systemd worker ownership and target protocol. There is no second
investigation scheduler or database. Development primitives are not a completed
user journey; existing software tests are not release or hardware qualification.

| Area | Existing foundation and evidence location | Integration still needed |
| --- | --- | --- |
| Installation | [Immutable archive installation](../src/quirkbench/controller_install.py), [setup diagnostics](../src/quirkbench/controller_setup.py), [installation tests](../tests/test_controller_install.py) | Signed release distribution, bundled installer, resumable setup, zero-target service startup, unified status |
| Recovery and targets | [Stock recovery](../src/quirkbench/recovery_stock.py), [private provisioning](../src/quirkbench/provisioning.py), [local console](../src/quirkbench/console.py), [capacity tests](../tests/test_capacity_setup.py) | Verified image download, connected setup screens, C4 pairing and credential lifecycle |
| Execution | [Controller](../src/quirkbench/controller.py), [durable jobs](../src/quirkbench/job_coordinator.py), [operator approval](../src/quirkbench/operator_approval.py), [operation tests](../tests/test_operations.py) | Investigation coordinator that joins preparation, proposals, attempts and evidence with crash-safe dispatch |
| Sources and agents | [Catalog](../src/quirkbench/baseline_catalog.py), [agent prototype](../src/quirkbench/agent.py), [contract fixtures](../tests/test_product_contracts.py) | Distributable pinned inputs, editable kernel workspace, full streaming source capture, installed investigation handoff |
| Evidence and use | [Observations](../tests/test_observations.py), [monitor](../src/quirkbench/monitor.py), [maintenance](../src/quirkbench/maintenance.py) | Scientific comparison/report, patch export, investigation views, safe shutdown and guided backup completeness |

The executable [CLI](../src/quirkbench/cli.py) is the evidence for available command
forms. The separate [product CLI parser](../src/quirkbench/product_cli.py) is an older
P0 specification fixture, not the executable interface. Retain its frozen fixtures;
add a new contract revision in the owning implementation packet. Do not rewrite
old fixtures and call that feature completion.

## Delivery milestones and ownership

M numbers identify user outcomes; P numbers remain bounded implementation packets.
Neither numbering replaces C contract identifiers. Select one packet per task.

| Milestone | Outcome and dependencies | Owning packets/contracts |
| --- | --- | --- |
| M1 — fresh controller | Install verified software, complete resumable setup and report readiness with zero enrolled targets | P0, P2a–d; C0/C2/C8 |
| M2 — connected target | After M1, obtain recovery, confirm external media, configure networking and pair securely; missing support is actionable | P3a–f, P1a/b; C1/C4/C8 |
| M3 — investigation baseline | After M2, select retained immutable inputs, create an editable kernel workspace and complete an explicitly approved baseline round trip | P1c, P4, source portion of P6a, needed P7a/b; C1/C3/C5/C8 |
| M4 — external-agent loop | After M3, hand off context, accept source/proposals durably and connect repeated approved experiments to evidence and observations | P6a/b, P7a/b; C2/C3/C5/C8 |
| M5 — report and everyday use | After M4, export attributable patches/results, monitor/pause/resume, shut down safely, manage storage and explain backup/restore coverage | P6b, P7 diagnostics/exports, P2c/e; C2/C3/C5/C8 |
| M6 — attended general release | After M1–M5, complete the main manual using shipped artifacts; separately authorize and pass the applicable frozen final release gates | P8; C7/C8 |
| M7 — optional modes | After the attended journey, add explicit managed invocation and separately authorized unattended qualification/grants | P6c and P5 independently; C3/C6/C8 |

M2 is the first usable fresh-user milestone. M3 proves the lab round trip, not that
the reported problem reproduces. M4 must support a non-audio investigation without
audio dependencies. M5 must support an honest inconclusive export as well as a fix.
M6 does not promise other platforms: retain the current Fedora/x86-64/UEFI/direct
USB implementation and qualify only the combinations actually checked. No second
image builder, web application, cloud account or MCP service is required.

## Manual implementation checklist

**Status legend:** partial = reusable implementation exists, but the stated manual
behavior remains incomplete; planned = no complete implementation of that behavior.
All rows remain open. Source links above and existing suites below identify where
to inspect evidence, not assertions that a suite was run in this documentation task.
Close a row only with implementation files, exact focused check/results, remaining
limitations and artifact qualification status. New acceptance scenarios below are
requirements to implement, not existing passing tests.

| Manual command or promise | Status and current evidence | Owner | Acceptance evidence required to close |
| --- | --- | --- | --- |
| Download/verify archive; `./quirkbench/install` | Partial: signed acquisition/installer, v1/v2 compatibility and authenticated installed-file re-verification; installed setup checked | M1/P2d | Production publisher provisioning/publication/rotation and native commissioning remain acceptance requirements; rollback/active-work checks retained |
| `setup`; `status`; service survival and actionable dependencies | Partial: journaled setup, native zero-target startup, signed runtime binding and durable builder capture/import/readiness; focused fixtures below | M1/P2d | Software integration implemented; enrollment exchange, native commissioning/service survival and complete M1 release acceptance remain open |
| `recovery download`; `recovery-images` | Partial: reviewed signed acquisition through the existing worker/CAS and bounded retained-image listing; production publication and native commissioning pending | M2/P3a3 | Authenticated release metadata and exact compatible image; missing/changed bytes rejected; interrupted download resumes or safely retries; no private factory state |
| Standard image writer; local external-drive confirmation/capacity screen | Partial: commissioning, capacity UI/journal; `test_capacity_setup.py` | M2/P3a2/a5 | Join delivered console flow to exact drive confirmation; restart same geometry, preserve existing filesystems, block insufficient capacity; retain existing implementation |
| Recovery **Network** and **Connect to controller**; `target add NAME` | Partial: reviewed managed exchange/repository listeners, operator invitations, initial console pairing and selected network persistence/replay; maintenance and native commissioning remain open | M2/P3c–e | C4 fingerprint-before-code, complete credentials/repositories, activation interruption and private settings across boots; no fake initial target token |
| `target show NAME`; supported/connected/attended states | Partial: versioned read-only enrollment/binding/planning facts and advisory authenticated contact window; focused fixtures below | M2/P1/P3b | Separate readiness facts; stale/missing report, wrong target, unsupported platform and missing peripheral explicit; pairing queues no experiment; native commissioning remains open |
| Reassign media, revoke credentials, repair changed endpoints | Partial: reviewed revocation, pending invitation maintenance, repeated attended retarget and archived evidence; endpoint migration/native acceptance remain open | M2/P3e/f | Paused explicit maintenance, old evidence attribution, both-service revocation, new trust reconfirmation and rollback; no reflash as routine connection repair |
| `investigation start NAME --target TARGET [--problem FILE]`; limits/baseline wizard | Planned facade; campaign and P0 records exist | M3/P1c/P4/P6a | Durable problem/limits/target/catalog identity, one active investigation per target; selected immutable inputs only; unavailable pinned input blocks, never substitutes |
| **Use existing source** and editable kernel workspace | Partial: source input capture and small snapshot prototype; `test_agent.py`, `test_build_pipeline.py` | M3/P6a | Separate workspace preserves original tree, actual Git base plus distro patch provenance, full tracked/allowed-untracked capture including modes/deletions; concurrent mutation rejected; reconstruct exact build inputs |
| Initial baseline preparation and approved round trip | Partial: build/compose/attempt primitives and approval tests | M3/P4/P7a/b | Joined application flow with injected adapters; no handwritten manifests; exact approval/rejection and restart; record round-trip readiness separately from problem reproduction; physical commissioning only when requested |
| `investigation brief NAME` and handoff to any external coding agent | Planned generated handoff; installed guide exists | M4/P6b | Correct workspace/state/schema/guide paths, durable hypotheses and history; a fresh agent can continue without original chat; no automatic agent call |
| Agent `investigation context/recipes/proposal-schema/propose/capture-source`; evidence/operation queries | Partial: versioned fixtures, operation queries, recipe registry and source primitives | M4/P6a/b/P7a | Atomic proposal/usage/outbox, prompt durable acknowledgment, replay and lost reply, source writer handoff, bounded context/cursors, eligible recipes only, no target shell escape |
| `experiment review ID`; `attempt approve ID` | Partial: approval exists with required request ID; `test_operator_approval.py` | M4/P6b | Review exact source/candidate/attempt, procedure and risks; human facade supplies durable retry identity; old explicit interface preserved; changed bytes/new attempt require authorization |
| `investigation respond NAME`; physical observations | Partial: `session` request/response commands; `test_observations.py` | M4/P7b | Interactive selection backed by same typed records; late/conflicting/missing replies, restart, request/attempt attribution; recipe schema extension explicitly versioned |
| Baseline/diagnostic/patched/regression/revert comparisons | Partial: immutable experiment/result/evidence records | M4–5/P6b/P7 | Exact identity joins, exposure counts, missing observations and confounders, non-audio and missing-peripheral cases; no unsupported causal conclusion |
| `monitor NAME`; `investigation status/pause/resume NAME` | Partial: monitor/campaign pause/reconciliation; `test_monitor.py`, `test_controller.py` | M5/P2c/P6b | Same service facts, investigation filtering, actionable waits, restart remains paused; distinguish admission stopped/workers draining/recovery/evidence; agent exit does not cancel work |
| `target poweroff NAME`; offline recovery shutdown screen | Planned coordinated lifecycle | M5/P6b/P3 | Reconcile active writers/attempts, locally durable evidence and ordered shutdown; show upload backlog independently; network silence never proves poweroff or safe eject |
| `investigation report/export NAME --output PATH`; documented bundle layout | Planned report and patch exporter | M5/P7 | `git format-patch` against actual recorded base, clean-tree application, exported source matches tested source or explicitly unvalidated; evidence/symbol retention, missing bytes explicit, secrets excluded, inconclusive export supported |
| `backup --output PATH`; `restore` wizard | Partial: positional backup/restore and retained closures | M5/P2e | Preserve positional API; consistent source checkpoint, offline target uncertainty, separate private identity requirements, omissions explicit, restore paused; export is not backup |
| `storage`; cache/retention guidance | Partial: settings/maintenance/build-cache commands | M5/P2c/e | Shared retention services, required/live/pinned data protected; explain usage and eligible cleanup without manual deletion |
| `agent configure`; `investigation driver NAME --managed` | Partial: prototype CommandAgent; durable scheduling planned | M7/P6c | Concrete supported command adapter, paused writer handoff, durable decisions, bounded output/time, auth/unknown-usage behavior; external remains default |
| `target qualify NAME`; authorize bounded unattended plan | Planned scoped grants/qualification integration | M7/P5 | C6 grant before activation, exact candidate/attempt/target/media/epoch, revoke/replay/deadline handling; independently recorded physical reset coverage; no unseen future-patch authorization |

## Completion and handoff rules

Each implementation packet updates its rows and the handoff with: contract/record
versions, files changed, exact validation command and result, outstanding integration
and qualification. Mark a command usable only when the executable parser and shared
application service implement it; help fixtures alone do not count. Leave unsupported
features unavailable with an explicit error, never a success-shaped placeholder.

Use focused software tests and injected failure scenarios while developing. Join
the actual application services in flow tests rather than merely chaining mocked CLI
outputs. Physical commissioning is an explicit product operation; image/QEMU/endurance
qualification remains the explicitly authorized final major-version gate. Changed
bytes retain their unqualified status until corresponding checks are performed.

Obtain the required higher-reasoning review before enabling new storage, enrollment,
source ownership, durable dispatch, shutdown or watchdog boundaries. Preserve old records, schema readers,
low-level commands and existing provenance. Missing artifacts remain unavailable.

The attended release is complete when a new user can follow the main README from
installation to an evidence-linked patch package or clearly inconclusive report
using released artifacts, without intervention from the original developer. Remove
the main aspirational warning only after that evidence exists; separately label
unavailable optional modes. The next implementation packet is recorded in the
[handoff](implementation-handoff.md).

M1a implements setup progress v1, shared injected readiness/setup services and the
executable facade in `setup_contracts.py`, `controller_setup.py` and `cli.py`, with
installed schema/example resources. Focused checks cover interrupted acknowledgments
and initial migrations, request replay/conflicts, state/runtime mismatch, changed
manifest/payload identity, filesystem links, active owners/work and read-only status.
Legacy install fixtures now match implemented resource and home-state behavior;
product CLI v1 fixtures remain unchanged. M1b adds an explicit empty-registry service
mode and additive credential metadata/revocation lookup shared by target and mutual-TLS
repository authentication. Local administrative helpers do not implement pairing or
retargeting. M1c adds independently trusted release-set verification and checks the
exact captured install bytes before publication. Optional supplied assets get digest
authentication, without image/runtime qualification or development-payload relabeling.

Validation: `.venv/bin/python -m pytest tests/test_credential_registry.py
tests/test_transport.py tests/test_repository_http.py tests/test_worker_service.py
tests/test_setup_contracts.py tests/test_resumable_setup.py tests/test_controller_setup.py
tests/test_controller_install.py tests/test_installation.py tests/test_product_contracts.py
--basetemp=/var/tmp/quirkbench-m1ab-final-01a0f80e --tb=short`: **171 passed**.
Release verification, install and actual packaged clean-home checks are recorded in
the handoff, including real-GPG fixture verification. Higher-reasoning reviews approved
the bounded setup and authentication changes. No live installation, image bytes or
hardware qualification changed. Full M1 remains open.

M1c acquisition/compatibility software command:

```sh
.venv/bin/python -m pytest tests/test_release_install.py tests/test_release_compatibility.py tests/test_controller_release.py tests/test_controller_archive.py tests/test_controller_install.py --basetemp=/var/tmp/quirkbench-m1c-compatible-final-01a0f80e --tb=short
```

Result: **57 passed**. Synthetic metadata/native fixture signing exercise fail-closed
trust, compatible readers, replay and clean-home packaging. Production publisher
key/fingerprint, trust rotation and compatible signed publication remain release
acceptance inputs; their absence does not block other ready software packets.

Initial native service/TLS integration software command:

```sh
.venv/bin/python -m pytest tests/test_setup_service.py tests/test_controller_tls.py tests/test_resumable_setup.py tests/test_controller_setup.py tests/test_controller_archive.py tests/test_credential_registry.py tests/test_controller_install.py tests/test_product_contracts.py --basetemp=/var/tmp/quirkbench-m1-services-final-01a0f80e --tb=short
```

Result: **145 passed**. Required higher-reasoning review approved the TLS, effective
unit identity, ownership and replay boundaries before enabling `setup --start-service`.
Tests use private temporary keys and injected native services; actual OpenSSL 3/
systemd commissioning, release acceptance and hardware qualification remain pending.

Signed installed-file and builder integration software command:

```sh
.venv/bin/python -m pytest tests/test_builder_setup.py tests/test_registry_submission.py tests/test_worker.py tests/test_worker_claim.py tests/test_worker_service.py tests/test_operations.py tests/test_recovery_podman.py tests/test_installed_release.py tests/test_release_install.py tests/test_controller_release.py tests/test_resumable_setup.py tests/test_controller_archive.py tests/test_controller_install.py --basetemp=/var/tmp/quirkbench-m1-signed-builder-checked-01a0f80e --tb=short
```

Result: **187 passed**. Required higher-reasoning review approved retained original
archive authentication, builder reserve/entrypoint protections, lifecycle/whole-unit
stop and coherent signed/manual identity admission. Tests use injected native
commands and synthetic OCI assets. Native image import/containment commissioning,
production publisher provisioning/publication/rotation and full M1 acceptance remain
outstanding. No live installation or release qualification ran.

M2/P3d invitation and request/key proof foundation commands:

```sh
.venv/bin/python -m pytest tests/test_enrollment_proof.py tests/test_enrollment.py --basetemp=/var/tmp/quirkbench-m2-proof-rate-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_enrollment_proof.py::test_versioned_request_and_challenge_schema_examples --tb=short
```

Results: **38 passed**, then **1 passed** for added schema examples. Higher-reasoning
review approved private code retention, durable clock/rate fences, pre-crypto invalid
attempt accounting, single-use native key proof, canonical stored request binding and
pending revocation. Tests inject native OpenSSL using maintained test-only crypto.
No anonymous HTTP routes, target credentials, activation or experiment authority are
enabled by these foundations. Full M2 acceptance remains open.

M2/P3d pinned TLS, private request/key and complete local generation command:

```sh
.venv/bin/python -m pytest tests/test_enrollment_credentials.py tests/test_enrollment_certificate.py tests/test_enrollment_proof.py tests/test_enrollment_target.py tests/test_enrollment_client.py tests/test_credential_registry.py tests/test_manual_provisioning.py --basetemp=/var/tmp/quirkbench-m2-generation-boundaries-01a0f80e --tb=short
```

Result: **96 passed**. Higher-reasoning reviews approved exact-leaf/SAN trust before
secret transmission, raw-read total deadline/header/depth bounds, verified private
target storage and native key persistence, CA identity capture and exact issuer
receipts, post-native clock fences and recaptured durable replies before atomic
credential publication. Native commands are injected; TLS uses real stdlib handshakes.
Repository publication requires explicit configuration and initialized repositories;
absence is unavailable. HTTP enrollment routes and complete guided M2 remain open.

M2/P3d/e explicit HTTPS exchange and authenticated activation command:

```sh
.venv/bin/python -m pytest tests/test_enrollment_credentials.py::test_full_response_envelope_limit_checked_before_credential_publication tests/test_enrollment_service.py tests/test_enrollment_activation.py tests/test_manual_provisioning.py tests/test_transport.py tests/test_enrollment_client.py --basetemp=/var/tmp/quirkbench-m2-http-activation-checked-01a0f80e --tb=short
```

Result: **57 passed**. Higher-reasoning reviews approved exact authenticated file-map
handoff, final activation binding/generation fences, actual-peer/fresh-proof routing,
bounded native TLS/request workers and full-envelope size before credential commit.
Activation reuses existing immutable private generations; no retargeting or storage
formatter added. Initial guided activation is limited to the currently supported
literal-IP controller TLS identity. Explicit injected applications exercise loopback
exchange; managed-service enablement, repository listener integration, guided console
and complete M2 acceptance remain open. No live installation or qualification ran.

M2/P3d managed publication corrections and strict TLS command:

```sh
.venv/bin/python -m pytest tests/test_enrollment_runtime.py tests/test_enrollment_certificate.py tests/test_controller_tls.py tests/test_setup_service.py --basetemp=/var/tmp/quirkbench-m2-publication-final-fences-01a0f80e --tb=short
```

Result: **61 passed**. Reviewed current-owner/configuration/trust/backend fences
prevent grants after native-boundary changes; real loopback mutual TLS verifies
registered-leaf access, revocation on an existing session and shutdown. Capability
status is separate from target enrollment and attempt authorization. Additional
repository/runtime shutdown checks passed **18 tests** in
`/var/tmp/quirkbench-m2-publication-corrections-01a0f80e` before the final fences.
Native commands remain injected; no native service or installed state changed.

M2 operator invitation/status compatibility command:

```sh
.venv/bin/python -m pytest tests/test_target_setup.py tests/test_product_contracts.py tests/test_resumable_setup.py::test_executable_parser_additive_contract --basetemp=/var/tmp/quirkbench-m2-operator-cli-checked-01a0f80e --tb=short
```

Result: **56 passed**. Human invitation retry retains custom lifetime across both
private/commit acknowledgment loss boundaries; JSON mutations require request IDs.
Read-only status validates recorded identities, hides secrets, distinguishes missing
reports/revocation and leaves live contact unknown. Frozen product CLI v1 remains
unchanged; additive target CLI v2 preserves the legacy flags-only client.

M2 operator/console integrated command after status review corrections:

```sh
.venv/bin/python -m pytest tests/test_enrollment_console.py tests/test_console.py tests/test_target_setup.py tests/test_enrollment_runtime.py tests/test_enrollment_activation.py tests/test_enrollment_service.py --basetemp=/var/tmp/quirkbench-m2-console-operator-integration-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_enrollment_console.py tests/test_console.py --basetemp=/var/tmp/quirkbench-m2-console-private-input-01a0f80e --tb=short
```

Results: **72 passed**, then **21 passed** for the terminal echo-control correction.
Higher-reasoning review approved shared publication locking, consistent name/ID
binding validation, same-snapshot wrong-media blockers and attended console ownership/
secret handling. Initial console retries retain keys and activate one complete private
generation after a lost reply. Active configuration is rejected before service stop.
Missing native crypto is actionable before secret input. Native recovery acceptance
still needs exact stock `openssl` RPM inputs/publication; no historical package digest
was changed. Expired unredeemed pending-intent recovery, saved network selection,
endpoint/retarget maintenance and full M2 acceptance remain open.

M2 selected network/private RAM integration command:

```sh
.venv/bin/python -m pytest tests/test_network_profiles.py tests/test_console.py tests/test_boot.py tests/test_runtime.py --basetemp=/var/tmp/quirkbench-m2-network-cleanup-fence-01a0f80e --tb=short
```

Result: **140 passed**. Higher-reasoning review approved explicit private selection,
same-target/media/runtime and final source/generation fences, nested-mount rejection
before secret reads, binding-before-password boot replay and existing native service
ordering. Complete rollback/pre-write rejection retains local setup; incomplete
RAM cleanup denies the oneshot ACK and blocks NetworkManager. Existing/operator
files remain intact. Source/module/native-unit changes require new captured recovery
and candidate runtime inputs; historical image qualification is not evidence for
these changes. No image production, live installation or qualification ran.

M2 authenticated contact/status command:

```sh
.venv/bin/python -m pytest tests/test_protocol_contact.py tests/test_target_setup.py tests/test_registry_submission.py tests/test_transport.py --basetemp=/var/tmp/quirkbench-m2-contact-facts-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_protocol_contact.py --basetemp=/var/tmp/quirkbench-m2-contact-negative-01a0f80e --tb=short
```

Results: **32 passed**, then **5 passed** after adding failed/stale request,
reconciliation and CLI-version coverage. Higher-reasoning review approved the
advisory receipt and same-snapshot status boundary. V2 contact requires matching
current boot/generation, live credentials and receipt age under 30 seconds; v1 JSON
stays compatible. No secrets or experiment authorization enter these observations.
Native commissioning and complete M2 acceptance remain open; no image or live
installation changed.

M2 initial pending-invitation maintenance command:

```sh
.venv/bin/python -m pytest tests/test_enrollment_maintenance.py tests/test_enrollment_target.py tests/test_enrollment_console.py --basetemp=/var/tmp/quirkbench-m2-initial-selection-review-01a0f80e --tb=short
```

Result: **60 passed**. Higher-reasoning review approved explicit initial replacement/
archived resume after correcting stale terminal-replay pointer publication and
post-native byte fences. Tests cover interrupted selection/key/rename/receipt boundaries,
wrong domain/media/UUID/mounts, private history bounds, prior work/activation refusal,
expired unredeemed invitations and completed lost-reply recovery without a second
credential generation. Keys are never silently discarded. Native crypto remains
injected; changed recovery runtime inputs need fresh image production/qualification.
Active retarget, endpoint maintenance, native commissioning and complete M2 acceptance
remain open. No image production, live installation or qualification ran.

Signed released-recovery acquisition (C0/C2/C4/C8) is implemented and reviewed in
`recovery_download.py` and the existing fixed worker/coordinator. Admission reads
bounded local intent only; the worker authenticates current installed release bytes,
exact publisher statement/signature, stock factory metadata and streamed image.
Status/headers/body have monotonic deadlines; the native unit caps runtime at one
hour independently of wall-clock rollback. Reserve checks, private staging, whole
worker stop, CAS recapture and final current trust/epoch/generation/deadline fences
precede atomic output/retention publication. Strict released-recovery-acquisition v1
and executable CLI v1 fixtures preserve older recovery RPM acquisition meanings.
Missing production trust returns UNAVAILABLE before state/downloads. Outputs remain
unqualified and grant no writing, builder, baseline or attempt authority.

Focused command: `.venv/bin/python -m pytest tests/test_recovery_download.py tests/test_worker_service.py tests/test_worker_claim.py tests/test_operations.py tests/test_release_compatibility.py tests/test_builder_setup.py --basetemp=/var/tmp/quirkbench-m2-recovery-acquisition-reviewed-01a0f80e --tb=short`
— **101 passed** (15.71 s). Streaming checks use small byte fixtures; factory and
service checks inject native adapters/measurement. No image production, live setup
or qualification ran. Production publisher provisioning/publication and actual
native acquisition remain acceptance requirements; the full M1/M2 journey is open.

Explicit local revocation (C0/C2/C4/C8) is implemented/reviewed in
`target_lifecycle.py`, the registry/controller execution gates and target CLI v4.
Exact receipts, generation revocation and all active campaign pauses commit together;
receipt replay never revokes a replacement generation. SQL-only identity selection
needs no readiness artifacts. Large histories retain complete counts and first-1000
sorted identity samples without blocking revocation. Both HTTP channels deny revoked
credentials. Registry attempts bind their admitted generation; replacement generations
cannot revive old tokens, approval replays or first candidate adoption. Static targets
without registry history retain existing behavior. Unproven historical registry attempts
remain blocked until reconciliation; old evidence and pending worker obligations retain
original attribution. Revocation reports physical state as unverified and supplies no
old-evidence drain or retarget authority.

Focused compatibility checks covered the controller, physical handoff, approval,
static authentication and worker suites. Corrected identity/contact checks passed
with `.venv/bin/python -m pytest tests/test_target_lifecycle.py tests/test_protocol_contact.py --basetemp=/var/tmp/quirkbench-m2-revocation-final-01a0f80e --tb=short`
— **26 passed** (6.68 s). The final count/receipt correction passed **21** checks
in `.venv/bin/python -m pytest tests/test_target_lifecycle.py --basetemp=/var/tmp/quirkbench-m2-revocation-large-replay-01a0f80e --tb=short`;
its corrected fixture passed with `.venv/bin/python -m pytest tests/test_target_lifecycle.py::test_large_history_revocation_is_complete_bounded_and_replayable_after_lost_ack --basetemp=/var/tmp/quirkbench-m2-revocation-history-01a0f80e --tb=short`
— **1 passed** (0.21 s). No live revocation, image production or qualification ran.

Shared installer/recovery native transport (C0/C2/C4/C8) is implemented/reviewed
in `release_http.py`. Fixed isolated DNS resolution kills/reaps timed-out children;
all addresses, native TLS, request writes and raw framing/body reads consume a
cumulative monotonic deadline. Native SAN verification, independent publisher trust,
signature/compatibility checks and existing durable receipts remain required.
An actual locally signed release over fixture TLS tests the installer with an
explicit injected test key, including signature rejection before archive download.
No test trust is shipped or activated as production trust.

Focused command: `.venv/bin/python -m pytest tests/test_release_http.py tests/test_release_install.py tests/test_recovery_download.py --basetemp=/var/tmp/quirkbench-release-http-final-01a0f80e --tb=short`
— **55 passed** (7.40 s). Loopback TLS and isolated fixture GPG ran; no live
installation, image production or qualification ran. Production provisioning and
complete M1/M2 acceptance remain open.

Released recovery listing (C0/C2/C4/C8) is implemented/reviewed in
`released_recovery.py`/`recovery_listing.py`, with additive recovery CLI v2 fixtures.
The existing read-only listing includes successful acquired sets and preserves
legacy prepared-image fields/cursors. Strict retained receipt/statement digests,
asset linkage, output references and regular unlinked object sizes gate retention.
Publication verification remains historical: listing does not rehash image bytes,
reverify current trust or grant qualification/writing/attempt authority.

Focused command: `.venv/bin/python -m pytest tests/test_recovery_listing.py tests/test_recovery_download.py --basetemp=/var/tmp/quirkbench-m2-recovery-listing-01a0f80e --tb=short`
— **46 passed**, with two reference-fixture failures corrected and checked by
`.venv/bin/python -m pytest tests/test_recovery_listing.py::test_missing_or_inconsistent_published_objects_are_unavailable[statement-linkage] tests/test_recovery_listing.py::test_cursor_mixes_legacy_signed_images_and_pending_acquisition --basetemp=/var/tmp/quirkbench-m2-recovery-listing-linked-fixtures-01a0f80e --tb=short`
— **2 passed** (0.80 s). No implementation changed after the first check.
No live state/image or qualification changed; full M2 acceptance remains open.

Explicit old-evidence grant prerequisite (P3e/C4/C0/C2/C8) is implemented/reviewed
in `evidence_drain.py`, `evidence_drain_client.py`, the existing controller upload/
evidence methods and two registry-only HTTPS routes. `target drain-approve TARGET
--file PLAN --request-id ID` approves only exact bounded original evidence; the
private token precedes digest-only SQL commit and public replies contain its path.
`target drain-revoke TARGET --grant ID` is terminal. Target CLI v5 and plan/grant v1
records preserve prior interfaces. Original revoked generation/media/UUID/boot,
all-target attempt reconciliation, PAUSED campaigns and whole-worker stop gate use.
Fixed per-entry upload IDs bound allocations; current lifecycle, scope, expiry and
durable clock fences apply before/after I/O and duplicate ACK. Expiry-denial samples
survive transaction rollback. Native CA/SAN transport refuses redirects/proxies and
bounds DNS/connect/TLS/POST writes/framing/body. Results, recovery arrival, registration,
candidate execution and repository access remain unauthorized; completed evidence and
expired retention stay immutable. The target spool facade is implemented below.

Focused commands/results:

```sh
.venv/bin/python -m pytest tests/test_evidence_drain.py tests/test_evidence_drain_client.py tests/test_release_http.py --basetemp=/var/tmp/quirkbench-m2-evidence-drain-fences-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_evidence_drain.py tests/test_evidence_drain_client.py --basetemp=/var/tmp/quirkbench-m2-evidence-drain-final-clock-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_transport.py tests/test_controller.py tests/test_target_lifecycle.py tests/test_target_setup.py --basetemp=/var/tmp/quirkbench-m2-evidence-drain-compatibility-01a0f80e --tb=short
```

**73 passed** (13.30 s), then **66 passed** (12.28 s) after the six fresh-clock
regressions; **55 passed** (12.86 s) for existing protocol/controller/CLI compatibility.
Higher-reasoning review approved after redirect, private-reader and expiry corrections.
Only small DB/CAS and loopback TLS fixtures ran. No live grants, setup, image production
or qualification ran. Active retarget/endpoint/native acceptance remain open.

Local original-spool drain (P3e/C4) is implemented/reviewed in
`evidence_drain_target.py`, the additive exact-scope `TargetAgent._drain` and recovery
console choice 7. `plan REQUEST_ID` exports at most 128 original records/1 GiB and
reports selected versus remaining counts. After explicit controller approval and
private staging, `drain REQUEST_ID GRANT_ID` updates only selected upload/ACK
progress. The strict private old-evidence-drain-source v1 record freezes the plan,
original enrolled source and all unselected journal facts across retries. Native
SMBIOS, media/storage, exact runtime/generation/CA and staged-grant bytes gate secret
access, each request and ACK save. Sealed blob capture rejects links, replacement
and changed bytes; each request receives the remaining 120-second batch budget.
Selected ACKed records may be restored after lost controller acknowledgment.
Original pending attempt/result/blobs stay retained; no step/register/recipe/complete,
new activation or physical authority is granted. Changed hardware is blocked pending
explicit retarget maintenance. The existing supervisor stops and restarts around
attended use, including cancellation and uncertain stop.

Focused command:

```sh
.venv/bin/python -m pytest tests/test_evidence_drain_target.py tests/test_target.py tests/test_console.py --basetemp=/var/tmp/quirkbench-m2-original-spool-final-01a0f80e --tb=short
```

**51 passed** (38.05 s). Higher-reasoning review approved the source/storage/ACK
boundaries. Only local injected fixtures ran. Target runtime/console image inputs
changed and remain unqualified; fresh image/native commissioning, active retarget,
endpoint maintenance and full M2 acceptance remain open.

One-shot clearance prerequisite (P3e/C4) is implemented/reviewed in
`boot.clear_once`, reused by `UsbBootControl.recover` between evidence-storage fences.
Full native recovery boot-root/USB/GPT verification remains required. The one exact
p3 whole-filesystem vfat mount, source/major:minor, private masks, restrictions and
no nested mounts gate held no-follow private directory/file descriptors. The existing
single-link 1024-byte environment must retain its device/inode/path identity through
native unset, file/directory fsync and bounded duplicate-rejecting native list
confirmation; all three one-shot fields must be absent, including empty values.
This disarm operation permits unavailable experiment/library mounts, without
weakening candidate arming, creating state, remounting or asserting controller
reconciliation/recovery arrival/physical shutdown. The reviewed system-observation
recipe's full-module checksum was refreshed for the runtime edit; its callable is unchanged.

```sh
.venv/bin/python -m pytest tests/test_one_shot_clearance.py tests/test_boot.py tests/test_runtime.py tests/test_recipe_registry.py --basetemp=/var/tmp/quirkbench-m2-one-shot-identity-final-01a0f80e --tb=short
```

**150 passed** (1.25 s). Higher-reasoning review approved p3 confinement and
confirmation. Only injected software fixtures ran, with no native GRUB/media/service
operation or release qualification. Changed recovery/candidate runtime inputs remain
unqualified pending fresh image/native commissioning. Complete retarget remains open.

Controller-scoped retarget invitation/authenticated reply prerequisite (P3e/C4)
is implemented/reviewed in `retarget_invitation.py`, the existing code/proof/
certificate/complete paths and strict enrollment-result v2. Explicit
`target retarget-code OLD --generation EXACT --new-name NAME --new-uuid UUID
--request-id ID [--ttl-seconds N] [--json]` never revokes implicitly. It requires
the exact revoked original generation/document digest/media/binding, all original
campaigns PAUSED, only terminal reconciled attempts with closed recovery obligations,
and confirmed whole-worker stops. A different new UUID/media must have no competing
live registry owner. Every exchange/publication/replay rechecks these facts;
COMPLETE lost-ACK replay permits only that request's own exact new generation.
Private scoped intent/code retention precedes atomic purpose+scope+code publication
in the existing DB. Missing committed scope/secret is terminal, never repaired or
allowed to fall back to initial enrollment. Fresh clock fences follow private retention.

CLI v6 and retarget-invitation v1 preserve prior fixtures/wires. Retarget alone
returns authenticated enrollment-result v2 with exact controller scope bound into
the retained result digest; first completion and COMPLETE replay reject altered
scope or downgrade to v1. Initial activation refuses v2 before effects. New device/
generation IDs cannot inherit the original IDs, attempts, approvals or campaigns.
Local one-shot clearance, old spool preservation, stopped atomic activation and
reset/watchdog invalidation remain separate unfinished retarget work.

```sh
.venv/bin/python -m pytest tests/test_retarget_invitation.py tests/test_enrollment.py tests/test_enrollment_proof.py tests/test_enrollment_credentials.py tests/test_evidence_drain.py tests/test_target_setup.py tests/test_target_lifecycle.py --basetemp=/var/tmp/quirkbench-m2-scoped-retarget-final-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_retarget_invitation.py tests/test_enrollment_credentials.py tests/test_enrollment_activation.py tests/test_enrollment_service.py --basetemp=/var/tmp/quirkbench-m2-authenticated-retarget-reply-01a0f80e --tb=short
```

First command: **178 passed**, one nested schema reference failure; the schema
was corrected. Second command: **72 passed** (39.15 s), covering that correction
and added authenticated v2/downgrade/activation regressions. Higher-reasoning review
approved both scoped issuance and authenticated replies. Only injected small DB/
crypto/service fixtures ran; no live invitations, activation, image or qualification
changed. Full retarget/M2/native and production publisher acceptance remain open.

Paused local retarget preparation/source preservation (P3e/C4) is implemented and
reviewed in `retarget_local.py` with strict private retarget-local-intent/source v1
records. Native recovery/p3 verification and the explicitly different actual new UUID
precede pause/intent writes under existing exclusive config/agent locks. Exact
confirmed old device/runtime/media identity is retained before effects. Fresh native
one-shot clearance precedes old credential/result/key source capture on every retry.
Bounded source file hashes and an exact bounded original journal freeze attribution
without traversing or promoting large blobs; 120-second cooperative deadlines and
source recapture fence publication. Runtime checks incomplete maintenance under
config ownership before provisioning/watchdog/client creation; network save/replay
checks before profile secrets or writes. Lost/corrupt/dangling pointers and retained
requests without a pointer stay blocked. Only the sole exact explicit stopped retry
can restore lost pointer publication and recheck clearance/source.

The original runtime, key/request/result/generation, journal, blobs and network
profiles remain in place. This internal prerequisite exposes no completion,
pointer removal, cancellation/unpause, new enrollment/activation or controller/
physical/reset claims; full local retarget remains unavailable.

```sh
.venv/bin/python -m pytest tests/test_retarget_local.py tests/test_network_profiles.py tests/test_runtime.py --basetemp=/var/tmp/quirkbench-m2-local-retarget-pause-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_retarget_local.py tests/test_boot.py tests/test_recipe_registry.py --basetemp=/var/tmp/quirkbench-m2-local-retarget-preparation-final-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_retarget_local.py::test_lost_pointer_cannot_unpause_but_exact_stopped_retry_restores_it tests/test_retarget_local.py::test_orphan_intent_before_pointer_publication_is_fenced_and_exact_retryable tests/test_retarget_local.py::test_unknown_request_cannot_adopt_retained_records_after_pointer_loss --basetemp=/var/tmp/quirkbench-m2-local-retarget-lost-pointer-01a0f80e --tb=short
```

**97 passed** (32.69 s), then **106 passed** (30.02 s) with recovery-before-effects
and schema regressions; **3 passed** (3.33 s) after the missing-pointer review fix.
Higher-reasoning review approved that fix and source/storage/pause boundaries.
Only local injected fixtures ran. Runtime recipe full-module checksum refreshed
without changing its callable; changed image inputs remain unqualified. No live
pause, GRUB, media, network, service, image or release qualification operation ran.

New request/key proof within paused retarget (P3e/C4) is implemented/reviewed in
`retarget_enrollment.py`, reusing initial native key/proof primitives under the
existing config and original-agent locks. Fresh recovery/actual new identity and
one-shot clearance precede exact old source capture on each operation. A separate
private namespace retains the new request/key; old runtime, credentials, spool and
attribution remain intact. Both old source and exact new intent/request/key bytes
are recaptured after native work before requests or signatures return. No local
activation, pointer removal or inherited authorization is exposed.

```sh
.venv/bin/python -m pytest tests/test_retarget_enrollment.py tests/test_enrollment_target.py tests/test_enrollment_maintenance.py --basetemp=/var/tmp/quirkbench-m2-retarget-key-proof-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_retarget_enrollment.py --basetemp=/var/tmp/quirkbench-m2-retarget-new-key-fences-01a0f80e --tb=short
.venv/bin/python -m pytest 'tests/test_retarget_enrollment.py::test_new_namespace_mutation_at_request_boundary_cannot_return_request[key.pem]' --basetemp=/var/tmp/quirkbench-m2-retarget-key-native-fixture-01a0f80e --tb=short
```

**69 passed** (45.56 s), then **26 passed** (43.06 s) plus **1 passed** (1.42 s)
after correcting the adversarial fixture to replace a key with valid native PEM.
Higher-reasoning review approved the final new-key/source fences. Only injected
fixtures ran; no live key, GRUB, service, media, image or qualification changed.
Complete retarget/M2 and production publisher/native acceptance remain open.

Pinned enrollment transport now reuses the reviewed bounded DNS/connect/TLS/write/
raw-read adapter used for release acquisition and restricted evidence drain.
Exact approved leaf comparison follows native SAN/purpose/validity verification
before request bytes. Public inspection remains secret-free and unauthenticated.
Duplicate Content-Length, transfer framing and encoded replies are rejected;
Cache-Control: no-store remains. This is a local P3d/e adapter correction.

```sh
.venv/bin/python -m pytest tests/test_enrollment_client.py tests/test_release_http.py tests/test_evidence_drain_client.py --basetemp=/var/tmp/quirkbench-m2-bounded-pinned-transport-01a0f80e --tb=short
```

**55 passed** (16.02 s), including native loopback TLS, injected DNS deadline,
exact-pin-before-secret and cumulative partial-write checks. Higher-reasoning
review approved the transport boundary. No live setup/service/image changed.

Authenticated retarget exchange/private bundle retention (P3e/C4) is implemented
in `retarget_enrollment.exchange`, reusing native enrollment key/proof and shared
activation validators. The approved leaf is verified under the exact retained
original CA before secret POSTs. A strictly validated challenge precedes signing;
every retry obtains fresh controller proof/reply, including COMPLETE/lost ACK.
The reply must be v2 with exact original device/generation/document digest/media/
bindings and approved code/endpoint/leaf pin. CA and repository aliases/URLs/public
keys stay exact; trust rotation requires separate maintenance. Private result and
bundle retention never activate or move the original runtime/spool. Final byte
recapture, expiry and deadline checks run under both locks after native work.

```sh
.venv/bin/python -m pytest tests/test_retarget_enrollment.py tests/test_enrollment_activation.py --basetemp=/var/tmp/quirkbench-m2-retarget-authenticated-bundle-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_retarget_enrollment.py::test_unvalidated_challenge_cannot_sign_new_key_or_redeem --basetemp=/var/tmp/quirkbench-m2-retarget-challenge-typed-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_retarget_enrollment.py::test_final_bundle_read_cannot_outlive_new_credential_expiry tests/test_retarget_enrollment.py::test_final_bundle_read_cannot_extend_retarg_cooperative_budget tests/test_retarget_enrollment.py::test_fresh_authenticated_exchange_retains_complete_bundle_without_activation --basetemp=/var/tmp/quirkbench-m2-retarget-final-freshness-01a0f80e --tb=short
```

**58 passed** (98.08 s), **4 passed** (7.06 s) for the strict challenge correction,
and **3 passed** (9.59 s) for final freshness and normal retry. Injected small
controller/native fixtures only; no live enrollment, activation or service changed.
Full atomic local retarget, archived old-evidence access and native acceptance
remain open.

Atomic stopped local retarget activation/original spool archival (P3e/C4) is
implemented/reviewed in `retarget_activation.py`. An immutable activation intent
binds frozen old source and authenticated new enrollment/bundle hashes. Fresh
controller proof precedes every unfinished continuation; authenticated handoff
hashes prevent source changes across lock reacquisition. The shared existing private
generation publisher preserves manual activation's retarget denial. Original agent
and pending enrollment move by same-filesystem rename with both parents fsynced;
original blob inodes/attribution remain intact. A protected blank new spool and
coherent four-file new enrollment are selected before runtime.json. Both agent lock
FDs remain owned and named-inode checked through final publication.

Runtime remains paused after a crash even when new runtime bytes are present.
Only exact canonical completion and pointer v2 permit resumed use, after current
new UUID/media, new runtime/enrollment/generation and existing private spool checks.
Completed ACK replay never clears one-shot state or changes later new work. No old
qualification, grants or saved network binding is inherited. Old-evidence drain,
attended retarget UI, repeated moved-media maintenance and native acceptance remain
separate unfinished work.

```sh
.venv/bin/python -m pytest tests/test_manual_provisioning.py tests/test_enrollment_activation.py --basetemp=/var/tmp/quirkbench-m2-shared-generation-publisher-checked-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_retarget_activation.py --basetemp=/var/tmp/quirkbench-m2-retarget-atomic-reviewed-01a0f80e --tb=short
```

**28 passed** (11.82 s), then **39 passed** (232.95 s), including every injected
crash phase, exact partial-stage retry, duplicate/mixed source rejection, preserved
later one-shot/journal state, canonical type substitutions, missing spools and lock
inode replacement. Higher-reasoning review approved the final source/ownership/
trust boundaries. Only injected native/storage/controller fixtures ran; no live
media, GRUB, key, supervisor, image or release qualification changed. Changed image
inputs remain unqualified pending native commissioning.

Explicit archived old-evidence adapter after completed retarget (P3e/C4) is
implemented in `retarget_evidence.py`, reusing existing grant, fixed source reader,
sealed-blob verifier and scoped spool writer. Native recovery/current new UUID and
strict selected completion precede old secrets. Existing config/new-agent/archive-
agent locks and named FD checks fence all source reads, requests and ACK writes.
The current new journal remains byte-exact throughout. Original static files,
generation and raw immutable journal snapshot match retained source; current old
journal identity may differ only in ACK/offset progress, with existing per-plan
unselected-progress checks. Only original device/generation/attempt/boot/media/UUID
and explicitly approved manifest are sent through upload/evidence routes. Neither
one-shot state, new work, active runtime, profiles nor attempt completion changes.

Shared helpers carry the single original absolute deadline through preflight and
request timeouts; the native client also caps serialization and HTTPS by that end.
Existing original-hardware entry points retain their strict binding gate. Explicit
archived-plan/drain actions are separate; no generic moved-hardware bypass exists.

```sh
.venv/bin/python -m pytest tests/test_evidence_drain_target.py --basetemp=/var/tmp/quirkbench-m2-shared-drain-view-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_retarget_evidence.py --basetemp=/var/tmp/quirkbench-m2-archived-drain-boundaries-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_evidence_drain_client.py tests/test_evidence_drain_target.py::test_remaining_batch_budget_caps_each_request tests/test_enrollment_console.py --basetemp=/var/tmp/quirkbench-m2-deadline-console-shared-01a0f80e --tb=short
```

**25 passed** (33.83 s), **13 passed** (202.52 s) and **27 passed** (12.21 s).
These include actual fixture-controller original attribution/ACK replay, wrong
hardware/incomplete selection, frozen-source and lock changes, later new journal
mutation, selected-only blob reads and original remaining request/serialization
budget. Only fixtures ran; no live grants, media, one-shot, network or service changed.
Native commissioning and complete M2 acceptance remain open.

Attended retarget and explicit archived drain screens (P3e/C4) are implemented and
reviewed in `retarget_console.py` and the existing recovery console/supervisor.
Exact original target/actual new UUID confirmation precedes pause; full observed
fingerprint approval precedes retained new key and no-echo code entry. Interrupted
exchange/activation resumes the same request. Completed ACK preserves later work
and one-shot state without HTTP or secret entry. Explicit `archived-plan RETARGET_ID
PLAN_ID` and `archived-drain RETARGET_ID PLAN_ID GRANT_ID` select the reviewed archive
adapter. Native prerequisites precede stopping the existing supervisor.

```sh
.venv/bin/python -m pytest tests/test_retarget_console.py --basetemp=/var/tmp/quirkbench-m2-attended-retarget-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_retarget_console.py::test_recovery_menu_gates_retarget_and_returns_after_interruption tests/test_retarget_console.py::test_terminal_echo_failure_keeps_new_request_key_and_sends_no_code tests/test_console.py --basetemp=/var/tmp/quirkbench-m2-retarget-menu-secret-01a0f80e --tb=short
```

**8 passed** (26.04 s), then **14 passed** (1.97 s). Higher-reasoning review approved
these boundaries and the archived adapter's original absolute deadline correction.
Only fixtures ran; no live media, service, trust, one-shot or image changed. The
first retarget and exact replay are supported by these software adapters; repeated
moved-media transitions, expired new-invitation maintenance, endpoint maintenance,
native commissioning and complete M1/M2 acceptance remain open.

Repeated moved-media retarget transition (P3e/C4) is implemented/reviewed in
`retarget_local.py` and the archived adapter. Local-intent v2 retains the exact
previous completed pointer in `previous-selection.json`; v1 remains readable.
Bounded public history verifies every device/runtime/media/binding edge, rejects
cycles/orphans/unlinked records and preserves all original spools/generations.
The successor intent precedes clearance, so crashes keep earlier runtime paused
even after moving back to its hardware. Only exact stopped retry repairs its one
unpublished successor. Historical completion verification occurs only after actual
NEW identity, recovery/p3, ownership, pause and fresh native one-shot clearance.
Runtime and archived evidence always gate secrets on the real current head UUID.
Explicit archived drain can select any completed linked member, validates its
frozen private bytes and leaves the current journal/runtime unchanged.

```sh
.venv/bin/python -m pytest tests/test_retarget_local.py tests/test_retarget_activation.py::test_atomic_new_runtime_preserves_original_spool_and_no_authorizations --basetemp=/var/tmp/quirkbench-m2-retarget-history-initial-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_retarget_history.py::test_repeated_preparation_links_original_completion_and_never_authenticates_old_key tests/test_retarget_history.py::test_repeated_activation_keeps_both_archives_and_strict_current_hardware --basetemp=/var/tmp/quirkbench-m2-retarget-history-smoke-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_retarget_history.py tests/test_retarget_evidence.py::test_archived_view_rejects_unfinished_moved_or_changed_sources --basetemp=/var/tmp/quirkbench-m2-retarget-history-boundaries-01a0f80e --tb=short
```

**37 passed** (39.01 s), **2 passed** (17.13 s), **28 passed** (231.92 s), covering
all public successor crashes, coherent semantic-edge changes, missing pointer/exact
retry, preserved older archives, actual fixture-controller original evidence drain,
wrong hardware and versioned schema compatibility. Higher-reasoning review approved
the final history/source/ownership gates. Fixtures only; changed image inputs remain
unqualified. Expired invitation/endpoint/native and complete M1/M2 acceptance remain
open; production publisher provisioning/publication remains a release-preparation input.

Explicit paused-retarget invitation maintenance (P3e/C4) is implemented/reviewed
in `retarget_maintenance.py`, the private initial selection engine, retarget proof/
activation and console adapters. Typed selection v1 binds the same local intent/
source, endpoint/full pin/media/NEW binding; original keys survive exact journaled
archive/selection/resume. Public initial-only policy remains strict. Result/bundle/
activation evidence blocks replacement; local selection never cancels controller
redemption. The controller requires every revoked overlapping identity to reconcile
campaigns/attempts/recovery return/workers before replacing its invitation.

Exact immutable selected maps and journal bytes survive pointer reconciliation and
no-pointer replay, fence native preparation and every request, and run after final
native source checks. Missing selected keys are never regenerated. Challenge expiry
is checked after all final file reads. Full fingerprint approval precedes explicit
replace/resume request confirmation and no-echo secret entry. Ordinary original
request and replacement COMPLETE/lost-ACK retries retain the same key.

```sh
.venv/bin/python -m pytest tests/test_enrollment_maintenance.py tests/test_enrollment_target.py --basetemp=/var/tmp/quirkbench-m2-selection-engine-reuse-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_retarget_maintenance.py::test_resume_restores_original_invitation_key_and_no_remote_authority tests/test_retarget_invitation.py::test_revoked_lost_complete_owner_must_reconcile_before_replacement --basetemp=/var/tmp/quirkbench-m2-retarget-selection-smoke-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_retarget_maintenance.py tests/test_retarget_invitation.py tests/test_retarget_console.py --basetemp=/var/tmp/quirkbench-m2-retarget-invitation-maintenance-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_retarget_maintenance.py::test_completed_choice_never_regenerates_missing_or_changed_selected_key tests/test_retarget_console.py::test_pending_retarget_invitation_requires_exact_replace_confirmation --basetemp=/var/tmp/quirkbench-m2-retarget-selection-key-console-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_retarget_maintenance.py::test_coherent_native_guard_key_request_swap_cannot_be_adopted tests/test_retarget_maintenance.py::test_final_native_guard_cannot_change_selection_bytes_or_original_record tests/test_retarget_maintenance.py::test_actual_replacement_exchange_retains_original_archive_and_complete_retry tests/test_retarget_console.py::test_pending_retarget_invitation_requires_exact_replace_confirmation --basetemp=/var/tmp/quirkbench-m2-retarget-selection-final-fences-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_retarget_maintenance.py::test_actual_replacement_exchange_retains_original_archive_and_complete_retry 'tests/test_retarget_console.py::test_pending_retarget_invitation_requires_exact_replace_confirmation[replace]' tests/test_enrollment_activation.py --basetemp=/var/tmp/quirkbench-m2-retarget-native-staging-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_retarget_maintenance.py::test_final_selection_file_fence_cannot_outlive_nonce_expiry --basetemp=/var/tmp/quirkbench-m2-retarget-selection-proof-expiry-01a0f80e --tb=short
```

**49 passed** (10.24 s), **2 passed** (18.00 s), **68 passed** (238.07 s),
**6 passed** (71.83 s). Review then found a coherent key/request recovery race;
immutable selection fences closed it. The focused final-fence run passed **7**
cases and exposed two ordinary exchange staging conflicts; moving native retarget
validation into its owned private parent preserved the strict selected namespace.
The **18** affected exchange/initial-native cases passed (50.13 s); the added final
nonce-expiry case passed (16.97 s). Higher-reasoning review approved final source.
Fixtures only; no live grants, CA, media, service, one-shot or image changed. Endpoint
maintenance, native commissioning and complete M1/M2 acceptance remain open.

Retained-CA controller endpoint identity staging (P3f/C4/C8) is implemented/reviewed
in `controller_endpoint.py` and the shared native TLS material helpers. Stopped
existing command/coordinator FD ownership and idle work fence the exact currently
configured managed identity throughout. TLS intent/identity v2 retain v1 readers,
bind the exact predecessor/configuration and enforce bounded immutable CA lineage.
All CA/key temporary copies, native outputs and serials belong to private successor
staging; no predecessor material, service, configuration or target trust changes.
Interrupted stages retain exact keys. Native destination SAN/purpose/keypair/validity
and actual expiry no later than the retained CA are checked before publication.
Final intent/old/new bytes, source-leaf validity and clock/deadline follow native work.

```sh
.venv/bin/python -m pytest tests/test_controller_tls.py --basetemp=/var/tmp/quirkbench-m2-shared-endpoint-tls-generator-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_controller_endpoint.py tests/test_controller_tls.py --basetemp=/var/tmp/quirkbench-m2-retained-ca-successor-checked-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_controller_endpoint.py tests/test_controller_tls.py --basetemp=/var/tmp/quirkbench-m2-retained-ca-successor-final-01a0f80e --tb=short
```

**9 passed** (0.43 s), **29 passed** (4.46 s), then **33 passed** (6.06 s) after
review corrections to final intent, source-expiry and native-temp containment.
These include all seven crash phases, exact private-key replay, owner/configuration/
source/new-key mutation, lifetime refusal and actual v1/v2 schema checks. Final
higher-reasoning review approved fresh-source staging. Injected native crypto only;
no live key, service, installation, media, image or qualification changed. This
prerequisite requires a valid current source leaf; expired-source renewal, operator
wizard/service switch and atomic target endpoint generations remain open P3f work.
Full M1/M2 and production publisher/publication acceptance remain open.

Explicit expired-source retained-CA renewal is implemented/reviewed separately
from fresh-source staging. TLS intent v3 fixes the renewal mode and exact source;
historical-time verification applies only to that captured expired source leaf.
Its retained CA, destination, normal readiness and transport retain strict current
validity checks. Exact source/configuration/FD/intent/final-byte fences and native
SAN/purpose/signature/keypair verification remain required.

```sh
.venv/bin/python -m pytest tests/test_controller_endpoint.py tests/test_controller_tls.py --basetemp=/var/tmp/quirkbench-m2-expired-source-ca-renewal-final-01a0f80e --tb=short
```

**40 passed** (7.71 s), including typed retry isolation, expired-CA refusal and
four renewal interruption points. Higher-reasoning review approved the source-only
temporal relaxation. Injected test crypto only; controller/target configuration
switches, wizard/native commissioning and full M1/M2 acceptance remain open.

Stopped controller endpoint switch/rollback (P3f) is implemented/reviewed in
`endpoint_switch.py`. Exact original configuration, retained CA and completed
successor TLS stage gate an immutable switch intent. Only bind host/cert/key and an
explicit matching-SAN separate-port repository URL may change; authentication,
runtime and worker ownership remain exact. Native stopped/effective-unit inspection
and bounded unit reads are refreshed before each publication/receipt, followed by
exact source/destination/configuration/FD/intent/receipt and expiry fences. Rollback
requires the exact switch digest, journals before restoring original bytes and
refuses superseded configuration. Restoring an expired source claims no readiness.

```sh
.venv/bin/python -m pytest tests/test_endpoint_migration.py --basetemp=/var/tmp/quirkbench-m2-stopped-endpoint-switch-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_endpoint_migration.py tests/test_setup_service.py tests/test_enrollment_runtime.py --basetemp=/var/tmp/quirkbench-m2-stopped-endpoint-switch-reviewed-01a0f80e --tb=short
```

**26 passed** (7.21 s), then **81 passed** (23.99 s) after review corrections,
including every forward/rollback crash phase, durable-boundary native state/override/
expiry changes, supersession refusal, full pin, explicit repository URL, strict schema
and shared configuration compatibility. Higher-reasoning review approved. Injected
native adapters only; wizard/service startup/reachability and atomic target endpoint
generations/native commissioning remain open. Full M1/M2 acceptance remains open.

URL-only target endpoint provenance (P3f) is implemented in `endpoint_generation.py`
and typed transition v1. The complete permitted delta is recomputed, preserving
all CA/token/client certificate/key/GPG bytes, binding and repository paths/aliases.
Bounded supplied history rejects cyclic, duplicate, missing or unlinked generations
and changed original enrollment labels. Original runtime must be reconstructed
independently from original enrollment evidence by the future owned activation
adapter. No record/publication/active-state/transport authority is implied.

```sh
.venv/bin/python -m pytest tests/test_endpoint_generation.py --basetemp=/var/tmp/quirkbench-m2-endpoint-generation-provenance-checked-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_endpoint_generation.py --basetemp=/var/tmp/quirkbench-m2-endpoint-generation-provenance-final-01a0f80e --tb=short
```

**31 passed** (0.24 s), then **34 passed** (0.25 s) after source review found URL
normalization of hidden control characters. Checks cover coherent trust/credential/
scope edits, exact origin/repository paths, chain limits and private fixed namespace.
Native preflight, owned atomic activation and completed retarget/archive reader
association remain open; full M1/M2/native acceptance remains open.

Native original-enrollment endpoint preflight is implemented/reviewed in
`endpoint_probe.py` and the additive read-only `/v1/endpoint-check` route. Exact
original request/result/key/CA/client/GPG material anchors the permitted URL delta.
Native SAN/purpose/current validity and exact approved full leaf precede protocol
token HTTP/repository client proof. Every native command and bounded reply shares
one deadline; strict typed acceptance cannot register/contact/claim/reconcile work.
All repository config reads verify current certificate credentials and exact leaf.

```sh
.venv/bin/python -m pytest tests/test_endpoint_probe.py tests/test_endpoint_generation.py tests/test_transport.py --basetemp=/var/tmp/quirkbench-m2-endpoint-native-preflight-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_endpoint_probe.py tests/test_transport.py --basetemp=/var/tmp/quirkbench-m2-endpoint-native-preflight-reviewed-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_endpoint_probe.py::test_real_controller_and_repository_acceptance_with_revocation_and_no_work --basetemp=/var/tmp/quirkbench-m2-endpoint-native-real-revocation-01a0f80e --tb=short
```

**52 passed** (11.16 s); after review corrections **22 passed** with one test-fixture
API error (21.29 s). The corrected actual controller/repository loopback TLS admission
and revocation check **passed** (1.81 s), preserving devices/jobs/attempts/contact rows.
Corrections cover request envelope, typed replies and ownership/time between each
native command. Reviewed; no active generation, physical authority or live service.

Owned same-binding endpoint source preparation is implemented/reviewed separately
in `endpoint_local.py`. Public exact target/runtime/media/full pin and existing
configuration/spool FD ownership gate a durable pause before secret capture. Fresh
native recovery/p3 and one-shot clearance fence every retry. Completed retarget's
public lineage is frozen before effects; full secret validation follows clearance.
Unfinished/orphan endpoint records block runtime/watchdog/network and retarget
preparation, with reverse exclusion too. Exact original generation/enrollment,
reconciled blank journal, capture completion and final retained byte/namespace checks
preserve original spool/blob inodes/attribution; no activation/unpause is exposed.

```sh
.venv/bin/python -m pytest tests/test_endpoint_local.py tests/test_network_profiles.py tests/test_runtime.py --basetemp=/var/tmp/quirkbench-m2-endpoint-paused-source-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_endpoint_local.py tests/test_retarget_local.py --basetemp=/var/tmp/quirkbench-m2-endpoint-source-boundaries-reviewed-01a0f80e --tb=short
```

**92 passed** (25.22 s), then **67 passed** (66.49 s) after review corrections.
Checks cover every source crash point, lost-pointer exact retry, no secrets before
clearance, completed-retarget ordering, mutual maintenance exclusion, bool-schema
refusal and retained metadata changes during the last native callback. Reviewed
software/injected native adapters only; owned preflight/activation/history readers,
wizard/native commissioning and full M1/M2 acceptance remain open.

Owned endpoint preflight is implemented/reviewed in `endpoint_preflight.py`.
Strict public intent/source/capture/retarget lineage, actual recovery/p3, UUID/media
and held config/spool FD owners precede fresh one-shot clearance and original secret
reads. Exact enrollment/generation/key/source-map/blank-journal and typed namespace
fences persist through native trust and read-only protocol/repository admission.
The original absolute 120 s budget includes clearance/capture and every native/
network operation. A captured non-native credential/CA/server/client validity fence
follows the last owned native/source check and terminal namespace check before success.

```sh
.venv/bin/python -m pytest tests/test_endpoint_preflight.py tests/test_endpoint_probe.py::test_each_native_operation_has_ownership_and_cumulative_deadline_fences --basetemp=/var/tmp/quirkbench-m2-owned-endpoint-preflight-01a0f80e --tb=short
.venv/bin/python -m pytest tests/test_endpoint_preflight.py tests/test_endpoint_probe.py --basetemp=/var/tmp/quirkbench-m2-owned-endpoint-preflight-final-01a0f80e --tb=short
```

Initial **16 passed** (15.24 s), then **37 passed** (30.56 s), including final
cleanup/expiry and source-canonical checks. Higher-reasoning review approved the final
source-only boundary. No activation/unpause, work/contact/approval, live service,
media/image or release qualification occurred. Atomic target selection/rollback,
enrollment association readers and wizard/native commissioning remain open.

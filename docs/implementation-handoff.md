# Implementation handoff

The [installation-to-patch map](installation-to-patch.md) is the command/screen checklist.
Implement fresh-user controller setup (M1), connected target setup/pairing (M2),
investigation/baseline (M3), external-agent loop (M4), reports and everyday use (M5),
then the explicitly authorized attended release gate (M6). Optional managed
invocation and unattended grants are independent M7 follow-ons. Preserve the manual compatibility path and
all exact-candidate approval, storage, trust and ownership requirements.

P0–P8 remain bounded packet identifiers; numeric order does not define dependencies.
P7a/b and immutable-source P6a foundations are needed by M3. P4's internal coordinator
can still be tested with manually provisioned fixtures; that is not evidence of a
complete M2 fresh-user journey. Enrollment and guided setup are now required before
general release, and guided backup completeness belongs to M5. Retain working
capacity UI and other foundations. P5 is never an attended prerequisite.

Use the [roadmap](product-roadmap.md) and [contracts](implementation-contracts.md).
The packet table specifies bounded work, not claims of implementation.
Names under tests/ below are acceptance suites to create where absent; their listing
does not mean they already exist or pass. Do not run release qualification to finish
a routine packet. Use injected adapters and focused software tests.

| Packet | Dependencies | Permitted scope and acceptance |
| --- | --- | --- |
| P0 — product contract fixtures | Initial portions of C0–C8 | Retain frozen fixtures; freeze only added first-journey CLI argument/schema/help fixtures and additive session/proposal/observation records; tests/test_product_contracts.py. Review source handoff, recipe privilege, readiness and service ownership before implementation. No new wire envelope or scheduler. |
| P1a — inventory | C0/C1 | Shared recovery collector and optional standalone wrapper; tests/test_inventory.py: limits, partial results, provenance/privacy, no device opens, network calls or installed-OS mutation. |
| P1b — profiles | P1a | Profile catalog/planner and explicit x86-64 platform adapter; tests/test_hardware_plan.py: multiple vendors/form factors, missing peripherals, unsupported architecture, protection conflicts. No automatic protection relaxation. |
| P1c — supported baselines | P1b, C8 | Versioned kernel/config/RPM/recipe catalog and deterministic HardwarePlan references; tests/test_baseline_catalog.py: supported kernel plus userspace changes, unknown combinations blocked, pinned replacement RPM paths. No inventory-to-arbitrary-kernel guessing. |
| P2a — durable operation records | C0/C2 | Additive DB migrations, request replay, source/input/result references and versioned local JSON. tests/test_operations.py: changed-request conflicts, pause, complete/partial outputs. No new scheduler database. |
| P2b — worker ownership | P2a | Controller systemd user services owning rootless workers, stable private paths, epoch/lease fencing, process-group lifecycle and restart reconciliation. tests/test_worker.py: CLI exit survival, stale completions, crashed owners, exclusive source writers. No lingering/firewall/host package changes without operator setup. |
| P2c — operations monitor | P2a/b | Bounded event cursors, progress/error rendering and output queries; no polling agent or fabricated percentages. Reuse monitoring tests. |
| P2d — installer and setup facade | P0/P2b, C8 | Controller archive/launcher, package resources, configured state discovery and user setup. tests/test_installation.py: install into clean home without checkout, fake services, terminal/logout/reboot behavior, legacy explicit state, no temporary credential home. No host package/lingering changes. |
| P2e — M5 backup completeness | C2/C3/C8, P6a | Guided checkpoint and consistent-cut report including dirty sources, private backup requirements and offline target uncertainty. tests/test_backup_completeness.py: interrupted capture, unknown target backlog, paused restore. Retain existing CAS/OSTree closure tests; no heavy backup qualification. |
| P3a1 — locked recovery recipe | C1/C4; recovery-base.md | Version the recovery recipe/profile contracts for stock Fedora kernel/module/firmware packages and separate boot-device policy. Keep old readers/artifact identities; stage packages through existing DNF5 assembly without a kernel compile. Focused fixtures: package/module provenance, locked replay, missing input, no host-derived config and no custom-build fallback. Live-image reuse requires a bounded proposal; no second builder here. |
| P3a2 — recovery runtime | P3a1 | systemd unit allowlist, offline console, RAM paths/resolver, NetworkManager/nmtui, explicit recovery-only SELinux disablement. Focused injected runtime fixtures: initramfs-to-userspace boot-device restriction, ambiguous identity blocks, permit passive kernel enumeration/partition-table reads while preventing userspace internal block opens and filesystem probes, no internal block opens/mounts/repair/swap, no blocking global network wait, credential-free factory tree, manual/restored networking and no candidate-policy leakage. |
| P3a3 — assembly and release record | P3a1/2 | Feed existing image adapter; versioned recovery manifest, signed checksums, capacity checks, explicit cache/input invalidation. Focused image/publication tests: interrupted output, no incomplete publication, no enrolled state in factory media. No new builder or physical writer; a later reuse proposal must demonstrate simpler integration under the same contract. |
| P3a4 — responsive recovery workloads | P2b, P3a2 | Run slow preparation in a bounded worker while the single control authority services evidence/status. Focused runtime tests: stalled worker, network loss, upload progress, restart reconciliation and no duplicate arming. No competing attempt owners. |
| P3a5 — M2 capacity integration | P3a2, C8 | Reuse the implemented local RAM-only identity/capacity screen, allowed sizing and journaled geometry; integrate the fresh-user console flow. tests/test_capacity_setup.py: every interrupted step, existing filesystems, low capacity, changed target RAM. No new formatter or automatic enrolled-media repartition. |
| P3b — identity gate | C4 | Extend current pre-kernel UUID guard and runtime check into versioned target/media binding, duplicate detection and mismatch UI. tests/test_binding.py plus boot/image tests: moved armed drive, missing/default UUID, old unbound provisioning, one-shot clear failure. No identity-as-authentication or unguarded fallback. |
| P3c — manual network and setup | P3a2/P3b, initial C4 | Document and integrate existing manual runtime configuration with explicitly provisioned CA/endpoint, scoped credentials and repository verification keys; local nmtui, selected private network state, minimum crash-safe complete private-generation activation, RAM activation and visible offline waits. This manual compatibility foundation is reused by M2 pairing. tests/test_setup.py: partial generations, interrupted activation/restarts, reboot/retry, secret exclusion, mismatch before profile replay, unavailable experiment/library. Never depend on network-online.target indefinitely. |
| P3d — M2 enrollment service/client | P2a/b, P3b/c | C4 pinned TLS bootstrap, one-use code/request/key binding, device/repository credentials and revocation. tests/test_enrollment.py: lost response, replay, wrong fingerprint, bad clock, expiry, rate limits and both-service revocation. Higher-reasoning review before enabling. |
| P3e — M2 enrollment activation/retarget | P3d | Extend initial P3c safe activation with automated enrollment/lifecycle generations and explicit retarget/evidence drain. tests/test_provisioning.py: every durable boundary, moved drive, old evidence attribution, no inherited authorizations. No credential-bearing factory seed. |
| P3f — M2 endpoint maintenance | P3c–e, C4/C8 | Controller certificate SAN/address wizard and atomic target config generations. tests/test_endpoint_migration.py: retained CA, changed trust reconfirmation, connectivity failure, interrupted switch rollback. No TLS bypass or implicit trust replacement. |
| P4a — attended commissioning coordinator | P1, P2a/b, P3a1–4/P3b/manual P3c, needed P7a/b | Existing attempt machinery with assembled runtime and fake privileged adapters. tests/test_commissioning_flow.py: session-owned inventory → supported baseline → upload → recovery, separate readiness and safe-shutdown facts, exact-candidate operator approval and interrupted/rejected approval; no fabricated registration or direct target shell. Manual fixtures suffice for this coordinator packet; the complete M3 journey follows M2 pairing. P5 is not required. |
| P4b — attended target validation | P4a, available target | Operator-requested real device round trip, recorded protection/identity/network/boot evidence. Not an automatic release suite or watchdog qualification claim. |
| P5a — later unattended watchdog authorization | C6, P2, P3 | Separate signed grants, policy epochs, revocation, exact attempt/build/media binding. tests/test_watchdog_authorization.py: replay/mismatch/expiry, no inheritance of qualification. |
| P5b — later unattended runtime activation | P5a/P4 | Verify grants before activation; preserve sole systemd ownership, offline finish and recovery waiting. Focused runtime/watchdog tests; no controller hardware watchdog access. |
| P5c — later physical reset coverage | P5b, available target | Operator-controlled matrix in watchdog-qualification.md. Record actual timeouts/stages and surviving evidence separately. No unsupported coverage claim. |
| P6a — proposals/source freeze | C3/C8/P2 | Streaming source captures, pinned external revisions, explicit dirty-writer handoff and proposal/outbox transaction. tests/test_proposals.py: prompt operation acknowledgement, concurrent edits rejected, incomplete capture never buildable, accepted inputs immutable. No auto commits in user trees. |
| P6b — attended external session journey | P6a/P4, needed P7a/b | Investigation facade over existing campaigns/session observations; source → build/compose/attempt → evidence/context → next proposal; pause, safe shutdown and restart. tests/test_sessions.py: fake end-to-end external flow, duplicate dispatch, lost responses, no managed agent call or external spend claims. |
| P6c — later managed decisions | P6b | One documented concrete command adapter, bounded pipes and durable deduplicated decision queue. tests/test_managed_sessions.py: batch/early-stop boundaries, human response, build failure, replayed usage, auth expiry, no model call on heartbeat/chunk, no overlapping source writer. Preserve current CommandAgent compatibility. |
| P7a — recipe registry | P0, C5/C8 | Versioned manifest/schema, RPM-installed registry and eligibility dispatch. tests/test_recipe_registry.py: identity mismatch, typed bounds, privileges, unknown recipe. No arbitrary shell or agent-approved privilege extension. |
| P7b — human observations | P0/P2a, C8 | Durable request/response API and CLI, then monitor client. tests/test_observations.py: readiness/live/post-test, duplicate/conflicting/late replies, restart, missing input, no extended physical deadline. |
| P7c — candidate suspend | P7a, C6/C8 | Separate recovery masks from narrowly authorized candidate sleep policy. tests/test_suspend_policy.py: forbidden default, allowed mode/recipe, unsupported reset coverage. Higher-reasoning review; physical trials remain explicit. |
| P7 — diagnostics and exports | C3/C5/C8/P6 | Separate bounded recipe/report/export packets. Actual Git base and format-patch series, tested-source comparison, reproducible distribution provenance, matched baseline/patched/revert/regression evidence, counts/uncertainty and incomplete exports; pin required sources/symbols/evidence. No vendor branches or inferred validation. |
| P8 — final release gate | Stable implementation, explicit release request | Run applicable retained infrastructure/hardware fixtures with predeclared endurance duration/rationale and fault matrix. No agent waits unless results block work. |

Every packet records contract sections, files changed, focused acceptance command,
remaining limitations and whether image bytes/qualification were invalidated. Mark
unimplemented features and unperformed hardware checks explicitly. Preserve existing
Experiment/Result envelopes, campaign readers, evidence and source checkpoints.

Before handoff, test failure behavior as well as success. Source/DB ownership,
controller credentials, target binding, privileged storage writes and watchdog policy
require higher-reasoning review at their boundaries. Surface conflicting contracts;
do not add a bypass flag, reinterpret a qualification field or silently weaken policy.

Retain P0 fixtures and add each packet's new contract revision. Follow M1–M7 in
the roadmap and update the command checklist when behavior becomes usable. Never
equate a fixture parser, fake adapter test or accepted operation with a completed
manual step. Each closure records exact focused checks and outstanding qualification.

## Completed software packets — M1a and M1b

M1a (C0/C2/C8): setup progress v1 in `setup_contracts.py`, packaged schema/example,
shared injected setup/readiness in `controller_setup.py`, executable `setup`/`status`
in `cli.py`. Intent precedes effects; retries reconcile selection, preferences and
archive/manifest identity. Fresh DB initialization publishes only complete owned
staging; preexisting migrations, linked managed paths and SQLite sidecars are refused.
Status never initializes state or starts the lifecycle. Human retry IDs survive
interruptions; different requests/changed choices conflict.

M1b bootstrap (C0/C2/C4/C8): `credential_registry.py` and additive migration,
explicit service/parser registry mode, per-request device-token and repository-leaf
fingerprint lookup/revocation in `transport.py`/`repository_http.py`. Empty registry
starts without fake targets or anonymous target routes. Static credentials remain
supported. Administrative helpers require future C4 caller policy; they do not
implement pairing, retargeting or attempt approval. Revocation denies subsequent
requests, not previously admitted requests/physical attempts.

Higher-reasoning reviews approved both bounded boundaries after initialization,
filesystem and runtime-identity corrections. Focused command/result: the 171-test
M1a/M1b command recorded in the checklist passed. Frozen product CLI v1 fixtures
remain intact; stale installed-resource, home-state, fake-controller and worker-stage
fixtures now match implemented behavior. No live installation/image bytes changed;
no hardware/release qualification performed.

## M1c completed software foundation; setup integration next

Signed acquisition/installation and structural compatibility are implemented in
`controller_release.py`, `release_trust.py`, `release_install.py` and the packaged
`install` launcher. Release-set v1 remains readable; v2 binds exact supported
interface versions, stock factory metadata and separate Fedora base/config/archive
builder identities. Supplied assets use existing catalog, stock candidate, factory
manifest and bounded native OCI readers. Byte authentication and structural
compatibility do not establish runtime readiness, retained baseline closure or
hardware qualification. Installation still labels development payloads unqualified.

The independent production trust bundle loader pins exact publisher key bytes and
full fingerprint. HTTPS acquisition has byte/time limits and denies redirects;
versioned synchronous request records preserve exact intent and accepted metadata
across interruption, lost ACK and cache eviction. No new owner/database is added.
Without shipped production trust, `release-install` and packaged `install` return
UNAVAILABLE before downloads/state writes. Explicit unsigned development installation
remains supported; test keys are injected only in fixtures.

The operator confirmed production publisher provisioning and signed publication are
release-preparation inputs, not a design blocker. They remain outstanding acceptance
requirements, including independently verified key/fingerprint, trust rotation and
compatible signed controller/recovery/builder/catalog assets. Do not ship test trust,
auto-trust a download key, weaken signatures or mark the M1 release journey complete.

Focused validation: 57 tests passed with the command in the checklist. Higher-reasoning
reviews approved acquisition and compatibility after captured-byte/deadline/schema
corrections. No live installation, image production or release qualification ran.

Initial native TLS/user-service integration is implemented and reviewed in
`controller_tls.py`, `setup_service.py` and service/TLS v1 records. `setup --start-service`
provisions private local CA/server trust, publishes the existing fixed unit/launcher,
and enables/starts zero-target registry mode. Runtime is inferred from managed installed
resources. Effective loaded-unit identity/no-overrides and owner/idle fences precede
start; lost ACK and active replay preserve trust and avoid restarting the owner.
Completed initial setup replay observes state without competing for ownership.
The service does not grant enrollment or physical execution approval. Native OpenSSL
3 is absent here; tests inject its command adapter using the existing test-only crypto
extra and real stdlib SSL key checks. Native systemd/host commissioning stays pending.

Signed release/setup binding and durable builder preparation are implemented in
`installed_release.py`/`builder_setup.py`, using the existing lifecycle, worker,
database and CAS. Original authenticated archives remain outside disposable cache;
installed readiness compares exact archive-derived files. Builder capture/import
uses fixed stages, reserve checks, native OCI readers, exact identity/marker checks,
whole-unit stop, epoch/generation/deadline publication fences and required input pins.
Partial signed builder overrides conflict; complete manual bindings remain supported.
Higher-reasoning reviews approved these boundaries, including coordinated runtime/
manifest tamper, copy reserve and OCI entrypoint corrections. Focused evidence is
recorded in the checklist. Native Podman/systemd commissioning remains unperformed.

M2/P3d local invitation and request/key proof foundations are implemented in
`enrollment.py` and `enrollment_proof.py`, with additive migrations and strict v1
schemas/examples. Private issuance precedes digest-only commit; exact replay retains
code/expiry. Durable clock high water, issuance and bootstrap attempt limits, one-use
native Ed25519 nonce proof and atomic request/key/media/TargetBinding reservation are
reviewed. Invalid attempts count before crypto; stored binding documents are revalidated.
Pending reservation can be revoked. BOUND grants no credentials or boot authority.
Focused commands/results are in the checklist; anonymous HTTP exchange is not enabled.

Pinned bootstrap, private target request/key retention and local complete credential
generation are implemented/reviewed in `enrollment_client.py`, `enrollment_target.py`,
`enrollment_certificate.py`, `enrollment_credentials.py` and `enrollment_result.py`.
Certificate inspection sends no secrets; exact approved-leaf TLS requires native SAN/
validity checks and bounded header/body deadlines. Target state rejects nested mounts
and checks actual UUID; native key/proof files stay on verified private control.
Issuer receipts pin exact leaf bytes/expiry and verified CA identity. Explicit
`repository_endpoint` plus existing repositories/composition-signing configuration
provide public OSTree trust; absent publication is unavailable. Private replies precede
atomic same-database two-channel registry/COMPLETE publication; fresh-key proof is still
required before exposing them over HTTP. No anonymous routes or guided console enabled.

Initial enrollment activation and explicit HTTPS application are implemented/reviewed
in `enrollment_activation.py`, `enrollment_service.py` and bounded native HTTP helpers.
Activation binds the captured generic bundle to exact authenticated files, uses the
currently supported literal-IP controller TLS identity, and rechecks storage/media/
UUID/expiry plus immutable generation bytes immediately before runtime publication.
Exchange exposes only two explicit registry-mode routes, binds actual peer addresses,
and requires fresh proof before every private reply. Native TLS handshake/request
workers are bounded; full response envelopes fit before credentials commit. Legacy
target routes retain token authentication and default servers have no anonymous routes.
Focused fixture evidence is recorded in the checklist; complete guided console
and native commissioning remain open.

Managed repository/enrollment publication is implemented/reviewed in
`enrollment_runtime.py` and the existing service/HTTP adapters. Explicit repository
configuration enables both listeners under the same lifecycle owner. Captured,
verified TLS bytes feed both contexts; current owner epoch, backend, configuration,
trust hashes and validity fence authoritative grants. Separate versioned capability
heartbeats drive read-only setup status. Shutdown closes accepted sessions; exact
registered leaf revocation applies to keep-alive requests. Native certificate
generation explicitly includes SKI/AKI for maintained strict TLS validation.

Operator `target add NAME` and `target show TARGET` delegate the existing invitation
and read-only inventory/registry foundations. Human retries retain the name-derived
request and original lifetime; new codes require explicit request IDs. Additive CLI
v2 fixtures preserve the old flags-only target client. Public target status v1
contains no secrets, reports current contact/unattended eligibility as unknown and
does not infer execution authority. Focused evidence is in the checklist.

Initial target console pairing is implemented/reviewed in `enrollment_console.py`
and console selection 5. Actual boot/evidence verification and native crypto
prerequisites precede stopping the existing supervisor. Explicit full fingerprint
approval precedes private code entry; unavailable terminal echo control fails closed.
Lost redemption replies reuse the retained key/request and authenticated activation.
Existing active configuration requires explicit maintenance. Native recovery RPM
inputs still need the stock `openssl` executable (the current candidate has only
`openssl-libs`). Expired unredeemed pending intents now use the explicit initial-only
selection packet below; active enrollment lifecycle maintenance remains open.

Explicit private network selection/replay is implemented/reviewed in
`network_profiles.py`, console selection 6 and the existing network-state oneshot.
Only selected Ethernet/Wi-Fi keyfiles enter an immutable private generation.
Current boot/media/binding/runtime and source/staged bytes fence publication;
nested profile mounts are rejected before secret reads. Boot replay validates
binding before reading passwords, then copies only into verified RAM before
NetworkManager. Interrupted replay cleans only its own unchanged RAM files;
uncertain cleanup fails the oneshot and blocks NetworkManager. No separate network
owner or public credential artifact was added. Changed runtime source/unit bytes
require fresh image production/qualification inputs; no image was built here.

Authenticated protocol-contact observations are implemented/reviewed in
`protocol_contact.py` and the existing HTTPS adapter, with an appended migration.
Strict target-status v2 reads the latest successful registry request alongside
current boot/credential facts in one read-only snapshot. Its 30-second window is
advisory; v1 JSON remains unchanged and no readiness or approval is inferred.
Focused checks include successful idle reconciliation/polling, stale/failed requests,
revocation, boot changes and versioned CLI/schema compatibility.

Initial pending-invitation maintenance is implemented/reviewed in
`enrollment_maintenance.py` and the attended connection console. Explicit replacement
or archived resume preserves every original key/request on the same verified private
media. Immutable bounded selection records reconcile interrupted renames; final
bytes/domain/UUID/media fences apply to completed replay too. Existing activation
evidence or target work refuses this initial-only path. A completed controller
redemption prevents another identity; the original archived request remains recoverable
with fresh proof. No remote revoke, active retarget or endpoint migration is implied.
Focused checks/remaining native and release requirements are in the checklist.

Signed released recovery acquisition is implemented/reviewed in `recovery_download.py`
and the existing fixed worker/coordinator/CAS. It authenticates the current installed
v2 statement, metadata and bounded streamed image, then rechecks current trust after
whole-worker stop/copy before atomic retention. Native runtime, monotonic raw reads,
reserve and epoch/generation/deadline fences remain separate. CLI/schema fixtures
and focused 101-test evidence are recorded in the checklist. No production publisher
or qualification authority is supplied; no live installation/image was changed.

Explicit operator credential/invitation revocation is implemented/reviewed in
`target_lifecycle.py` and additive target CLI v4. Revocation and all active campaign
pauses commit with exact command receipts in the existing database; retries never
select a later generation. Bounded SQL-only identity resolution is independent of
missing/corrupt readiness artifacts. Counts and sorted samples keep large histories
revocable; strict receipt readers preserve lost acknowledgments beyond 16 KiB.
Registry attempts retain an additive exact credential-generation reference. Claim,
execution/adoption and approval replays require the same live generation and strict
media/UUID report binding. Legacy static targets remain supported; historical registry
attempts without a provable generation require reconciliation. Unresolved attempts,
issued handoffs, evidence attribution and worker stop obligations remain intact.
No physical stop, one-shot clearance, retarget or drain permission is inferred.
Focused evidence is recorded below and in the checklist.

Shared installer/recovery native HTTPS is implemented/reviewed in `release_http.py`.
DNS uses a fixed isolated child with timeout/kill/reap; cumulative monotonic connection,
TLS, write and raw framing/body deadlines preserve native SAN checks and independent
trust. Locally signed release/loopback TLS fixtures explicitly inject test trust;
55 focused checks and production acceptance limits are recorded in the checklist.
No live installation or image changed.

Released recovery listing is implemented/reviewed in `released_recovery.py` and
the existing read-only `recovery_listing.py`. Retained strict index/statement linkage,
references and asset sizes gate local paths; historical publication verification
does not imply current trust or image rehashing. Old prepared-image fields/cursors
remain compatible. Focused 46+2 checks and limitations are in the checklist.
No live state, image bytes or qualification changed.

Explicit old-evidence grant prerequisites (P3e/C4) are implemented/reviewed in
`evidence_drain.py`/`evidence_drain_client.py`, existing controller upload/evidence
guards and two registry-only routes. Operator CLI v5 grants exact original bytes;
private secrets precede same-DB digest publication. Fixed upload IDs, all-target
reconciliation/worker stop, owner, retention, native TLS and durable expiry fences
preserve attribution and grant no execution/result/physical authority. Focused
73/66/55-test evidence and limitations are in the checklist; no live grant or image changed.

Local original-spool drain (P3e/C4) is implemented/reviewed in
`evidence_drain_target.py`, the additive scoped `TargetAgent._drain` and recovery
console choice 7. Frozen bounded selections, exact enrolled generation and original
SMBIOS/media/source fences precede private access, TLS requests and ACK saves.
Stable no-follow sealed blobs and remaining batch deadlines protect retries; only
selected upload/ACK progress changes, including lost-ACK restoration. Original
pending result and unselected records remain intact. Focused 51 checks passed as
recorded in the checklist. Changed hardware remains blocked; retarget maintenance
and native recovery commissioning remain open. No live grant/service/image changed.

One-shot clearance prerequisite (P3e/C4) is implemented/reviewed in
`boot.clear_once`, reused by `UsbBootControl.recover`. Full native recovery/GPT
checks and exact private p3 mount/device/no-follow state checks surround native
unset/fsync/strict list confirmation. It requires neither available experiments
nor library, creates/remounts nothing, and does not imply controller/physical
reconciliation. Runtime recipe full-module checksum refreshed; its callable behavior
is unchanged. Focused 150 software checks passed; native image commissioning remains
open. No live storage/service changed.

Controller-scoped retarget invitation/authenticated reply prerequisite (P3e/C4)
is implemented/reviewed in `retarget_invitation.py`, existing enrollment proof/
certificate/complete paths and strict enrollment-result v2. Exact revoked original
generation/media/binding, all original reconciliation/worker-stop fences and new
UUID/media ownership gate every exchange/publication/replay. Code purpose and
private scope precede atomic same-DB publication; missing committed scope is never
repaired or treated as initial enrollment. COMPLETE replay allows only its own
exact new generation. CLI v6 and retarget-invitation v1 preserve older interfaces.
Authenticated v2 carries exact scope in retained reply identity; initial activation
rejects v2 before effects. Focused 178+72 checks and schema correction are in the
checklist. This prerequisite does not complete local retarget or inherit old grants.

Paused local retarget preparation/source capture (P3e/C4) is implemented/reviewed
in `retarget_local.py` and existing runtime/network guards. Exact different actual
UUID, native recovery/p3 and old public identity checks precede intent/pause writes.
Fresh one-shot clearance precedes bounded old-secret/source capture on every retry;
original bytes and journal attribution remain in place. Every retained incomplete
request, including lost/corrupt pointer, blocks runtime/watchdog/saved networking.
Only its sole exact stopped retry repairs lost pointer publication. No completion,
pointer removal, exchange or new activation is exposed. Focused 97/106/3 checks
are recorded in the checklist; full local retarget and native acceptance stay open.

New request/key proof within paused retarget (P3e/C4) is implemented/reviewed in
`retarget_enrollment.py`, reusing existing enrollment native crypto. Fresh recovery/
identity/clearance and exact old source fences surround a separate retained new
request/key namespace. Exact new intent/request/key bytes are checked after native
work under both existing locks before returning requests/signatures. Original runtime
and spool stay paused and intact; no activation or exchange adapter is exposed.
Focused 69 checks plus 26 checks and one corrected native-key fixture passed.

Pinned enrollment transport correction (P3d/e) is implemented/reviewed: shared
bounded native DNS/connect/TLS/write/raw-read adapter, exact approved leaf before
HTTP and strict framing. Public inspection remains unauthenticated and secret-free.
Focused 55 checks passed; no live setup or native commissioning ran.

Authenticated retarget exchange/private bundle retention (P3e/C4) now reuses
existing native validators and bounded pinned transport. Original CA validates the
approved leaf before secrets; exact authenticated v2 source/code/trust scope gates
retention. Every retry uses fresh remote proof, final exact bytes/expiry/deadline
fences hold both locks, and original runtime/spool remain paused and intact.
Focused 58/4/3 checks and review corrections are recorded in the checklist.

Atomic stopped local retarget activation/original spool archival (P3e/C4) is
implemented/reviewed in `retarget_activation.py` and existing generation/pause/source
readers. Immutable exact old/new intent, fresh unfinished remote proof, same-filesystem
archival, blank new spool, held lock-inode checks and runtime-last/strict-completion
publication preserve attribution and forbid inherited grants/qualification/network
binding. Completed replay preserves later work and one-shot state. Focused 28/39
checks are recorded in the checklist; no attended UI or native commissioning ran.

Explicit archived old-evidence adapter (P3e/C4) now reuses original scoped grants,
source/blob verification and spool writer after strict completed retarget/current
new binding. Both spool owners, exact unchanged new journal and frozen original
source/attribution fence reads and ACK writes; no one-shot/new-work/active state
changes. Legacy hardware-only drain remains strict. Original absolute preflight/
request deadline is preserved. Focused 25/13/27 checks are recorded in the checklist.

Attended retarget and explicit archived drain screens (P3e/C4) are implemented and
reviewed. Exact OLD/actual NEW confirmation precedes pause, full fingerprint approval
precedes new key/secret, no-echo failure retains the original new key, and interrupted
exchange/activation resumes the same request. Completed ACK observes later work without
HTTP, secret prompts or one-shot clearance. Native prerequisites precede stopping
the existing supervisor; restart uses that same owner. Focused 8+14 checks passed.
Repeated moved-media preparation/activation and access to every completed linked
archive are now implemented and reviewed, with strict current-hardware gates.
Explicit paused-retarget invitation replacement/archive/original-key resume is now
implemented and reviewed; endpoint maintenance remains unfinished.

Repeated retarget uses local-intent v2 with an immutable exact predecessor selection.
Bounded public history checks semantic device/runtime/media/binding continuity and
rejects cycles, orphans, changed completion and ambiguous retries before secrets.
Historical completion verification occurs only inside stopped explicit preparation
after fresh native clearance. First v1 readers/fixtures remain supported. Focused
37 initial checks, 2 repeated smoke checks and 28 history/archive checks passed.

Retarget invitation maintenance reuses the initial private selection engine through
a fixed owned view and typed envelope binding exact local intent/source. Original
keys, same trust/media/NEW binding and bounded history remain intact. Result/bundle/
activation evidence blocks replacement; remote redemption is never canceled locally.
Every revoked overlapping controller identity must also reconcile its work before
replacement. Exact immutable selected maps fence preparation/signing/exchange/
activation and final native/file boundaries; lost pointers require exact stopped
choice retry. Full fingerprint approval precedes explicit replace/resume confirmation.
Focused 49/2/68/6/7/18/1 checks and review corrections are recorded in the checklist.

Retained-CA endpoint identity staging (P3f/C4/C8) is implemented and reviewed.
Existing stopped controller/command ownership, idle work, exact currently configured
managed identity and immutable intent gate native staging. TLS intent/identity v2
preserve v1 readers and bind bounded public CA lineage. All native temporary copies
and outputs stay in private successor staging; predecessor CA/key remain exact.
Actual successor expiry is bounded by the retained CA, with source/destination/clock
freshness and exact bytes after native work. No service/config/target switch occurs.
Focused 9/29/33 checks are recorded in the checklist. Fresh-source-only; expired
source renewal, wizard/config switch and atomic target endpoint generations stay open.

Explicit expired-source retained-CA renewal (P3f/C4/C8) is implemented and reviewed.
The separate renewal adapter retains an exact historical source leaf under typed
TLS intent v3. Only that captured leaf uses historical-time verification; its
retained CA and the successor use normal native validity checks. Ordinary staging,
inspection, readiness and transport remain strict. Focused 40 checks passed (7.71 s).

Exact stopped controller endpoint configuration switch/rollback is implemented and
reviewed in `endpoint_switch.py`. Existing native unit, command/coordinator ownership,
idle work and exact staged source/successor fence a narrowly permitted configuration
delta. Full fingerprint approval and an explicit same-SAN repository URL precede
publication. Immutable switch/rollback intent and completion retain original bytes,
refuse superseded configuration and repair each interrupted boundary. Every native
boundary refreshes stopped/effective unit checks before final byte/expiry fences.
Focused 26 then 81 checks passed; no startup/reachability/target migration is claimed.

URL-only target generation provenance is implemented in `endpoint_generation.py`,
with typed transition v1 and bounded explicitly supplied history. Every trust,
credential, binding and repository identity byte remains exact; coherent recomputed
hashes cannot admit a wider delta. Original enrollment is an independently verified
caller input, never a record/current-pointer fallback. Focused 31 then 34 checks
passed after original-URL control-character review correction. No active generation
or transport authority is published by this foundation.

Native target endpoint trust/read-only reachability preflight is implemented and
reviewed. Original request/result/key/CA/client/GPG evidence remains exact; native
validity and approved full leaf precede token HTTP and repository client proof.
Read-only endpoint-check creates no registration/contact/work; bounded repository
config checks honor live credential revocation. Every native command shares the
absolute deadline. Focused 52, corrected 22 and actual revocation 1 checks passed.

Owned paused endpoint source preparation is implemented/reviewed in
`endpoint_local.py`. Exact same actual binding/runtime/media and existing owners
gate public pause; fresh native one-shot clearance precedes original secret capture.
Completed retarget validation follows clearance; public lineage stays frozen.
Both maintenance directions exclude each other. Exact source/approved PEM/capture
completion and original bytes/attribution fence every retry and final receipt.
Focused 92 then 67 checks passed (25.22 s / 66.49 s). No unpause or activation.

Owned native endpoint preflight is implemented/reviewed in `endpoint_preflight.py`.
Actual recovery/binding/media and existing config/spool owners surround strict
prepared public source/capture records. Fresh clearance precedes every secret read.
Exact original enrollment/generation/key/blank-journal namespace remains fenced
through native trust and read-only network checks, using one original 120 s deadline.
Captured non-native trust/credential freshness follows the final owned/native/source
checks; no activation/unpause is exposed. Focused 16 checks passed, with final
cleanup freshness checks recorded in the checklist.

Next bounded software packet: exact stopped target endpoint rollback, followed by
completed-retarget and repeated endpoint association readers (P3f/C4/C8). Keep code creation, authentication,
enrollment and exact-attempt approval separate. Production publisher provisioning/
publication does not block ready software work with explicit fixtures. M1 release
acceptance remains open.

Development paused at the user's request on 2026-10-01 after authorized local review
corrections. First original-enrollment atomic endpoint selection is implemented and
boundary-reviewed in `endpoint_activation.py`, using the existing private publication
phase and completed-pointer readers. Completed reads reconstruct original enrollment
and bind the transition to immutable intent; completed ACK retains both existing
owners, recaptures recovery/storage and supports stable private journals up to 4 MiB.
Exact retained initial activation-bundle/namespace checks precede effects and the
final destination/runtime/receipt byte fence after the last native guard, before
trust freshness/deadline checks. Orphan requests remain unavailable. Focused 91 checks
passed, then final 47 activation checks passed after guard alignment; see checklist.

No next packet was started. Exact rollback, completed-retarget association, repeated
endpoint history and guided wizard/native acceptance remain open. No live installation
or release qualification was run; full M1/M2 acceptance is still open.

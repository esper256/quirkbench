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

Next bounded software packet: repeated endpoint association/history and moved-media
source/archive integration, followed by guided maintenance (P3f/C4/C8). Keep code creation, authentication,
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

Development resumed from reviewed checkpoint `0699f7b`. Exact stopped original-
enrollment endpoint rollback is implemented and boundary-reviewed in
`endpoint_rollback.py`. It confirms the captured source hash, retains a pause intent
before fresh one-shot clearance/source secrets, requires reconciled work and existing
owners, and restores only reconstructed original bytes. Interrupted activation and
damaged successor generations can be canceled without HTTP or readiness claims.
Selection v3 preserves earlier readers; completed ACK preserves later journal work.
Focused rollback 29, compatibility 81, reviewed rollback 31 and final joined
rollback/activation 78 checks passed. Runtime and actual network replay guards
now reject incomplete rollback from public records before source credential reads.
The unused root `endpoint_generation.py` duplicate was confirmed and removed;
the packaged implementation remains. Next: completed-retarget and
repeated endpoint association, then guided maintenance.

First endpoint change after completed retarget is implemented and boundary-reviewed.
Source v2 pins the exact completed retarget selection; its immutable new enrollment,
generation, private bundle and old archive anchor the URL delta. Initial v1 remains
unchanged. Exact source-version pairing prevents downgrade; preparation/preflight
recapture private origin after the final native guard. Normal readers require terminal
endpoint selection; only existing stopped ownership after fresh clearance can inspect
an exact unfinished forward publication for retry. Activation/rollback preserve all
original records and evidence. Focused 15 then 24 checks passed; 165 directly affected
compatibility checks passed. Repeated endpoint history and endpoint-before-retarget
source/archive/drain integration remain open before guided maintenance.

Repeated same-binding endpoint selection is implemented and boundary-reviewed.
Intent v2/source v3 retain an exact predecessor selection; a bounded closed public
history and independent private reconstruction preserve the original enrollment,
credentials and repository identity. Returning to earlier URLs is a distinct linked
request. Exact rollback restores the immediately preceding selected runtime;
completed ACK keeps later work and repeats no clearance or HTTP. Interrupted links,
changed private ancestors, source-version downgrades and unknown records stay paused.
Focused validation: 15 initial, 74 joined local/history/retarget and 4 late-native/
namespace checks passed. The 95-check activation/preflight/rollback compatibility
run had 93 passes and two diagnostic-text mismatches; both diagnostics were restored
and their focused rerun passed. See checklist for exact commands. Native artifacts
remain unqualified. Next: endpoint-before-retarget source/archive/drain association,
then guided endpoint maintenance. Production trust/publication and native M1/M2
acceptance remain outstanding; no live services, images or release suite were run.

Endpoint-before-retarget association and archival are implemented and reviewed.
Retarget intent v3/source v2 pin the exact terminal endpoint selection. Actual NEW
binding and fresh one-shot clearance precede scoped private reconstruction; unchanged
old enrollment, generations, spool and endpoint history move into the existing archive.
Transient approved URL projection supports new enrollment and old-evidence drain
without rewriting the retained result or authenticating as old hardware. Repeated
retarget/endpoint origins remain explicit and linked; later new endpoint state can
coexist with the immutable original archive. Review tightened final private source,
stable single-link journal/snapshot, attribution and held config/agent lock fences.
Validation: 18 initial joined, 95 compatibility, 7 targeted boundary and 2 deep-origin
checks passed; final joined run: 31 passed (286.28 s). Actual scoped upload/replay and
legacy joined checks are recorded in the checklist. Next: guided endpoint commands
and recovery screens, including interrupted public-link retry. Native M1/M2 and
production trust/publication acceptance remain open; no live installation or release
qualification ran.
Final scoped upload/replay plus legacy enrollment/activation/drain validation:
4 passed (307.52 s). The reviewed association packet is complete in software.

## Guided endpoint maintenance — software complete

Controller `endpoint show/stage/renew/apply/rollback/wizard` and verified recovery
menu choice 9 now call the existing maintenance services. The wizard confirms the
original identity, full successor fingerprint and repository address; retained
switch replay survives configuration publication and completion ACK loss. Public
certificate output supports offline target preparation. Target apply/check/rollback
confirm exact source/selection; supervisor stop/restart retains the existing owner.
Completed-retarget screen ACKs recheck both held owner inodes after native guards.
Frozen additive syntax: `examples/controller-endpoint-cli.v1.json`.

Higher-reasoning source review approved the final ownership/replay corrections.
Focused command: `.venv/bin/python -m pytest tests/test_endpoint_console.py
 tests/test_endpoint_facade.py tests/test_console.py tests/test_retarget_console.py
 --basetemp=/var/tmp/quirkbench-endpoint-guided-checked-01a0f80e --tb=short`:
55 passed; one malformed later-work test fixture failed. The corrected fixture and
added syntax fixture passed (2 tests), with basetemp
`/var/tmp/quirkbench-endpoint-guided-last-corrections-01a0f80e` and log
`/tmp/quirkbench-endpoint-guided-last-corrections.log`. Joined log:
`/tmp/quirkbench-endpoint-guided-checked.log`. Read-only CLI validation compares
logical SQLite contents and forbids initialization/housekeeping; delayed connection
checkpointing is independent of command mutation.

Next: M3 immutable source/workspace foundations (P6a/C3/C8), then the joined attended
baseline coordinator and investigation facade. M2 native commissioning and full
acceptance, production publisher provisioning/publication and final qualification
remain outstanding. No live service or release qualification was performed.

## M3/P6a streaming source capture foundation — complete

`source_capture.py` streams tracked actual Git-base/index contents, approved
untracked files, modes, deletions and internal relative links to a reproducible
archive and JSONL manifest. Distribution source/patch digests require retained CAS
bytes. Receipt v1 keeps actual Git OIDs distinct from artifact SHA-256 identities.
Explicit writer handoff and repeated existing-claim verification are required.
Changing source, base/index, private staging or serialized bytes rejects completion.
Git reads have bounded pipes/deadlines, terminate/reap on lost ownership and disable
hooks, fsmonitor, lazy fetch, all transports and replacement-object interpretation.
Submodules require separately pinned references and remain blocked in this adapter.

Final focused validation: `.venv/bin/python -m pytest tests/test_source_capture.py
 --basetemp=/var/tmp/quirkbench-source-capture-final-01a0f80e --tb=short` — 40 passed,
4.19s; log `/tmp/quirkbench-source-capture-final.log`. Higher-reasoning source review
approved the final byte/ownership/no-network corrections. This is a worker adapter,
not an investigation CLI, source writer grant or execution approval. Next packet:
durable workspace/capture admission and existing worker/owner publication; then
editable workspace preparation and joined attended baseline integration. Native and
production release acceptance gaps remain unchanged.

## M3/P6a durable capture integration — complete

Private source-workspace v1 records and writer handoff now share the existing
controller database. Handoff and operation admission commit together; historical
request replay preserves a later editing period or another capture. Campaign pause,
credentials, native worker ownership, restart fencing and explicit resume retain
their existing meanings. The fixed source worker freezes the registered scope;
after whole-worker stop the existing coordinator independently checks exact Git
base/index/approved paths, bounded tar metadata and archive/manifest identities.
Successful publication atomically retains CAS references with the existing claim
fences. Editing requires explicit release after terminal/reconciled ownership.

Final joined check: `.venv/bin/python -m pytest tests/test_source_operation.py
 tests/test_worker.py tests/test_worker_claim.py tests/test_builder_setup.py
 --basetemp=/var/tmp/quirkbench-source-operation-joined-final-01a0f80e --tb=short`:
52 passed, 11.42s; log `/tmp/quirkbench-source-operation-joined-final.log`.
Existing operation/migration compatibility plus new replay/scope/parser checks:
37 passed, 9.33s (`/tmp/quirkbench-source-operation-reviewed.log`, basetemp
`/var/tmp/quirkbench-source-operation-reviewed-01a0f80e`). Higher-reasoning review
approved the final corrections. No live service or image work occurred.

Next: prepare an editable private workspace from explicitly handed-off existing
source/retained distribution inputs, then expose the M3 investigation/baseline
journey. This packet registers an already prepared private Git workspace through
application services; it does not yet supply `investigation start/capture-source`,
proposal acceptance, build dispatch or physical approval. Native commissioning,
production publisher provisioning/publication and release acceptance remain open.

## M3/P6a private editable source preparation — complete

`source_preparation.py` reconstructs the approved dirty capture in a fresh private
Git workspace with its actual SHA-1/SHA-256 base, modes/deletions, internal links,
explicitly staged ignored additions and retained distribution provenance. Original
files/index/config remain unchanged. Local fetch accepts one exact pinned form;
no remote transport, inherited template/config or user-tree commit is enabled.
Extraction uses repeated ownership/reserve checks, no-follow parent/file descriptors
and descriptor-based modes; late directory/link/hardlink substitution cannot redirect
effects. Final replica and Git-metadata fences follow all callbacks. Interrupted
staging remains private and requires a fresh reconciled worker stage.

Focused check: `.venv/bin/python -m pytest tests/test_source_preparation.py
 tests/test_source_capture.py --basetemp=/var/tmp/quirkbench-source-preparation-owner-final-01a0f80e
 --tb=short` — 81 passed; log `/tmp/quirkbench-source-preparation-owner-final.log`.
Legacy build/cache compatibility: 25 passed, 4.54s; command uses
`tests/test_build_pipeline.py tests/test_build_cache.py`, basetemp
`/home/eric/.cache/quirkbench-tests/source-preparation-build-final-01a0f80e`, log
`/tmp/quirkbench-source-preparation-build-final.log`. Fixtures use fake build runners;
no container/image/live service work occurred. Schema/example/strict reader agree;
`git diff --check` passed. Higher-reasoning source review approved final corrections.

Next: durable preparation on the existing worker, whole-worker-stop independent
validation and atomic live workspace publication/recovery, then the usable
investigation/baseline facade. This adapter grants no editable workspace, build,
experiment or physical approval. Native commissioning and production release
acceptance remain open; changed implementation artifacts are unqualified.

## M3/P6a durable existing-source preparation — complete

Source preparation uses the existing operation intake, worker, whole-unit stop and
coordinator publication. Versioned input pins actual Git base, original root inode,
approved paths and provenance. Independent stopped-owner validation checks exact
working/Git namespaces, object closure and bytes. A retained operation selection
precedes the move; the final byte fence follows all CAS callbacks, with fresh claim
checks before the atomic live workspace/editing grant. Original user files remain
unchanged. Historical acknowledgements preserve later editing periods.

Failed/interrupted pending selections remain protected from retention and stage
housekeeping. Explicit source-preparation retry recovers the same selection through
a fresh worker. Other operation failure/retry meanings remain unchanged. Private
staging and a filesystem move alone never grant editing or experiment approval.
Higher-reasoning source review approved the final ownership, freshness, bounded
namespace and recovery corrections.

Final joined check: `.venv/bin/python -m pytest tests/test_source_prepare_operation.py
 tests/test_source_operation.py tests/test_worker.py tests/test_worker_claim.py
 tests/test_operations.py tests/test_builder_setup.py
 --basetemp=/var/tmp/quirkbench-source-prepare-reviewed-joined-01a0f80e --tb=short`
— 100 passed, 47.24s; log `/tmp/quirkbench-source-prepare-reviewed-joined.log`.
Fixtures only; no live service, container/image or release work occurred.

Next: usable investigation/source commands over these services, supported immutable
distribution-source preparation and the joined attended baseline round trip.
Native commissioning, production publisher provisioning/publication and complete
M1–M3 acceptance remain outstanding; changed artifacts are unqualified.

## M3/P6a installed investigation source commands — complete

`investigation prepare-source/source/capture-source/release-source` expose the
existing application services for an existing campaign. Explicit original/copy writer
quiescence, exact actual base, approved untracked paths and stable retry identities
remain required. Workspace inference works only when unique; explicit selection is
campaign-bound. Read-only source/status never initialize or prune. Pause/resume use
the original campaign lifecycle and credential/recovery gates. No command starts a
worker or grants an attempt. `investigation start` remains explicitly unsupported.

Focused check: `.venv/bin/python -m pytest tests/test_investigation_sources.py
 --basetemp=/var/tmp/quirkbench-investigation-source-command-01a0f80e --tb=short`
— 11 passed, 2.96s; log `/tmp/quirkbench-investigation-source-command.log`.
Joined fixtures cover dirty preparation → stopped grant → agent edit → capture →
stopped publication → release, historical replay preserving later edits, paused
admission and read-only CLI behavior. Higher-reasoning source review approved.
No live service/image work or release qualification occurred.

Next: supported immutable distribution-source preparation, then investigation
creation and joined attended baseline integration. Native commissioning and
production publisher provisioning/publication remain explicit acceptance gaps.

## M3/P6a prepared distribution-source Git adapter — complete

`distribution_source.py` imports the exact existing rootless SRPM source-stage
output into a reproducible, clearly labeled private Git base and delegates separate
editable-copy preparation. It runs no package code. Catalog/SRPM/NEVRA/spec/tree
identities are exact; retained package inputs and provenance v1 explain distribution
preparation, with unproved upstream Git ancestry explicitly unknown. Native Git
remains bounded, offline and template-free. No live editing grant is supplied.

Higher-reasoning review approved corrected held-output serialization, fresh strict
Git config and bounded recursive no-follow metadata checks. Pure launch/final
checks reject linked object buckets/reflogs, filters, alternates and replacements;
in-flight reads tolerate ordinary Git lock/index replacement while retaining type,
ownership and directory/root checks. Final source/package checks follow all callbacks.

Joined preparation/capture/command validation: 107 passed, 18.15s; command uses
`tests/test_distribution_source.py tests/test_source_preparation.py
 tests/test_source_capture.py tests/test_investigation_sources.py`, basetemp
`/var/tmp/quirkbench-distribution-source-joined-01a0f80e`, log
`/tmp/quirkbench-distribution-source-joined.log`. Subsequent reviewed adapter
corrections and adversarial fixtures: `.venv/bin/python -m pytest
 tests/test_distribution_source.py
 --basetemp=/var/tmp/quirkbench-distribution-source-reviewed-final-01a0f80e --tb=short`
— 20 passed, 3.19s; log `/tmp/quirkbench-distribution-source-reviewed-final.log`.
Schema/example/strict reader agree; `git diff --check` passed. Injected package
fixtures and local Git only; no native RPM/container or live service work occurred.

Next: versioned distribution preparation admission/execution and stopped-owner
publication using existing operation machinery; retain the complete provenance
closure with any live workspace. Then investigation creation and approved baseline
integration. Native commissioning and production release acceptance remain open.

## M3/P6a fixed distribution-source worker adapter — complete

`distribution_source_worker.py` feeds exact retained SRPM bytes to the existing
source-stage recipe in the existing delegated rootless launcher. Retained OCI
archive/config identities are independently authenticated; the distinct Fedora base
marker must match the pinned baseline before native package work. An image entrypoint
is refused. The container has no network/pull, database/private/CAS or
signing mounts. Installed code and private package staging are its only mounts.
Production RPM commands require the container and enforced cgroup resource limits;
the outer worker uses existing claim/deadline checks and returns unpublished private
Git preparation. Admission and stopped-owner publication remain the next packet.

Higher-reasoning review approved the final shared input-copy correction: unbuffered
guarded short writes cannot flush bytes to a moved destination during refusal.
Existing OCI readers gain optional manifest binding; old caller meanings remain.
Focused check: `.venv/bin/python -m pytest tests/test_distribution_source_worker.py
 tests/test_recovery_source_stage.py tests/test_recovery_podman.py
 --basetemp=/home/eric/.cache/quirkbench-tests/distribution-source-worker-reviewed-01a0f80e
 --tb=short` — 40 passed; log
`/tmp/quirkbench-distribution-source-worker-reviewed.log`. Native package/container
execution is explicitly injected in all fixtures. No real container, image build,
live service or release qualification ran; native/release acceptance remains open.

## M3/P6a distribution preparation admission and stopped grant — complete

Source preparation input v2 adds pinned distribution/catalog/SRPM/builder/epoch
inputs to the existing source_prepare operation, stage, stop and selection journal.
The stopped owner independently verifies exact prepared/package bytes, parentless
import commit identity and every Git path/mode/blob; complete reconstruction CAS
closure is retained atomically with the editing grant. Original source/Git/package
files, prepared.json and checked ancestors through controller root are synced before
selection. Interrupted/failed selected work retries without rerunning package code.
Final byte fences follow callbacks, and historical acknowledgements preserve later
writer periods. V1 user-source inputs/readers remain distinct.

Higher-reasoning review approved after exact-base, durable-parent and final-callback
corrections. Native Git's two ordinary checkout locks are accepted only in flight;
strict launch/stopped/final checks reject them. Focused final check:
`.venv/bin/python -m pytest tests/test_distribution_prepare_operation.py
 tests/test_distribution_source.py
 --basetemp=/home/eric/.cache/quirkbench-tests/distribution-reviewed-complete-01a0f80e
 --tb=short` — 41 passed, 44.99s; log
`/tmp/quirkbench-distribution-reviewed-complete.log`. Earlier original-preparation
compatibility cases: 30 passed in the joined reviewed run; shared admission/source/CLI
checks before final origin corrections: 72 passed. Source records have their own
bounded readers; larger catalogs and >256 provenance references are covered.

Next: investigation creation/default supported source commands, then approved
baseline build/compose/attempt integration. All package/container/services are
injected fixtures. No live installation, image, native commissioning or release
qualification occurred; production publisher/publication and native acceptance stay open.

## M3 investigation creation and default source interface — complete software packet

`investigations.py` and investigation v1 retain immutable external session, problem,
limits, primary workspace and exact inventory/catalog/plan/baseline CAS records in
the existing database. Creation is atomic and paused, with target-registration
recheck; replay preserves original selection despite catalog changes. Source
reservation, managed-adapter exclusion and one-running-investigation target gates
reuse current services/campaigns. Legacy records remain explicit and compatible.
New owned limits reject conflicting legacy budget changes; matching changes are no-ops.

Installed `start`, `baseline`, human/JSON `brief` and `prepare-distribution` report
missing exact input SHAs and separate current recovery, input availability, pending
build validation and physical authority. Default source preparation reuses the
shared signed installed-release/prepared-builder fallback from ordinary submissions,
including partial-binding mismatch refusal; no manual builder tuple is required.
No command starts an agent, worker or attempt. Existing source commands work for
new investigations and respect their primary workspace.

Higher-reasoning review approved after budget/fresh-builder corrections. Final check:
`.venv/bin/python -m pytest tests/test_investigations.py tests/test_builder_setup.py
 tests/test_registry_submission.py
 --basetemp=/home/eric/.cache/quirkbench-tests/investigation-reviewed-final-01a0f80e
 --tb=short` — 39 passed, 10.76s; log
`/tmp/quirkbench-investigation-reviewed-final.log`. Earlier joined creation/source,
operations migration, legacy agent/controller and original preparation: 88 passed,
45.83s; log `/tmp/quirkbench-investigation-ownership-compatibility.log`. Brief
read-only command check passed after final human-output wording; log
`/tmp/quirkbench-investigation-brief.log`. Joined fixtures cover start → distribution
source → whole-worker stop/editing grant → edit → capture → restart and exact replay.
Signed installation/builder fixtures inject independent test trust and native execution.

Next: supported candidate baseline inputs/rootfs → existing build/compose → explicitly
approved attempt and recovery/evidence integration, then M4 external proposal/context
and M5 report/export/everyday use. Native commissioning and production publisher
provisioning/publication remain acceptance gaps; no live install, image or release
qualification was changed/run.

## M3 candidate baseline package inputs — complete foundation

Candidate rootfs input v1 pins the selected baseline, exact RPM snapshot and target
lock. `baseline_inputs.py` verifies the complete retained closure through bounded
owned nofollow file descriptors, freezes metadata before callbacks and rechecks all
bytes after publication. Candidate assembly reuses the existing Fedora DNF5
installroot package assembler with local RPMs and empty repositories, retaining
separate candidate and recovery records. Legacy/stock recovery signature dispatch
is unchanged. This adapter has no execution or physical approval authority.

Higher-reasoning review approved. Focused injected check:
`.venv/bin/python -m pytest tests/test_baseline_inputs.py tests/test_recovery_rootfs.py
 tests/test_recovery_stock.py
 --basetemp=/home/eric/.cache/quirkbench-tests/candidate-rootfs-inputs-final-01a0f80e
 --tb=short` — 52 passed, 2.94s; log
`/tmp/quirkbench-candidate-rootfs-inputs-final.log`; `git diff --check` passed.
Missing/changed RPMs, linked/special files, byte limits and callback/publication
mutations are rejected. Native assembly and release acceptance remain pending.

Next: correct distribution-worker Fedora base/OCI identity separation, then wire
candidate input assembly into the existing job owner and build/compose flow.

Distribution base identity correction: the fixed worker preserves
`builder_image_digest` as the Fedora base marker, independently of OCI manifest,
config and archive identities. Native inner execution rejects missing, oversized or
wrong markers before RPM work. Higher-reasoning review approved; 42 injected worker,
investigation and source-stage checks passed in 7.88s:
`.venv/bin/python -m pytest tests/test_distribution_source_worker.py
 tests/test_investigations.py tests/test_recovery_source_stage.py
 --basetemp=/home/eric/.cache/quirkbench-tests/distribution-base-correction-reviewed-01a0f80e
 --tb=short`; log `/tmp/quirkbench-distribution-base-correction-reviewed.log`.
Next: fixed candidate-rootfs worker adapter, then durable owner integration.

## M3 fixed candidate-rootfs worker adapter — complete foundation; development paused

`candidate_rootfs_worker.py` stages the exact retained candidate closure and invokes
the existing bounded rootless launcher/worker execution adapter. Read-only private
inputs and installed code, plus private writable output, are its only mounts. Native
assembly requires the container/resource limits and exact Fedora base marker; it
returns an unpublished sysroot with retained input and tree identity. Stopped
consumers independently check input/marker/tree bytes. Held stage/nested-CAS owners
and exact stable launch records fence every external callback before copy or launch.

Higher-reasoning review approved after early stage, nested CAS and launch-record
corrections. Focused final check:
`.venv/bin/python -m pytest tests/test_candidate_rootfs_worker.py
 tests/test_baseline_inputs.py
 --basetemp=/home/eric/.cache/quirkbench-tests/candidate-rootfs-worker-verified-01a0f80e
 --tb=short` — 36 passed, 3.73s; log
`/tmp/quirkbench-candidate-rootfs-worker-verified.log`. Fixtures inject all native
package/container calls and exercise outside-directory substitution, coherent other
input replacement, lost claim, failed assembly and changed result/sysroot refusal.
`git diff --check` and added schema/example consistency checks passed.

Paused at the user's request before the next packet. Next ready work: durable
candidate-job admission and stopped-owner publication using the existing database,
operation service and worker stages, then join supported build/compose and explicit
attempt approval/recovery/evidence. The rootfs adapter is not a usable candidate
command or complete M3 round trip. M4 proposals/context and M5 report/export/everyday
use remain unfinished. All session work remains uncommitted; checkpoint 0699f7b
precedes these changes. Production publisher provisioning/publication and native
commissioning/release acceptance remain open; no live installation, real container,
image, QEMU, hardware campaign or release qualification ran.

# Implementation handoff

**Delivery split, 2026-09-29.** Initial attended essentials: relevant existing P0
fixtures; P1 candidate planning; P2a/b/c and minimal P2d installation; P3a1–4,
P3b and manual P3c configuration; P4; immutable-source P6a/external P6b; and the
needed P7a/b recipe/observation records. Each physical attempt needs explicit
operator approval of exact immutable bytes and attempt identity before arming.
P4 does not depend on automated enrollment, P5 or the later wizards.

Later capabilities: P2e guided backup completeness, full P2d setup wizard, P3a5
advanced capacity UI, P3d/e automated enrollment/lifecycle, P3f endpoint wizard,
P5 unattended authorization/qualification and P6c managed scheduling. Keep existing
implemented checks and schemas; do not delete working features to simplify delivery.
Use the [storage policy](architecture.md#storage-protection-policy) as authority.

Use the [roadmap](product-roadmap.md) and [contracts](implementation-contracts.md).
This file contains bounded work packets, not a record of past implementation.
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
| P2e — later backup completeness | C2/C3/C8, P6a | Guided checkpoint and consistent-cut report including dirty sources, private backup requirements and offline target uncertainty. tests/test_backup_completeness.py: interrupted capture, unknown target backlog, paused restore. Retain existing CAS/OSTree closure tests; no heavy backup qualification. |
| P3a1 — locked recovery recipe | C1/C4; recovery-base.md | Version the recovery recipe/profile contracts for stock Fedora kernel/module/firmware packages and separate boot-device policy. Keep old readers/artifact identities; stage packages through existing DNF5 assembly without a kernel compile. Focused fixtures: package/module provenance, locked replay, missing input, no host-derived config and no custom-build fallback. Live-image reuse requires a bounded proposal; no second builder here. |
| P3a2 — recovery runtime | P3a1 | systemd unit allowlist, offline console, RAM paths/resolver, NetworkManager/nmtui, explicit recovery-only SELinux disablement. Focused injected runtime fixtures: initramfs-to-userspace boot-device restriction, ambiguous identity blocks, permit passive kernel enumeration/partition-table reads while preventing userspace internal block opens and filesystem probes, no internal block opens/mounts/repair/swap, no blocking global network wait, credential-free factory tree, manual/restored networking and no candidate-policy leakage. |
| P3a3 — assembly and release record | P3a1/2 | Feed existing image adapter; versioned recovery manifest, signed checksums, capacity checks, explicit cache/input invalidation. Focused image/publication tests: interrupted output, no incomplete publication, no enrolled state in factory media. No new builder or physical writer; a later reuse proposal must demonstrate simpler integration under the same contract. |
| P3a4 — responsive recovery workloads | P2b, P3a2 | Run slow preparation in a bounded worker while the single control authority services evidence/status. Focused runtime tests: stalled worker, network loss, upload progress, restart reconciliation and no duplicate arming. No competing attempt owners. |
| P3a5 — later advanced capacity selection | P3a2, C8 | Local RAM-only pre-commission identity/capacity screen, advanced allowed sizing and journaled confirmed geometry. tests/test_capacity_setup.py: every interrupted step, existing filesystems, low capacity, changed target RAM. No new formatter or automatic enrolled-media repartition. |
| P3b — identity gate | C4 | Extend current pre-kernel UUID guard and runtime check into versioned target/media binding, duplicate detection and mismatch UI. tests/test_binding.py plus boot/image tests: moved armed drive, missing/default UUID, old unbound provisioning, one-shot clear failure. No identity-as-authentication or unguarded fallback. |
| P3c — manual network and setup | P3a2/P3b, initial C4 | Document and integrate existing manual runtime configuration with explicitly provisioned CA/endpoint, scoped credentials and repository verification keys; local nmtui, selected private network state, minimum crash-safe complete private-generation activation, RAM activation and visible offline waits. No pairing wizard prerequisite. tests/test_setup.py: partial generations, interrupted activation/restarts, reboot/retry, secret exclusion, mismatch before profile replay, unavailable experiment/library. Never depend on network-online.target indefinitely. |
| P3d — later enrollment service/client | P2a/b, P3b/c | C4 pinned TLS bootstrap, one-use code/request/key binding, device/repository credentials and revocation. tests/test_enrollment.py: lost response, replay, wrong fingerprint, bad clock, expiry, rate limits and both-service revocation. Higher-reasoning review before enabling. |
| P3e — later enrollment activation/retarget | P3d | Extend initial P3c safe activation with automated enrollment/lifecycle generations and explicit retarget/evidence drain. tests/test_provisioning.py: every durable boundary, moved drive, old evidence attribution, no inherited authorizations. No credential-bearing factory seed. |
| P3f — later endpoint migration wizard | P3c–e, C4/C8 | Controller certificate SAN/address wizard and atomic target config generations. tests/test_endpoint_migration.py: retained CA, changed trust reconfirmation, connectivity failure, interrupted switch rollback. No TLS bypass or implicit trust replacement. |
| P4a — attended commissioning coordinator | P1, P2a/b, P3a1–4/P3b/manual P3c, needed P7a/b | Existing attempt machinery with assembled runtime and fake privileged adapters. tests/test_commissioning_flow.py: session-owned inventory → supported baseline → upload → recovery, separate readiness and safe-shutdown facts, exact-candidate operator approval and interrupted/rejected approval; no fabricated registration or direct target shell. Automated enrollment and P5 are not prerequisites. |
| P4b — attended target validation | P4a, available target | Operator-requested real device round trip, recorded protection/identity/network/boot evidence. Not an automatic release suite or watchdog qualification claim. |
| P5a — later unattended watchdog authorization | C6, P2, P3 | Separate signed grants, policy epochs, revocation, exact attempt/build/media binding. tests/test_watchdog_authorization.py: replay/mismatch/expiry, no inheritance of qualification. |
| P5b — later unattended runtime activation | P5a/P4 | Verify grants before activation; preserve sole systemd ownership, offline finish and recovery waiting. Focused runtime/watchdog tests; no controller hardware watchdog access. |
| P5c — later physical reset coverage | P5b, available target | Operator-controlled matrix in watchdog-qualification.md. Record actual timeouts/stages and surviving evidence separately. No unsupported coverage claim. |
| P6a — proposals/source freeze | C3/C8/P2 | Streaming source captures, pinned external revisions, explicit dirty-writer handoff and proposal/outbox transaction. tests/test_proposals.py: prompt operation acknowledgement, concurrent edits rejected, incomplete capture never buildable, accepted inputs immutable. No auto commits in user trees. |
| P6b — attended external session journey | P6a/P4, needed P7a/b | Session facade over campaigns; source → build/compose/attempt → evidence/context → next proposal; pause, safe shutdown and restart. tests/test_sessions.py: fake end-to-end external flow, duplicate dispatch, lost responses, no managed agent call or external spend claims. |
| P6c — later managed decisions | P6b | One documented concrete command adapter, bounded pipes and durable deduplicated decision queue. tests/test_managed_sessions.py: batch/early-stop boundaries, human response, build failure, replayed usage, auth expiry, no model call on heartbeat/chunk, no overlapping source writer. Preserve current CommandAgent compatibility. |
| P7a — recipe registry | P0, C5/C8 | Versioned manifest/schema, RPM-installed registry and eligibility dispatch. tests/test_recipe_registry.py: identity mismatch, typed bounds, privileges, unknown recipe. No arbitrary shell or agent-approved privilege extension. |
| P7b — human observations | P0/P2a, C8 | Durable request/response API and CLI, then monitor client. tests/test_observations.py: readiness/live/post-test, duplicate/conflicting/late replies, restart, missing input, no extended physical deadline. |
| P7c — candidate suspend | P7a, C6/C8 | Separate recovery masks from narrowly authorized candidate sleep policy. tests/test_suspend_policy.py: forbidden default, allowed mode/recipe, unsupported reset coverage. Higher-reasoning review; physical trials remain explicit. |
| P7 — diagnostics and exports | C5/P6 | One bounded recipe per packet; observed device selection, stimuli hashes, physical checks, matched baseline/patched/revert/regression evidence. No vendor branches in core. |
| P8 — final release gate | Stable implementation, explicit release request | Run applicable retained infrastructure/hardware fixtures with predeclared endurance duration/rationale and fault matrix. No agent waits unless results block work. |

Every packet records contract sections, files changed, focused acceptance command,
remaining limitations and whether image bytes/qualification were invalidated. Mark
unimplemented features and unperformed hardware checks explicitly. Preserve existing
Experiment/Result envelopes, campaign readers, evidence and source checkpoints.

Before handoff, test failure behavior as well as success. Source/DB ownership,
controller credentials, target binding, privileged storage writes and watchdog policy
require higher-reasoning review at their boundaries. Surface conflicting contracts;
do not add a bypass flag, reinterpret a qualification field or silently weaken policy.

Retain P0 fixtures and add only needed first-journey contracts; then implement
durable visible execution, supported baselines, stock-kernel recovery, manual
authenticated setup/binding and the attended external-agent journey. Deliver later
wizards, enrollment automation and managed scheduling as separate follow-on packets. P7a/b are
foundations required early; later P7 packets expand scientific diagnostics. P5 gates
unattended use. P8 includes archive/image/API compatibility and signed distribution
acceptance; release fixtures never run merely because these packets changed docs.

## Deferred USB gadget extension

**Post-v1 only; unimplemented and unqualified.** These packets implement the
[external-hardware design](external-hardware.md), outside the P0–P8 dependency chain.
They do not authorize extension work, hardware acquisition or tests during v1.
Suite names are future acceptance targets, not existing passing tests. Preserve
OSTree and frozen wire records.

| Packet | Dependencies | Permitted scope and acceptance |
| --- | --- | --- |
| X1 — media ownership contracts | Stable v1; explicit extension work | Define lifecycle, versioned accessory/media associations, maintenance authorization and restart reconciliation. Fake tests/test_media_ownership.py: exclusive ownership, uncertain shutdown, request replay and stale actions. Higher-reasoning storage/trust review; no new deployment authority. |
| X2 — Pi mass-storage adapter | X1 | Linux gadget provisioning, allocated backing capacity and one stable LUN. tests/test_gadget_media.py: geometry, rejected concurrent access/resize/replacement, duplicate identities and crash-safe ownership. No new deployment backend, firmware navigation or automatic image reset. |
| X3 — durability and recovery integration | X2 | Reuse attempts, maintenance fences and evidence retention; add accessory failures/progress and consistent-cut private backup. tests/test_accessory_recovery.py: disconnect/restart, exhaustion, lost acknowledgements, pending evidence and accessory-alive/target-unavailable. Fakes do not qualify physical durability. |
| X4 — optional diagnostics | X1/X3; demonstrated investigation need | One bounded packet per CDC, HID, Ethernet or DbC channel. tests/test_accessory_channels.py: authenticated provenance, unknown attribution, capability limits, target HTTPS trust and no synthetic heartbeat/recovery. DbC needs a separate USB-host path; no physical reset or independent scheduling. Optional, not a media prerequisite. |
| X5 — hardware qualification | Stable X1–X3; X4 only for claimed channels; explicit qualification request | Recovery/candidate/fallback, interrupted commissioning, independent power, I/O/flush/power-loss durability, internal-disk/firmware preservation and actual capture coverage. Record Pi/target/kernel/storage identities and surviving evidence. No automatic heavy tests or agent polling. |

X1 specifies observable failure behavior before X2 implementation. Qualify media
and diagnostic channels independently; passing one does not qualify the other.
Future reset/actuation authority requires a separate reviewed extension.

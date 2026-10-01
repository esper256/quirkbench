# Implementation handoff

**Start here:** select the [next M1a slice](#next-packet--m1a-resumable-controller-setup-contract),
read its named source/contracts and use the [fast development loop](testing-policy.md#fast-development-loop).
The large packet table is a reference, not a reading or implementation checklist for
one turn. Completed historical work does not need another audit before each slice.

**General-purpose correction:** [Boundary audit and implementation packets](general-purpose-boundary.md).
Installation identities describe software bytes; controller environments and supported
platforms are explicit selections. External agents are the primary workflow. The
[completion record](general-purpose-boundary.md#completion-record--2026-09-30) records
the initial software checks and native installation migration. The
[follow-up record](general-purpose-boundary.md#follow-up-audit-and-completion--2026-10-01)
closes the remaining adapter/default/documentation gaps with 161 focused passing
cases and a refreshed native activation.

**Boundary follow-up, P1b/c and C1/C8:** select planning adapters from reviewed
profiles, centralize backend architecture/EFI command data, derive omitted recipe
IDs from the selected lock and remove remaining case-history setup dependencies.
Use profile/baseline, recipe-input and command-planning regressions; no new platform,
schema relaxation, kernel/image production or qualification. Record follow-up evidence
separately from the earlier completion claim.
Completed: reviewed profile/backend dispatch, unchanged frozen v1 restrictions,
lock-derived recipe IDs, bundled current documentation and native readiness verified.

**2026-09-30 implementation:** [Upload retention and durable build/composition handoffs](upload-and-background-jobs-handoff-2026-09-30.md).
`build` and `compose` now acknowledge jobs immediately; explicit `--wait` reads their
final outputs. Real service/build/boot containment remains unqualified.
The [hardware-specific experiment kernel design](targeted-experiment-kernels.md)
is a proposed M3 input. Start the revised delivery sequence with M1a below.

**Delivery revision, 2026-10-01.** The
[installation-to-patch map](installation-to-patch.md) is the command/screen checklist.
Implement fresh-user controller setup (M1), connected target setup/pairing (M2),
investigation/baseline (M3), external-agent loop (M4), reports and everyday use (M5),
then the explicitly authorized attended release gate (M6). Optional managed
invocation and unattended grants are independent M7 follow-ons. This supersedes
the 2026-09-29 manual-first ordering. Preserve the manual compatibility path and
all exact-candidate approval, storage, trust and ownership requirements.

P0–P8 remain bounded packet identifiers; numeric order does not define dependencies.
P7a/b and immutable-source P6a foundations are needed by M3. P4's internal coordinator
can still be tested with manually provisioned fixtures; that is not evidence of a
complete M2 fresh-user journey. Enrollment and guided setup are now required before
general release, and guided backup completeness belongs to M5. Retain working
capacity UI and other foundations. P5 is never an attended prerequisite.

Use the [roadmap](product-roadmap.md) and [contracts](implementation-contracts.md).
The packet table specifies bounded work, not claims of implementation. Dated
completion records explicitly identify what was actually changed.
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

## Next packet — M1a resumable controller setup contract

**Ready to implement next; not implemented by this documentation revision.**
Owner: P0/P2d, with C0/C2/C8. Read `controller_install.py`, `controller_setup.py`,
`controller_service.py`, `transport.py`, the current `cli.py`, and the old
specification-only `product_cli.py`. Reuse stable install/state identity and current
setup response fields. Keep the current native installation running unchanged during
software development.

Implement M1a as three sequential slices, each ending in usable code and its focused
checks. Freeze only the records/arguments consumed by that slice, alongside its
implementation; do not make a schema-only milestone for the whole future product.

| Slice | Change and boundary | Focused validation |
| --- | --- | --- |
| M1a.1 — read-only status | Add the `status` facade over existing setup/readiness services; retain CLI/configured/live identities and existing fields. No new setup journal, worker owner or initialization from queries. | Extend `tests/test_controller_setup.py` and relevant `tests/test_cli.py` cases for unconfigured state, mismatched revisions, missing native tools/container visibility and read-only behavior. |
| M1a.2 — resumable setup service | Add only the required versioned setup-progress record and shared application service: selected runtime/state, intent/request identity, step reconciliation and independent readiness. Inject external effects. Review the new durable boundary before enabling mutations. | Extend setup/installation suites for interrupted acknowledgment/resume, request replay/conflict, incompatible selected state, active-work refusal and crash recovery. Use real temporary state and fake service management. |
| M1a.3 — human setup facade | Connect `setup` prompts and noninteractive inputs to that service; record resources/connection/logout choices. Retain `setup-state`/`setup-check` compatibility and explicit host-change instructions. Never claim readiness while later M1 integration is missing. | Extend focused CLI/setup cases for prompt retry, cancellation, missing dependencies, no-Distrobox operation, shared validation and stable resume identity. |

These are software packets: no recovery image, native service migration, package
download or release qualification. Reuse `test_installation.py` fixtures and current
service adapters instead of rebuilding a new harness. Run archive tests when archive
behavior changes, not for every prompt/status edit.

Follow M1a with a bounded zero-target service/bootstrap packet, then signed
release/installer/builder integration. Settle the zero-target/credential-store interface
before constructing pairing against it; implement the restricted C4 exchange in M2.
Current `transport.make_server()` requires device tokens and loads a static mapping;
do not work around fresh setup with fake targets or anonymous target routes. Review
the trust change before enabling it. Tiny signed release fixtures and injected
repositories unblock distribution code; choosing publishing infrastructure or waiting
for qualified release artifacts must not block ordinary facade work. M1 is complete
only when its full checklist criteria pass.

## Development scheduling and review

Milestones describe the delivered journey. Only concrete code/record dependencies
block the next implementation slice. Fresh setup remains the primary priority, but
an authorized long operation, unavailable target or pending release artifact need not
hold up independent source/export/UI work against settled interfaces. Do not declare
the whole milestone complete merely because its software portion passes.

| Potential delay | Scheduling rule |
| --- | --- |
| Rebuilding images or waiting for a target to test a wizard | Use injected system/network/boot boundaries and real application records; retain physical acceptance for explicit commissioning/release. |
| Treating M1–M7 as one serial chain of qualified releases | Track software completion separately from physical/release evidence; integrate in order without blocking independent implementation. |
| Discovering mismatched interfaces late | Join a small real-service flow as each slice lands; freeze the immediate consumer's record/adapter interface. Do not postpone all integration until M6 or design every future schema first. |
| A large setup task mixes presentation, credentials and ownership | Use M1a.1–3; separate zero-target trust changes and distribution integration. Each slice has its own focused failure cases. |
| Repeated expensive reviews for ordinary UI edits | Review new/changed invariants at the owning boundary; retain the review reference and scope. Unchanged reviewed invariants do not require another high-reasoning review just because another facade calls them. |
| Every task updates several large planning documents | Update the owning checklist row and one compact handoff. Edit normative contracts only when their decisions change; link to evidence instead of duplicating logs/counts across docs. |
| Waiting for downloads, signing publication or a real kernel build | Test semantics with small local immutable/signed fixtures and miniature source trees. Retain missing production inputs as blockers to the actual operation; never substitute inputs or imply qualification. |

For required higher-reasoning review, provide a concrete diff, the affected invariant,
crash/replay cases, focused results and remaining limits. Review before enabling the
boundary; for a materially new design, settle its authority model before broad
implementation. After corrections, review the affected changes rather than restarting
an unrelated architecture audit. Reopen review when ownership, storage, trust, schema
meaning or authorization assumptions change. A review is not routine permission for
every edit, and switching to a cheaper implementation model does not remove it.

Keep the per-slice handoff short:

- Outcome, affected checklist row and implementation files.
- Exact focused command, result/elapsed time and any known environment correction.
- Review reference/scope when required; changed artifact identity when applicable.
- Remaining software blockers separately from commissioning/release evidence.
- Next ready slice and the few files/contracts it needs.

Do not add exhaustive reading, time accounting, performance dashboards or task
orchestration infrastructure to satisfy this process. Use ordinary tools and the
existing durable run records. Follow the testing policy's no-polling rule; do useful
independent work or return with a durable pending status when a necessary run is long.

## Contract-alignment completion record — 2026-10-01

Implemented plan sections 1 and 2 as documentation: the starting-point inventory,
M1–M7/P0–P8 mapping, every manual command/screen's owner/status/acceptance, the revised
C8 facade/versioning rules and this bounded next packet. Updated the roadmap,
C0–C8 introductions, agent guide and repository instructions to the same ordering.
The README remains aspirational. Existing source schemas, parser fixtures, runtime
behavior, local installation/state and historical evidence are unchanged. Validation
passed: local Markdown links/anchors (36 documents, 257 links), manual command
coverage inspection (21 shell command forms plus inline commands/screens), referenced
existing test-file checks, Markdown fence/stray-marker checks and `git diff --check`.
No pytest, image build or hardware qualification was run for this documentation-only
packet. Future implementation and acceptance rows remain open.

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

## Historical investigation records

**2026-09-30 integration follow-up:** [First audio-patch cycle](audio-patch-cycle-2026-09-30.md)
records launcher/builder/heartbeat corrections, the candidate-only audio recipe,
native controller setup and pending recovery/target product evidence.

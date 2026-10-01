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

## Next packet — M1a resumable controller setup contract

**Next implementation task.**
Owner: P0/P2d, with C0/C2/C8. Read `controller_install.py`, `controller_setup.py`,
`controller_service.py`, `transport.py`, the current `cli.py`, and the old
specification-only `product_cli.py`. Reuse stable install/state identity and current
setup response fields. Keep the current native installation running unchanged during
software development.

1. Freeze the additive setup-progress record and new `setup`/`status` argument,
   error and JSON contracts. Specify selected runtime/state, request identity,
   completed steps, prerequisites and independent readiness facts. Keep legacy
   parser fixtures and implemented `setup-state`/`setup-check` behavior.
2. Implement shared setup/readiness services with injected service/filesystem/network
   adapters and a durable resumable journal. Persist intent before side effects;
   reconcile actual configuration after interrupted acknowledgments. Read-only status
   must not initialize state or create a scheduler. Preserve existing fields and
   CLI/configured/live service identity reporting.
3. Connect the human facade to those services. Missing prerequisites identify actual
   native tools; container visibility is separate. Record state/resources, connection
   and logout choices without implicit package, firewall or lingering changes. Do not
   report completion when later M1 release/builder/service integration is missing.
4. Use focused clean-home/fake-service tests for interrupted setup/resume, same/different
   request replay, conflicting state/runtime, no-Distrobox operation, active-work
   refusal and read-only status. Extend existing setup/install suites as appropriate;
   no image, real service migration, package download or release qualification.

Review new durable setup boundaries at higher reasoning before enabling them. Follow
M1a with a bounded zero-target service/enrollment-bootstrap packet, then signed
release/installer/builder integration. Current `transport.make_server()` requires
device tokens and loads a static mapping; do not work around fresh setup with fake
targets or anonymous target routes. Enrollment needs a restricted C4 exchange and
durable credential/revocation lookup. Review that trust boundary before enabling it.
M1 is complete only when the checklist's complete fresh controller criteria pass.

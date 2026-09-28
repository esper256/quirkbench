# Implementation milestones and bounded briefs

**Test scheduling:** the acceptance sections describe release evidence, not a
requirement to run expensive gates for every task or milestone. Follow the
[testing policy](testing-policy.md): focused software checks during development;
real builds, VM and endurance qualification only for an explicitly requested final
major-version release. Avoid agent waiting unless results block continued work.

These milestone numbers follow the approved project plan. M1 is achieved. The revised M2 was achieved for OSTree layout revision 1; [fresh six-partition qualification](v1-qualification.md) now records the replacement image checks and remaining physical gates. Some later-milestone adapters exist as scaffolding; passing their unit tests does not complete their hardware or build gates. Each implementation task receives this brief, its listed contracts, and its acceptance tests, rather than the entire planning conversation.

## Milestone 1 — architecture, contracts, and executable specification

**Owner:** higher-reasoning implementation and review.

Deliver the controller/target state machine, strict v1 experiment/artifact/capability/result/checkpoint/progress contracts, local administration CLI, SQLite migrations, durable blob store, fake builder, scripted agent, and simulated target vertical slice. The same public behavioral tests must run over local and HTTPS transports. Provide QEMU/UEFI fixture code with sentinel internal disks and snapshots of both the immutable firmware template and the guest's variable store. Provide concrete qualification fixtures for hardware endurance and patch claims.

Human visibility is a required contract: phase, state, last report/contact, last measurable advance, elapsed time, deadline, and truthful measured counters. Periodic heartbeats must not count as useful progress. An unknown percentage stays unknown. Explicit waiting, late reporting, suspected stalls, overdue work, and uncertain physical execution remain distinguishable. Monitoring and event history survive restart and require no coding-agent tokens. Retry of an old progress report must not refresh liveness; progress must not renew execution leases.

**Permitted changes:** project scaffolding, schemas, controller/store, adapters/interfaces, CLI, simulated runner, tests, documentation, CI and acceptance fixtures. **Exclusions:** writing USB devices, modifying firmware, installed-OS validation, claiming real boot/crash recovery or issue fixes.

**Acceptance:** `make acceptance-m1`. This runs the complete software suite, a subprocess CLI vertical slice, and monitor output checks. `make acceptance-qemu` is a separately gated fixture, not an implicit skip or a required claim of a boot without a built image. Architecture review findings and limitations live in `docs/m1-review.md`.

**Freeze:** the six original v1 contract field sets, durable acknowledgement rules, lease/attempt semantics and software protection boundary. Public contract, persistence or boot-safety changes require higher-reasoning review plus migration/replay tests. Internal build/image helpers are explicitly prototype APIs, not frozen v1 wire interfaces.

## Milestone 2 — OSTree builds and bootable recovery

**Owner:** higher-reasoning review of adapter contracts, persistence and protection changes; bounded implementation tasks within those interfaces.

**Brief 2A: composition and publication.** Build experimental RPMs in the isolated Fedora environment, compose coherent kernel/modules/initramfs/userspace/default configuration, sign the exact OSTree commit and publish it through authenticated HTTPS. Retain provenance and matching symbols. Preserve existing source/checkpoint state and resource limits. Separate Composer, deployment backend and BootControl; keep backend paths and commands out of the controller. Add a versioned deployment manifest through the existing experiment artifact envelope. Repository retention, backup and restore must include referenced commit content.

**Brief 2B: boot medium and target adapter.** Use layout revision 2: fixed recovery/EFI, dedicated one-shot state, experiments, read-only library and separate evidence, with restartable first-boot commissioning. Replace four-file bundles with isolated OSTree deployments per attempt. Configure candidate boot-entry generation without system bootloader regeneration. Validate recovery and candidate roots correctly, keep execution allowed for candidate storage and restricted for evidence, and consume one-shot state before handoff. Preserve allowlisted USB writes and internal-controller exclusion for the initial profile. No USB writer, host firmware writes or automatic recovery replacement.

**Brief 2C: architecture cleanup.** Replace obsolete examples, bundle-specific tests, dispatch assumptions and capability checks. Unsupported legacy kernel-only experiments fail explicitly. Keep campaign data, checkpoints, build outputs and evidence readable; old prototype images require rebuilding, not conversion. Maintain a fake deployment backend for shared behavioral tests rather than two production implementations.

**Acceptance:** run the full software suite, then clean-container composition and container recreation with preserved state; build a revision changing kernel and userspace with matching modules and reported commit identity; fault-inject interrupted publication/download/deployment; qualify recovery, candidate, subsequent recovery and failed-candidate fallback in QEMU. Require unchanged fixed recovery, internal sentinels, host package inventories and boot configuration. Firmware settings must satisfy the [reviewed semantic comparison](m2-ostree-review.md): every effective variable is unchanged except the exact firmware-owned MTC bookkeeping increment, with raw snapshots retained. Every long phase reports truthful progress, waiting and deadlines. Missing tools or evidence are explicit unmet gates, not skips or inferred passes.

**Historical layout revision 1 exit gate (2026-09-27):** 286 software tests, signed minimal Fedora composition, recreation in an init-managed rootless container, real authenticated transfer/fault and incremental-update fixtures, complete source/symbol/OSTree backup and restore, and ten UEFI boot trials passed. The latter include missing-fragment fallback, missing-initramfs fallback after kernel loading, and actual panic/reset/recovery. Host inventories and VM preservation checks passed. See [the review](ostree-review.md); this does not commission the Acer.

## Milestone 3 — durable physical execution integration

**Owner:** bounded transport/target and controller tasks.

**Brief:** connect deployment preparation to authenticated attempt handoff, full-reboot reconciliation and evidence upload. Retain leases/generations, durable outbox, bounded recipes and acknowledgement-based cleanup. OSTree owns OS content download and deployment; ordinary artifacts and evidence retain their existing transport. Add restart/retry coverage around preparation, arming, reboot and acknowledgement. Upload sealed chunks during execution; record result completion separately from recovery return. Do not chain candidates. Hold explicit library-maintenance scheduling fences while publishing optional packs. Preserve fresh attempt state, retained evidence, paused queues and backups containing all referenced OSTree content. No arbitrary controller-provided shell commands on the target.

**Acceptance:** shared behavioral tests pass over simulated and real adapters. Interrupted and duplicate operations converge without silent physical replay; incomplete deployments cannot arm; completed evidence and queues survive restart; pause occurs between repetitions; backup/restore verifies both ordinary artifacts and referenced commits. Uncertain physical execution requires explicit reconciliation. Progress and heartbeats remain separate from authorization and useful advancement.

## Milestone 4 — hardware recovery and adaptive agent operation

**Owner:** cheaper models for scoped integration; higher-reasoning review of recovery and agent decisions.

**Brief 4A: qualification (required before any unattended hardware campaign).** Use `acceptance/hardware-endurance.template.json`. Commission the Acer, wired adapter, boot identity, watchdogs, panic reboot, fixed kdump, netconsole and suspend/resume. Identify supported versus unsupported recovery; stop cleanly for human reset when necessary. Neither network silence nor a watchdog timeout proves a kernel crash. Separately qualify boot selection, automatic reset and surviving diagnostic evidence at multiple boot stages, including failures before crash capture is armed. Follow `docs/recovery-and-evidence.md`; VM acceptance does not satisfy this physical gate. Review the current kexec prohibition before implementing kdump. Record unsupported early-boot failures explicitly and require human intervention where necessary.

**Brief 4B: agent campaigns.** Own `agent.py`, campaign orchestration and adapter tests. The provider-neutral JSON command adapter, source checkpoints, compact context, hypothesis ledger, cumulative accounting and budgets already have a scaffold. Finish automated decision/build/experiment scheduling, continuous snapshots of scoped unfinished edits, provider session replacement, bounded failure backoff, and audit export. Credentials remain local to Distrobox. A monitoring human must see provider wait versus compiler activity versus target silence, without inspecting agent chat history.

**Acceptance:** `pytest tests/test_agent.py tests/test_monitor.py`, then fill and verify the hardware fixture using `make acceptance-hardware REPORT=/absolute/report.json`. Require an actual campaign longer than 30 hours, controller restart, network loss, agent replacement, repeated pause/resume, retained outcomes/checkpoints and resource-use evidence. Accelerated-clock unit tests cannot satisfy endurance. This gate is explicitly unqualified in M1.

## Milestone 5 — initial issue investigations and patch bundles

**Owner:** higher-reasoning experimental design/causal review; cheaper scoped implementation and execution.

**Brief:** consume the result/evidence contracts and `acceptance/patch-bundle.template.json`. Add guided physical observation recipes for (1) frozen audio across direct ALSA/PipeWire/power/idle/suspend; (2) real media-key input path and releases; (3) webcam microphone bus/ALSA/UCM/routing with known acoustic stimulus. Establish baseline before editing components. Minimize each causal experiment and preserve rejected hypotheses. Synthetic input or loopback does not prove physical behavior.

**Acceptance:** each resolved issue has source/build and exact deployment identities, patches, baseline/patched/revert observations, regression checks, exposure counts and uncertainty, all linked to immutable evidence. Use `make acceptance-patch REPORT=/absolute/report.json`. An issue without adequate reproduction stays inconclusive. Installed Bazzite validation and patch publication remain outside v1.

## Required progress behavior in every implementation brief

Every long operation owns a stable activity ID, monotonic report sequence, phase, meaningful message, expected reporting interval, and bounded deadline. Use `monitor.Activity` for local work and authenticated `/v1/progress` for target observations. Bytes and completed repetitions may have progress bars; unknown compiler or agent totals must not invent one. Report phase changes and observable counters; keep the reporting process's heartbeat separate from actual advancement. A stalled monitor itself is visible through its snapshot timestamp. Resource exhaustion, authentication loss and persistent infrastructure failure leave durable reasons and preserved work for explicit resume.

## Current v1 layout/reset revision

The [debug image contract](debug-image.md) supersedes shared experiment/evidence storage assumptions. [Fresh layout-2 results](v1-qualification.md) record 405 software tests, ten UEFI trials, standard-image supervision and complete deployment backup/restore separately from historical M2 evidence. Watchdog activation, runtime reset, initramfs handoff, shutdown and suspend coverage are independently qualified before unattended campaigns. No unit-test or VM pass completes that physical gate.

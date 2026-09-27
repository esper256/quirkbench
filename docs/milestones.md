# Implementation milestones and bounded briefs

These milestone numbers follow the approved project plan. M1 is the executable foundation. Some later-milestone adapters exist as scaffolding; passing their unit tests does not complete their hardware or build gates. Each implementation task receives this brief, its listed contracts, and its acceptance tests, rather than the entire planning conversation.

## Milestone 1 — architecture, contracts, and executable specification

**Owner:** higher-reasoning implementation and review.

Deliver the controller/target state machine, strict v1 experiment/artifact/capability/result/checkpoint/progress contracts, local administration CLI, SQLite migrations, durable blob store, fake builder, scripted agent, and simulated target vertical slice. The same public behavioral tests must run over local and HTTPS transports. Provide QEMU/UEFI fixture code with sentinel internal disks and snapshots of both the immutable firmware template and the guest's variable store. Provide concrete qualification fixtures for hardware endurance and patch claims.

Human visibility is a required contract: phase, state, last report/contact, last measurable advance, elapsed time, deadline, and truthful measured counters. Periodic heartbeats must not count as useful progress. An unknown percentage stays unknown. Explicit waiting, late reporting, suspected stalls, overdue work, and uncertain physical execution remain distinguishable. Monitoring and event history survive restart and require no coding-agent tokens. Retry of an old progress report must not refresh liveness; progress must not renew execution leases.

**Permitted changes:** project scaffolding, schemas, controller/store, adapters/interfaces, CLI, simulated runner, tests, documentation, CI and acceptance fixtures. **Exclusions:** writing USB devices, modifying firmware, installed-OS validation, claiming real boot/crash recovery or issue fixes.

**Acceptance:** `make acceptance-m1`. This runs the complete software suite, a subprocess CLI vertical slice, and monitor output checks. `make acceptance-qemu` is a separately gated fixture, not an implicit skip or a required claim of a boot without a built image. Architecture review findings and limitations live in `docs/m1-review.md`.

**Freeze:** the six v1 contract field sets, durable acknowledgement rules, lease/attempt semantics and software protection boundary. Public contract, persistence or boot-safety changes require higher-reasoning review plus migration/replay tests. Internal build/image helpers are explicitly prototype APIs, not frozen v1 wire interfaces.

## Milestone 2 — reproducible builds and bootable recovery image

**Owner:** cheaper implementation models within M1 boundaries; higher-reasoning review of boot safety.

**Brief 2A: build pipeline.** Inputs are immutable source/config/package/toolchain identities; outputs implement `Builder` and publish through `ArtifactRepository`. Own `build.py`, `environments/`, and build tests. Finish exact dependency replay, kernel and userspace (`DESTDIR`) recipes, unstripped matching module symbols, incremental caches, controller-wide one-build locking, and enforced half-CPU/half-RAM plus 20 GiB reserve. Distrobox credentials stay outside snapshots/artifacts. Report build phases, compiler output activity and object counters where available; bounded silent stages explicitly report waiting. Never install or load experimental components on the host.

**Brief 2B: boot medium.** Own `image.py`, `qemu.py`, `target-assets/`, boot tests, and commissioning fixtures. Replace the current two-partition proof fixture with the agreed layout: fixed recovery payload, dedicated small GRUB state partition, and journaled ext4 experiment/evidence data. Implement the supervisor service, explicit boot-device identity, no internal filesystem discovery, Secure Boot verification, safe restartable data expansion, full reboot and consumed-before-boot candidate state. Bind kernels to validated configs and source provenance. Qualify fixed kdump kernel separately; current prototype excludes kexec and therefore does not implement kdump. Output an atomically published image/checksum for Etcher; no custom writer or host firmware commands.

**Acceptance:** `pytest tests/test_build.py tests/test_image.py tests/test_qemu.py`; then clean-container build, recreation with preserved controller state, and `make acceptance-qemu` for recovery, candidate, failed-candidate fallback, sentinel disks and persistent guest firmware-variable policy. The latter requires actual image/OVMF tools and serial assertions. Record before/after host package and boot-config inventories. A pristine template hash alone does not prove guest variables were preserved.

## Milestone 3 — real controller/target transport and durable execution

**Owner:** cheaper implementation models.

**Brief:** own transport/target modules and protocol tests. Consume `TargetTransport`, `BootControl`, `RecipeRunner`, and frozen contracts. Extend the existing authenticated HTTPS and durable outbox to commissioned full-reboot kernel handoff, resumable artifact downloads, streaming logs and large dumps, continuous safety checks, terminal acknowledgements, and acknowledged-only spool cleanup. Current evidence uploads resume; artifact downloads are whole-file bounded transfers. Connect heartbeat and progress reporting to each phase without treating either as proof that an experiment progressed. Finish service packaging, startup reconciliation, graceful shutdown, environmental resume validation, and fault-injected restore integration. No arbitrary controller-provided shell commands on the target.

**Acceptance:** `pytest tests/test_behavioral.py tests/test_transport.py tests/test_target.py tests/test_controller.py tests/test_controller_review.py tests/test_store.py tests/test_monitor.py`. Add lost replies, duplicate events, incomplete deployment, process-kill and power-loss fixtures at every new boundary. The same behavioral suite must pass against simulated and real adapters. Preserve paused queues and unacknowledged evidence. An uncertain physical execution requires explicit reconciliation, never automatic replay.

## Milestone 4 — hardware recovery and adaptive agent operation

**Owner:** cheaper models for scoped integration; higher-reasoning review of recovery and agent decisions.

**Brief 4A: qualification.** Use `acceptance/hardware-endurance.template.json`. Commission the Acer, wired adapter, boot identity, watchdogs, panic reboot, fixed kdump, netconsole and suspend/resume. Identify supported versus unsupported recovery; stop cleanly for human reset when necessary. Neither network silence nor a watchdog timeout proves a kernel crash.

**Brief 4B: agent campaigns.** Own `agent.py`, campaign orchestration and adapter tests. The provider-neutral JSON command adapter, source checkpoints, compact context, hypothesis ledger, cumulative accounting and budgets already have a scaffold. Finish automated decision/build/experiment scheduling, continuous snapshots of scoped unfinished edits, provider session replacement, bounded failure backoff, and audit export. Credentials remain local to Distrobox. A monitoring human must see provider wait versus compiler activity versus target silence, without inspecting agent chat history.

**Acceptance:** `pytest tests/test_agent.py tests/test_monitor.py`, then fill and verify the hardware fixture using `make acceptance-hardware REPORT=/absolute/report.json`. Require an actual campaign longer than 30 hours, controller restart, network loss, agent replacement, repeated pause/resume, retained outcomes/checkpoints and resource-use evidence. Accelerated-clock unit tests cannot satisfy endurance. This gate is explicitly unqualified in M1.

## Milestone 5 — initial issue investigations and patch bundles

**Owner:** higher-reasoning experimental design/causal review; cheaper scoped implementation and execution.

**Brief:** consume the result/evidence contracts and `acceptance/patch-bundle.template.json`. Add guided physical observation recipes for (1) frozen audio across direct ALSA/PipeWire/power/idle/suspend; (2) real media-key input path and releases; (3) webcam microphone bus/ALSA/UCM/routing with known acoustic stimulus. Establish baseline before editing components. Minimize each causal experiment and preserve rejected hypotheses. Synthetic input or loopback does not prove physical behavior.

**Acceptance:** each resolved issue has source/build identities, patches, baseline/patched/revert observations, regression checks, exposure counts and uncertainty, all linked to immutable evidence. Use `make acceptance-patch REPORT=/absolute/report.json`. An issue without adequate reproduction stays inconclusive. Installed Bazzite validation and patch publication remain outside v1.

## Required progress behavior in every implementation brief

Every long operation owns a stable activity ID, monotonic report sequence, phase, meaningful message, expected reporting interval, and bounded deadline. Use `monitor.Activity` for local work and authenticated `/v1/progress` for target observations. Bytes and completed repetitions may have progress bars; unknown compiler or agent totals must not invent one. Report phase changes and observable counters; keep the reporting process's heartbeat separate from actual advancement. A stalled monitor itself is visible through its snapshot timestamp. Resource exhaustion, authentication loss and persistent infrastructure failure leave durable reasons and preserved work for explicit resume.

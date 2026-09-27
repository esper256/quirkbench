# Milestone 1 architecture review

Status: executable software foundation, including progress visibility.

Acceptance recorded on 2026-09-27: `make acceptance-m1` passed all 128 tests on Python 3.14.7 with no skips. The persistent CLI demo finished two attempts across pause/restart/resume. The terminal and JSON monitors were exercised. `doctor` found no Podman, Distrobox, dracut, GRUB or QEMU executable in this environment. Physical boot, build reproducibility and Acer qualification are not completed by this review.

Review found and fixed:

- Claim insertion initially failed against the database schema. Actual-controller tests now exercise claims through both local and TLS adapters.
- Completed attempts could gain new evidence references. Finalized evidence is now immutable; identical acknowledgements remain repeatable.
- Restore validation mutated the source backup through SQLite sidecars and artifact-directory creation. It now validates the source read-only/immutable and synchronizes the restored destination before publication.
- Lowering a token budget did not immediately pause. Budget tests cover lowering, cumulative usage, session reset and duplicate decisions.
- A lost claim acknowledgement could lose the request identity. The target journals the request before sending it and reuses it after failure.
- Target acknowledgement bodies were not validated. Invalid evidence/result acknowledgements now retain the outbox.
- Concurrent supervisors could duplicate local execution. A file lock covers the target journal/execution boundary, and a concurrency test checks one actual recipe execution.
- Recipe execution was unbounded. A separate process group now has a timeout and supervisor heartbeats; interruption preserves uncertainty instead of replaying the recipe.
- Build command validation short-circuited on a prior compile command. Every command in a batch is now validated before any command executes.
- Previous boot registrations could invalidate a newer boot. Boot history now rejects returning stale boot identities.
- Periodic liveness could conceal no advancement. Progress records keep last report and last advancement separate, reject stale sequences, retain fixed deadlines, and never renew execution leases.

The shared behavioral suite checks replay, uncertainty, durable evidence and restart under both adapters. Fault tests cover interrupted publication before/after rename, missing acknowledgements, controller reconstruction, target restart, resource pressure, backup/restore and pause between repetitions. JSON schemas and runtime validators are both exercised. CLI acceptance uses a separate interpreter so packaging and multiprocessing paths are exercised beyond in-process calls.

Review boundaries still open for later milestones:

- The image builder is an unqualified two-partition prototype. It is not the agreed final recovery/state/data layout and must not be commissioned as the finished lab image.
- No real kernel, initramfs or USB image has been built or booted here. QEMU/OVMF tools and artifacts are explicit prerequisites, never blanket test skips.
- The QEMU fixture records immutable-template and actual guest-variable-store hashes separately. Guest variables may change through firmware behavior; M2 must define and assert the permitted set from a settled fixture baseline.
- No physical reboot control, storage expansion, Secure Boot check, qualified watchdog/netconsole/kdump, or crash-dump streaming is implemented. Kernel-candidate execution fails closed.
- The build resource model estimates memory; cgroup enforcement, controller-wide serialization and source/package replay require M2 qualification. The current no-kexec prototype is incompatible with kdump and must be reviewed when introducing the fixed crash kernel.
- Automatic agent campaign scheduling and continuous source snapshots are M4 work. The explicit agent-step API and scoped checkpoint function are available and tested.
- Power-loss durability assumes a filesystem/device that honors fsync. Process/fault tests do not prove hardware behavior during power removal.

Wire contracts, persistence semantics and safety policy require higher-reasoning review before changes. Review logs must state what was observed and what remains unqualified; a green software suite is not a hardware success report.

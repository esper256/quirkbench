# OSTree architecture revision review

> Historical qualification: this report records layout revision 1. The six-partition revision and live/reset runtime require their own qualification; see [current image contract](debug-image.md).

This review replaces the prototype deployment assumptions recorded in the historical M1 review. M1 remains achieved. The revised M2 gate is achieved: real composition, deployment and all ten VM boot trials passed on 2026-09-27. Physical Acer qualification belongs to M4.

## Reviewed boundaries

- The v1 experiment envelope remains intact. Its `deployment` artifact identifies a strict v1 manifest, an exact OSTree revision, a configured repository and the protection profile. Moving references never authorize a boot.
- Composer, DeploymentBackend and BootControl are separate interfaces. Only the OSTree adapter is a production deployment backend. A simulated backend supports shared behavioral tests.
- Candidate content cannot update fixed recovery or the removable EFI bootloader. OSTree writes candidate boot entries with bootloader generation disabled; Quirkbench validates and translates them into USB-only one-shot selection.
- Each attempt has its own stateroot, default configuration and variable state. Repeated preparation reconciles the same attempt. Evidence lives outside that mutable state.
- Driver exclusion remains the initial protection mechanism, alongside positive USB identity and write destination checks. The architecture permits a reviewed replacement with equivalent preservation tests. No general-purpose host block-device writer is introduced.
- Recovery selection, automatic reset and diagnostic survival are separate capabilities. OSTree supplies transactional content deployment; it cannot reset a hung machine. Current kexec exclusion still prevents kdump and requires an explicit future policy review.

## Findings corrected during implementation

Strict signature verification uses libostree's signature result. `ostree show --gpg-verify-remote` is a display command that can return success for an unsigned commit and is not used as authorization. Cached preparation rechecks signatures, object integrity, kernel protection and the actual boot files.

OSTree can renumber boot entries. Durable attempt identity binds the manifest and revision; inspection resolves the current entry rather than trusting an old filename. Trust changes require recommissioning, and cleanup requires reconciled evidence acknowledgement plus inactive, unretained deployment state.

Backups include retained OSTree object closures and ordinary build evidence: source archives, configuration, matching symbols, modules and build provenance. Referenced content is verified before restore. Backup and restore copies do not share object inodes with the source repository.

Long preparation commands expose output activity, last-output age, elapsed time and a fixed deadline. A silent process is not reported as a percentage complete. Locks also require visible, bounded waits. Container-specific composition requirements are recorded in `environments/README.md`.

## VM findings and reviewed corrections

Recovery is mounted read-only with `rootflags=noload fsck.mode=skip rd.skipfsck`: an initramfs filesystem check otherwise changed its ext4 superblock even with a read-only root request. Fixed recovery partition hashes must remain exactly unchanged.

Candidates use OSTree's normal writable deployment root with read-only `/usr` and isolated writable `/etc` and `/var`. Applying recovery's whole-filesystem read-only policy prevented Fedora services from starting. Candidate boot validation must bind `/`, `/sysroot`, `/boot`, `/usr` and `/var` to their exact expected roots on the positively identified external data partition; require `nosuid,nodev` on writable data mounts, read-only `/usr`, and the attempt's own writable variable state. Generic fstab remounting remains disabled. This does not permit internal storage writes or candidate changes to fixed recovery. See [OSTree root preparation](https://ostreedev.github.io/ostree/man/ostree-prepare-root.html).

The OVMF acceptance gate compares decoded effective variables after initial firmware settling, while retaining raw before/after flash snapshots and hashes. EDK2 increments its own `MTC` variable on each boot and may rewrite flash log records. The sole permitted semantic difference is one increment modulo 2^32 of the four-byte, attributes-7 `MTC` under GUID `eb704011-1402-11d3-8e77-00a0c969723b`; unchanged is also accepted. All other variable names, values, attributes and authentication metadata must match exactly. Added/deleted variables or larger counter changes fail. This is firmware-owned bookkeeping, not permission for Quirkbench to write firmware settings. [EDK2 counter initialization](https://github.com/tianocore/edk2/blob/master/MdeModulePkg/Universal/MonotonicCounterRuntimeDxe/MonotonicCounter.c) explains the observed increment. Physical Acer firmware remains separately unqualified.

The missing-candidate VM trial showed that GRUB `fallback=0` alone could leave an interactive menu when the candidate fragment was absent. Candidate selection therefore also requires that the generated fragment exists on the same external data partition; otherwise the fixed recovery entry remains selected. One-shot state is still consumed before that check. GRUB `source` itself returns zero after evaluating a file, even when an inner loader command fails ([Fedora GRUB source](https://github.com/rhboot/grub2/blob/master/grub-core/commands/configfile.c)). The fixed entry clears a readiness flag before loading the generated fragment; only successful kernel and initramfs commands may set it. The entry calls `boot` only with that flag. Otherwise, or if `boot` returns, it explicitly loads fixed recovery. The load-failure trial uses a valid kernel and missing initramfs and must prove immediate recovery without keyboard input; kernel hangs after control has transferred remain a reset-qualification concern.

The panic qualification uses kernel-emitted boot identity and the actual sysrq panic trace, plus reboot/one-shot/preservation checks. A real panic lost the immediately preceding journald message despite Python stdout being flushed. Userspace pre-panic markers are therefore optional observations, not prerequisites or proof of a panic; the separate normal candidate trial still verifies userspace and modules. This does not qualify physical crash-log survival.

## Evidence and remaining integration

Executable fixtures are in `acceptance/`; software tests include shared adapter behavior, stale/duplicate protocol operations, interrupted publication, corruption, trust changes, and backup retention. The real deployment fixture injects download loss and a lost deployment acknowledgement over mutually authenticated HTTPS.

The achieved M2 gate has recorded real results, beyond the software fixtures: a complete signed compose; kernel and userspace execution; fresh attempt state; retry convergence; container recreation; a full QEMU recovery/candidate/failure cycle; and preservation of host inventories, fixed recovery, internal sentinels and settled firmware settings. See `docs/milestones.md` for the authoritative exit gate. M3 scheduling and physical reboot handoff remain explicitly unavailable until commissioned; legacy kernel-only requests fail closed.

[Focused qualification observations](ostree-review.md) record the real repository/signature checks and host-inventory comparison. The observed host preservation report and after-inventories are in `.quirkbench/ostree/host-preservation/`; their scope is recorded package identities and boot configuration, not all host files.

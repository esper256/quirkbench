# Bare-metal experiments, recovery, and diagnostic coverage

Quirkbench experiments boot the candidate Linux kernel directly on the physical
laptop through its firmware and USB GRUB. QEMU tests Quirkbench's infrastructure:
image assembly, kernel/initramfs smoke boot, one-shot consumption, recovery
selection, and storage/firmware protection under controlled fixtures. It cannot
establish that an Acer audio, input, power-management, or microphone issue is
reproduced or fixed. A hardware-specific candidate need not support virtual
hardware; any VM smoke requirement must declare the applicable hardware profile.

## Three independent requirements

1. **Selection:** after a failed candidate, the next boot chooses fixed recovery.
2. **Reset:** a stalled candidate actually leaves its current execution and reboots.
3. **Evidence:** useful observations survive the failure and reach the controller.

A rollback mechanism addresses selection. It does not by itself reset a hung
CPU, preserve a panic trace, or repair a broken USB device. Qualification reports
must describe each requirement separately, including the earliest boot stage
covered. No report may infer crash recovery from a successful boot selection test.

## Intended physical execution sequence

The controller durably records the attempt, hypothesis, source/configuration,
artifact hashes and debug symbols before deployment. Recovery uploads any pending
evidence first, then fetches the authorized exact OSTree commit, verifies its signed content and prepares an isolated candidate deployment on USB.
Arming the candidate is one-shot: GRUB consumes and verifies the cleared state
before handing over to the candidate kernel. Recovery remains the default even
if the candidate never reaches userspace. Candidates never replace recovery.

The candidate establishes available diagnostic channels as early as practical.
Progress distinguishes kernel startup, initramfs, writable USB evidence, network
and experiment readiness. It uploads sealed chunks while running and sends its
terminal result promptly. Final uploads are bounded; normal completion returns
through recovery, where missing acknowledgements and pending chunks are retried.

**Deferred capture extension:** after a separate no-kexec policy revision, a fixed,
qualified capture kernel could collect a vmcore and reboot. That kernel would be
separate from recovery and from the experiment. Current images do not load it;
crash-capture readiness must remain unavailable. The first attended session needs
honest log/reset coverage, not a claim that all early-boot failures yield dumps.

Recovery reconciles the prior attempt and uploads retained evidence before any
new experiment. The target retains evidence until durable controller
acknowledgement. Returning to recovery without a dump is an observation, not
proof of a kernel crash. A silent or ambiguous execution remains uncertain and
requires reconciliation; it is not automatically repeated.

## Failure coverage to qualify on the Acer

Capture-kernel rows below describe deferred capability, not current image behavior.
The [forward plan](product-roadmap.md) prioritizes actual watchdog driver support,
an attended baseline round trip and diagnostic logs before that extension.

| Failure | Route back to recovery | Evidence and remaining limit |
| --- | --- | --- |
| GRUB cannot load the candidate | Bootloader fallback, if GRUB itself remains functional | Bootloader console and durable attempt identity; detailed bootloader error capture needs qualification. |
| Kernel panics before capture is loaded | Timed panic reboot, if the panic/reboot path works | Independent early console or qualified persistent RAM may retain output; kdump is unavailable at this stage. |
| Kernel panics after capture is loaded | Fixed capture kernel, then full reboot | Matching vmcore and symbols, with network or USB fallback; capture can itself fail. |
| Kernel hangs without panic | Qualified hardware watchdog, if it remains armed and functional | Last streamed/flushed records; a watchdog reset does not inherently produce a vmcore. |
| Complete hang before any working watchdog | Qualified external reset or human reset | May have only the last externally observed boot stage. |
| USB or network becomes unavailable | Depends on remaining independent channels | Preserve partial evidence and report the missing coverage; never classify silence as a crash. |

Use continuous controller-side logs once networking works, plus USB evidence
once writable storage works. Netconsole depends on a usable driver and netpoll;
an ordinary USB Ethernet adapter must not be assumed to support it. A serial
console also requires a real early-console-capable hardware path; plugging in a
USB serial dongle alone does not establish that capability. RAM-backed pstore
would need explicitly reserved memory and warm-reset preservation qualification;
EFI-backed pstore remains prohibited. No software-only design can promise both
automatic recovery and a useful trace from every arbitrarily early hard hang.

## Current implementation versus planned capability

The historical revised M2 OSTree build and VM qualification was achieved for layout revision 1; [recorded results](ostree-review.md) include explicit initramfs-load failure and kernel panic/reset/recovery. Earlier custom-bundle work produced kernel, initramfs and image artifacts; those historical results do not qualify the replacement OSTree deployment. No such build or VM result constitutes Acer crash-recovery acceptance.

The current kernel protection policy explicitly disables KEXEC and KEXEC_FILE,
so **kdump is not implemented or available in the current image**. Enabling it
requires a reviewed policy revision, tests for the capture kernel's identical
internal-storage/firmware exclusions, verified capture artifacts, and physical
qualification. Ordinary experimental boots will continue to use full firmware
reboots. This revision must not silently broaden the frozen protection contract.

Before unattended hardware campaigns, qualify deliberate failures at several boot
stages and record reset latency, return to recovery, actual surviving diagnostics,
and successful upload. Unsupported cases require a visible human-intervention
state. A monitor must show the last observed stage, its age, deadline, available
recovery/capture mechanisms, and whether recovery was actually observed. A timeout
is evidence of missing progress, not proof that a reboot or crash occurred.

M2 owns the boot-selection fixture; M3 owns durable upload/reconciliation; M4's
first hardware gate owns reset and diagnostic coverage. This gate must precede
adaptive or overnight hardware campaigns, rather than being inferred from them.
New kernel policy or relevant driver changes invalidate affected qualifications.

## OSTree supplies deployment, not crash recovery

V1 uses Silverblue's OSTree/rpm-ostree technology with minimal Fedora userspace. Each exact commit describes coherent kernel, modules, initramfs and userspace, reducing custom deployment and interrupted-update handling. Quirkbench retains an independent recovery image and explicitly arms each candidate for one boot. OSTree rollback alone does not reset a hung CPU or preserve diagnostics.

Traditional OSTree repository transport is the v1 choice; bootc and the full Silverblue desktop are deferred. The installed Bazzite deployment and its bootloader remain outside Quirkbench. Neither OSTree nor a change of deployment adapter relaxes the internal-disk or firmware protections.

## Primary references

- [Fedora Silverblue: retained previous versions and rollback](https://fedoraproject.org/atomic-desktops/silverblue/)
- [Linux kdump: prerequisites, reserved memory, loading and capture](https://docs.kernel.org/admin-guide/kdump/kdump.html)
- [Linux netconsole: transport and driver constraints](https://docs.kernel.org/networking/netconsole.html)
- [Linux ramoops: reserved RAM and persistence requirements](https://docs.kernel.org/admin-guide/ramoops.html)

## Current implementation

[Layout revision 2](debug-image.md) separates experiments, evidence and the optional library into distinct filesystems. The target runtime connects exact-revision handoff, streaming evidence, bounded final upload and recovery acknowledgement. Systemd watchdog activation uses versioned qualification profiles and observed hardware state; see [watchdog qualification](watchdog-qualification.md). No physical watchdog recovery is inferred from software tests or the prior QEMU image.

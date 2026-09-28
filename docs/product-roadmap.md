# Quirkbench product roadmap

This is the authoritative forward plan. Use the [implementation contracts](implementation-contracts.md)
and [bounded handoff tasks](implementation-handoff.md) for implementation. Planned
interfaces below are not claims that setup or session orchestration already exists.
Use [controller, target and builder](terminology.md) consistently.

## Product flow

1. Build or obtain a generic recovery `.img` for a supported platform, verify its
   checksum and flash it with a standard writer such as Etcher. No installed-OS
   collector, target credentials or controller address is required to build it.
2. Boot that external drive on the target using owner-controlled boot selection.
   Recovery verifies protection and commissions only the positively identified
   external storage. Secure Boot must already be disabled; no firmware changes.
3. A local recovery setup screen offers Ethernet or Wi-Fi configuration, controller
   address/port and pairing. NetworkManager owns networking; use its existing TUI
   rather than implementing wireless configuration. Only controller reachability
   is required, not public internet access from the target.
4. The operator verifies the controller trust fingerprint through an independent
   controller display and enters a short-lived enrollment code. Recovery persists
   device-scoped credentials, network settings and a target binding in private
   control storage on the evidence partition. Pairing does not start a campaign.
5. Recovery sends a bounded hardware inventory. The controller selects a reviewed
   protection/hardware profile and builds the first exact signed OSTree baseline.
   Missing support blocks the affected capability with an actionable explanation.
6. Run an attended baseline round trip: preparation, one-shot candidate boot,
   observation, durable upload and recovery return. Then start an investigation
   through the local CLI, with a problem statement and configured coding agent.
7. Later attempts transfer OSTree objects. Every attempt returns through fixed
   recovery; successful candidates cannot chain updates or promote themselves.

A drive moved to another target returns to setup instead of resuming the previous
investigation. Matching identity permits reconnecting but does not bypass campaign
pause, restart reconciliation or experiment authorization. Rebinding is explicit,
invalidates old boot authorization and preserves evidence with its original target.
A mandatory pre-kernel identity gate protects even a drive moved while armed.

## Recovery implementation choice

The [recovery image decision](recovery-base.md) selects a minimal Fedora appliance,
built using locked DNF5 installroot inputs, a protected Fedora-configured kernel,
dracut and the existing GRUB/GPT image adapter. No KIWI/Lorax tool-selection task
remains. P3a implements the documented synthesis pipeline and release manifest.
Recovery has a read-only ext4 root, bounded RAM runtime, NetworkManager/nmtui and
SELinux explicitly disabled only in recovery. It ships no desktop, installer or
automatic updater. Refresh the base approximately annually while respecting Fedora
support lifetime; earlier important fixes produce explicit replacement releases.
Recovery uploads must remain responsive during bounded deployment-worker operations.

## Scope and supported hardware

The initial platform is x86-64/UEFI with external USB storage. Recovery should ship
a reviewed broad input, display/console, Ethernet, Wi-Fi and firmware support set,
subject to the storage policy. This is a supported-platform image, not a promise
that every computer boots. The current build infrastructure does not yet supply
or qualify that full compatibility set. Unknown platforms get a precise unsupported
result; new architectures belong in adapters, not scattered vendor branches.

Keep internal-controller exclusion for the initial protected profile, positive
external identity and destination allowlists. Broad network/peripheral support is
not permission to enable internal storage controllers, automount, resume, firmware
writes or os-prober. If required USB/network support conflicts with protection,
stop for a reviewed profile change. Inspect actual config, modules and initramfs.

Collect hardware details in recovery using the same bounded collector that may
optionally run on an installed Linux OS. The optional report helps compare the
production environment or diagnose recovery incompatibility; it is never required
for first boot and never mounts internal disks. Record the collection environment:
recovery observations do not describe production drivers or desktop configuration.
HardwareInventory is descriptive data, not build commands or authentication.
HardwarePlan selects the baseline/candidate and records recovery compatibility;
it does not normally generate a target-specific recovery image. Recovery updates
are explicit maintenance/rebuilds, separate from experiments.

## State, trust and reuse

Generic factory images contain no deployment authorization, controller trust pin,
network passwords or device secrets. Public signed distribution checksums identify
release bytes; controller trust is acquired interactively during enrollment.
Media filesystem/GPT identities identify the boot disk, not a physical target or
an enrollment. A cloned factory image must receive a fresh enrollment/media-instance
identity at setup; enrolled drives must not be cloned as additional devices.

Persist private immutable configuration generations under `evidence/control`, with
an atomic active pointer published last. Evidence exports include only the spool,
never adjacent control state. Network configuration is a narrowly selected runtime
input copied into RAM for both environments, not inherited experimental `/etc` or
shared `/var`. No candidate may edit the durable recovery connection or enrollment.
Failed experiment/library mounts must not prevent setup or recovery uploads.

Use a hardware binding distinct from target ID, media instance and credentials.
CPUID/model name and portable machine-id cannot identify a target. Initial automatic
boot binding uses a valid SMBIOS system UUID, checked before GRUB candidate selection
and again before supervisor authentication/watchdog activation. It is an accidental
mismatch detector, not attestation: absent/default/known-duplicate values require
explicit attended handling, and spoofing is outside this software protection model.
Do not silently fall back to a weaker identity or use a fingerprint as a secret.
See C4 for enrollment, retargeting, trust and early-boot details.

## Durable operations and agent sessions

Keep one authoritative SQLite database, content-addressed immutable artifacts and
retained OSTree object closures. Add managed background operations for image build,
composition and agent decisions; CLI mutations return durable IDs promptly. Worker
ownership, request idempotency, checkpointing and restart fences are specified in
C2/C3. State lives outside disposable containers. Rootless Fedora builds never
install experimental packages/modules on the controller OS.

Use shell commands with versioned JSON and typed application services. No MCP or
remote administration server is required in v1. A deterministic session runner
invokes a configured coding-agent command only at decision boundaries. It owns
build/compose/dispatch, preserves interrupted source edits, records hypotheses and
rejected approaches, and feeds compact evidence deltas to the next decision.
Unknown usage, expired authentication, exhausted storage and persistent failures
pause durably rather than retrying indefinitely. Session duration is unrestricted;
operator budgets and experiment deadlines control resource use.

Pause prevents new scheduling immediately, finishes the active bounded unit and
returns an active target attempt through recovery. Resume reconciles outstanding
work. Controller restart always requires explicit resume. Neither an agent's
proposal, a completed upload nor a matching target fingerprint authorizes execution.

## Recovery and observability

Systemd is the single userspace watchdog owner. Hardware reset protects a candidate
when qualified, and recovery safely services that same device. Service supervision
and recipe deadlines detect different failures. First setup does not require a
hardware watchdog; unsupported reset remains attended-only. Qualify activation,
boot handoff, runtime, shutdown and suspend separately. Exact kernel qualification
and operator-approved experimental-kernel activation are different records (C6).
A reset does not establish a kernel crash or guarantee a dump. Kdump remains blocked
by the no-kexec policy until separately reviewed.

Expose local setup/network/pairing state even before controller contact. All long
operations show phase, last heartbeat, last measurable progress, waiting reason,
deadline and bounded errors. Use bars only for measured totals, and never fabricate
watchdog countdowns. Display recovery arrival and evidence acknowledgement separately.
Offline recovery waits without reboot loops. Monitoring requires no AI invocation.

## Implementation sequence

| Brief | Deliverable | Gate |
| --- | --- | --- |
| P1 | Shared bounded inventory and deterministic candidate profiles | Multi-platform fixtures, privacy/limits, unsupported/protection conflicts |
| P2 | Local JSON API and durable background operations | Request replay, restart fencing, pause, managed worker survival |
| P3 | Generic recovery compatibility, local setup, network persistence, secure pairing and target binding | Unconfigured boot, interrupted enrollment, moved media, wrong trust, private-state isolation |
| P4 | Attended recovery-to-baseline commissioning workflow | Actual runtime assembly with fake privileged adapters, then operator-run device round trip |
| P5 | Qualified watchdog and scoped candidate activation authorization | Exact identity/grant/revocation tests, then physical reset coverage |
| P6 | Durable investigation/session runner and a concrete coding-agent adapter | Source snapshots, proposal/outbox replay, usage, auth failures, pause/resume |
| P7 | Bounded diagnostic recipes and patch exports | Explicit stimuli, baseline/patched/revert identities, regression evidence and uncertainty |
| P8 | Final major-version release qualification | Stable release bytes, infrastructure preservation, fault coverage and declared endurance objective |

P1/P2 can proceed independently. P3's generic recovery and early binding are required
before advertising portable setup; enrollment depends on P2's durable services.
P4 uses P1–P3; P5 and P6 follow with separate authority review. Higher-reasoning review
is required for storage protection, enrollment/boot binding, worker fencing and
watchdog policy. Bounded implementations use focused tests and the handoff briefs.

## Release and non-goals

Follow [the testing policy](testing-policy.md). Routine development does not rebuild
images, run QEMU or launch endurance tests. Explicitly requested image production
and attended device commissioning are product operations, not automatic release gates.
Final release qualification records a duration objective and rationale chosen before
the run, failure injections, resource bounds and surviving evidence. There is no
universal campaign-hour threshold. A short unit test cannot claim endurance.

No automatic firmware changes, installed-OS writes, custom USB writer, moving-ref
updates, automatic repeat after uncertain execution or unattended operation without
appropriate qualification. Keep hardware outcomes inconclusive when observations
cannot establish causality. Do not publish patches or alter the installed OS implicitly.

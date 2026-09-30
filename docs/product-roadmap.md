# Quirkbench product roadmap

This is the authoritative forward plan. Use the [implementation contracts](implementation-contracts.md)
and [bounded handoff tasks](implementation-handoff.md) for implementation. Planned
interfaces below are not claims that setup or session orchestration already exists.
Use [controller, target and builder](terminology.md) consistently.

## Delivery contract

The [product interface contract](product-interface.md) separates initial attended
requirements from later automation. Existing commands and schemas retain their
meaning; proposed interfaces are not implementation claims.

**First delivery: owner-controlled, attended lab.** Use fixed recovery, manual
network/controller configuration with explicitly provisioned authenticated trust
and device credentials, bounded inventory, a reviewed candidate baseline, immutable
source inputs, observable resource-bounded builds, explicit operator approval of
each exact candidate attempt, evidence upload and return to recovery. Keep one
durable execution owner, restart reconciliation and explicit pause/resume. A person
must be available to handle unsupported hangs; no unattended reset claim is made.

**Later capabilities:** pairing/credential lifecycle automation (P3d/e), endpoint and
advanced capacity wizards (P3f/P3a5), guided whole-session backup reporting (P2e),
managed decision scheduling (P6c), and unattended watchdog grants/qualification (P5).
These do not gate the attended journey. Managed remains the eventual configured UX
default, after the external-agent workflow is usable. Basic backups report actual
contents/omissions and do not imply full resumability.

## Product flow

1. Build or obtain fixed recovery `.img` media for a supported platform. Verify
   provenance/checksum and applicable signatures, then flash with a standard writer.
   Factory media has no credentials, target inventory or experimental deployment.
2. Boot the owner-selected external drive. Resolve its physical identity and expected
   roles under the [storage policy](architecture.md#storage-protection-policy).
   Explicitly confirm the device and journaled commissioning geometry before writes.
   Secure Boot must already be disabled; do not change firmware automatically.
3. Use local NetworkManager/nmtui and manual controller configuration. Explicitly
   install validated controller trust, repository verification keys and device-scoped
   credentials using the existing configuration mechanisms. Keep secrets private;
   never disable TLS or signature checks. Pairing automation comes later.
4. Collect bounded passive recovery inventory and select a reviewed experimental
   profile/baseline. Establish target/media binding and early wrong-target checks
   before credentials activate or a candidate is armed. Unknown support blocks the
   affected capability rather than relaxing protection.
5. An external agent proposes pinned source inputs and a bounded recipe. The
   controller builds/composes an exact signed candidate in observable workers. The
   operator reviews its identity and storage-sensitive changes, then explicitly
   approves that attempt before one-shot boot. Submission/build success is not approval.
6. Observe the attended run, retain/upload attributed evidence, and return through
   fixed recovery. Reconcile uncertainty before another attempt; approved candidates
   cannot chain updates or promote themselves into recovery.

A moved drive returns to setup; explicit rebinding preserves old evidence attribution
and invalidates old authorizations. Manual setup must satisfy C4 binding, privacy and
verification rules without depending on the later automated enrollment exchange.

## Recovery implementation choice

The [recovery image decision](recovery-base.md) selects a minimal Fedora appliance,
built using locked DNF5 installroot inputs, stock Fedora kernel/module packages,
dracut and the existing GRUB/GPT image adapter. No recovery kernel compile is
required. Upstream image reuse may be proposed when simpler under the same contract;
the current assembly remains default and no second builder is introduced here. P3a implements the documented synthesis pipeline and release manifest.
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

Follow the [storage policy](architecture.md#storage-protection-policy): fixed recovery
may enumerate internal controllers but confines block/filesystem operations to its
identified boot device. Experimental profiles retain internal-controller exclusions,
actual config/module/initramfs checks and independent storage-sensitive review.
Required internal-storage drivers block experiments; broad recovery compatibility
does not relax experiment policy. Neither mode permits installed-OS or firmware writes.

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
release bytes; controller trust is provisioned explicitly during initial manual
setup, and through verified interactive pairing in the later enrollment workflow.
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
an explicit blocked candidate state. Attendance alone cannot replace the early gate;
any alternative needs separate review. Spoofing is outside this software protection model.
Do not silently fall back to a weaker identity or use a fingerprint as a secret.
See C4 for enrollment, retargeting, trust and early-boot details.

## Durable operations and agent sessions

Keep one authoritative SQLite database, content-addressed immutable artifacts and
retained OSTree object closures. Add managed background operations for image build,
composition and agent decisions; CLI mutations return durable IDs promptly. Worker
ownership, request idempotency, checkpointing and restart fences are specified in
C2/C3. Controller systemd user services own the coordinator and rootless workers;
setup records logout/lingering behavior. State and credentials live outside disposable
containers at stable configured paths independent of the invoking working directory. Rootless Fedora builds never
install experimental packages/modules on the controller OS.

Deliver supported baseline selection and documented manual authenticated setup first.
A supported packaged release should not require a source checkout or manually
assembled experimental build manifests; automated installation/setup can follow the
attended development path without inventing readiness. Distinguish recovery boot,
enrollment, experiment eligibility and unattended qualification.

Use shell commands with versioned JSON and typed application services. No MCP or
remote administration server is required in v1. A deterministic session runner
invokes a configured coding-agent command only at decision boundaries. It owns
build/compose/dispatch, preserves interrupted source edits, records hypotheses and
rejected approaches, and feeds compact evidence deltas to the next decision.
Unknown usage, expired authentication, exhausted storage and persistent failures
pause durably rather than retrying indefinitely. Session duration is unrestricted;
operator budgets and experiment deadlines control resource use.

Proposal acceptance returns a durable operation ID before background preparation.
Managed agents exit before capture; external agents hand off pinned revisions or
completed explicit dirty-source captures. No build reads a changing worktree.

Pause prevents new scheduling immediately, finishes the active bounded unit and
returns an active target attempt through recovery. Resume reconciles outstanding
work. Controller restart always requires explicit resume. Neither an agent's
proposal, a completed upload nor a matching target fingerprint authorizes execution.
Report draining workers, recovery arrival, pending evidence and safe shutdown separately.
Backups report consistent-cut source coverage, target-only evidence and separate private
credential requirements; a controller archive alone cannot claim full session recovery.

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
| P0 | First-journey CLI/source/observation contracts; retain existing fixtures | Schema/help fixtures, additive compatibility, authority review |
| P1 | Shared bounded inventory and deterministic supported baseline catalog | Multi-platform fixtures, privacy/limits, unsupported/protection conflicts |
| P2 | Stable setup/service topology, release installation, JSON API and durable operations | Request replay, restart fencing, pause, managed worker survival |
| P3 | Stock-kernel recovery, manual authenticated setup and binding; later pairing/wizards | Boot-device restrictions, wrong trust, moved media, private-state isolation; later enrollment interruption |
| P4 | Attended recovery-to-baseline commissioning workflow | Actual runtime assembly with fake privileged adapters, then operator-run device round trip |
| P5 | Later unattended qualification and scoped candidate activation authorization | Exact identity/grant/revocation tests, then physical reset coverage |
| P6 | Attended external investigation; later managed scheduling and adapter | Source snapshots, proposal/outbox replay, usage, auth failures, pause/resume |
| P7 | Bounded diagnostic recipes and patch exports | Explicit stimuli, baseline/patched/revert identities, regression evidence and uncertainty |
| P8 | Final major-version release qualification | Stable release bytes, infrastructure preservation, fault coverage and declared endurance objective |

Implementation order is the needed P0 fixtures; P1 supported baselines and P2 durable
observable execution; P3 stock-kernel recovery/manual setup/binding; P4 attended
baseline; P6 external proposals and operator-approved investigation. Implement the
minimum source/recipe/observation interfaces for this path before optional UX.
Then add P3 automation, P2 completeness tooling and P6 managed scheduling as bounded
follow-on work. Foundational P7 recipe/observation records are needed by P4/P6;
broader diagnostics follow. P5 qualification is required before unattended operation,
not before the attended external journey. P3 initially uses explicit device confirmation
and current journaled geometry; advanced capacity choices and endpoint migration
wizards are later packets. P8 packaging acceptance is prepared in P2; expensive final
release qualification remains last. Higher-reasoning review
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

## Deferred external hardware

Post-v1 Pi USB gadget media support is reserved in the
[external-hardware design](external-hardware.md) and X1–X5 handoff packets. It is
unimplemented and unqualified, outside the P0–P8 dependency chain. Continue the
direct-drive recovery/OSTree path; no gadget framework, hardware purchase or extra
release qualification is required for v1. Future presentation must preserve target
protection, evidence durability and experiment authority.

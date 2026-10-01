# Quirkbench product roadmap

This is the authoritative forward plan. Use the [implementation contracts](implementation-contracts.md)
and [bounded handoff tasks](implementation-handoff.md) for implementation. Planned
interfaces below are not claims that setup or session orchestration already exists.
Use [controller, target and builder](terminology.md) consistently.

## Delivery contract

Deliver the [README manual](../README.md) from fresh
installation to an attended, evidence-backed patch export. The
[implementation map and checklist](installation-to-patch.md) records the starting
point, owners and acceptance evidence. [C8](product-interface.md) defines the revised
public interface; C0–C7 safety/ownership rules remain binding. Existing manual setup remains supported,
but cannot substitute for the fresh-user release journey.

**First general release:** guided controller setup, verified recovery acquisition,
authenticated pairing, supported baseline/source preparation, an external-agent
investigation with exact-candidate operator approval, scientific comparisons,
patch export and ordinary pause/resume/shutdown/backup use. Deliver fresh-user
setup before completing the investigation loop. A person remains available for
unsupported hangs; no unattended reset claim is made.

**Optional later modes:** managed invocation (P6c) is explicitly configured, never
the default. Unattended authorization/qualification (P5) is a separate capability;
managed AI does not grant physical execution authority. No future unseen patch is
covered by an earlier candidate approval.

## Product flow

1. Install a verified compatible controller release. Complete resumable setup of
   private persistent state, resources, trust and native systemd user services.
   The controller starts with zero enrolled targets; no fabricated device identity.
2. Download verified fixed recovery for the supported platform and use a standard
   image writer. Factory media contains no credentials, inventory or candidate.
   Boot and explicitly confirm the external drive/capacity before journaled writes.
   Secure Boot must already be disabled; Quirkbench does not change firmware settings.
3. Configure target networking and pair through C4 fingerprint verification and
   expiring code exchange. Activate complete private settings atomically. Show
   connection, enrollment, experiment eligibility and attended limits separately.
4. Start an investigation, describe the problem and limits, select an immutable
   supported baseline and prepare a separate editable source workspace. Review
   and approve its first baseline attempt; a lab round trip is not reproduction.
5. Give the generated brief to an external coding agent. It submits immutable
   source/proposals; the controller builds/composes in bounded durable workers.
   Review each exact candidate attempt and supply requested physical observations.
6. Reconcile results through recovery; compare baseline/patched/regression/revert
   evidence, retaining uncertainty. Pause/resume and safely shut down through the
   same application services. Export patches against the recorded source base and
   attributable evidence, or an explicitly inconclusive report.

A moved drive returns to setup; explicit reassignment preserves old attribution
and invalidates old authorizations. Retain installed-OS/internal-disk protection,
authentication and the existing fixed-recovery/experimental separation throughout.

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
release bytes; controller trust is provisioned through verified interactive pairing
in M2. Existing manual provisioning remains subject to the same trust requirements.
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

Deliver fresh controller setup and authenticated pairing before the complete
investigation loop. A supported release requires neither a source checkout to run
Quirkbench nor manually assembled experimental build manifests. Distinguish recovery boot,
enrollment, experiment eligibility and unattended qualification.

Use shell commands with versioned JSON and typed application services. No MCP or
remote administration server is required in v1. Investigation application services
own build/compose/dispatch in both modes, preserve interrupted source edits and
record hypotheses, rejected approaches and evidence deltas. Only the optional
managed driver invokes a configured coding-agent command at decision boundaries.
Unknown managed usage, expired authentication, exhausted storage and persistent
failures pause durably rather than retrying indefinitely. Session duration is unrestricted;
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
| P3 | Stock-kernel recovery, guided pairing/lifecycle and binding; preserve manual compatibility | Boot-device restrictions, wrong trust, moved media, private-state isolation and enrollment interruption |
| P4 | Attended recovery-to-baseline commissioning workflow | Actual runtime assembly with fake privileged adapters, then operator-run device round trip |
| P5 | Later unattended qualification and scoped candidate activation authorization | Exact identity/grant/revocation tests, then physical reset coverage |
| P6 | Attended external investigation; later managed scheduling and adapter | Source snapshots, proposal/outbox replay, usage, auth failures, pause/resume |
| P7 | Bounded diagnostic recipes and patch exports | Explicit stimuli, baseline/patched/revert identities, regression evidence and uncertainty |
| P8 | Final major-version release qualification | Stable release bytes, infrastructure preservation, fault coverage and declared endurance objective |

Delivery follows these user outcomes; P numbers remain implementation ownership,
not numeric scheduling order. See the [checklist](installation-to-patch.md#manual-implementation-checklist)
for every command/screen, current evidence and closure criteria.

| Order | Outcome | Packet mapping |
| --- | --- | --- |
| M1 | Fresh controller installation and resumable setup | P0, P2a–d |
| M2 | Verified recovery, local setup, authenticated target pairing and lifecycle | P3a–f, P1a/b |
| M3 | Investigation/source workspace and approved baseline round trip | P1c, P4, source P6a, needed P7a/b |
| M4 | Complete external-agent experiment loop | P6a/b, P7a/b |
| M5 | Patch/report export and ordinary monitor/shutdown/backup/storage use | P7, P6b, P2c/e |
| M6 | Attended general release | P8, after separately authorized final qualification |
| M7 | Optional managed invocation; independently optional unattended operation | P6c; P5 |

Prepare only each packet's necessary P0 fixtures; retain existing frozen schemas.
Reuse implemented capacity choices, recipes and observation records instead of
rebuilding them or treating them as absent. Enrollment and guided setup now gate the
fresh-user journey. P5 still does not gate attended operation. Hardware-specific
kernel tailoring is a bounded M3 implementation input, not a prerequisite to starting
M1. Higher-reasoning reviews apply before enabling storage, trust/binding, source
ownership, durable dispatch, shutdown or watchdog changes. Routine packets use
focused software tests; actual device commissioning is an explicit product operation.

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

Direct USB storage is the supported media path. USB gadget accessories are outside
this implementation plan.

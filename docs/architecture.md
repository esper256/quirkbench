# Quirkbench: architecture and v1 contracts

This repository is a local, evidence-first laboratory for Linux experiments. The controller owns scheduling and durable state. A booted target reports capabilities, claims one bounded attempt, runs a locally installed recipe and uploads observations. The physical runtime and OSTree boot control are implemented; actual target commissioning remains outstanding. Simulation `smoke` and physical `system-observation` recipes do not claim to reproduce an issue. The [roadmap](product-roadmap.md) specifies product scope and [GitHub tracker #29](https://github.com/esper256/quirkbench/issues/29) owns remaining integration work; planned extensions below are not current runtime guarantees.

## Single-user installation

Quirkbench is exclusively a single-user application. One person installs a
per-user instance and uses it exclusively. A shared installation, multiple
application users, tenants, accounts, roles, delegated administration, shared
workspaces and collaboration/access-control features are out of scope, not
requirements to anticipate.

Controller, builder, target and coding agent are components acting for that one
user, not separate application users. Multiple worker processes, investigations
or targets do not imply multi-user support. Worker ownership means coordination
and restart fencing within the user's installation, not isolation between users.

Rely on the host OS for the user's account boundary. Do not design against other
local users or the installation owner as adversaries to justify application-wide
permission enforcement. Keep ordinary secret hygiene, authenticated controller/
target communication, explicit experiment approval, and prevention of accidental
data loss or unintended writes. These serve the single user's workflow; they do
not establish a multi-user authorization model.

The [file-access policy](implementation-contracts.md#file-access-and-permission-policy)
defines ordinary-data permissions and secret defaults. Existing blanket mode
checks are implementation debt tracked in [#66](https://github.com/esper256/quirkbench/issues/66),
not an expansion of this product scope.

## Forward product boundary

Use [controller, target and builder](terminology.md) as distinct roles. The core
state machine and protocol are independent of target vendor/model/form factor.
Hardware discovery selects versioned platform profiles and adapters, including
explicit architecture/boot constraints. Unsupported profiles remain unsupported;
generic architecture does not imply unqualified universal hardware support.

The target boots fixed generic recovery media before discovery. Current manual setup
configures networking and provisions authenticated controller trust; recovery collects the
hardware inventory used to plan the experimental baseline. The installed-OS
collector is optional. Private network/credential/binding generations live in
independent evidence/control storage; factory images contain none. Early boot and
runtime identity checks prevent a moved drive from resuming another target's work.
See C4 in the implementation contracts for the enrollment and retargeting boundary.
The storage policy below separates recovery boot-device confinement from candidate
controller exclusions. Every privileged destination needs positive boot-device identity. Candidate OSTree updates cannot change recovery or the fixed bootloader.

Agents use a local CLI with typed application services, JSON and idempotent
operation IDs. A persistent worker performs long work without a waiting agent;
session orchestration reuses campaign state in the existing SQLite database.
There is no v1 MCP server or remote administrative API. The proposed experimental
watchdog authorization is separate from exact-build qualification and must be
implemented/reviewed before use. See the roadmap for contracts and bounded briefs.

## Trust and execution boundaries

The controller is authoritative for campaign, job, attempt, lease, and evidence metadata in SQLite. Content-addressed blobs live in an immutable store keyed by SHA-256; their hashes are checked when read and transferred. State changes that authorize work or acknowledge evidence must commit durably before the controller responds. File publication uses a temporary file, flush/fsync, atomic rename, and directory fsync. A backup must capture a consistent database, all referenced blobs, and the complete OSTree content reachable from retained deployment revisions. Restoring verifies both artifact hashes and repository content. A database plus ordinary artifacts alone is not a complete backup when deployment references exist. Retained checkpoints protect deployment manifests and their referenced commits from cleanup.

The target alone runs privileged recipes. The controller can offer artifacts and instructions through the target-facing protocol but cannot run those tasks on the controller. HTTPS uses a server certificate, a device bearer token, bounded request bodies, and a device-scoped artifact endpoint. Administrative operations remain local to the controller. A target journals an outbound operation before sending it; it removes that outbox item only after the server has durably committed and acknowledged it. Retries carry stable identities so a duplicate message is either an idempotent replay or a conflict, never a second execution.

The target's boot mode is an independent fact from experiment outcome. `recovery`, `experiment`, and `simulation` are capability report modes; they are not `PASS` or `FAIL`. A reboot changes the boot identity. Registration of a new boot makes unfinished attempts from an old boot uncertain, while retaining their evidence. An operator must resolve an uncertain attempt explicitly before any new attempt of that work is issued. A lease expiry stops authorization for new execution; it does not erase prior observations or prove that an already-running task stopped.

## State model

| Entity | States | Meaning |
| --- | --- | --- |
| Campaign | `RUNNING`, `PAUSE_REQUESTED`, `PAUSED` | Pause is durable; the controller stops new claims at the request and reaches `PAUSED` after in-flight work is reconciled. Resume is explicit. |
| Job | `QUEUED`, `ACTIVE`, `DONE` | A job is a durable plan for an experiment and its repetitions; attempt outcomes determine its eventual disposition. |
| Attempt | `CLAIMED`, `BOOT_PENDING`, `RUNNING`, `UNCERTAIN`, `RESOLVED`, `COMPLETE` | A claim reserves work for a specific device, boot, and lease. `BOOT_PENDING` records exact-revision handoff before USB arming. `UNCERTAIN` preserves ambiguity after reboot, lease loss, or lost acknowledgement. An operator moves it to `RESOLVED` with a retry or abandon disposition and a reason. `COMPLETE` records a final result and evidence references. |

The controller startup recovery step marks unfinished claims and runs `UNCERTAIN`, pauses campaigns, and requires reconciliation before resume. A retried resolved job gets a new attempt ID; the old attempt, evidence, and operator note remain. An explicit resume starts a new session only when the device reports recovery or simulation mode, no unresolved attempt remains, and no library-maintenance fence is held. Result completion does not authorize another physical attempt until recovery return is acknowledged.

Each session defaults to eight hours and one million recorded decision tokens. Expiry or exhaustion requests a pause, blocking new claims while allowing an already issued attempt to report evidence. Lowering a budget below usage already recorded must trigger that pause immediately. Repeated decision IDs are idempotent; a changed replay is a conflict. These budgets constrain controller scheduling, not the physical duration of an already executing recipe.

`PASS`, `FAIL`, `INCONCLUSIVE`, `INFRA_FAILURE`, and `NEEDS_HUMAN` classify a completed attempt. These outcomes are distinct from lifecycle states. A crash or missing reply is not automatically a recipe failure. Every claim, start, heartbeat, evidence reference, completion, and operator resolution should remain auditable with stable identifiers. The controller is the source of truth for claim eligibility; a target must not interpret an expired lease or a cached job as fresh permission.

## Versioned wire records

The six original v1 record shapes have matching runtime validators in `src/quirkbench/contracts.py` and JSON Schema 2020-12 definitions in `schemas/`. They reject unknown fields, unsupported versions, invalid identifiers, and malformed digests. Omitted optional fields take the runtime defaults; when `schema_version` is supplied it must be the integer `1`. Each example in `examples/` is a valid shape illustration, not a certified recipe or outcome. Deployment example digests are placeholders; use actual composer output and retained evidence for submission. JSON values in arbitrary `parameters`, `inventory`, `measurements`, `notes`, and `provenance` objects still have to be finite, serializable JSON.

| Record | Purpose |
| --- | --- |
| `Experiment` | Hypothesis, recipe, bounded repetitions and timeout, required capabilities, artifact digests, success criteria, and provenance. |
| `Artifact` | Digest and byte size for an immutable blob. |
| `CapabilityReport` | Device and boot identity, boot mode, declared capabilities, and inventory. |
| `Result` | Attempt identity, one outcome, concise interpretation, evidence digests, measurements, and limitations. |
| `Checkpoint` | Campaign identity plus durable artifact references and operator notes. |
| `Progress` | Activity/campaign identity, phase, sequence, measured counters, expected report interval, stall threshold and fixed deadline. |
| `DeploymentManifest` | Backend, exact revision, configured repository identifier, build provenance and protection profile; stored as an immutable artifact. |

The original v1 envelope field sets and outcome vocabulary remain frozen. `Experiment.artifacts["deployment"]` points to a deployment manifest without changing that envelope. Incompatible changes require a new schema version and migration/replay tests; adding an unknown field to a v1 message is intentionally rejected. This prevents an older target from silently ignoring a control or evidence field.

## Composition and deployment boundary

The production backend is minimal Fedora composed with rpm-ostree. The controller builds experimental RPMs inside a resource-bounded rootless Fedora container, then composes a complete filesystem revision. Kernels, modules, initramfs, userspace and default configuration travel together. Matching symbols and source/toolchain identities remain retained build evidence. No experimental package is installed on the controller or assembled on the target.

A signed OSTree repository is served through authenticated HTTPS. Device credentials and repository trust configuration are distinct from agent credentials and signing private keys, which remain on the controller. An experiment authorizes an exact commit, never a moving branch. OSTree supplies object verification, incremental fetching and synchronized deployment transactions; optional static deltas are deferred until measurements justify them.

| Interface | Responsibility |
| --- | --- |
| Composer | Build and publish an immutable deployment reference with provenance. |
| Deployment backend | Prepare an attempt's revision, report installed/running identity, retain and remove deployments. |
| Boot control | Arm one prepared deployment, reboot and report recovery state. |
| Quirkbench core | Attempts, authorization, evidence, progress, reconciliation and checkpoints. |

`DeploymentManifest.provenance.build_evidence` is a versioned closure with `schema_version: 1` and an `artifacts` mapping of role to CAS digest. Controller submission and checkpoint retention require build provenance, matching `vmlinux`, `system_map`, `config`, `modules`, and kernel/userspace source archives; the build provenance must bind those hashes and kernel release. The controller retains these blobs with the manifest. Composition publication pins an owner `deployment:<manifest-digest>` before any experiment is submitted, so a freshly composed result is included in backup. Retention releases those references only when configured counts and live/pinned dependencies permit cleanup. Ordinary `artifact_sha256` payload identities do not implicitly become references. Missing evidence causes new submissions or backup completion to fail, while existing campaign history remains readable.

Backend commands and filesystem paths stay behind adapters. A fake backend exercises shared behavior; the old four-file bundle is not a second supported deployment backend. Unsupported or legacy kernel-only execution must fail explicitly. Build artifacts may remain readable even when they cannot be deployed by the current adapter.

## Boot and protection boundary

Layout revision 2 has six named roles: fixed EFI boot, fixed read-only recovery, dedicated GRUB state, experiments, library and evidence. The compact factory image has the first four GPT entries; restartable first-boot commissioning grows experiments and creates the preidentified library/evidence partitions on the positively identified USB. Candidate OSTree state and evidence use separate filesystems. Recovery remains independently bootable and can upload evidence when experiments/library are unavailable. See [the debug image contract](debug-image.md).

OSTree generates candidate boot entries without regenerating the system bootloader. Quirkbench validates those entries and integrates them with USB GRUB one-shot state. GRUB consumes and verifies cleared candidate state before handoff; recovery remains the permanent default. Normal experiments use full firmware reboots. A fresh deployment group and mutable state are created for every physical attempt, while preparation retries for that attempt are idempotent. Configuration starts from the commit defaults; neither shared `/var` nor modified `/etc` may contaminate a later attempt. Evidence lives outside that disposable state and remains until acknowledged.

Candidate data mounts must permit OS execution; evidence mounts remain restricted.
Candidate roots follow OSTree semantics: read-only `/usr`, an exactly identified
writable deployment root and attempt-local `/etc` and `/var`; recovery alone uses
a wholly read-only root. Secure Boot must be verified disabled.

## Storage protection policy

**Normative policy.** Internal storage and the installed OS are
outside investigations. The principal variable risk is the experimental kernel
and recipe proposed by an agent. Recovery is fixed, trusted code maintained and
verified separately; it never executes agent recipes or adopts candidate changes.

**Recovery: boot-device-only storage operations.** Use a pinned stock Fedora kernel
and matching modules/firmware. Passive kernel disk enumeration and partition-table
reads are permitted, along with bounded sysfs/proc hardware metadata. Recovery
userspace, including Quirkbench, udev helpers and dracut, must not open internal
block devices or probe their filesystem signatures. Controller exclusions apply
to experimental kernels independently of this recovery policy.
Do not open internal block devices, read filesystem
contents, mount their partitions, activate swap/resume, run filesystem repairs or
write them. Apply the policy from initramfs through normal userspace, including
udev helpers and maintenance tools. Disable automount, installed-OS discovery,
os-prober, automatic filesystem repair, swap/resume and firmware updates. Do not
write EFI variables, use EFI-backed pstore or load an unapproved alternate kernel
through kexec/kdump. Stock kernel feature availability is not runtime authorization.
This policy is not satisfied merely
because Quirkbench itself never calls mount.

Resolve the recovery-root backing chain to one positively identified physical
external boot device. Authorize only expected Quirkbench partitions with validated
roles and layout on that device. USB bus membership, a label, UUID or `/dev/sdX`
name alone is insufficient. Duplicate identities, unresolved backing chains or
changed devices block storage operations and show a local diagnostic. Never fall
back to the first USB disk. Initramfs must apply this identity rule before mounting
the recovery root; later privileged tools revalidate before mutation. Explicit
commissioning can alter only the confirmed boot device through the existing
journaled geometry plan. A privileged maintenance shell is operator administration,
not a policy bypass or proof of containment; its actions remain within this scope.

**Experiments: restricted kernels and approved execution.** Retain internal-controller
exclusions in reviewed candidate profiles and check the final configuration, modules
and initramfs after dependency resolution. Required internal-storage drivers block
that experiment. Review source/configuration/recipe changes affecting storage access,
boot, privileged destinations or firmware independently of the proposing agent.
Bind operator approval to the exact immutable candidate/attempt before arming;
proposal acceptance or build completion does not grant boot authority. Restrict
writes to validated experiment/evidence roles on the boot device; candidates cannot
modify fixed recovery, its trust/configuration or the fixed bootloader. No agent
request expands these permissions. Preserve pre-kernel wrong-target checks.

These controls reduce accidental damage; they cannot sandbox an arbitrary modified
kernel that deliberately or accidentally bypasses them. Attendance, a successful
recovery boot and driver-exclusion checks do not establish safety of every kernel
patch. Record that limit with experiment evidence. No universal hardware safety
claim follows from a generic design or passing software fixtures.

Recovery recipe/rootfs-lock/release v2 represent stock package identity and
boot-device policy separately from candidate exclusions. Recovery execution accepts
v2 only; historical v1 readers retain original custom-kernel provenance meaning.
Do not change old identifiers or relabel historical artifacts. Software validation
does not establish physical boot or storage protection on a target.

See [recovery and evidence](recovery-and-evidence.md) for separate selection, reset
and diagnostic requirements. Current no-kexec policy leaves kdump unavailable.
QEMU tests disposable infrastructure, internal-disk sentinels and settled firmware
variables under the [acceptance policy](../acceptance/README.md). It does not establish
physical hardware behavior. Experiments run directly on the target; unsupported
complete hangs require human reset.

## Human visibility

Progress is a versioned protocol record, not ephemeral terminal output. The controller assigns receipt times, persists event history, and distinguishes last report from last meaningful phase/state/message/counter advancement. Replay cannot refresh liveness, move counters backward or extend the original deadline. Reported progress never authorizes execution or renews a lease. See `docs/monitoring.md` for display semantics and `tests/test_monitor.py` for deterministic stalled/slow/waiting tests. The terminal monitor also shows target contact, job counters, lease/deadline state and controller sample timestamps without invoking an AI agent.

## Verification claims

Unit and integration tests exercise record rejection, durable state transitions, protocol retry and identity rules, target journaling, and local demo flows. The QEMU acceptance path is opt-in because it needs a built image and OVMF fixtures. CI can establish software behavior under its fixtures; it cannot establish a real hardware safety claim or physical endurance result. Any capability report describes what one boot observed or advertises, not a universal property of the device. Keep each result's limitations and evidence alongside the outcome so a later reader can audit the claim.

## Physical execution and reset

A durable `BOOT_PENDING` handoff precedes one-shot arming. Candidate adoption verifies exact revision and attempt identity; recovery return is acknowledged separately from result completion. Streaming recipes seal/upload evidence during execution; bounded final uploads return through recovery even on network failure. Recovery alone prepares/arms deployments. Systemd watchdog activation requires a matching qualification profile or an explicit attended qualification run. Kernel and settings changes invalidate coverage. See [debug image](debug-image.md) and [watchdog qualification](watchdog-qualification.md).

## Product orchestration boundary

The [product interface contract](product-interface.md) specifies planned services and
UX over these existing primitives. The foreground controller owns bounded container
workers; sessions reference campaigns. Deliver external-agent proposals first, then a
managed decision queue over the same source-capture and dispatch API. Proposal receipt
is distinct from immutable-source readiness. Recipe extensions and human observations
are versioned records, not arbitrary target commands. Recovery remains suspend-disabled;
only eligible candidate recipes may exercise reviewed sleep modes. The planned general release
starts with guided setup/pairing and attended operator-approved attempts. Guided backup
completeness belongs to M5; managed scheduling and unattended grants are optional M7
capabilities. Current manual setup and backup commands retain their stated limits.

The target Python payload is the reviewed module list in `target_payload.py`, plus
recipe metadata and target boot assets. Runtime capture and installation use the
same list. Controller commands, database services and host installation code are
outside that payload; shared wire validators, file operations, process recording
and kernel policy live in shared modules. `target_install.py` owns image-side
installation/audits. Historical Python imports remain available through aliases
or lazy host factories; target services do not invoke those factories. Software
checks import the payload with site packages disabled and exercise safe entrypoint
help. This verifies packaging, not native boot readiness.

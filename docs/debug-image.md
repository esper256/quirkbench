# Debug image and execution

Recovery confines storage operations to its physical boot device; experimental
kernels retain internal-controller exclusions. See the authoritative
[storage policy](architecture.md#storage-protection-policy). The
[roadmap](product-roadmap.md) defines guided setup and the attended external-agent
journey; [recovery operations](recovery-operations.md) describes current manual use.

The recovery OS is fixed, independent of the experimental OSTree repository, and
always the default boot. Each physical attempt gets an isolated OSTree deployment
with fresh `/etc` and `/var`. A successful experiment uploads its results and
returns through recovery; it cannot prepare or authorize the next experiment.

## Recovery build policy

The [recovery decision](recovery-base.md) specifies the synthesis pipeline: Fedora
RPMs installed with locked DNF5 inputs, stock Fedora kernel/module packages,
dracut and the existing image adapter. Recovery runs without SELinux enforcement
(`selinux=0`), desktop, installer or automatic updates. It uses read-only ext4 plus
bounded RAM runtime state and NetworkManager/nmtui. Stock synthesis is implemented; actual image and hardware qualification must be
recorded for the selected bytes.

## Layout revision 2

The standard image writer receives a compact GPT `.img` plus checksum. The factory
image contains four partitions and six preassigned partition identities. Attended
first-boot setup will commission the final layout on the positively identified USB
after local capacity confirmation. The screen and larger supported sizing choices
exist in the software runtime; assembled-boot validation remains open. Factory boot
blocks until a completed commissioning journal matches the observed layout:

| Partition | Default capacity | Access and contents |
| --- | --- | --- |
| EFI boot | 256 MiB | Fixed removable loader and recovery boot files; normally unmounted |
| Recovery | 2 GiB | Fixed read-only Fedora root; transient state in RAM |
| Boot state | 32 MiB | Restricted GRUB consumed one-shot state and commissioning journal |
| Experiments | 32 GiB | Candidate OSTree sysroot, isolated deployments and disposable caches |
| Library | 32 GiB | Immutable optional packs; read-only during experiments |
| Evidence | Remaining capacity | Logs, pending uploads, acknowledgements and durable control state |

Recovery mounts experiments at `/var/lib/quirkbench/experiments`. Candidates use
`/sysroot` for that filesystem. Both mount evidence and library at
`/var/lib/quirkbench/evidence` and `/var/lib/quirkbench/library`. Evidence is
`nosuid,nodev,noexec`; library is `ro,nosuid,nodev`; experiment storage permits
execution. OSTree `/usr` remains read-only. Recovery uses `ro,noload` and no fsck
writes. No code searches for installed operating systems or internal data disks.

Commissioning records geometry before modifying GPT, preserves existing p4 data,
grows its filesystem and creates only the expected new library/evidence partitions.
It uses standard tools, rechecks identity before mutations and observes completed
steps on retry. An ambiguous interrupted format with no recognizable expected
filesystem stops for human investigation; it never blindly reformats. The required
evidence allocation includes the configured log budget, twice detected RAM and
20% capacity headroom. Defaults target a 250/256 GB or larger external SSD.

Before any partition mutation, a local RAM-backed screen shows positively identified
media, RAM/capacity requirements and proposed sizing. Confirmed geometry is durably
journaled on boot state before expansion; credentials and evidence storage are not
prerequisites. A retry uses the same plan. Moving media to a higher-RAM target rechecks
capacity eligibility and never triggers automatic repartitioning. See the
[product contract](product-interface.md#endpoint-changes-and-media-capacity).
Sizing is chosen at commissioning; later layout changes require rebuilding media.
Version 1 prototype images require rebuilding, not in-place conversion. Existing controller records retain their original interpretation.

Recovery mounts evidence before optional filesystems. An unavailable experiment or
library filesystem blocks new work but does not prevent recovery evidence upload.
All partitions share one physical USB failure domain, so continuous controller
uploads remain necessary.

## Live evidence and physical handoff

`Recovery → prepare exact revision → authorize handoff → arm once → reboot → experiment + live upload → reboot → recovery`

The controller commits a `BOOT_PENDING` handoff before arming. Candidate adoption
requires the expected physical attempt, revision, device and boot identity. GRUB
consumes and verifies cleared one-shot state before loading the candidate. There
is no automatic successful-candidate promotion or direct candidate chaining.

Recipes may yield sealed `EvidenceChunk` values before their terminal result. The
supervisor persists each chunk before transmission. Recovery and experiment use
the same resumable upload/evidence acknowledgement path. Evidence remains local
until durable controller acknowledgement. Completion and return to recovery are
separate records: even a completed result does not permit another physical attempt
until recovery is observed. Lost acknowledgements are retried; lost boot identity
or execution remains uncertain and requires explicit reconciliation.

Finishing candidates spend a bounded interval on upload, then request recovery.
Recovery retries network outages without rebooting repeatedly. Pause immediately
stops scheduling and takes effect between physical repetitions; controller restart
pauses and requires explicit resume. Live results let controller analysis/builds
overlap the recovery boot. Direct candidate chaining is deferred until measured
boot overhead justifies its additional handoff states.

## Provisioning and optional library maintenance

The boot verifier writes a fresh `/run/quirkbench-boot.json`; the target supervisor
revalidates current boot and USB identity. Recovery without device provisioning
waits visibly. Put the device configuration and its separate TLS/token files under
`/var/lib/quirkbench/evidence/control/`, owned by root with mode 0700 for directories
and 0600 for credentials. `runtime.json` contains:

```json
{
  "schema_version": 1,
  "device_id": "target-01",
  "target_binding": {"schema_version": 1, "system_uuid": "01234567-89ab-cdef-0123-456789abcdef"},
  "controller_url": "https://192.168.1.10:8443",
  "ca": "controller-ca.pem",
  "token_file": "device-token",
  "remotes": {
    "lab": {
      "url": "https://192.168.1.10:8444/lab",
      "ca": "repository-ca.pem",
      "public_key": "ostree-public.asc",
      "client_cert": "device-cert.pem",
      "client_key": "device-key.pem"
    }
  }
}
```

The current low-level configuration reader supports manual development fixtures.
Activation now also requires `target_binding` with schema_version 1 and a valid
`system_uuid` matching the running target. Old unbound files remain readable but
cannot silently activate credentials or experiments. This guard is not a complete
enrollment system. Manual configuration is the current development setup path,
with existing trust, authentication, binding and signature validation preserved.

**Current manual path:** local Ethernet/Wi-Fi setup and explicitly provisioned controller
trust/device credentials use validated private configuration. Complete state must
activate safely; no verification bypass or credentials in public artifacts.
**Planned M2 pairing (P3d/e):** will publish private generations atomically.
NetworkManager is the sole network manager. Saved profiles belong to control state
and are copied into RAM for each boot only after binding checks. No AI credentials
or controller private signing keys enter media. Generic factory images have no
controller/device credentials. See [C4](implementation-contracts.md#c4--recovery-setup-enrollment-and-target-binding-p3).

GRUB consumes one-shot selection and compares its expected system UUID before loading
a candidate. Missing/mismatched identity returns to recovery. Runtime checks enrollment
binding again before authentication or watchdog activation. Retargeting needs explicit
setup; old evidence retains original attribution and cannot authorize another attempt.
Only the spool is exported as evidence, never the adjacent private control directory.
The installed `system-observation` recipe collects kernel identity and logs without
claiming a hardware issue was reproduced. Additional recipes are local versioned code
in the deployment, never remote shell text.

Library selections are immutable ordinary artifacts (`library` role) naming exact
pack-manifest hashes. Packs declare architecture, runtime requirements, file hashes
and permitted entrypoints. There are no moving aliases or default model weights.
The library API refuses traversal, symlinks, special files and incomplete packs.
Privileged cleanup stays in recovery; a library script receives no arbitrary disk
wipe authority. Kernel-coupled tools belong in the experimental deployment.

Begin maintenance with the controller CLI after pausing all campaigns on the device:
`quirkbench library-maintenance begin target-01 --selection HASH`. The durable fence
prevents resume until `quirkbench library-maintenance finish target-01`. The target
`python3 -m quirkbench.library_maintenance` command verifies the fence, downloads through authenticated resumable
artifact transport, publishes verified packs atomically, then restores read-only
access. Backup/checkpoint retention includes selections, manifests and file content.

## Reset coverage and human visibility

Systemd is the only userspace hardware-watchdog owner. The initial qualification
request is 120 seconds; reports contain the observed hardware timeout and armed
state, never an assumed countdown. A hardware reset returns to recovery because
the one-shot candidate selection was already consumed. Early boot, runtime,
shutdown and suspend need independent qualification. No usable watchdog or a
missing observation is reported honestly; software heartbeat is not hardware proof.

This hardware reset primarily protects the experimental OS. Recovery services the
same watchdog if configured/armed; there is no second hardware-watchdog subsystem.
Integration code alone does not establish driver support or target reset coverage. The roadmap distinguishes exact-kernel qualification from a
future explicit campaign authorization to activate it on experimental kernels.

The supervisor has systemd service supervision, caller-driven heartbeat and bounded
phase/recipe deadlines. It reports actual progress separately from responsiveness.
A stalled recipe is stopped without replay; failure recovery requests are durable
when evidence storage works and still attempt reset when it does not. A recovery
supervisor failure requests human intervention rather than a reboot loop.

See [watchdog qualification](watchdog-qualification.md) for kernel prerequisites,
policy identities and physical tests. Current kexec prohibition still excludes
kdump. A watchdog reset alone does not create a vmcore or prove a kernel crash.

Monitoring includes boot mode/stage, exact attempt/revision, watchdog observations,
last heartbeat/advancement, phase deadline, evidence byte counts and per-partition
capacity. Long healthy work, waiting for the controller, expired deadlines and
human intervention remain distinct. Audio alerts are optional; visible status is
primary because optional notification peripherals may be unavailable.

## Acceptance boundary

Software tests cover commissioning retries, strict identities, library publication,
live uploads, stale leases, uncertain arming, controller restart, pause, bounded
finish, service deadlines and missing watchdog observations. Real QEMU acceptance
must commission this layout, boot recovery/candidate/fallback and preserve fixed
recovery, library, internal sentinels and effective firmware settings. Target USB/reset/suspend/crash-capture coverage and the release-specific endurance
objective remain separate physical gates. Changed image bytes need fresh qualification.

For the first attended protocol trial, adapt
[`examples/physical-experiment.json`](../examples/physical-experiment.json), replacing
the placeholder deployment hash with the artifact ID printed by `compose`. Register
the provisioned target, create a paused campaign, submit the experiment, and resume
explicitly. `system-observation` deliberately returns `INCONCLUSIVE`: its logs prove
protocol/boot observations, not a fix for any target issue. Require independently
qualified reset coverage before using this mechanism unattended.

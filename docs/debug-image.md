# Quirkbench v1 debug image and execution

The recovery OS is fixed, independent of the experimental OSTree repository, and
always the default boot. Each physical attempt gets an isolated OSTree deployment
with fresh `/etc` and `/var`. A successful experiment uploads its results and
returns through recovery; it cannot prepare or authorize the next experiment.

## Layout revision 2

The standard image writer receives a compact GPT `.img` plus checksum. The factory
image contains four partitions and six preassigned partition identities. First
recovery boot commissions the final layout on the positively identified USB:

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

Sizing is chosen at commissioning; later layout changes require rebuilding media.
Version 1 prototype images require rebuilding, not in-place conversion. Historical
controller data and qualification files remain readable and are not removed.

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
  "device_id": "acer",
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

For initial provisioning, first boot completes partition commissioning. Shut down and attach the USB to the controller, then copy only device configuration/trust files into the positively identified evidence partition using normal Linux filesystem tools. Subsequent boots reuse that configuration. No automatic internal-disk mount or custom USB writer is involved.

Wired interfaces use DHCP; the controller can have a static IP. Provisioning is an
explicit commissioning step, never an AI credential transfer. Only the spool is
exported as evidence; the adjacent control directory must never be exported.
The installed `system-observation` recipe collects real kernel identity and logs
without claiming a hardware issue was reproduced. Additional recipes are installed
as local code through the experimental OSTree composition, not remote shell text.

Library selections are immutable ordinary artifacts (`library` role) naming exact
pack-manifest hashes. Packs declare architecture, runtime requirements, file hashes
and permitted entrypoints. There are no moving aliases or default model weights.
The library API refuses traversal, symlinks, special files and incomplete packs.
Privileged cleanup stays in recovery; a library script receives no arbitrary disk
wipe authority. Kernel-coupled tools belong in the experimental deployment.

Begin maintenance with the controller CLI after pausing all campaigns on the device:
`quirkbench library-maintenance begin acer --selection HASH`. The durable fence
prevents resume until `quirkbench library-maintenance finish acer`. The target
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
primary because audio is itself an investigation target.

## Acceptance boundary

Software tests cover commissioning retries, strict identities, library publication,
live uploads, stale leases, uncertain arming, controller restart, pause, bounded
finish, service deadlines and missing watchdog observations. Real QEMU acceptance
must commission this layout, boot recovery/candidate/fallback and preserve fixed
recovery, library, internal sentinels and effective firmware settings. Old M2 VM
results qualify only the old image. Acer USB/reset/suspend/crash-capture coverage
and the campaign exceeding 30 hours remain explicit hardware gates.

[Recorded layout-2 qualification](v1-qualification.md) includes the fresh ten-boot
suite, standard-image supervisor trial, signed composition and backup/restore.

For the first attended protocol trial, adapt
[`examples/physical-experiment.json`](../examples/physical-experiment.json), replacing
the placeholder deployment hash with the artifact ID printed by `compose`. Register
the provisioned target, create a paused campaign, submit the experiment, and resume
explicitly. `system-observation` deliberately returns `INCONCLUSIVE`: its logs prove
protocol/boot observations, not a fix for any Acer issue. Require independently
qualified reset coverage before using this mechanism unattended.

# Recovery operations

Current low-level stock-recovery commands, manual setup and initial console pairing.
The complete guided journey remains unfinished; see
[GitHub tracker #29](https://github.com/esper256/quirkbench/issues/29) and the
[acceptance guide](installation-to-patch.md).
Software support does not establish image or hardware qualification. Use
[controller installation](controller-installation.md) and explicit
[acquisition specifications](recovery-acquisition.md) before admitting work.

## Boot progress and diagnostics

Newly built images display a five-second GRUB menu. Press an arrow key during
that interval to stop the countdown. Select **Quirkbench recovery - verbose boot
diagnostics** when investigating a failed boot; no kernel command editing is needed.
This starts the same fixed recovery kernel and initramfs, with verbose kernel,
dracut, systemd and udev logging. Both recovery entries keep serial output and
route local boot diagnostics to VT1 rather than the active console. An already armed, identity-checked
candidate retains its existing one-shot default; selecting diagnostics boots recovery.

The expected destination is the **Quirkbench recovery dashboard**, painted without pressing Enter.
Use arrows/Enter, `?` for help, `L` for logs and `T` for the terminal.
Status keeps current boot checks, USB storage, networking, pairing and authenticated
controller contact separate. Disabled actions explain the reason and next step.
Leaving an action screen does not cancel or duplicate its operation.
New USB layouts are prepared on the controller; the target menu cannot partition
or format them. If boot
stops earlier, photograph the last errors and any dracut timeout/root-device messages;
kernel USB-event messages alone do not establish successful recovery startup.
The diagnostic entry grants no experiment approval and changes no storage protections,
mounts, firmware settings or emergency-shell policy.

The recovery UI owns VT2. The independent local root terminal owns VT3: choose
**Open terminal** / press **T**, or use Ctrl+Alt+F3. `exit` returns to the running
UI; if the UI is unavailable, the terminal explains the failure and stays usable.
This terminal has no password or pairing requirement and commands can modify
internal disks. Quirkbench's automated actions retain their storage protections;
opening a shell grants no experiment approval. Boot logs remain on VT1 and serial.
These are staged-software behaviors; actual VT switching requires physical acceptance.

Temporary **Network** setup requires private RAM profile storage and NetworkManager,
independently of evidence storage, boot verification or pairing. Saved connections
remain gated by verified media and target binding. Incomplete secret cleanup blocks
NetworkManager. If boot checks finish later, no saved secrets are automatically
replayed into the running manager; explicitly retry saved-profile restoration.

Dracut diagnostic reports, when produced, are in `/run/initramfs/rdsosreport.txt`;
the boot journal and early reports are held in RAM and may disappear on reboot.
Do not expect persistent logs before evidence storage is verified. Retain photographs
or a VM serial log before restarting, and review verbose logs for private details
before sharing them. Keep the boot USB connected while recovery is running.

Controller preparation reports actual usable experiment/evidence capacities. The
new prepared layout reserves no hypothetical library payload and splits remaining
space equally, after fixed recovery and filesystem overhead. There is no RAM
admission limit or promise that every experiment/dump fits. Historical v2 media
retains its original commissioned geometry; reprepare rather than resize on-target.
Existing images require rebuilding from fresh inputs to receive changed software.

If a keyboard combination reports **this sysrq is disabled**, the kernel received
the request but its current SysRq policy disallows that operation. Where permitted,
SysRq **h** displays help, **w** dumps blocked tasks, **t** dumps task states and
**l** dumps CPU backtraces. These are diagnostics, not recovery readiness. Chromebook
key mappings vary; numbered keys may represent function keys rather than digits.
This diagnostic entry does not change the SysRq enablement policy. SysRq **b**
immediately resets without syncing/unmounting and is not attended safe shutdown;
do not treat generic reboot or crash-key sequences as evidence-preserving recovery.
See the [kernel SysRq reference](https://docs.kernel.org/admin-guide/sysrq.html).

Prepared media contains the controller's public trust and a USB-specific initial
invitation. Once verified writable storage, target identity and networking are
available, the dashboard automatically attempts normal pairing. A bounded network
request timeout retains the same request/key; limited transient retries do not
expire the invitation. Authentication/trust failures require explicit repair.
Initial v2 invitations are single-use, non-expiring and explicitly revocable;
historical v1 expiry and exceptional retarget invitations retain their meanings.
Successful activation erases the staged bootstrap secret. Pairing never approves
an experiment. Prepared trust does not authorize evidence removal or retargeting.

**Connection details** contains existing binding/address/evidence maintenance.
**Remember selected connections** saves only explicitly selected supported profiles
in verified control storage. Temporary networking works independently. Uncertain
secret replay cleanup leaves NetworkManager stopped; **Troubleshooting → Reset
temporary connections** explicitly forgets this session after whole-manager
shutdown and removes its RAM copies. Saved USB selections remain intact.

## Recovery debug reports

**Troubleshooting → Collect recovery report** captures reviewed current-boot sources
in private RAM, bounded by ten seconds, 16 MiB payload and a 1 MiB manifest. Missing
boot records, pairing, hardware identity or evidence storage do not prevent collection
or explicit local export. Four RAM report directories bound retained snapshots;
export/remove an old snapshot explicitly before another collection. RAM reports
survive a UI restart, but disappear on reboot. Collection never scans/mounts internal
disks or recursively copies configuration, evidence, home directories or history.

Review the reported identities, omitted/truncated sources and escaped preview.
Known credentials/private-key material are excluded before hashing or transmission;
free-form logs can still identify machines or networks. **Review and send recovery
report** requires normal pairing/current binding and explicit `SEND REPORT_SHA256`.
A failed send retains local files. Only a durable controller receipt means received;
a lost reply retries the same frozen report/request without duplication. Contents
are reported diagnostics, never proof of healthy recovery or experiment approval.

If the dashboard fails, the root terminal uses the same implementation:

```sh
python3 -m quirkbench.recovery_reports collect
python3 -m quirkbench.recovery_reports list
python3 -m quirkbench.recovery_reports show REPORT_SHA256
python3 -m quirkbench.recovery_reports export REPORT_SHA256 --output /absolute/new-export
python3 -m quirkbench.recovery_reports send REPORT_SHA256
```

Sending still requires interactive review/confirmation. On the controller:

```sh
quirkbench admin diagnostics list
quirkbench admin diagnostics show REPORT_SHA256
quirkbench admin diagnostics export REPORT_SHA256 --output /absolute/new-export
quirkbench admin diagnostics delete REPORT_SHA256
```

Reports use existing managed storage with independent retention. Deletion requires
an idle stopped controller and tombstones only the selected report, removes its
partial uploads and unreferenced attachments, and preserves all ordinary CAS roots.
Completed reports are included in controller backup/restore; partial uploads are
not completion evidence and can require retry. No command posts reports publicly.
A full controller rejects writes without receipt; export or retry after explicit cleanup.

## Recovery inputs and preparation

New recovery input generation uses `RecoveryRecipe` v2, rootfs-lock v2 and
release-candidate v2. The installed `stock-x86_64-uefi-usb-v1` policy has a separate
identity from candidate profiles. Existing v1 records retain their original
custom-kernel interpretation.

New acquisition selects an immutable specification with repository and trust
identities and requests exact NEVRAs.
Preparation refuses missing packages, changed recorded bytes, invalid
signatures, inconsistent module releases and an unexpected key fingerprint. It
cannot select a newer moving kernel as a fallback. RPM inspection, GPG and DNF5
are explicit tooling prerequisites; preparation never installs controller tooling.
The retained RPM/key/policy/lock objects travel in CAS and through operation backup.

Available controller commands, using its existing selected private state:

```sh
quirkbench dev recovery inputs candidate-spec --candidate fedora44-pairing-v1 \
  --repository /absolute/reviewed.repo --repository-id SELECTED_REPO_ID \
  > /absolute/reviewed-candidate.json
quirkbench dev recovery inputs acquire-plan /SELECTED_STATE/inputs/new-generation --spec /absolute/reviewed-candidate.json
quirkbench dev recovery inputs lock /SELECTED_STATE/inputs/new-generation/rpms \
  --public-key /absolute/fedora-signing-key \
  --builder-image-digest sha256:ACTUAL_DIGEST \
  --diagnostics /SELECTED_STATE/inputs/new-signature-run
quirkbench dev recovery inputs recipe --lock ACTUAL_LOCK_SHA256 \
  --builder-image-digest sha256:ACTUAL_DIGEST --epoch RECORDED_EPOCH
```

`acquire-plan` records the managed generation but downloads nothing. Execute its
returned wrapper argv in the controller environment before `lock`; pass the returned
RPM directory. Diagnostics must be a fresh managed directory. See [retention and
settings](local-state-maintenance.md). Pin the retained builder archive's
`input:SHA256` owner if preparing multiple generations before image admission.
Raw input and recipe histories are counted separately; successful signature staging
does not consume an extra slot. The other commands inspect actual retained inputs.
Replace placeholders with recorded
identities. Download/image production are separate explicitly requested product
operations. Unavailability of the selected immutable candidate blocks preparation.
No source catalog, SRPM, candidate source archive, exclusion fragment or candidate
build record is a recovery prerequisite.

The existing synthesis API dispatches v2 through stock installation, payload/module
checks, runtime installation, generic dracut generation and archive/storage audits.
It then feeds the existing GRUB/GPT assembler and checksum/signature machinery.
There is no compiler invocation in the stock branch. Its cache namespace and
identities differ from the retained custom-kernel cache.

## Storage and setup

The initial guard supports x86-64 UEFI with **one attached USB whole disk**. Multiple
USB disks, unexpected role identifiers, non-512-byte sectors, invalid/overlapping GPT
geometry or changed backing devices block authorization. This bounded platform
profile is not universal hardware support. Disconnect unrelated USB storage before
using it; do not relax the guard to get past an ambiguity diagnostic.

Reviewed initramfs/udev rules authorize only expected boot-device partitions before
root discovery. Vendor filesystem-discovery rules and automatic storage/firmware
writers are masked. Swap/resume, automatic mounts, filesystem repair, GPT auto-mount,
EFI pstore writes and hibernation-variable updates are disabled. Recovery stays
read-only, with runtime diagnostics in RAM. Library/experiment availability does
not gate evidence uploads.

Commission the positively identified external medium through the existing explicit,
journaled operator flow. After the evidence filesystem is mounted, configure networking
with `nmtui` and stage a complete private bundle in `/run` (RAM) or verified
`evidence/control`. Its `runtime.json` keeps the existing provisioning fields; each
trust/credential filename must be a direct bundle filename. Provision controller
CA/hostname trust, device token, target binding, and repository CA, public signing
key and matching TLS client certificate/key. Recovery-only configuration can have
an empty repository mapping; experiments require a usable repository.

For first setup without a shell, record the target system UUID shown on the
recovery console and use it in the bundle's `target_binding`. After commissioning,
shut down the target and
stage the complete bundle under `control/setup` on its explicitly selected evidence
partition using the controller. Keep files private. Safely unmount that medium,
boot recovery, configure networking with menu item 1, then select menu item 4.
That action stops the existing supervisor, runs the same validated activation
against `/var/lib/quirkbench/evidence/control/setup`, and restarts recovery-only
reporting. It rejects a changed generation requiring maintenance; it is not an
enrollment wizard. Failed activation retains the previous usable configuration.

With an operator shell, stop the target supervisor before activation:

```sh
systemctl stop quirkbench-supervisor.service
PYTHONPATH=/usr/lib/quirkbench python3 -m quirkbench.provisioning /run/quirkbench-setup
systemctl start quirkbench-supervisor.service
```

A changed generation additionally requires explicit `--maintenance`. Activation
validates the complete generation, uses private immutable files, fsync/atomic
activation, preserves the previous generation and shares the runtime/journal locks.
Pending work or an unresolved claim request blocks activation. It cannot silently
retarget already bound media. TLS trust and usable public signing material are
validated before activation. Guided initial pairing is implemented in the recovery console; full native M2
acceptance remains open.

Runtime revalidates mounted p6 and actual journal/spool/private destination devices
before mutation. A removed evidence mount or a nested mount redirecting control data
blocks writes. Storage-failure diagnostics use `/run`; this does not claim evidence
was durably retained.

## Recovery-only and attended experiments

Stock recovery defaults to recovery-only mode. It registers status, reconciles
existing attempts and uploads recognized sealed evidence with resumable acknowledgements.
It refuses fresh claims, deployment preparation and candidate arming. Private control
files beside the evidence journal are never treated as uploadable attempt evidence.

Enable an attended experiment runtime through an explicit temporary systemd service
override adding `--allow-experiments` to the existing target service command. Keep
systemd as the sole runtime owner; do not run a competing target process. This
switch permits preparation, while exact local operator approval still gates boot.
Use the existing pinned source/build/composition adapters and candidate profile
review; preserve matching symbols and build provenance.

```sh
quirkbench run show ATTEMPT_ID
quirkbench run approve ATTEMPT_ID --request-id UNIQUE_DECISION_ID
quirkbench run reject ATTEMPT_ID --request-id DIFFERENT_DECISION_ID
```

Approval binds the attempt, claimed target/media, immutable experiment/deployment,
revision, lease and controller epoch. It is recorded outside agent proposals.
New CLI/external-agent physical submissions require `operator-approval.v1`;
legacy submission remains the explicit compatibility API. An older runtime cannot
execute approval-required work. The authenticated attempt-scoped status route grants
no operator authority. Changed bytes/identity, rejection, pause, expired ownership
or unsupported capability prevent `BOOT_PENDING`. Each subsequent attempt requires
its own approval. Interrupted arming is reconciled without repeating boot/execution.

Focused fixtures join recovery → approval → candidate → streamed/final evidence →
recovery over local HTTPS with fake privileged adapters. This establishes software
behavior, not real boot compatibility or internal-storage preservation.

## Durable workers and image publication

Stock rootfs intent v2 binds the builder archive/config and rootfs lock, with
no candidate catalog. A complete image operation additionally binds its v2 recipe;
its dracut/runtime/unit references are retained transitively in the same CAS tables. The existing single lifecycle owner dispatches the fixed
bounded container worker. Logs and stage completion stay private. The current
owner stops/verifies the complete worker container, rechecks installed lock/package closure,
stock modules and tree identity, then transactionally publishes an audit with the
exact claim/deadline and live owner fence. Native coordinator RPM inspection is an
explicit prerequisite, with confined database paths, bounded output and elapsed
time; a dedicated verifier can be injected.

Rootfs audit adoption **does not complete `image_prepare`**. Its retained inputs and
audit survive backup; the private installed rootfs directory does not. Interrupted
work therefore needs explicit reconciliation and reconstruction from retained inputs.
The complete-image fixed worker now runs stock installation, runtime and dracut
stages and the existing assembler inside the pinned builder. Signing keys and
writable controller state are not mounted there. The current coordinator stops the
whole unit once, journals exact stop proof, independently reconstructs the candidate
from staged bytes, signs/verifies outside the worker, checks imported CAS hashes
against the verified bytes and publishes terminal state under the exact live claim.
A collected transient unit is not mistaken for an unverified stop. Expiration before
or during completion becomes a durable failed operation; ownership loss stays fenced.
Completed image/manifest/checksum/candidate/signature artifacts are retained in CAS;
private staging directories remain omitted from backup. An adopted rootfs alone is
not a delivered recovery image or a backup-complete private workspace.

Configure the authenticated foreground controller and recovery worker/signing
inputs using [controller installation](controller-installation.md), then start its
existing lifecycle before submitting work:

```sh
quirkbench admin controller run
```

The configured runtime supplies its packaged worker entry points. Recovery signing
still requires the operator's actual signing home, public key and full fingerprint;
starting the controller does not invent those inputs or authorize a target run.

In a second terminal, admit an explicitly requested image operation after retaining
actual inputs:

```sh
quirkbench dev recovery submit --recipe ACTUAL_RECIPE_SHA256 \
  --builder-archive ACTUAL_OCI_ARCHIVE_SHA256 --request-id UNIQUE_IMAGE_REQUEST
```

Earlier-owner queued operations remain visible without automatic adoption and do
not stop the service. An interrupted worker requires the existing explicit owner
reconciliation/resume contract; do not edit epochs or reuse its partial stage.

The optional executor only advances admitted full recovery-image operations. It uses
the existing foreground lifecycle owner and bounded containers while HTTPS stays responsive; it does
not schedule agent decisions or cancel rootfs-only work. Restart leaves interrupted
operations for explicit reconciliation/resume, rather than automatically rebuilding.
Use `admin operation show/watch/events/output` for durable executive progress and the
recorded private stage log for compile/tool stdout. Use the manual monitor and recorded logs; do not launch popup viewers or waiting agents.

## Automatic first-boot hardware report

Recovery now invokes the shared passive collector when the authenticated target
service starts, including in recovery-only mode. It sends HardwareInventory v2
through the existing authenticated registration route, before any experiment claim.
No information-gathering kernel needs to be built. The controller retains canonical
validated report bytes in CAS with their actual target, boot and media context;
lost registration acknowledgements replay without duplicate evidence.

V2 adds PCI/USB modalias and bound-driver observations, ACPI identifiers, I2C/HID
observations, selected non-serial DMI model/BIOS metadata and first-logical-CPU
identity/features to v1's architecture, firmware mode, memory and device identifiers.
These describe the recovery environment, not installed-OS drivers or configuration.
CPU features are a sample, not proof of features on every logical CPU. No block
device contents, serial numbers, MAC addresses, firmware blobs or active probes
are collected. Empty optional ACPI properties are recorded as absent.

The runtime uses a 512 KiB report budget and a cooperative 10-second collection
deadline inside the existing bounded registration envelope. Exceeding budgets
produces explicit partial status. Collection failure does not stop retained evidence
uploads; preparation shows a blocker instead of inventing missing observations.
V1 reports and the standalone collector's existing default remain supported;
`python3 -m quirkbench.inventory --extended` emits v2 for manual diagnostics.

On the controller, retrieve the report and deterministic candidate planning facts:

```sh
quirkbench --state /absolute/controller-state target inventory TARGET_ID --json
```

The response includes the report/digest, recovery boot context, HardwarePlan, exact
baseline-input availability and explicit preparation blockers. Historical recovery
reports remain readable after a candidate boots, but are marked non-current.
Unsupported/partial inventory, missing target/media binding or unavailable retained
candidate inputs do not become ready implicitly. This command queues no build and
grants no execution approval. Later experiment creation consumes these observations
alongside pinned sources, reviewed profiles, operator goals and dependency resolution;
modalias/driver strings are descriptive data, never shell commands or approval to
relax experimental storage exclusions.

## Released factory acquisition

`quirkbench recovery download` admits the matching signed installed-controller v2
factory set using the existing native background owner. Machine calls require
`--request-id ID --json`; `--trust-bundle PATH` explicitly selects independently
provisioned trust, never a key from the download. Missing production trust is
UNAVAILABLE. Inspect the returned operation/status command and manually open
`quirkbench monitor` for progress. Interrupted acquisition requires confirmed worker
stop and explicit ordinary admin operation resume; retries retain exact intent.

A successful operation retains the released-recovery-acquisition v1 index and its
public factory/metadata/signature digests. The image is an ordinary retained CAS
file at the configured state's `artifacts/objects/IMAGE_SHA256`; use a standard image
writer separately. `quirkbench recovery list [--limit N] [--before CURSOR] [--json]`
lists these acquired sets alongside existing prepared images, with retained publisher
statement/signature paths and full fingerprint. It validates bounded retained metadata,
references and local asset sizes. It reports publication verification as historical;
it does not rehash the whole image or reverify current publisher trust. Missing,
linked or inconsistent retained objects are unavailable. Verify image bytes and
signature with independently provisioned trust before writing confirmed external media. This
command supplies no media writer, device selection, flash approval or qualification.
Production publication/native commissioning and stock recovery crypto RPM inputs
remain acceptance requirements.

## Attended original evidence drain

**Connection details → Review original evidence** requires verified recovery/evidence storage and the original
hardware binding. Enter `plan REQUEST_ID` to freeze at most 128 unacknowledged
original records/1 GiB. The public plan is retained under
`evidence/control/evidence-drain/plans/REQUEST_ID_SHA256/plan.json`; the console
reports its path and selected versus additional retained counts. Preserve the same
request ID for retries. A new selection needs a new explicit request.

On the original controller, explicitly revoke the original generation, pause and
reconcile all target work and confirm whole-worker stops using the existing lifecycle
commands. Then `quirkbench target evidence approve TARGET --file PLAN --request-id ID`
produces a private credential file. Stage that exact file privately as
`evidence/control/setup/GRANT_ID.json` (a credential: use a private file or
its enclosing secret store). Enter
`drain REQUEST_ID GRANT_ID` in choice 7 to use only its selected manifest and original
attribution. Each invocation has a 120-second batch deadline; interruptions retry
the same selection/grant without widening or extending authorization.

This action stops and restarts the existing supervisor around local maintenance.
It preserves original pending attempt/result/blobs and unselected records, and repairs
selected acknowledgments after lost responses. It does not complete an attempt,
clear one-shot state, retarget hardware or authorize execution/shutdown. Changed
hardware is blocked. Native commissioning and fresh runtime image checks remain open.

## Retarget invitation prerequisite

See [attended safe shutdown](#attended-safe-shutdown) before moving or disconnecting
media. Retargeting and shutdown remain separate explicit transactions.

After explicit original-generation revocation and reconciliation/whole-worker stops,
the controller can issue `quirkbench target reassign OLD --generation EXACT
--new-name NAME --new-uuid ACTUAL_NEW_UUID --request-id ID`. It retains original
media and scopes the invitation to that different UUID. Compare the full controller
certificate fingerprint through the independently attended path. Retarget-only
exchange returns an authenticated v2 reply carrying exact old/new scope; initial
console pairing/activation refuses that reply. This controller prerequisite alone
does not implement local retarget activation, clear one-shot state, move old evidence
or transfer reset/watchdog/attempt approval. The full paused local transaction remains
unavailable pending its implementation and native commissioning.

The implemented attended recovery menu now offers **Explicitly retarget enrolled
media to this hardware**. Confirm the exact old target and actual new UUID, then
compare and approve the full controller certificate fingerprint before entering
the retarget invitation. Reuse the local request ID after interruption; its private
new key is retained. The software facade supports repeated moved-media retargets and exact replay.
For an expired pending retarget invitation, enter the new code ID under the same
local retarget request and explicitly confirm replacement; the old key remains
recoverable. An archived invitation requires explicit original-key resume. A remote
COMPLETE generation requires explicit revocation and work reconciliation before
replacement; local maintenance cannot cancel it. Retarget grants no candidate or reset authority.

The evidence-drain screen accepts explicit `archived-plan RETARGET_ID PLAN_ID` and
`archived-drain RETARGET_ID PLAN_ID GRANT_ID` after a strict current completed retarget, selecting any completed linked history
member explicitly.
Original evidence uses its original target/generation/attempt attribution and
explicit scoped grant; new work and one-shot state remain unchanged. Native image
commissioning is still required for the changed recovery inputs.

## Attended safe shutdown

On the controller, explicitly request shutdown of the enrolled, registered target:

```sh
quirkbench target shutdown request TARGET --request-id shutdown-1
quirkbench target shutdown status TARGET --json
quirkbench monitor INVESTIGATION --once
```

The first command atomically pauses all target campaigns and fences new attempts
and resume. Existing target evidence/recovery exchanges remain available so current
work can drain. Remaining attempts, worker units and library maintenance block
execution. The immutable intent binds target, current credential generation, media,
hardware UUID and the exact reported boot. Only supported recovery on that same boot
can accept it through the existing authenticated controller service and lifecycle
owner. A candidate must return and reconcile first; its changed recovery boot needs
an explicit replacement request, never an automatic grant:

```sh
quirkbench target shutdown request TARGET --request-id shutdown-2 --replace shutdown-1
```

An exact request retry returns its original receipt, even after credentials or
readiness change. Use `poweroff-status` for current observations. It reports REQUESTED,
DELIVERED or PREPARED, with admission, unresolved work, remaining workers, boot match,
sealed local evidence and upload backlog separately. PREPARED means the controller
accepted preparation; physical poweroff and safe removal remain unverified.
A disconnected or stopped controller cannot prove either outcome.

**Power → Prepare safe shutdown** supplies
independent local authority when offline. Type `poweroff FULL_BOOT_DISK_GUID` to
confirm the displayed boot device. A failed remote exchange never supplies that
confirmation implicitly. Local shutdown needs no controller acknowledgment of
uploads and preserves their original journal, result, keys, sealed bytes and upload
offsets. Unpaired/manual recovery retains this local path. Private runtime inputs
must remain beneath evidence/control; ambiguous identity, nested mounts, unsafe
files, unknown claim replies, running recipes or unreconciled arming block shutdown.

Both paths use the existing supervisor and configuration/agent ownership. The remote
path verifies the current native supervisor PID/cgroup, releases its shared runtime
configuration ownership and takes exclusive locks; it does not stop its own unit.
The local path stops that unit and verifies whole-group shutdown before taking the
same locks. Every explicit retry freshly verifies recovery storage, clears and reads
back the USB one-shot boot selection, verifies and fsyncs each sealed evidence object
and its journal, and rechecks the original source and lock identities. Ordinary
`systemctl poweroff` owns final orderly unmount and watchdog handling. There is no
forced/lazy unmount, forced poweroff, new service or direct watchdog manipulation.

A durable local shutdown fence blocks runtime startup before credentials/watchdog
activation and blocks configuration/network maintenance and new target work. An
atomic `shutdown/active.json` stores the complete authoritative continuation;
per-request history is written afterward, so a failed history write cannot release
restart admission. Native stopped-unit checks include empty descendant cgroups.
Self-owned native calls have ten-second timeouts and feed the existing service
heartbeat between calls, within the installed thirty-second watchdog interval.
Original ordered result declarations must exactly match the verified sealed inventory.
An interrupted preparation never automatically executes after reboot. On the same boot,
choose **Power → Prepare safe shutdown** and explicitly retry the exact request. A changed boot requires
explicit local cancellation/reconciliation first. Type `cancel REQUEST_ID` at that
console to remove only the local fence, retaining its history and evidence. Same-boot
cancellation refuses a poweroff request that may already be queued. The supervisor
is not automatically restarted. Reconcile controller state separately before
explicitly starting the supervisor or resuming an investigation.

For an unprepared remote request, cancel its controller admission fence explicitly:

```sh
quirkbench target shutdown cancel TARGET --request-id shutdown-1
```

This does not clear any local fence or resume investigations. A PREPARED request
cannot be cancelled remotely because poweroff may already be queued; confirm locally
and use a reconciled changed-boot replacement. Cancelled request identities cannot
be reactivated; use a new explicit request. Version 1 intent, preparation, receipt,
status, cancellation and private continuation records have strict
[schemas](../schemas/shutdown.v1.schema.json) and [examples](../examples/shutdown.json).
The additive `target-shutdown.v1` capability and registration `shutdown_protocol: 1`
negotiate the routes; legacy/static clients keep their existing protocol and use
attended local shutdown.

Software regressions inject native service/GPT boundaries. They establish neither
native shutdown nor watchdog qualification. Before removing media, confirm actual
physical poweroff locally. Pending uploads can remain durable on the USB and are
never described as acknowledged. Native acceptance remains the separately authorized
operator gate [#43](https://github.com/esper256/quirkbench/issues/43).

## Foreground image generation

`dev recovery build-recipe` generates an unsigned stock recovery image without systemd,
a running controller, target enrollment or signing credentials. It uses the same
verified RPM inputs, DNF5 installroot, dracut audits and regular-file GRUB/GPT
assembler as managed image preparation. Docker and local Podman are supported
container engines; no privileged container or physical device mount is requested.
The controller and its workers have no host systemd requirement. Target images
retain systemd as their normal Linux init system.

First build the [Fedora tool image](../environments/README.md), inspect its exact
local configuration ID (`docker image inspect --format '{{.Id}}' IMAGE` or
`podman image inspect --format '{{.Id}}' IMAGE`), and use that `sha256:...` identity
for the existing `dev recovery inputs lock` and `recipe` commands above. The builder
must be Linux amd64 with no entrypoint. Keep the selected RPM closure and public
key; [retained RPM replay](recovery-rpm-replay.md) works when moving repositories
have dropped exact versions. Locking verifies every RPM against the selected key.

```sh
quirkbench dev recovery build-recipe --engine docker \
  --store /SELECTED_STATE/artifacts --recipe ACTUAL_RECIPE_SHA256 \
  --builder-image sha256:ACTUAL_CONFIG_ID \
  --output /absolute/new-image-build --memory-gib 4 --timeout 3600
```

The default CPU cap reserves half the visible CPUs, up to four; `--cpus` selects
an explicit cap. The builder needs at least 4 GiB and enforces the existing
half-host CPU/RAM reserve, so smaller hosts should use suitable limits before
building. `--free-space-reserve-gib` defaults to 2 for foreground builds; dracut
staging checks that reserve continuously, and assembly additionally requires twice
the image's size for image/temporary files. This is independent of the controller's
state-storage reserve. Existing managed build callers retain their existing default.

Podman preserves its configured `systemd` or `cgroupfs` manager; a deliberate
`--podman-cgroup-manager` override is available. The retained build record pins
that manager for cleanup. Before payload release, the owner verifies the actual
container cgroup's CPU, memory, zero-swap and PID limits. Cleanup requires both
exact engine shutdown and an empty recorded cgroup, including descendants.
Unsupported delegation fails with retained diagnostics rather than relaxed limits.
The output directory must be new; checkout-local paths and ordinary ancestor aliases are allowed. The command copies only the
recipe's selected inputs, mounts those copies read-only, runs an offline bounded
container, verifies it has stopped, and exports `recovery.img`, its manifest,
checksum and unsigned candidate record. The image checksum is checked after export.
The container's deadline remains active if the terminal disappears. `build.log`
and `build.json` record diagnostics and the unique container identity. A successful
build removes its stopped container. Failure retains it for diagnosis, with no
completed-build claim; use the recorded engine's `cp`/`logs` commands to retrieve
private staging diagnostics if needed. Explicit cleanup stops/verifies/removes
only that recorded container and preserves files:

```sh
quirkbench dev recovery cleanup /absolute/new-image-build
```

A generated image is unqualified and untested on hardware. Foreground generation
neither signs/publishes a release nor authorizes flashing, commissioning or an
experiment. Existing managed operations and their stored records remain compatible.


Explicit build/output paths may be inside a checkout. Ordinary ancestor aliases
are resolved once when selected; existing outputs and protected system destinations
remain rejected. This does not make the output directory disposable. Foreground
cleanup stops only its recorded container. The low-level `dev image assemble` command assembles
in managed staging and exports the image plus `.sha256` and `.json` sidecars without
overwriting files. Its selected output parent must already exist. An interrupted
export may leave completed files for inspection; select a new destination to retry.

For the controller-free prepare/verify/build workflow and portable input snapshots,
see [recovery input bundles](recovery-input-bundles.md). Existing low-level commands
remain available for diagnosis.


## Controller USB preparation

`quirkbench recovery prepare` plans and applies the final layout of an explicitly
selected whole USB. It requires a compatible controller-prepared v3 artifact;
older images return an actionable fresh-build requirement and remain readable by
the historical boot path. Stock image generation has not yet switched to v3 while
the new console journey is being integrated.

Configure and run the controller first. Select its reachable LAN endpoint, then
plan without writing USB bytes:

```sh
quirkbench --state /absolute/controller-state recovery prepare \
  --image /absolute/recovery.img --device /dev/SELECTED_USB \
  --target ACTUAL_TARGET_NAME --plan-out /absolute/new-usb-plan.json \
  --public-key /absolute/publisher.asc --fingerprint FULL_VERIFIED_FINGERPRINT
```

An unsigned local development build uses `--unsigned-development` explicitly
instead of publisher arguments. Production publisher trust is never synthesized.
Review the exact device bytes, capacities, old-layout description and confirmation
reference. Apply the returned command with `--plan`, `--confirm` and `--erase`.
The erase acknowledgement covers **all existing USB data, evidence and credentials**.
Both invocations need access to the narrow sudo device helper; the controller itself
runs as the user. No prompt selects or approves a device.

Remaining capacity is split equally between experiments and evidence after fixed
artifact extents, alignment/GPT and the empty library's filesystem overhead. No
RAM sizing requirement is imposed. Preparation stages the configured controller's
public trust and a non-expiring, single-use invitation for the selected target name;
it copies no controller private key. Supported trust maintenance is fenced while
final staging/writing is active. Explicit invitation cancellation remains available;
a cancelled invitation prevents a successful final preparation response.

A fresh staging directory retains components, handoff, invitation ID and failure
information outside the checkout by default; `--output` selects a new directory.
Preserve it after failure. Completion is unconfirmed after a short write, sync,
readback, deadline or attachment failure, even if a final marker is readable. Retry
by observing a fresh plan and explicitly acknowledging erasure again. Staged
invitations are never silently renewed; cancel unwanted ones using the existing
`target pairing cancel` action. Preparation is not a secure erase or physical boot
qualification. The software gates use regular-file destinations and explicit test
trust; actual USB writing/boot remains separately authorized acceptance.

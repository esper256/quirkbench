# Recovery operations

Current low-level stock-recovery commands, manual setup and initial console pairing.
The complete guided journey remains unfinished; see
[GitHub tracker #29](https://github.com/esper256/quirkbench/issues/29) and the
[acceptance guide](installation-to-patch.md).
Software support does not establish image or hardware qualification. Use
[controller installation](controller-installation.md) and explicit
[acquisition specifications](recovery-acquisition.md) before admitting work.

For initial pairing on verified recovery, configure temporary networking with
**Network**, then choose **Connect to controller**. Supply the endpoint from
controller `target add NAME`; compare and type the full displayed certificate SHA-256
before entering its code ID and one-use code. The console retains its own private
key/request before exchange, stops the existing supervisor for activation and
restarts it afterward. Pairing grants no candidate or attempt approval. Missing
native `openssl`/`gpg`, unknown clock, mismatched trust or an active configuration
block this initial path. The current recorded stock RPM candidate needs an updated,
exact `openssl` package input before native pairing acceptance. Staged manual setup
remains available. After pairing/manual activation, **Save selected network connections**
lists only RAM connection filenames; choose the numbered connections to retain
privately. Only supported Ethernet/Wi-Fi profiles are saved. Passwords stay outside
public artifacts. Later recovery/candidate boots check actual target/media/binding
and active configuration before replay into private RAM. Changed or invalid selections
stay blocked; uncertain replay cleanup blocks NetworkManager. Local Network setup
remains available for clean pre-write rejection or complete rollback. Lifecycle
maintenance for active enrollments remains unfinished. A lost COMPLETE reply can
retry the same request/key. For an expired unredeemed invitation, obtain a new
controller invitation and enter its code ID in **Connect to controller**. Confirm
replacement of the displayed pending request; its original key stays in private
history. Entering an archived invitation ID offers explicit original-request resume.
An already completed controller redemption blocks a second identity; recover the
original request or use explicit controller lifecycle maintenance. These initial-only
choices refuse prior activation evidence, target work and changed trust/binding.

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
quirkbench recovery-inputs acquire-plan /SELECTED_STATE/inputs/new-generation --spec /absolute/reviewed-candidate.json
quirkbench recovery-inputs lock /SELECTED_STATE/inputs/new-generation/rpms \
  --public-key /absolute/fedora-signing-key \
  --builder-image-digest sha256:ACTUAL_DIGEST \
  --diagnostics /SELECTED_STATE/inputs/new-signature-run
quirkbench recovery-inputs recipe --lock ACTUAL_LOCK_SHA256 \
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
quirkbench attempt status ATTEMPT_ID
quirkbench attempt approve ATTEMPT_ID --request-id UNIQUE_DECISION_ID
quirkbench attempt reject ATTEMPT_ID --request-id DIFFERENT_DECISION_ID
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
bounded systemd/Podman worker. Logs and stage completion stay private. The current
owner stops/verifies the complete service, rechecks installed lock/package closure,
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

Start the existing authenticated controller service with explicit worker/signing
configuration **before admitting work**, so admission belongs to its current durable
execution owner. All other server/TLS arguments remain required:

```sh
quirkbench serve --cert /absolute/server-cert --key /absolute/server-key \
  --tokens-file /absolute/private-tokens \
  --recovery-worker /absolute/installed/bin/quirkbench-worker \
  --recovery-signing-home /absolute/private-signing-home \
  --recovery-public-key /absolute/trusted-release-key \
  --recovery-fingerprint ACTUAL_FULL_FINGERPRINT
```

In a second terminal, admit an explicitly requested image operation after retaining
actual inputs:

```sh
quirkbench recovery-image --recipe ACTUAL_RECIPE_SHA256 \
  --builder-archive ACTUAL_OCI_ARCHIVE_SHA256 --request-id UNIQUE_IMAGE_REQUEST
```

Earlier-owner queued operations remain visible without automatic adoption and do
not stop the service. An interrupted worker requires the existing explicit owner
reconciliation/resume contract; do not edit epochs or reuse its partial stage.

The optional executor only advances admitted full recovery-image operations. It uses
the existing lifecycle owner and user services while HTTPS stays responsive; it does
not schedule agent decisions or cancel rootfs-only work. Restart leaves interrupted
operations for explicit reconciliation/resume, rather than automatically rebuilding.
Use `operation status/watch/events/output` for durable executive progress and the
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
quirkbench --state /absolute/controller-state target-inventory TARGET_ID --json
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
stop and explicit ordinary operation resume; retries retain exact intent.

A successful operation retains the released-recovery-acquisition v1 index and its
public factory/metadata/signature digests. The image is an ordinary retained CAS
file at the configured state's `artifacts/objects/IMAGE_SHA256`; use a standard image
writer separately. `quirkbench recovery-images [--limit N] [--before CURSOR] [--json]`
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

Recovery console choice 7 requires verified recovery/evidence storage and the original
hardware binding. Enter `plan REQUEST_ID` to freeze at most 128 unacknowledged
original records/1 GiB. The public plan is retained under
`evidence/control/evidence-drain/plans/REQUEST_ID_SHA256/plan.json`; the console
reports its path and selected versus additional retained counts. Preserve the same
request ID for retries. A new selection needs a new explicit request.

On the original controller, explicitly revoke the original generation, pause and
reconcile all target work and confirm whole-worker stops using the existing lifecycle
commands. Then `quirkbench target drain-approve TARGET --file PLAN --request-id ID`
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
the controller can issue `quirkbench target retarget-code OLD --generation EXACT
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
quirkbench target poweroff TARGET --request-id shutdown-1
quirkbench target poweroff-status TARGET --json
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
quirkbench target poweroff TARGET --request-id shutdown-2 --replace shutdown-1
```

An exact request retry returns its original receipt, even after credentials or
readiness change. Use `poweroff-status` for current observations. It reports REQUESTED,
DELIVERED or PREPARED, with admission, unresolved work, remaining workers, boot match,
sealed local evidence and upload backlog separately. PREPARED means the controller
accepted preparation; physical poweroff and safe removal remain unverified.
A disconnected or stopped controller cannot prove either outcome.

Verified recovery console choice **10 — Prepare attended safe shutdown** supplies
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
choose console 10 and explicitly retry the exact request. A changed boot requires
explicit local cancellation/reconciliation first. Type `cancel REQUEST_ID` at that
console to remove only the local fence, retaining its history and evidence. Same-boot
cancellation refuses a poweroff request that may already be queued. The supervisor
is not automatically restarted. Reconcile controller state separately before
explicitly starting the supervisor or resuming an investigation.

For an unprepared remote request, cancel its controller admission fence explicitly:

```sh
quirkbench target poweroff-cancel TARGET --request-id shutdown-1
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

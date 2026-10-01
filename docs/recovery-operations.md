# Recovery operations

Current low-level stock-recovery commands and manual target setup. The guided
user journey remains planned; see the [implementation checklist](installation-to-patch.md).
Software support does not establish image or hardware qualification. Use
[controller installation](controller-installation.md) and explicit
[acquisition specifications](recovery-acquisition.md) before admitting work.

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
validated before activation. Guided enrollment is not yet implemented; it is planned in M2.

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

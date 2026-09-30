# Recovery inventory: first-device handoff

> **Local artifact reset, 2026-09-30:** the user authorized permanent deletion of
> the checkout-local `.quirkbench` tree, including delivered images, signing keys,
> retained inputs and local validation/qualification logs. Historical identities
> and results below remain records of those runs; their bytes are no longer
> available. No replacement image or new qualification is supplied by the reset.
> See [current local state](local-state-maintenance.md).


Updated 2026-09-30. This handoff concerns the stock recovery product operation and
first attended hardware report. It does not record boot or release qualification.

## Delivered software

Recovery collects bounded passive HardwareInventory v2 automatically when its
manually configured authenticated target service starts. It reports PCI/USB device
IDs, modaliases and bound drivers; ACPI/I2C/HID observations; selected non-serial
machine/BIOS identity; CPU identity/features; memory, architecture and firmware mode.
It reads kernel metadata rather than internal filesystems. Collection errors or
limits remain explicit; retained evidence upload still works without an experiment.
The controller validates and retains the report with its target, recovery boot and
external-media context before acknowledging registration. Replay and restart keep
that association durable. Legacy inventory v1 remains supported.

Read the actual report and candidate preparation blockers on the controller:

```sh
quirkbench --state /absolute/controller-state target-inventory TARGET_ID --json
```

This does not create or approve an experiment. First-experiment implementation can
consume the retained report, with pinned candidate sources and a reviewed profile.
Hardware observations do not authorize inclusion of internal-storage controllers.
Partial inventory or unavailable candidate inputs block preparation explicitly.

## Current flash artifact

The final operation `4c1077c200d74818b10bc4cae9daf43f` succeeded. The exported, signature-verified
bundle is `.quirkbench/deliveries/stock-recovery-first-investigation-20260930/`.
Its `HANDOFF.md` records the exact image, checksum, signing fingerprint, disk GUID
and first-device steps. Image SHA-256 is `30511eddc1730e7b1fe123d5b685a0585158ae20ab7f0dff3609ba029e5425cb`.
The earlier signed inventory image and failed private stages remain retained;
use this final bundle for console-assisted setup. No physical flashing, target boot
or real target report has occurred. Qualification remains **unqualified**.

## Image and first-device operation

The recorded image run directory is
`.quirkbench/inputs/stock-recovery-inventory-20260930/`. Its
`run-record-v10.json` records the exact command, immutable inputs, bounded services,
worker stage and result location. The worker installs exact signed Fedora 44 stock
kernel/module RPMs, release `7.2.7-200.fc44.x86_64`, and assembles the existing
4,096 MiB GPT/GRUB factory image. It does not compile a recovery kernel.
Its native Konsole viewer shows the actual worker logs; closing it leaves the
worker running. Failed prior attempts and their diagnostics are retained.

Only a `SUCCEEDED` operation with coordinator-validated signed output is an image
handoff. The resulting release candidate remains **unqualified**. Image production
writes workspace artifacts; this work has not flashed a drive or booted a target.
Follow [the first-boot operator procedure](first-boot-operator.md) for checksum/key
verification, explicitly selected external media and journaled commissioning.
The initial platform is x86-64 UEFI, one attached USB whole disk and 512-byte sectors.

After commissioning, follow [manual authenticated setup](stock-recovery-attended.md#storage-and-setup)
with NetworkManager/nmtui, controller CA/hostname trust and provisioned credentials.
The recovery console offers menu item 4 to activate the fixed `control/setup`
bundle on the commissioned evidence partition, using the existing validated
activation and supervisor owner. Interrupted activation restarts the previous
usable generation; a changed generation requiring maintenance is rejected.
Recovery-only mode is the default and can report inventory without a repository
or an experiment. Keep that mode for the first report. Start the target service,
then retrieve `target-inventory` on the controller. Record the actual report digest,
target/media binding, completeness and boot diagnostics before using it to drive
first-experiment work. Software tests and a report collected from the development
OS are not evidence of reporting from the delivered recovery image.

## Minimal manually staged bundle

The `control/setup` directory on the commissioned evidence partition contains
`runtime.json`, the independently trusted controller CA file and the device token
registered for this exact `device_id` on the controller. Keep the directory and
credential files private. A recovery-only bundle needs no experiment repository:

```json
{
  "schema_version": 1,
  "device_id": "target-1",
  "controller_url": "https://controller.example:8443",
  "ca": "controller-ca.pem",
  "token_file": "device.token",
  "remotes": {},
  "target_binding": {
    "schema_version": 1,
    "system_uuid": "REPLACE_WITH_UUID_DISPLAYED_ON_RECOVERY_CONSOLE"
  }
}
```

Replace the example endpoint, device ID and binding with the real values. The
endpoint hostname must match the controller certificate. The token file contains
the actual separately provisioned credential (at least 32 characters). Menu item 4
validates the complete bundle and target identity, atomically activates it, then
restarts `quirkbench-supervisor.service`. It never creates trust or authorizes an
experiment. Retrieve the report using that same device ID.

## Validation and remaining evidence

Focused software logs are retained under
`.quirkbench/validation/recovery-inventory-20260930/`: `software-tests-final.log`,
`https-test-final.log`, `image-boundary-tests-final.log` and
`runtime-graph-tests.log`. The HTTPS test exercises authenticated recovery-only
registration and lost acknowledgement replay without an experiment attempt.
Storage/durable boundary review closed after the exact recursive installed-unit
link graph was enforced, including rejection of unknown nested drop-in links.
Native publication audits inspect staged regular files without following vendor
links into the controller, and query an explicitly selected disposable RPM database
copy rather than letting host defaults mutate the sysroot. Native host validation
confirmed the complete retained package closure with that corrected audit.
Stock rootfs audit v2 explicitly names the assembler's four existing factory
password-file hash exclusions; legacy audit v1 retains its original meaning.
Permissions remain restrictive, and the complete delivered image still has its own
byte digest. Native validation and signing passed in a focused retained-image
diagnostic before the final immutable run. Failed runs remain unpublished.

Real recovery boot, external-device commissioning, authenticated connection and
receipt of the first target report remain attended product observations to record.
Internal-storage preservation, compatibility and reset coverage remain separate
physical/release evidence. No release gates, QEMU cycles or hardware campaigns
were run for this implementation task.

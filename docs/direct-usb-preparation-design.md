# Direct USB preparation (#174)

Owner-approved design for [#174](https://github.com/esper256/quirkbench/issues/174).
Replaces capacity-sized staging in the
[recovery console plan](recovery-console-ux-plan.md); layout and pairing decisions remain.

## Choices and tradeoffs

**One preparation workflow; no preliminary flash.** Keep the existing read-only
plan and explicit erase/apply confirmation. Accept blank, previously flashed or
previously prepared USBs. Confirm the selected device, not a match between its old contents and the image.
This removes a user step while protecting against erasing the wrong drive.

**Copy content; create empty filesystems in place.** Reuse the existing assembler
for final GPT geometry and copy the fixed boot/recovery payload. Create mutable
filesystems directly on the selected USB, then add controller connection, public
trust and enrollment data. Preserve required factory contents rather than assuming
every mutable partition is empty. Keep the existing capacity split and zero shipped
library payload. This avoids Media Writer’s capacity-sized transfer and large staging files.
Filesystem creation still takes time; this is not secure erasure.

**Expand the existing helper narrowly.** Let the current bounded privileged helper
format and access only the intended partitions of the explicitly selected USB.
Retain device/attachment checks, mounted-device refusal, internal-disk protection,
parent-process ownership and whole-operation cancellation. Controller database
access and credential issuance remain in the normal user process; controller private
keys never enter the helper or USB. This replaces the helper's current prohibition
on native filesystem tools. It avoids a second service or running the controller as root. Update the contract
and review implementation before merging.

**Verify useful results, not empty space.** Synchronize writes and use one readback
of the copied boot/recovery payload to detect failed transfers. Check partition
geometry, filesystem metadata and the small configuration records needed to boot
and pair. Remove the second full-partition verification and all scans of unused
capacity. This catches preparation errors without an exhaustive media-health test or
local-code attestation. It does not test unused flash cells.

**Completion last; honest restart after failure.** Mark prepared media complete only
after filesystems, configuration and payload checks succeed. Interrupted media is
incomplete; retain diagnostics and require a fresh plan plus erase confirmation to
retry. Reuse existing enrollment cancellation/replay behavior. A resumable copy engine adds complexity
for a disposable operation. Existing evidence requires express erase approval.

## User-visible result and proof

Show phases such as “Writing recovery”, “Creating evidence filesystem” and
“Configuring pairing”, with byte progress for transfers and a clear completion
message. Do not present the operation deadline as an ETA.

Use focused joined tests for blank/already-flashed media, wrong-device refusal,
interruption and successful pairing handoff. Deterministic I/O counts must show
that bulk writes/readbacks scale with shipped content, not empty USB capacity.
Physical preparation and boot acceptance remain separate.

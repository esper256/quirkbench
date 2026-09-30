# First attended recovery boot

> **Local artifact reset, 2026-09-30:** the user authorized permanent deletion of
> the checkout-local `.quirkbench` tree, including delivered images, signing keys,
> retained inputs and local validation/qualification logs. Historical identities
> and results below remain records of those runs; their bytes are no longer
> available. No replacement image or new qualification is supplied by the reset.
> See [current local state](local-state-maintenance.md).


This procedure records observations of the delivered artifact. The revised
[storage policy](architecture.md#storage-protection-policy) permits stock recovery
controller enumeration while restricting storage operations to the boot device;
old custom-kernel artifacts keep their historical identity. Automated pairing is
not a prerequisite for later attended manual authenticated setup.

This procedure is for the signed, **unqualified** x86-64 UEFI factory image.
The first boot checks recovery and the external-drive commissioning plan. It
does not pair a controller, deploy a baseline, run a kernel experiment or
qualify hardware.

## Before flashing

Use the exact image path, SHA-256 and development signing fingerprint in the
image handoff. Verify the `.sha256` file against the image and the detached
`.checksums.json.sig` against `.checksums.json` with a separately retained
copy of the exported public key. Confirm that the key's full fingerprint
matches the handoff. The public-key sidecar beside an image is convenient to
copy; its location alone does not establish trust.

The factory file is 4,096 MiB. Commissioning later reserves at least 32,768
MiB each for experiments and the library. The evidence partition must hold
at least `ceil((4096 + 2 × target RAM MiB) / 0.8)` MiB, plus a few MiB for GPT
and alignment. A nominal 128 GB USB drive is generally sufficient for a
16 GiB RAM target; a 32 GiB RAM target needs a larger drive, such as 256 GB.
The target's actual RAM and disk geometry determine the final eligibility.

## First boot

1. With the target powered off, attach only the owner-selected USB media for
   this test. Disable Secure Boot and explicitly choose that USB device in
   the firmware boot menu. Do not change firmware storage settings for this
   image.
2. Keep the test offline. Observe `QUIRKBENCH_GRUB recovery`, then the
   `Quirkbench target recovery` console on the display or serial console.
3. Confirm recovery status and inspect its reported boot-device identity and
   storage policy. Internal filesystems must not be mounted or used for swap,
   repair or writes; console status alone cannot prove their bytes unchanged. The expected factory state reports recovery
   identity/evidence as pending until capacity setup is completed.
4. Choose menu item 3 to inspect the read-only commissioning plan. Confirm
   the selected disk is the booted external USB and compare the **entire disk
   GUID** with the image handoff. Review the planned partition boundaries and
   evidence capacity for this target's measured RAM.
5. Enter the full GUID only if the external disk and plan are correct. Press
   Enter to cancel otherwise. Record the console output, any refusal message,
   target model, RAM size and USB drive capacity for debugging.

Commissioning writes only after that local GUID confirmation. A successful
first boot and commissioning remain device-specific observations; the image
and software release stay unqualified until their separate gates.

## Hardware report before the first experiment

After commissioning, manual network setup and authenticated private configuration,
stage the private bundle under `control/setup` on the confirmed evidence partition
while attached to the controller, then boot recovery and use console menu item 4
to activate it. The supervisor starts in its default recovery-only mode. It automatically sends
bounded passive hardware observations to the controller; there is no preliminary
experiment kernel to compile. Read `quirkbench target-inventory TARGET_ID --json`
on the controller to inspect the report, boot/media context and preparation blockers.
Collection failure or partial observations remain visible while retained evidence
uploads continue. See [the current software handoff](stock-recovery-attended.md#automatic-first-boot-hardware-report)
for the observed properties, trust setup and limits. Do not enable candidate boots
merely to obtain inventory.

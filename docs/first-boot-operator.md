# First attended recovery boot

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
3. Confirm the recovery status and that the protected internal storage has
   not been mounted or changed. The expected factory state reports recovery
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

# Recovery boot check

Run this explicitly on a Linux host with KVM, QEMU, OVMF, GPT tools, mtools,
GRUB tools and ext4/FAT tools installed. It is outside the normal pytest suite.
It uses an already built recovery image; it does not download or build one.

```sh
.venv/bin/python boot_tests/check_recovery.py .quirkbench/images/IMAGE_ID
```

The check boots the actual EFI loader and generated GRUB menu from a regular
USB disk image. It starts a separate temporary controller, waits up to 120 seconds
for recovery to pair and connect, and requests real diagnostics. It checks that
recovery's live root is read-only and shared data is writable, using a 4 GiB VM. It saves a screen image
for reviewing the recovery console. No host disks are mounted or erased.

A successful result is reused when the image bytes, generated menu, test script,
QEMU version and firmware are unchanged. Use `--force` to repeat it. Failures keep
the VM disk, screen and logs under `.quirkbench/boot-checks`; successful checks
remove the large temporary disk files. Missing tools are errors, not skipped tests.
Run after changing recovery boot files or media setup, using the newly built image.
Normal CI remains fast and never invokes this check.

This checks recovery boot and connectivity. It does not establish candidate boot,
physical USB writes, laptop firmware compatibility or Wi-Fi support. Screen
appearance needs human review; the automated success condition is connection and
returned mount diagnostics, not matching a screenshot.

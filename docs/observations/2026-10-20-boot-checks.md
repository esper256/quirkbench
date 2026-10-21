# Earlier recovery and candidate boot checks

Historical observations for the image IDs below, not claims about the latest build.
See [current installation](../controller-installation.md).

## First real check

The October 20 QEMU check reproduced the GRUB shell with image
`75b9c229076e64a4c91c7b1c`. Image `057f965cfa62528821be00a2` fixes the EFI
loader's module lookup and passed recovery connection and mount checks; its
console showed “Connected.” Boot logs still contain non-blocking Fedora setup
attempts to write read-only `/etc`, including an authselect service failure.
These are not proof of a completely clean boot or candidate support.

Image `2e71871f635d7349c8c217f6` adds the recovery dashboard and passed a
manual QEMU check on October 20: controller connection, MCP inspection, read-only
OS, local terminal return and Activity view. Injected kernel messages appeared
on `tty1` while the dashboard remained on `tty2`; service messages remained in
the journal. Fast console integration tests cover menu interaction, resizing,
current work, failures and power controls without QEMU. Physical boot of this
image was subsequently reported working by the owner; physical candidate deployment
remains unverified.

## Candidate command check

The manual QEMU check passed with recovery `b051e208fc72ebe5d315a814` and candidate
`22850d03247d21175bb3481e35de7a2b17d183d12b329ba7ee1175a854d38b72`, derived from
Bazzite `sha256:5d49529cf809ff5b9e1ba52fd4833ec4099005673af4587b15d55b69210264e3`.
Recovery deployed the candidate, GRUB booted it once, and it reconnected. MCP
commands read the running kernel and mounts, then enabled `ostree admin unlock`
and wrote through its temporary `/usr` overlay. The requested return booted
read-only recovery, uploaded command output and diagnostics, and finished cleanup.

This check exposed and corrected ownership/mode loss during image import, the
candidate's missing `/boot` mount, and incomplete OSTree deployment cleanup.
The final progress-row UI changes have fast integration coverage; they are newer
than this image. No physical media was changed during this check. Physical
candidate boot, crash recovery and live package replacement remain unverified.

With explicit authorization, first build the recovery image, then select the USB
for erasure. Observe recovery boot and MCP inspection, candidate preparation,
one-time candidate boot, return, uploaded diagnostics and retained network settings.
Check read-only recovery mounts, fresh `/etc` and `/var`, GRUB clearing `next_entry`,
and the initial candidate connection timeout. Record image ID, candidate digest,
logs, observed USB usage and failures. A physical boot failure must be investigated,
not replaced with a passing fixture result.


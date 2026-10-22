# Independent rsync deployment: assembly feasibility checkpoint

This is a proposal and an unfinished experiment, not a supported backend.
It was inspected against main `4f615a1ac9d27c9e25932b7f3b0777bf24b46b82`.
The separately completed rsync transport prototype still deploys with OSTree
and does not satisfy the requested independent boot deployment.

## Proposed boundary

Keep the existing OSTree path as the default. Select the deployment backend
explicitly when preparing a candidate, record it in candidate provenance, and
require the target to advertise that backend. Never silently fall back.

For rsync, prepare an ordinary ext4 root image, its kernel and its matching
dracut live initramfs. Record the exact source commit, base image digest, assembly
recipe and tools identity, and the size and SHA-256 of every boot artifact in
the candidate identity. Bazzite's conversion belongs in its assembly recipe,
not the shared controller or target API. OSTree may remain a Bazzite build-time
tool; the rsync target deployment and boot path must not depend on it.

Use rsync on demand over the existing authenticated connection. Transfer into
an isolated reusable cache, then verify every artifact against the manifest.
A successful rsync process alone is insufficient evidence of completion.
Create a fresh writable root image for each deployment; never modify the
cached image, running root, settings or evidence. Publish its BLS entry only
after its files and configuration are complete and synced. Existing Recovery
boot selection remains shared and requires a separate explicit boot request.

Dracut supports an uncompressed `rootfs.img` under a selected live directory
and a writable linear device with `rd.live.overlay=none`. That offers a
conventional route without another boot manager or a custom initramfs hook.
Do not use `rd.writable.fsimg=1`: that would copy the writable image into RAM.
The exact candidate dracut version still needs qualification.

The small target boundary would own deployment, removal and access to the
deployment's saved journal. The common target lifecycle would continue to own
admission, cancellation records, boot attempts, explicit boot selection and
evidence receipt. Cancellation must reap writers and unmount temporary mounts
before cleanup. Cleanup must refuse an image still attached to a loop device.
Interrupted deployment must leave Recovery selected and no usable partial
entry. This is staging plus explicit boot selection, not atomic rsync or
transactional rollback.

No backend starts a daemon, scans a repository or prepares storage at Recovery
startup. The rsync sender and receiver exist only for a selected transfer.
Ordinary package size increases are acceptable; RAM image-copy cost is not
measured and must not be described as zero boot impact.

## Costs and unresolved assembly proof

The data partition is ext4. A fresh writable image normally requires a full
copy, even when rsync saves network bytes. It also requires full-image hashing
and an additional cached image. `/usr` changes in this ordinary writable root
would persist across repeat boots, unlike plain OSTree unlock's temporary
changes. These differences need explicit documentation and tests.

The experimental conversion checks out the prepared OSTree commit, creates
ordinary `/etc` and `/var`, disables OSTree-specific boot services, rebuilds
initramfs and populates ext4. It has not been run. This changes Bazzite's boot
assembly; copying a tree alone is not sufficient qualification.

The next gate is a small actual rootless-container metadata round-trip through
OSTree checkout and `mkfs.ext4 -d`, inspecting ownership, modes, hardlinks,
symlinks, real file capabilities and SELinux labels in the resulting image.
A `user.*` xattr test does not prove capabilities or SELinux preservation.
Rootless labeling is host- and policy-dependent, not inherently impossible.
If this gate succeeds, proceed with target/controller integration and fast
interruption tests, then a bounded Bazzite image/boot check. If it requires
custom metadata replay, patched upstream tools or privileged building, report
that concrete tradeoff before expanding the design.

## Available executor evidence

- Podman is absent. The executor maps only UID 1000; changing a temporary file
  to UID/GID 0 fails with `EINVAL`.
- `unshare -Ur true` fails because `/proc/self/uid_map` is read-only. Installing
  Podman alone therefore does not supply the intended rootless build environment.
- A temporary-file test successfully wrote and read `security.selinux` and
  `user.quirkbench`. This does not prove the container/image round-trip and
  explicitly rules out claiming that all label writes fail here.
- No security settings, owner state, media or hardware were changed.
- Independent review confirmed the conventional target boot approach and
  recommends the bounded assembly proof before completing the lifecycle.

The blocker is validation access to a disposable Linux environment supporting
rootless Podman, not a demonstrated need for privileged image assembly.
No draft PR has been published. The exploratory files are uncommitted.

References: [dracut live boot implementation](https://github.com/dracut-ng/dracut/blob/16c0eadb0065833e3e7b961f1e196b2b5cba07b3/modules.d/70dmsquash-live/dmsquash-live-root.sh),
[Linux capabilities](https://man7.org/linux/man-pages/man7/capabilities.7.html),
[extended attributes](https://man7.org/linux/man-pages/man7/xattr.7.html).

## Hosted assembly proof

Draft PR 227 provides a bounded, read-only-permissions GitHub-hosted job.
The first run at `54b8fd5` successfully built and ran rootless Podman, then
failed setting the SELinux fixture label with `Operation not permitted`.
The host-directory checkout/relabel recipe therefore is not portable to that
ordinary supported controller environment. Do not weaken or omit the label test.

The next bounded test uses packaged libguestfs with its direct QEMU backend
and software emulation. Serialized ownership, capabilities, labels and links
are imported into ext4 inside an unprivileged appliance, then checked directly
in the image. It requires no host mounts, KVM access, privileged container,
new service, secrets or host security changes. This is a proposed replacement
for the host-directory assembly recipe, not a claim that its failed test passed.
It adds controller tools and image-assembly cost; Recovery does not need it.

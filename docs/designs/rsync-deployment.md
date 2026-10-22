# Independent rsync deployment

OSTree remains the default. `rpm_ostree_prepare_candidate(backend="rsync")`
selects an ordinary writable root image instead. The controller requires
`rsync` and Recovery must advertise `rsync-deployment-v1`; there is no implicit
fallback. The existing tools remain compatible. Backend-neutral
`recovery_deploy_candidate`, `recovery_boot_candidate`,
`recovery_discard_candidate` and `recovery_cancel_deployment` share the existing
admission and work records. Operation sequences support the first three.

## Responsibility boundary

`deployment.py` selects two concrete implementations; it is not a plugin API.
It dispatches preparation on the target, cleanup and saved-journal access.
Controller-side OS assembly is separate, in the Bazzite recipe.

| Responsibility | OSTree default | Rsync image |
| --- | --- | --- |
| Transport | Existing authenticated OSTree HTTPS pull | On-demand rsync pipe over authenticated HTTPS |
| Identity and completion | Exact OSTree commit and its object checksums | Manifest identity plus exact byte size and SHA-256 of root image, kernel and initramfs |
| Staging | Existing isolated per-deployment OSTree storage root | Separate cache, then a fresh writable ext4 image per deployment |
| Activation preparation | OSTree creates deployment and BLS entry | Trusted Recovery code configures fstab, copies boot artifacts and publishes BLS only after file synchronization |
| Boot selection | Shared explicit one-time GRUB selection; Recovery stays default | The same shared selection, without target-side OSTree |
| Cleanup | OSTree removal plus owned run state | Remove only owned entry, boot files and image; refuse unrelated mounts or attached images |

The image manifest includes the source commit, conversion recipe and tools image
identity. The candidate also retains its pinned Bazzite digest, inputs and package
inventory. This identifies the exact produced artifact; it does not promise
byte-identical ext4 images across separate builds.

## Assembly and boot

The Bazzite preparation still uses rpm-ostree and OSTree on the controller.
For rsync, a small helper uses the public OSTree archive API with PAX format to
retain security attributes. The CLI's GNU tar export lost them in a real test.
Packaged libguestfs imports the archive inside an unprivileged, software-emulated
appliance. No host mounts, privileged container or host security changes are used.
This optional tools image is built only for rsync preparation.

The Bazzite recipe creates ordinary `/etc`, `/var` and `/tmp`, masks OSTree boot
services and rpm-ostreed, builds a matching dracut live initramfs and labels the
completed root inside the appliance. These are distro adaptations, not shared
API assumptions. Live rpm-ostree transactions are not supported in the ordinary
root; prepare a new candidate for package changes.

Recovery transfers `rootfs.img`, `vmlinuz` and `initramfs.img` into
`quirkbench/image-cache/<bundle-id>` on the shared data partition. A completed
transfer is verified before any run image or boot entry is created. Rsync does
not write in place or delete destination files. A corrupt or interrupted cache
can be repaired by a new request using checksummed rsync transfer.

Each deployment owns `quirkbench/image-deployments/<deployment-id>` and
`quirkbench-images/<deployment-id>` on the boot partition. The image copy is
configured through a temporary loop mount. Recovery does not execute candidate
programs: it preserves the already-verified fstab attributes using trusted code.
Enabled SELinux refuses an unlabeled fstab.

Dracut uses `root=live:UUID=<data-uuid>`, the deployment's `rd.live.dir`, and
`rd.live.overlay=none` to boot the writable image. It does not copy the experiment
root into RAM. Existing internal-disk protection, target connection, explicit
return and Recovery default selection remain shared.

## Interruption, ownership and evidence

The target persists ownership before image mutations. Cancellation waits for
writers and temporary unmounts. Failed cleanup retains ownership, blocks another
deployment and supports explicit discard retry. A cleanup retry only unmounts
a temporary mount whose device is associated with the exact owned image; any
remaining attachment prevents removal. An interrupted copy is not bootable, and
restarting Recovery does not resume or select it automatically.

The BLS entry is published last. Boot selection is a separate explicit operation.
This is not atomic rsync deployment, transactional rollback or an automatic
restoration of a damaged run. Recovery remains the boot default, while a later
explicit boot may reuse a successfully prepared image.

Saved journals are read through a temporary read-only image mount with journal
replay disabled. Collection failure is recorded separately and does not suppress
other evidence. Settings, evidence and cached candidates lie outside run images
and are not removed by discard. Explicit candidate-cache pruning also removes
the image cache, only under the existing idle-Recovery admission rules.

## Costs and verification limits

The ext4 data partition generally cannot reflink: a fresh deployment requires a
full sparse copy plus full artifact hashing. Rsync may reduce network bytes; it
does not establish a faster total experiment loop. The image leaves 4 GiB plus
20 percent sizing allowance beyond its archive size. A retained image's ordinary
root, including `/usr`, remains writable across repeat boots. This differs from
plain OSTree unlock's temporary `/usr` edits.

Neither backend starts a new daemon, scans storage or prepares repositories merely
because Recovery boots. Rsync subprocesses exist only for an explicit transfer.
Recovery adds the rsync package, not libguestfs or its appliance. Added image bytes
have an unmeasured RAM-copy cost; zero boot impact is not claimed. Rebuilding
Recovery with the added package requires refreshing its pinned package selection.

Fast tests exercise real controller/target admission, explicit boot and evidence
return, corruption, retry, cancellation, interrupted copying, cleanup failure,
capability rejection and idle startup. Separate real-rsync tests exercise the TLS
stream and failure paths. A bounded hosted job verifies real source-commit export
through libguestfs/ext4, including exact capabilities, SELinux labels, ownership,
modes, hardlinks and symlinks. Rootless host-directory labeling failed; the
appliance check passed without dropping metadata or changing host security.

A complete converted Bazzite image and real boot have **not** been qualified.
The bounded small-image proof establishes metadata handling, not the complete
distro normalization/initramfs path, firmware behavior, SELinux boot connectivity,
crash durability or hardware recovery. Keep this implementation in draft until
that qualification is assessed. No hardware or owner data was used in development.

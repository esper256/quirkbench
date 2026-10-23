# Independent rsync deployment

OSTree remains the default. `rpm_ostree_prepare_candidate(backend="rsync")`
selects an ordinary writable root image instead. The controller requires
`rsync`, `guestmount` and `guestunmount`; Recovery must advertise `rsync-tree-v1`; there is no implicit
fallback. The existing tools remain compatible. Backend-neutral
`recovery_deploy_candidate`, `recovery_boot_candidate`,
`recovery_discard_candidate` and `recovery_cancel_deployment` share the existing
admission and work records. Operation sequences support the first three.

## Responsibility boundary

`deployment.py` selects two concrete implementations; it is not a plugin API.
It dispatches preparation on the target, cleanup and saved-journal access.
Controller-side OS assembly is separate, in the Bazzite recipe.

| Responsibility | OSTree default | Rsync file tree |
| --- | --- | --- |
| Transport | Existing authenticated OSTree HTTPS pull | On-demand rsync pipe over authenticated HTTPS |
| Identity and completion | Exact OSTree commit and its object checksums | Prepared source provenance, rsync completion/checksums, and SHA-256 verification of boot artifacts |
| Staging | Existing isolated per-deployment OSTree storage root | One owned writable ext4 backing file, created once and updated by file-tree rsync |
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

The controller opens the prepared source image as a read-only libguestfs/FUSE
view only while selected. This preserves numeric ownership, hard links, ACLs,
capabilities and SELinux xattrs instead of losing them in a user-owned checkout.
Native `guestmount` needs a usable appliance kernel and FUSE; these are optional
controller dependencies, not Recovery services. The appliance remains part of
assembly and source access. Removing it would require another metadata-faithful
source representation, not just deleting a package.

Recovery creates a sparse ext4 backing file sized from the prepared image on the
first transfer. It mounts that file and runs ordinary `rsync -aHAX --numeric-ids
--super` into its root. Subsequent operations mount the same file; they neither
transfer nor copy an entire image. Each deployment owns
`quirkbench/image-deployments/<deployment-id>` and
`quirkbench-images/<deployment-id>` on the boot partition. Source and destination
are fixed to the selected candidate root and selected experiment root, never the
running root, settings or shared evidence.

`recovery_deploy_candidate` accepts `destination_run_id` to select the retained
experiment. Each operation gets a new run/work identity while retaining the
filesystem's deployment identity and earlier evidence. Omit the destination for
a new filesystem. `rsync_options` provides:

- `mode="full"`: `--whole-file`, with no delta matching. This is the default.
- `mode="delta"`: `--no-whole-file`, reusing matching destination blocks.
- `ignore_times=true`: transfer every selected file, even when the quick check
  matches. Full mode alone still skips unchanged files.
- `checksum=true`: compare content rather than size/time when selecting changes.
- `delete=true`: delete destination paths absent from the source. Default false.
- `exclude=[...]`: ordinary rsync patterns; excluded destination paths are also
  protected from deletion. External filter files and arbitrary server flags are
  not accepted. Symlinks are preserved, not followed into outside source trees.
- `dry_run=true`: report without changing files; requires an existing destination
  and `update_boot=false`. The filesystem is mounted read-only without replay.
- `update_boot=true`: after successful transfer, configure fstab and install the
  separately verified kernel/initramfs and boot entry. Default true. False keeps
  existing boot artifacts; it does not implicitly exclude `/etc/fstab` or any
  other root files. For a userspace-only update, explicitly exclude `etc/fstab`
  if its existing target configuration should remain.

Statistics and itemized changes are in the operation's target deployment log.
Normal temporary-file/rename behavior is used, not the literal `--inplace` flag.
There is no similarity optimizer or automatic backend fallback. File updates can
leave a mixed tree on failure; the agent chooses retry, inspection or discard.
Rsync's successful exit verifies protocol completion and transferred-file
checksums, not equality of the entire destination to an immutable source. Quick
checks, exclusions, retained extras and experiment edits deliberately allow a
different resulting tree. The saved candidate identity and operation options
record provenance; they are not a checksum of the mutated destination.

Recovery runs only trusted host tools. Boot preparation accepts its own prior
fstab mount line, retains file xattrs, and refuses an unlabelled fstab when SELinux
is enabled. It never executes candidate programs. Kernel changes require matching
boot artifacts; selecting those and deciding when to boot remain agent choices.

Dracut uses `root=live:UUID=<data-uuid>`, the deployment's `rd.live.dir`, and
`rd.live.overlay=none` to boot the writable image. It does not copy the experiment
root into RAM. Existing internal-disk protection, target connection, explicit
return and Recovery default selection remain shared.

## Interruption, ownership and evidence

The target persists ownership before filesystem mutations. Cancellation waits for
writers and temporary unmounts, clears pending boot selection, and retains partial
files and ownership. Failure also retains the experiment. Retry selects the
latest retained run ID; explicit discard is the only operation that removes it.
A cleanup retry only unmounts a device associated with the exact owned image;
remaining attachments prevent removal. Recovery restart never resumes a transfer
or selects the experiment automatically.

New boot entries are published after successful transfer and boot preparation.
Existing entries can remain after an interrupted update, but boot selection is
always separate and explicit. This is not atomic deployment or rollback. A later
explicit boot may boot a partially updated tree; the agent must assess it first.
Recovery remains the default.

Saved journals are read through a temporary read-only image mount with journal
replay disabled. Collection failure is recorded separately and does not suppress
other evidence. Settings, evidence and cached candidates lie outside run images
and are not removed by discard. Explicit candidate-cache pruning also removes
the image cache, only under the existing idle-Recovery admission rules.

## Costs and verification limits

File-tree delta transfer can reduce network bytes; it does not establish a faster
total loop. Source appliance startup, filesystem mounting and checksum comparison
have costs. Existing backing files are not automatically resized; insufficient
space fails visibly and retains the experiment. Initial sizing includes the
recipe's 4 GiB plus 20 percent allowance. Ordinary root edits, including `/usr`,
persist across boots, unlike plain OSTree unlock's temporary `/usr` edits.

Neither backend starts a new daemon, scans storage or prepares repositories merely
because Recovery boots. Rsync subprocesses exist only for an explicit transfer.
Recovery adds the rsync package, not libguestfs or its appliance. Added image bytes
have an unmeasured RAM-copy cost; zero boot impact is not claimed. Rebuilding
Recovery with the added package requires refreshing its pinned package selection.

Fast tests exercise real controller/target admission, explicit boot and evidence
return, corruption, retry, cancellation, interrupted transfers, cleanup failure,
capability rejection and idle startup. Separate real-rsync tests exercise the TLS
stream and failure paths. A bounded hosted job checks real source-commit export
through libguestfs/ext4, including exact capabilities, SELinux labels, ownership,
modes, hardlinks and symlinks. Rootless host-directory labeling failed; the
appliance check passed without dropping metadata or changing host security.

A complete converted Bazzite image and real boot have **not** been qualified.
The bounded small-image proof establishes metadata handling, not the complete
distro normalization/initramfs path, firmware behavior, SELinux boot connectivity,
crash durability or hardware recovery. Keep this implementation in draft until
that qualification is assessed. No hardware or owner data was used in development.

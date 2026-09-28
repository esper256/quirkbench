# Fedora build environment replay

The Containerfile takes a verified immutable `registry.fedoraproject.org/fedora@sha256:...`
base reference. `assemble.ini` creates a rootless Distrobox with a private home
and 4 CPU / 4 GiB cgroup caps. The pipeline checks that those caps are no more
than half the controller resources and refuses to build if the cgroup limits are
missing. Adjust the caps downward on smaller hosts. Its controller-wide file
lock permits one build at a time.

After the initial container and Fedora target rootfs are populated, run
`quirkbench.build.capture_package_lock`, `capture_target_package_lock`, and
`capture_toolchain_lock` inside the container. Keep these lock files and their
SHA-256 values with the source/config identities. On container recreation,
run `replay_rpms.py LOCK LOCK_SHA256 BASE_DIGEST` as UID 0 **inside** the rootless
container, using an RPM repository snapshot that retains every full NEVRA.
The script verifies the entire installed RPM set after replay; it fails if a
locked version is unavailable or any extra/different package remains. It does
not install packages on the controller OS. The pipeline independently rechecks both
RPM locks and toolchain versions before every cache miss.

`userspace-fixture.tar.xz` contains a tiny C `make all` / `make install`
package. The install target requires an explicit `DESTDIR` and writes only
`DESTDIR/usr/bin/quirkbench-health`; it is a real smoke input for the staged
userspace path, not the target supervisor.

The package pin syntax follows [DNF5's NEVRA matching](https://dnf5.readthedocs.io/en/latest/misc/specs.7.html).
The Distrobox resource flags are passed through
[`additional_flags`](https://github.com/89luca89/distrobox/blob/main/docs/usage/distrobox-assemble.md)
to rootless Podman. Podman's [CPU and memory limit support](https://docs.podman.io/en/latest/markdown/podman-run.1.html)
requires suitable cgroups; the pipeline fails closed when those limits are
not visible.

## Fedora OSTree composition

`quirkbench.compose.FedoraComposer` packages staged kernel, modules, initramfs
and additive userspace outputs as RPMs, installs the candidate runtime, composes
minimal Fedora with rpm-ostree, signs the exact commit, and publishes an archive
OSTree repository. Its bounded commands emit phase/output counters and preserve
logs under the compose workspace. The manifest is returned only after signing,
repository integrity verification and synchronization. Interrupted private
staging directories are retained for diagnosis; another invocation can retry.

Use a separate controller signing home, outside both workspace and published
repository. The public verification key is provisioned separately on targets.
Neither the signing home nor AI authentication is copied into RPMs. Configure
HTTPS/device authentication in the controller service; the composer does not
start a public server or store network credentials in the deployment manifest.

The strict JSON input accepted by `ComposeInputs.from_mapping` contains:

- `artifact_paths` and `artifact_sha256`: mappings for `kernel`, `config`,
  `initramfs`, `modules`, `userspace`, and `build_provenance`. Paths are absolute,
  regular files. Module tar members are relative to their kernel release;
  userspace tar members are relative to the target root and limited to `usr/`
  and `etc/` defaults. Provenance must bind all five output digests and release.
- `kernel_release`, `fedora_release`, `repository` (configured target remote
  alias), `source_date_epoch`, and `protection_profile` (currently
  `usb-excluded-controllers-v1`).
- `signing_key` (full fingerprint), `signing_home`, `fedora_repo_file` and
  `fedora_repo_sha256`. Fedora repository files require package GPG verification
  and TLS verification, with HTTPS or local snapshot locations.
- Optional `replacement_rpms`: absolute RPM paths mapped to SHA-256 digests.
  Rebuild an existing Fedora component using its native package spec and supply
  the resulting replacement RPMs here. They are selected from the local build
  repository by exact NEVRA. The additive userspace tar is for new files; it must
  not overwrite files belonging to unrelated Fedora RPMs.

Build the candidate initramfs using `target-assets/candidate-dracut.conf` and a
sysroot containing `ostree`. Composition fails if `lsinitrd -m` does not report
its `ostree` module. The composed kernel/initramfs live under
`/usr/lib/modules/RELEASE`; rpm-ostree's `no-initramfs` option preserves the
explicitly built initramfs instead of silently regenerating it. The treefile
records signed protection-profile/config/build identity metadata. Recovery uses
its independent initramfs.

Fedora RPM sources must be retained as a repository snapshot for exact rebuilds.
A pinned `.repo` file alone does **not** freeze remotely changing metadata.
The composer uses `--download-only-rpms` to retain dependency RPMs and record a
strict package lock before composing with `--cache-only`. Private stage
`dependency-rpms/` and `dependency-lock.json` must be retained with the checkpoint;
they are recorded by digest in deployment provenance. The signed commit and
composed RPM database identify what was actually selected. Retain the custom
RPMs and builder OCI image as well. Build source/configuration and debug symbols remain
in Quirkbench's ordinary artifact store.

The composition design follows the upstream [server workflow](https://coreos.github.io/rpm-ostree/compose-server/)
and [treefile reference](https://coreos.github.io/rpm-ostree/treefile/).

### Nested composition sandbox

Use a dedicated **rootless Podman** container for rpm-ostree composition, alongside
normal non-root Distrobox controller/kernel builds. Run its process as container
UID 0 with the default rootless UID mapping: UID 0 maps to the unprivileged controller
user, and subordinate UIDs remain mapped. Do not use keep-id for this composition
profile: rpm-ostree finalization preserves root-owned metadata and fails as UID
1000; a single-UID nested namespace also fails on unmapped ownership.

The required additional Podman flags are:

```text
--init --user=0 --cap-add=SYS_ADMIN --cap-add=NET_ADMIN
--security-opt=label=disable --security-opt=seccomp=unconfined
--security-opt=unmask=ALL --device=/dev/fuse
```

Keep the existing 4 CPU / 4 GiB caps and mount only the explicit workspace. Do
not use `--privileged`, host networking, or attach any host block device.
`assemble.ini` remains the normal Distrobox controller/build profile; these extra
flags belong to the dedicated composition container.

`--init` is required for this long-lived container: Podman's init process reaps
orphaned GPG and OSTree helper processes. Using `sleep infinity` directly as PID
1 leaves exited children as zombies and eventually exhausts the process limit.
The normal Distrobox lifecycle remains separate.

The namespace-scoped SYS_ADMIN capability permits `rofiles-fuse --copyup`, which
protects cached package hardlinks during scriptlets. `/dev/fuse` is its virtual
filesystem interface, not a storage disk. NET_ADMIN permits bubblewrap to set up
loopback in its private network namespace. The seccomp/unmask flags permit nested
namespace creation and procfs mounts. These capabilities do not become host-root
capabilities in rootless Podman. Qualification verified no block devices in the
container and successful package scriptlets and metadata finalization with this
profile. Target storage protection is unchanged.

### Retained build evidence

`evidence_paths` and `evidence_sha256` are required composition input maps. They
include `build_provenance`, `vmlinux`, `system_map`, `kernel_source`,
`userspace_source`, `config`, and `modules`; additional locks, patches, and symbol
files may be included. Build provenance must bind both source archives and the
matching symbol/configuration/module outputs. The CLI stores these in the CAS
before composition.

The composer exposes `evidence_files` for generated replay inputs: complete
custom/dependency RPM archives, dependency lock, composer package inventory,
Fedora repository configuration, treefile, toolchain report, and finalization
hook. Their hashes join `provenance.build_evidence.artifacts`. The CLI stores
these before retaining or publishing the deployment manifest, making checkpoint
retention independent of disposable workspace caches.

The prebuilt initramfs is packaged under `/usr/lib/quirkbench/initramfs` and linked
to `/usr/lib/modules/RELEASE/initramfs.img` by a hash-verifying `finalize.d` hook.
The installed rpm-ostree rejects preexisting module-directory initramfs files
before checking `no-initramfs`; the finalization hook runs after that kernel
processing. This preserves the recorded initramfs bytes without regeneration.

### Real composition acceptance

Run `python3 environments/qualify_composition.py` inside the dedicated builder
with absolute `--manifest`, `--inputs`, `--controller`, `--repository`,
`--public-key`, and a new `--work` directory. This is an explicit integration
gate: missing tools or failed checks are errors, not skips. It verifies retained
CAS evidence, package/toolchain identity, SQLite integrity, exact kernel/initrd/
config bytes, module and userspace payloads, and NSS `altfiles` configuration.
It requires an init-managed PID 1, proves orphaned children are reaped, and checks
that the qualification leaves no zombie processes behind.
It interrupts a real signature-checked OSTree pull, retries it, runs `fsck`, and
checks out the resulting revision. Run the same command in a freshly created
builder container to demonstrate progress survives disposal of the old container.
It emits `compositionqualification.json`; this does not replace the QEMU boot
or physical hardware acceptance gates.

The initial implementation uses a fresh per-attempt download cache because an
already imported rpm-ostree package cache can satisfy `--download-only-rpms`
without retaining RPM files. This deliberately trades download speed for a
complete replay snapshot. The downloaded RPMs are archived in retained CAS
evidence before the cache becomes disposable.

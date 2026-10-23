# Optional deployment comparison

This developer experiment compares production OSTree and rsync preparation and
deployment. It is manual, expensive and separate from release qualification and
ordinary CI. It does not decide in advance which backend is better.

The command takes one locked JSON workload and a **new** output directory:

```sh
uv run --no-sync python development/deployment_benchmark.py scenario.json \
  /scratch/comparison-001 --recovery /scratch/recovery-build/images/IMAGE_ID \
  --firmware /usr/share/OVMF/OVMF_CODE.fd
```

The Recovery directory must contain the current production `image.json`,
`recovery.img` and `BOOTX64.EFI`. Build it with the normal Recovery builder in
separate disposable storage before running this command. It is a shared, recorded
prerequisite, not part of candidate preparation timing. Install the normal
controller build dependencies plus QEMU, OVMF, guestfish, guestmount, rsync and
GRUB's environment editor. The guest needs enough RAM for RAM-backed Recovery;
the default is 8 GiB. Each backend receives a newly created 64 GiB sparse regular
file, presented as USB media. No host block device or physical media is accepted.
Libguestfs creates its filesystems without mounting them on the host. Allow ample
real disk capacity for two separate container stores, OCI caches, OSTree
repositories, root images and target disks. Nothing deletes an existing cache.

## Locked workload

Use five steps with kinds `initial`, `kernel`, `modules`, `modules`, `no-op`.
Every step describes its complete intended additional/replacement RPM set, not a
patch to the previous recipe. The last two recipes and checks must be identical.
Use prebuilt kernel and module RPMs; their acquisition or compilation belongs
outside the timed experiment. Debugging tools are explicit RPM payloads in
`packages`, shared by both backends. Automatic debugging-package installation is
disabled. Both recipes request `dracut-live` so that rsync's boot prerequisite is
also present in the comparison OSTree image. Resolved package inventories remain
in production preparation logs; repository dependency resolution is still a
source of variability and must be inspected before interpreting a comparison.

The JSON structure is:

```json
{
  "format": 1,
  "base": "ghcr.io/ublue-os/bazzite@sha256:EXACT_DIGEST",
  "steps": [
    {
      "kind": "initial",
      "packages": [{"path": "artifacts/debug-tools.rpm", "sha256": "SHA256"}],
      "replacements": [],
      "kernel_release": "EXACT_RELEASE",
      "kernel_build_id": "ELF_BUILD_ID",
      "checks": [
        {"role": "kernel", "path": "/usr/lib/modules/EXACT_RELEASE/vmlinuz", "sha256": "SHA256"},
        {"role": "module", "path": "/usr/lib/modules/EXACT_RELEASE/extra/example.ko", "sha256": "SHA256"},
        {"role": "tool", "path": "/usr/bin/strace", "sha256": "SHA256"}
      ]
    }
  ]
}
```

This abbreviated example is a schema illustration, **not a runnable or measured
workload**. Supply all five steps and actual artifact hashes. Paths to RPMs are
relative to the manifest. Admission hashes every input before creating output.
Boot validation checks `uname`, the running kernel's ELF build ID from
`/sys/kernel/notes`, and each expected file SHA256. Module file checks establish
installed content, not successful module loading or device behavior.

## Cache and measurement scope

Each backend gets a separate empty controller root, Podman graph/run store and
target filesystem. Its own state survives the sequence. OSTree discards each
finished deployment after evidence return while retaining repository objects;
rsync updates the same retained filesystem. The first rsync transfer uses full
mode and later transfers use delta mode, with checksums and deletion enabled.
Normal rsync temporary-file/rename semantics apply. These updates are not atomic.

OS page caches are **not** dropped. Cold artifact stores do not mean cold RAM
caches. Record run order and repeat in reverse order if useful; one run supports
no statistical claims. Host packages, Python dependencies, OVMF, the libguestfs
appliance and Recovery build are prerequisites outside cold candidate scope.
Production preparation includes base download, builder setup, target RPM build,
OCI export/import and rsync root conversion. Individual external command timings
and existing production preparation metrics help distinguish those costs.

`results.json` retains per-step status, identity, timings, environment and errors;
`report.md` is the short comparison. Preparation plus deployment and time from
preparation start to hash-validated boot are separate. Return and evidence upload
are included in step elapsed time. Allocated and apparent storage are counted
separately. The guest pcap delta includes SSH/control traffic and capture framing;
it is not rsync payload bytes. Host download traffic and physical disk I/O bytes
are not measured. Source-view startup is included in deployment time, not yet a
separate metric. Keep production logs and evidence for detailed transfer stats.

A no-op recipe does not guarantee zero target bytes: boot changes mutable files,
fstab is configured on the target, and rsync preparation may rebuild boot
artifacts. The existing real-transfer proof checks zero transfer for unchanged
files and full-versus-delta matching independently. Do not describe that small
fixture as full-OS performance evidence.

## Bounded preparation probe

`--prepare-only` leaves deployment and boot explicitly unrun. The narrower
`--probe-base ghcr.io/ublue-os/bazzite@sha256:DIGEST` attempts only initial real
preparation without a debug workload. It is useful for discovering build, package
and resource failures, **not** for choosing between deployment backends.

The optional GitHub workflow runs this bounded probe only on manual dispatch or
when the `run-deployment-probe` label is explicitly added to the draft PR. Normal
pushes do not request it. The pinned digest is in the workflow, both stores are
fresh, and results/logs are retained for seven days. This workflow does not yet
run the full five-step VM comparison. A successful preparation probe is not a
boot qualification or a recommendation to merge rsync.

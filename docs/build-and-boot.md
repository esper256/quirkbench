# OSTree build and boot workflow

The production deployment backend is minimal Fedora composed with rpm-ostree and published through a traditional signed OSTree repository. The old four-file kernel/initramfs bundle is retired. Historical artifacts remain available, but old images require rebuilding; there is no in-place image conversion.

The revised M2 build and VM gate is achieved. See [qualification observations](ostree-review.md) for exact identities, reports and physical integration limits.

## Build and compose on the controller

Use the versioned Fedora container environment, immutable base-image identity and recorded build/package/toolchain inputs. Podman and Distrobox are host prerequisites; all build packages and experimental installations stay inside the container. Persistent project state lives outside its disposable filesystem. Credentials, repository signing private keys and agent authentication must never enter a target filesystem, RPM payload, build log or exported source snapshot.

Build kernels and modules in dedicated output trees. Stage userspace using `DESTDIR` and modules using `INSTALL_MOD_PATH`, preserve matching debug symbols and source archives in the controller artifact store, and package the experimental components as RPMs for composition. The composer produces one revision containing matching kernel, modules, initramfs, userspace and default configuration. Do not apply package overrides on the target. Capture exact source, configuration, package and toolchain identities in the deployment provenance.

Default to one build at a time, no more than half host CPUs and RAM, and a 20 GiB free-space reserve. Report compiler output activity, measured object/byte counters where available and bounded phase deadlines. Cache reuse is an optimization; checkpoints and source identities remain recoverable without caches.

### Capacity planning

Use an existing suitable external USB SSD where possible. Budget approximately 32 GiB for recovery, active deployments and logs, plus twice the target's RAM for pending crash dumps, then leave at least 20% of the device free. A 250/256 GB SSD is a comfortable starting point; 500 GB adds headroom for retained evidence. This is capacity planning, not a claim that crash capture is already qualified. Unacknowledged evidence must never be deleted to free space for another experiment.

Controller storage is separate: initially budget roughly 200 GiB for sources, builds, symbols, retained RPMs and evidence, in addition to the enforced 20 GiB free-space reserve. Keep enough extra capacity for independent backups; their OSTree objects do not share hardlinks with the source repository. A compact initial image can expand its existing data partition during commissioning to use the external device's available capacity.

### CLI entry points

Run these commands inside the appropriate isolated builder described in [the environment setup](../environments/README.md), with the persistent project mounted at `/workspace`. Replace the input paths with actual recorded files. The `build` command uses a build-input manifest; `compose` uses the separate `ComposeInputs` JSON contract documented in that environment guide, including source and symbol evidence maps. The illustrative `examples/deployment.json` is output-format documentation, not a compose input.

```sh
PYTHONPATH=src python3 -m quirkbench --state /workspace/.quirkbench/controller \
  build /workspace/inputs/build.json --workspace /workspace/.quirkbench/build

PYTHONPATH=src python3 -m quirkbench --state /workspace/.quirkbench/controller \
  compose /workspace/inputs/compose.json \
  --workspace /workspace/.quirkbench/compose \
  --publish-repo /workspace/.quirkbench/published
```

Composition prints the deployment manifest and its retained artifact identity as JSON; phase reports go to stderr. Keep the manifest for the deployment qualification fixture. `compose` records the repository alias in the controller's `repositories.json` when none is configured. Use the same persistent state directory when serving that repository or backing it up. Add `--campaign CAMPAIGN_ID` to build/compose to expose their progress in an existing campaign's monitor.

## Publish and prepare exact revisions

The controller publishes a signed OSTree repository over authenticated HTTPS. The target requires Python GI and calls libostree’s strict signature verifier even for cached content; successful `ostree show` output is not sufficient signature authorization. Device credentials are distinct from AI credentials. The deployment manifest is stored in the ordinary content-addressed artifact store and references a configured repository identifier, exact commit and protection profile. Experiments refer to it using the `deployment` artifact role. Never authorize a moving branch or autonomous target update. Publication durably retains the manifest, its build-evidence closure and exact commit before experiment submission; published builds remain pinned until an explicit future cleanup operation.

OSTree handles missing-object retrieval, verification and transactional deployment with synchronization enabled. Incomplete content must not become armable. Optional static deltas are deferred until transfer measurements justify them. Repository retention must protect all commits referenced by experiments or retained checkpoints. Complete backups include their independent object copies, SQLite, and the explicit build-evidence closure. Required evidence roles are `build_provenance`, `vmlinux`, `system_map`, `kernel_source`, `userspace_source`, `config`, and `modules`; dependency locks and additional debug data can be retained alongside them. Credentials and signing private keys are excluded; restoration requires separately configured repository locations and credentials.

Each physical attempt gets a fresh deployment group and mutable state. Repeating preparation for that same attempt must converge on the same prepared deployment, not create another experiment. `/etc` starts from revision defaults and `/var` is not shared across attempts. Evidence is retained separately from disposable OS state.

## Build the external image

Image assembly writes regular files only; it does not write physical drives, change host firmware, install host kernels or invoke host package installation. Output a partitioned `.img` or `.img.xz` plus checksum for a normal writer such as Etcher.

The image retains four partitions: fixed removable EFI bootloader/recovery files, read-only recovery root, a small GRUB one-shot state filesystem, and journaled ext4 data. Data holds the candidate OSTree sysroot and separately retained evidence. Only that existing data partition may expand, after strict positive identification of the external boot device and restartable geometry checks.

Recovery remains independent of candidate deployments. OSTree generates candidate boot entries without regenerating the system bootloader; Quirkbench validates and translates the entry into the fixed USB boot control. GRUB clears, saves and verifies one-shot state before candidate handoff. If that fails it selects recovery. Candidate content never replaces fixed recovery or the bootloader. Do not invoke `grub-reboot` against the host installation or use `efibootmgr`.

The `image` command accepts a separate JSON input. For a VM qualification image, use actual recovery outputs and the prepared data tree returned by [the deployment fixture](../acceptance/README.md):

```json
{
  "output": "/workspace/output/quirkbench-qualification.img",
  "recovery_kernel": "/workspace/recovery/vmlinuz",
  "recovery_initramfs": "/workspace/recovery/initramfs.img",
  "recovery_config": "/workspace/recovery/config",
  "recovery_provenance": "/workspace/recovery/provenance.json",
  "rootfs_dir": "/workspace/recovery/rootfs",
  "prepared_data_tree": "/workspace/deployment-fixture/data",
  "size_mib": 4096,
  "root_mib": 1024,
  "smoke": true
}
```

All paths are placeholders. The output's parent must exist and the output must be new. Build the Fedora recovery root with `target-assets/build-rootfs.sh FEDORA_RELEASE ABSOLUTE_OUTPUT_DIRECTORY` as UID 0 inside the rootless builder; use its corresponding recorded kernel/initramfs build provenance. Set partition sizes to fit the actual recovery and deployment contents. The small initial image is distinct from the full external-device capacity budget; commissioning expands its data partition before real campaigns. Save the JSON as `/workspace/inputs/image.json`, then run:

```sh
PYTHONPATH=src python3 -m quirkbench image /workspace/inputs/image.json
```

The command produces the regular-file disk image, a `.sha256` checksum and an adjacent image manifest. Qualification uses `quirkbench qualify-image --help` or `make acceptance-qemu`; [the acceptance guide](../acceptance/README.md) lists its required inputs. `smoke: true` enables VM trial behavior and is not a commissioned hardware image. Hardware preparation must use `smoke: false` and pass its separate gates before writing with Etcher.

## Protection and qualification

The initial supported profile keeps internal-controller support excluded from recovery and candidate kernels, verifies source/configuration provenance, and allowlists USB destinations before privileged writes. Disable internal discovery, automount, swap/resume and firmware writes. Recovery and OSTree candidate roots require different boot verification; executable deployment storage must not be mounted with a blanket `noexec`, while evidence remains restricted. A replacement storage-protection mechanism needs reviewed equivalent tests.

The current no-kexec policy does not support kdump. Review that policy and independently qualify the fixed capture kernel before enabling crash capture. Secure Boot is assumed disabled and must be verified. Owner-controlled USB boot selection and manual recovery of unsupported complete hangs remain explicit boundaries.

QEMU proves infrastructure behavior, not Acer fixes. The revised M2 gate requires a clean-container compose, preserved state after container recreation, one revision changing kernel and userspace with matching modules, interrupted update fault cases, recovery/candidate/subsequent-recovery/failed-candidate boots, and unchanged sentinel disks, fixed recovery and settled persistent firmware settings. Record host package and boot configuration inventories before and after. A process exit, timeout or immutable OVMF template hash alone is not successful boot qualification.

The hardware gate separately verifies actual storage protection, reset, diagnostic capture and evidence upload before unattended campaigns. See [recovery coverage](recovery-and-evidence.md). Unit tests and old bundle-based VM results must not be represented as completion of the OSTree M2 gate.

## References

- [rpm-ostree server composition](https://coreos.github.io/rpm-ostree/compose-server/)
- [OSTree atomic upgrades](https://ostreedev.github.io/ostree/atomic-upgrades/)
- [OSTree deployment state](https://ostreedev.github.io/ostree/deployment/)
- [GRUB environment block requirements](https://www.gnu.org/software/grub/manual/grub/html_node/Environment-block.html)

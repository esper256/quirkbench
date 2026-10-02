# OSTree build and boot workflow

The deployment backend is minimal Fedora composed with rpm-ostree and published
through a signed OSTree repository. Current development use requires the
[manual controller service setup](controller-installation.md) and
[manual target provisioning](recovery-operations.md#storage-and-setup).
The [product roadmap](product-roadmap.md) defines the guided journey;
[GitHub tracker #29](https://github.com/esper256/quirkbench/issues/29) tracks remaining work.

Fixed recovery uses locked stock Fedora packages, DNF5 installroot, dracut and the
existing GRUB/GPT assembler. No recovery kernel compile is required. See
[recovery operations](recovery-operations.md) for current image production and
[recovery design](recovery-base.md) for its contract. Recovery and experimental
kernels have distinct [storage policies](architecture.md#storage-protection-policy).

## Build and compose on the controller

Use the versioned Fedora container environment, immutable base-image identity and recorded build/package/toolchain inputs. Rootless Podman is the current builder prerequisite; Distrobox is optional for development; all build packages and experimental installations stay inside the container. Persistent project state lives outside its disposable filesystem. Agent authentication and repository/CA signing private keys never enter target filesystems or build outputs. Device-scoped credentials belong only in private provisioning and evidence/control, never in RPMs, OSTree commits, build logs, source snapshots or exported debugging evidence.

Build kernels and modules in dedicated output trees. Stage userspace using `DESTDIR` and modules using `INSTALL_MOD_PATH`, preserve matching debug symbols and source archives in the controller artifact store, and package the experimental components as RPMs for composition. The composer produces one revision containing matching kernel, modules, initramfs, userspace and default configuration. Do not apply package overrides on the target. Capture exact source, configuration, package and toolchain identities in the deployment provenance.

Default to one build at a time, no more than half the controller CPUs and RAM, and a 20 GiB free-space reserve. Report compiler output activity, measured object/byte counters where available and bounded phase deadlines. Cache reuse is an optimization; checkpoints and source identities remain recoverable without caches.

### Capacity planning

Use an existing suitable external USB SSD where possible. Default allocations are 2 GiB recovery, 32 GiB experiments and 32 GiB library, plus EFI/state. Evidence receives the remaining capacity and must fit the configured log budget plus twice target RAM, with at least 20% capacity headroom. A 250/256 GB SSD is a comfortable starting point; 500 GB adds headroom for retained evidence. This is capacity planning, not a claim that crash capture is already qualified. Unacknowledged evidence must never be deleted to free space for another experiment.

Controller storage is separate: initially budget roughly 200 GiB for sources, builds, symbols, retained RPMs and evidence, in addition to the enforced 20 GiB free-space reserve. Keep enough extra capacity for independent backups; their OSTree objects do not share hardlinks with the source repository. The compact initial image commissions the six-role layout on first recovery boot; see [the image contract](debug-image.md).

### CLI entry points

Submit from the controller's normal terminal after the [manual user-service setup](controller-installation.md#durable-build-and-composition-service).
The pinned builder image must already exist locally. Inputs use the existing
BuildInputs/ComposeInputs manifests and absolute declared paths; expensive capture
and hash checks happen in the first worker stage. Persistent state and private
worker staging live under the selected XDG home state.

Retain the finished builder's verified OCI archive and config ID separately from
the Fedora base marker. Version 2 job inputs preserve the original base identity
in build provenance and execute the verified finished builder. Source archives
keep contained links; captured target root filesystems also retain absolute OS
symlinks and permissions without extracting members through those links.

```sh
quirkbench build /absolute/inputs/build.json --request-id build-001
quirkbench compose /absolute/inputs/compose.json --request-id compose-001 \
  --publish-repo "$HOME/.local/state/quirkbench/repositories/lab"
quirkbench monitor
quirkbench operation status JOB_ID --json
```

Default output is the existing C2 operation response with job ID, request ID,
status/monitor commands and log location. Accepted work continues after the CLI
returns. Add `--wait` to await the original final build-output map or composition
manifest/artifact JSON. Ctrl+C stops the waiter. Exact request retries return the
same job; changed inputs conflict. `--workspace` is rejected because the controller
owns private staging. Configure composition repository alias/signing identity in
private service configuration first. Add `--campaign CAMPAIGN_ID` to bind campaign
pause behavior. Explicit interrupted-job resume uses `operation resume JOB_ID
--request-id NEW_REQUEST_ID`; verified retained inputs avoid source recapture once
capture has completed.

Software support does not establish real service containment or boot qualification.

### Worker validation and publication

Workers receive captured inputs, private outputs and read-only cache hints; writable
Kbuild state and cache proposals remain private. Only the current controller owner
adopts inputs and approves reusable outputs after whole-unit shutdown. Missing cache
space skips optional cache publication, never required evidence retention.

Composition workers stage unsigned output without signing secrets or writable shared
repositories. The owner validates input/runtime identity, OSTree closure, kernel,
configuration, initramfs and modules before signing and publishing. Repository pins
precede the short fenced database commit. Failed signing/publication cannot report
success; a lost commit may leave a conservative pin, and post-commit cleanup failure
preserves the committed result. See [ownership contracts](implementation-contracts.md#c2--local-operations-ownership-and-restart-p2).

## Publish and prepare exact revisions

The controller publishes a signed OSTree repository over authenticated HTTPS. The target requires Python GI and calls libostree’s strict signature verifier even for cached content; successful `ostree show` output is not sufficient signature authorization. Device credentials are distinct from AI credentials. The deployment manifest is stored in the ordinary content-addressed artifact store and references a configured repository identifier, exact commit and protection profile. Experiments refer to it using the `deployment` artifact role. Never authorize a moving branch or autonomous target update. Publication durably retains the manifest, its build-evidence closure and exact commit before experiment submission; published builds remain pinned while retained attempts, investigations or configured build counts require them. Housekeeping uses the existing command/event-driven retention policy.

OSTree handles missing-object retrieval, verification and transactional deployment with synchronization enabled. Incomplete content must not become armable. Optional static deltas are deferred until transfer measurements justify them. Repository retention must protect all commits referenced by experiments or retained checkpoints. Complete backups include their independent object copies, SQLite, and the explicit build-evidence closure. Required evidence roles are `build_provenance`, `vmlinux`, `system_map`, `kernel_source`, `userspace_source`, `config`, and `modules`; dependency locks and additional debug data can be retained alongside them. Credentials and signing private keys are excluded; restoration requires separately configured repository locations and credentials.

Each physical attempt gets a fresh deployment group and mutable state. Repeating preparation for that same attempt must converge on the same prepared deployment, not create another experiment. `/etc` starts from revision defaults and `/var` is not shared across attempts. Evidence is retained separately from disposable OS state.

## Build the external image

Image assembly writes regular files only; it does not write physical drives, change controller firmware, install controller kernels or invoke controller OS package installation. Output a partitioned `.img` or `.img.xz` plus checksum for a normal writer such as Etcher.

The final image has fixed EFI/recovery, one-shot state, experiments, library and evidence partitions. Its compact factory form contains the first four GPT entries with all six identities reserved. First-boot commissioning records geometry, grows experiments and creates the preidentified library/evidence filesystems. It never reformats an ambiguous existing filesystem. See [layout revision 2](debug-image.md).

Recovery remains independent of candidate deployments. OSTree generates candidate boot entries without regenerating the system bootloader; Quirkbench validates and translates the entry into the fixed USB boot control. GRUB clears, saves and verifies one-shot state before candidate handoff. If that fails it selects recovery. Candidate content never replaces fixed recovery or the bootloader. Do not invoke `grub-reboot` against the controller installation or use `efibootmgr`.

Use `recovery-image` with a retained v2 recipe and builder archive for stock media;
see [image admission and publication](recovery-operations.md#durable-workers-and-image-publication).
`recovery-images --json` lists retained publications and their actual availability.
No image is delivered merely because rootfs preparation completed.

Low-level `image` and `qualify-image` tools remain available to developers; their
argument help and [acceptance fixtures](../acceptance/README.md) describe fixture
inputs. Smoke images are not commissioned hardware images. Physical writing uses a
standard image writer on the operator-selected external drive.

## Protection and qualification

Follow the [storage policy](architecture.md#storage-protection-policy). Recovery uses
stock packages and permits passive internal-controller enumeration, while all block/
filesystem operations are confined to expected roles on its identified boot device
from initramfs onward. Candidate profiles retain internal-controller exclusions and
final config/module/initramfs verification. Raw writes, swap/resume, filesystem repair
and firmware updates are covered, not just mounts. Require exact-candidate operator
approval before arming. Executable candidate storage cannot use blanket `noexec`;
evidence remains restricted. This is accident prevention, not arbitrary-kernel containment.

The current no-kexec policy does not support kdump. Review that policy and independently qualify the fixed capture kernel before enabling crash capture. Secure Boot is assumed disabled and must be verified. Owner-controlled USB boot selection and manual recovery of unsupported complete hangs remain explicit boundaries.

QEMU proves infrastructure behavior, not target fixes. Final release qualification requires a clean-container compose, preserved state after container recreation, one revision changing kernel and userspace with matching modules, interrupted update fault cases, recovery/candidate/subsequent-recovery/failed-candidate boots, and unchanged sentinel disks, fixed recovery and settled persistent firmware settings. Record controller package and boot configuration inventories before and after. A process exit, timeout or immutable OVMF template hash alone is not successful boot qualification.

The hardware gate separately verifies actual storage protection, reset, diagnostic capture and evidence upload before unattended campaigns. See [recovery coverage](recovery-and-evidence.md). Software tests cannot substitute for physical qualification.

## References

- [rpm-ostree server composition](https://coreos.github.io/rpm-ostree/compose-server/)
- [OSTree atomic upgrades](https://ostreedev.github.io/ostree/atomic-upgrades/)
- [OSTree deployment state](https://ostreedev.github.io/ostree/deployment/)
- [GRUB environment block requirements](https://www.gnu.org/software/grub/manual/grub/html_node/Environment-block.html)


Recovery uses NetworkManager with selected private connection state copied into RAM.
GRUB checks the expected SMBIOS system UUID before loading a candidate. Changes to
packaging or boot code require evidence for the resulting image bytes; software
branch tests do not qualify physical firmware or bootloader behavior.

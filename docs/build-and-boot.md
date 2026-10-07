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

Default to one build at a time and conservative interactive resource budgets.
[Workload profiles and explicit overrides](../environments/README.md#resource-overrides)
use effective capacity and avoid reserving desktop resources twice. Managed builds
default to a configurable 20 GiB free-space reserve. Report compiler output activity, measured object/byte counters where available and bounded phase deadlines. Cache reuse is an optimization; checkpoints and source identities remain recoverable without caches.

### Capacity planning

Use an existing suitable external USB SSD where possible. Default allocations are 2 GiB recovery, 32 GiB experiments and 32 GiB library, plus EFI/state. Evidence receives the remaining capacity and must fit the configured log budget plus twice target RAM, with at least 20% capacity headroom. A 250/256 GB SSD is a comfortable starting point; 500 GB adds headroom for retained evidence. This is capacity planning, not a claim that crash capture is already qualified. Unacknowledged evidence must never be deleted to free space for another experiment.

Controller storage is separate: initially budget roughly 200 GiB for sources, builds, symbols, retained RPMs and evidence, in addition to the configured free-space reserve. Keep enough extra capacity for independent backups; their OSTree objects do not share hardlinks with the source repository. The compact initial image commissions the six-role layout on first recovery boot; see [the image contract](debug-image.md).

### CLI entry points

Use the [agent guide](agent-guide.md) for the normal investigation workflow:

    quirkbench experiment submit first-fix --file experiment.json --request-id test-001
    quirkbench experiment status first-fix --request-id test-001
    quirkbench experiment logs first-fix --request-id test-001
    quirkbench monitor first-fix

The submission retains exact source, baseline, recipe and publication choices.
The configured controller performs capture, candidate preparation, kernel build
and system assembly. Preparation returns promptly and never approves target execution.
After reconciliation, use experiment resume with the original request ID and
a distinct resume request ID. See each action's help for the required inputs.

For deliberate manual stages, investigation build prepare, kernel and system use
the same services. They require exact completed inputs; they are not a separate
build workflow. Private worker staging remains controller-owned.

Software checks do not establish real containment or boot qualification.

### Durable candidate sysroot preparation

`investigation build prepare` prepares a retained package sysroot through the same operation
controller and bounded container worker. The input is derived from the investigation's
recorded baseline. Its RPM snapshot, locks, recipes and packages must already be
retained in the controller artifact store. Missing pinned bytes are a blocker,
not permission to substitute newer inputs.

```sh
quirkbench investigation build prepare first-fix --request-id candidate-001
quirkbench investigation status first-fix
```

Use this individual stage only for deliberate diagnosis; `experiment submit`
coordinates it automatically. Admission returns promptly. The configured builder
identity stays pinned, and acceptance does not mean assembly succeeded. Use `--json`
for the response envelope; action help explains resource overrides.

Only after whole-worker stop does the current owner independently verify inputs,
builder, result and tree, serialize a bounded sysroot archive and validate its
contents (at most 250,000 nodes and 128 GiB of file contents, with the configured
free-space reserve). The [retained result v1](../examples/candidate-rootfs-result.json) binds
the exact input, builder, tree and archive digests. Package modes, absolute OS
symlinks and observed rootless UID/GID metadata are retained; internal package
hardlinks become independent regular archive members. Credential exclusions match
the build pipeline. No private signing/control state enters the worker.

Restart interrupts the job. Reconcile the old whole service before explicitly
using `admin operation resume JOB_ID --request-id NEW_REQUEST_ID`; it uses a fresh
worker generation/stage and the same pinned inputs. A failed job is terminal;
retry with a new candidate request ID. Partial artifact writes confer no readiness
and remain unreferenced. The operation retains failed diagnostics and complete
input/output references for existing retention and backup.

This result is a package sysroot, not a built/composed candidate, physical attempt
or operator approval. The submission coordinator connects it to captured source build and composition. Cloud tests inject
package/container calls and do not establish native installroot/build readiness.

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

Use `dev recovery submit` with a retained v2 recipe and builder archive for stock media;
see [image admission and publication](recovery-operations.md#durable-workers-and-image-publication).
`recovery list --json` lists retained publications and their actual availability.
No image is delivered merely because rootfs preparation completed.

Low-level `dev image assemble` and `dev image qualify` tools remain available to developers; their
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

The current no-kexec policy does not support kdump. Review that policy and independently qualify the fixed capture kernel before enabling crash capture. Owner-controlled USB boot selection and manual recovery of unsupported complete hangs remain explicit boundaries.

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

# Fedora build environment

To run Quirkbench itself from a clone, use the repository's root `./quirkbench`
executable; no development virtualenv is needed. See the
[source checkout guide](../docs/controller-installation.md#run-from-a-source-checkout).
This directory contains build/development tooling; `environments/quirkbench` is
retained as a compatibility launcher.

Controller execution requires no systemd integration. Foreground recovery image generation uses
Podman or Docker without a running controller; see
[foreground image generation](../docs/recovery-operations.md#foreground-image-generation).
The foreground controller owns bounded container workers. Distrobox is an
optional development shell. `build` and `compose` submit durable jobs through
[controller service setup](../docs/controller-installation.md#foreground-build-and-composition-controller)
and return operation IDs; `--wait` reads their results. No image build is required
for ordinary software edits. Recovery uses stock packages; kernel compile guidance
below applies to experimental kernels and explicitly requested custom builds.

The Containerfile requires a digest-pinned
`registry.fedoraproject.org/fedora@sha256:...` base reference and checks its
`/etc/os-release` against the `FEDORA_RELEASE` build argument. Select `FEDORA_RELEASE` and `BASE_IMAGE` from the reviewed immutable inputs,
then build only when that product operation is requested:

```sh
podman build --pull=never --build-arg="FEDORA_RELEASE=$FEDORA_RELEASE" \
  --build-arg="BASE_IMAGE=$BASE_IMAGE" \
  -f environments/Containerfile -t localhost/quirkbench-build:local environments
```

If HTTPS package access uses an organization or proxy CA, supply a PEM CA bundle
with the standard build secret option:

```sh
podman build --pull=never --secret=id=ca_bundle,src=/absolute/ca-bundle.pem \
  --build-arg="FEDORA_RELEASE=$FEDORA_RELEASE" \
  --build-arg="BASE_IMAGE=$BASE_IMAGE" \
  -f environments/Containerfile -t localhost/quirkbench-build:local environments
```

The bundle should include the public roots needed by the selected Fedora mirrors
and any required organization CA. DNF uses it only during package installation;
the secret is not stored in an image layer. TLS and RPM signature verification
remain enabled. Without the secret, DNF uses Fedora's default trust. Docker with
BuildKit accepts the same `--secret` option when preparing this builder image
(omit Podman's `--pull=never`). Registry pulls use the container engine's own trust
configuration, so configure that separately if pulling the base fails. Building
the tool image does not select a workload supervisor; use the foreground image
command or an explicitly configured controller backend for execution.

This locally built image is a builder candidate until its installed package and
toolchain locks are captured and reviewed. `assemble.ini` describes a rootless
Distrobox development profile with a private home and 4 CPU / 4 GiB cgroup caps.
Its current Distrobox-generated command uses `--privileged` and binds the host
`/dev`; it must not run the recovery rootfs, image or other storage-sensitive
stages. The pipeline requires enforced cgroup CPU/memory bounds and resolves them against
effective capacity; missing bounds still prevent a build.
Adjust the caps downward on smaller hosts. Its controller-wide file lock permits
one build at a time.

### Observable bounded kernel builds

For ad hoc kernel builds, `start-bounded-podman-build.sh` runs one recorded
rootless Podman container in the foreground. Defaults use up to four CPUs and 8 GiB RAM, reserving half an unconstrained
host for interactive use. Already constrained cgroups are not halved again.
Kernel builds require at least 4 GiB. Zero additional swap, 4,096 processes and
a 24-hour timeout remain enforced. See resource overrides below. It uses Podman's
configured supported cgroup manager; Quirkbench requires no controller user service.

Use a fresh run identity, canonical owned stage and exact locked image:

```sh
environments/start-bounded-podman-build.sh \
  quirkbench-build-NEW_RUN_ID "$STAGE" build.log build.exit.status \
  --pull=never --network=none --userns=keep-id \
  --volume "$STAGE:$STAGE:Z" "$IMAGE_ID" python3 "$STAGE/kernel.py"
```

The historical `.service` suffix is accepted as an input spelling, but creates no
service. Container naming and resource/lifetime options belong to the launcher.

First run `quirkbench setup`. Choose `RUN_ID=quirkbench-build-NEW_RUN_ID`
and set `STAGE` to the canonical selected state's
`development-runs/$RUN_ID/work` directory. Create both the run directory and work
directory with usable owner permissions; no exact mode is required. `IMAGE_ID`
remains the exact pinned local builder image.
Every run needs a fresh identity; the starter rejects staging in a Git checkout.
Logs, container/boot identity and eventual exit status live beside `work`, so they
can survive disposal of bulky work. The launcher prints the monitor command and
stays attached until completion. Use another terminal for the monitor.

```sh
quirkbench monitor --run "$RUN_ID"
quirkbench monitor --run "$RUN_ID" --once
```

The monitor runs in your existing terminal. No Konsole, desktop environment,
watching agent or popup window is required. Closing the monitor leaves the bounded container
running; Ctrl-C in the launcher terminal stops it. Existing cgroup, CPU, memory and task restrictions remain mandatory;
do not use detached/restarting/replacement containers or override the bounds.
The starter records `queued`, then `running`, then a numeric exit status. An
interrupted recorder can leave a nonterminal status; inspect the recorded container
and verify it stopped before retaining interrupted work rather than treating silence as completion.

Ad hoc commands do not declare which outputs are important. Before disposing of a
successful run's work, explicitly retain each required artifact relative to `work`:

```sh
quirkbench admin storage retain-run "$RUN_ID" --output artifacts/bzImage --output artifacts/vmlinux
quirkbench admin storage prune --dry-run
quirkbench admin storage prune
```

`retain-run` refuses a live container, verifies whole-container shutdown and retains the
selected files in CAS. Choose the complete artifact set for the actual build,
including matching modules, configuration, provenance and symbols; the example is
not a complete kernel release bundle. Failed/interrupted work requires explicit
`retain-run "$RUN_ID" --abandon` before cleanup; optional outputs can still be
retained using `--output`. Failed work remains for seven days after abandonment.
No command here authorizes an experimental boot or changes target storage.

Retention counts, pins and the optional-cache limit are configurable with
`quirkbench admin settings show/set`; see [current storage policy](../docs/local-state-maintenance.md).
New `build`/`compose` staging defaults to a fresh selected-state `workspaces/` directory;
explicit workspaces must also be managed beneath that state. Composition repositories
belong in state `repositories/`. New `image` exports and qualification staging require
fresh managed paths and are retained by count. Software-test fixtures remain temporary.
Housekeeping runs with mutating commands and owner startup/completion; no cron/timer.

Stock RPM acquisition starts with `recovery-inputs acquire-plan` against a fresh
state `inputs/GENERATION` directory. This records pending acquisition and prints a
module-wrapper argv and the exact underlying DNF5 argv; it downloads nothing. Run
the wrapper in the controller's existing Python environment when acquisition is
requested, then pass its returned `directory` (`GENERATION/rpms`) to `lock`. Signature
diagnostics use another fresh managed directory. Verified imports retain RPMs/locks
in CAS and remove the duplicate download generation. Failures preserve bounded
diagnostics and protected/resumable work. The package manager still owns its native
metadata cache; Quirkbench adds no second reusable RPM cache.

For future kernel scripts, derive `limits = ResourceLimits.from_cgroup()` inside
the bounded worker and pass `jobs=limits.jobs` to `KernelBuild` and the same
limits to its runner. Automatic job planning budgets 2 GiB per compiler job,
capped by the CPU quota: a 4-core/4-GiB worker selects two jobs and the default
4-core/8-GiB development worker selects four. The starter caps memory at 8 GiB
or half the controller's reported total, whichever is smaller. Admission also
reserves 2 GiB of currently available controller memory for desktop use, so low
headroom can reject a requested job count. This heuristic does not guarantee peak compiler or
linker memory consumption; cgroups enforce the actual bounds. Controller resource
reserves still apply, and explicit `jobs=1` stays serial. Do not edit a running run's inputs.

The starter creates one foreground rootless Podman container with CPU, memory,
swap, task and elapsed-time bounds, recording Podman's selected manager. It verifies
requested engine settings and actual kernel controls before releasing the payload.
Its trusted PID1 wrapper retains the deadline and stop proof checks descendant
population in the recorded cgroup. `podman stats` reports that container's
resource use. The container is the aggregate limit and shutdown boundary, including
detached descendants. Each build has a unique stage, log and exit-status record;
do not reuse a running build's stage or identity. Recovery workers retain their
separate fixed command and fenced claim.

Interrupted launch acknowledgements retain the recorded creation name and immutable
container identity for reconciliation. Artifact retention verifies whole-container
shutdown before removing the stopped container and making work eligible for cleanup.

The builder includes Fedora source-preparation macros and JSON Schema validation.
Changing its Containerfile requires recapturing the builder identity and package locks;
a previously retained image does not acquire new packages automatically.

### Restricted recovery rootfs worker

`quirkbench.recovery_podman.stage_rootfs_inputs` copies only reviewed catalog,
lock, CAS and Quirkbench source bytes into an empty, private worker stage.
`rootfs_command` then prepares a fixed local Podman invocation against that
stage. It mounts only staged code, the two input files and staged CAS read-only,
plus a private staged output directory writable. Those private copies may be
relabelled with Podman's `:Z` bind option; original source and CAS labels are
untouched. The command uses a local nonroot Podman process with no network,
host device or broad home mount, private PID/IPC/UTS namespaces and bounded
Podman cgroups. It has no arbitrary command or extra-flag parameter.
Its derived image config ID, retained builder archive, catalog and rootfs lock
must match the current worker operation's immutable input record. The existing
recipe `builder_image_digest` still names the Fedora base marker; admission
must separately review the exact derived builder archive/config correspondence.
The planner verifies an OCI archive's sole manifest, expected x86-64/Linux
config and referenced layer hashes before returning an argv. A rebuilt builder
needs its own newly retained archive, image ID and operation input record.

The controller container adapter resolves workload budgets against effective
capacity: 1 GiB for preparation/capture/download, 4 GiB for recovery, up to 8 GiB
for kernel work by default. It requests no extra swap and 4096 tasks, then checks the engine's
recorded bounds before accepting a launch. Every phase has a fixed entry point and
elapsed deadline; the journal records the engine, immutable container ID and claim.
The owner verifies whole-container termination and retains bounded logs before
publishing results or reusing resources. The command plan alone does not execute a
product operation. Stock v2 recovery uses its retained recipe/lock instead of a
candidate catalog.
Admit only complete selected inputs; Distrobox is not a worker prerequisite.

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
the foreground controller. An optional development shell has no worker
ownership role. Run the composition process as container
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
`assemble.ini` is an optional development profile; these extra flags belong to
the dedicated composition container.

`--init` is required for this long-lived container: Podman's init process reaps
orphaned GPG and OSTree helper processes. Using `sleep infinity` directly as PID
1 leaves exited children as zombies and eventually exhausts the process limit.
An optional Distrobox lifecycle remains separate from product workers.

The namespace-scoped SYS_ADMIN capability permits `rofiles-fuse --copyup`, which
protects cached package hardlinks during scriptlets. `/dev/fuse` is its virtual
filesystem interface, not a storage disk. NET_ADMIN permits bubblewrap to set up
loopback in its private network namespace. The seccomp/unmask flags permit nested
namespace creation and procfs mounts. These capabilities do not become host-root
capabilities in rootless Podman. The release gate must verify no block devices in the container and successful
package scriptlets and metadata finalization. Target storage protection is unchanged.

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

## Service ownership

The foreground controller owns durable container workers; see
[installation](../docs/controller-installation.md#foreground-build-and-composition-controller).
The optional assemble development environment has no execution ownership role.
Its temporary home is not a place for persistent state, signing keys or agent
credentials. Controller setup publishes configuration without installing a daemon.


### Resource overrides

The controller, foreground recovery builder and bounded Podman helpers share the
same budget resolver. Set `QUIRKBENCH_RESOURCE_MODE=dedicated` for a headless machine
that need not reserve desktop capacity. Set `QUIRKBENCH_CPUS` and
`QUIRKBENCH_MEMORY_GIB` to positive integers for explicit budgets; foreground
`--cpus`/`--memory-gib` take precedence. Overrides may exceed interactive defaults
but cannot exceed effective CPU affinity/cgroup/memory capacity. Recovery and kernel
work retain a 4 GiB minimum; preparation needs 1 GiB. These are admission floors,
not guarantees that every input fits. Out-of-memory failures retain diagnostics.

Example: `QUIRKBENCH_RESOURCE_MODE=dedicated QUIRKBENCH_CPUS=8
QUIRKBENCH_MEMORY_GIB=16 quirkbench ...` (on one shell line). Worker-side compiler
planning uses the actual enforced budget, with 2 GiB per compiler job; it does not
reserve desktop capacity again. Container identity, task/deadline limits and verified
whole-container shutdown are unaffected. The shell helper supports
`--workload=preparation` before Podman arguments for its fixed preparation callers;
use `PYTHON=/path/to/python` when Quirkbench is installed in a selected interpreter.

Free-space reserve remains independent: managed workers use configured
`reserve_gib`, foreground recovery uses `--free-space-reserve-gib` (default 2),
and low-level `run_commands(..., reserve_bytes=...)` accepts the caller's reserve
instead of imposing another hardcoded value. No reserve override grants cleanup
or target-device access.

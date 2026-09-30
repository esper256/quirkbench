# Development controller archive

The revised first delivery uses manual authenticated controller/target setup;
a full setup/pairing wizard is later work. The provisional archive and worker below
remain partial implementation, not evidence of a complete attended journey. See
[delivery tiers](product-roadmap.md#delivery-contract) and the
[storage policy](architecture.md#storage-protection-policy).

The controller can now be packaged as an unsigned development archive, with
Python code, target assets, schemas, examples and the agent guide. It runs from
an extracted directory without a checkout or virtualenv. Python 3.11+ must
already be available; this does not install host packages or persistent services.

From the development checkout, choose a new output filename under an existing
directory:

```sh
make controller-archive OUTPUT=/absolute/output/controller.tar.gz
```

This builds a wheel in a temporary copied project and packages only its runtime
files. It does not build kernels/images or run release qualification. The returned
JSON and archive's `controller-manifest.json` retain wheel and file hashes; the
manifest explicitly records unsigned, unqualified status.

Extract the archive into a user-owned directory, then run:

```sh
./quirkbench-controller-0.1.0/bin/quirkbench --help
./quirkbench-controller-0.1.0/bin/quirkbench setup-state
./quirkbench-controller-0.1.0/bin/quirkbench setup-check
```

The provisional commands select durable private state and report prerequisites.
They do not claim background-work readiness or implement the complete planned
`setup` wizard. State selection stays valid if the extracted archive is moved.
Run native service checks from the controller shell, because Distrobox's PID 1
and PATH can differ from Bazzite's native service/tool environment. No lingering,
firewall or power policy is changed. Build toolchains stay in the isolated builder.

## Installed rootfs worker

The archive also includes `bin/quirkbench-worker`, which is the configured executable
for `SystemdUserWorkerServices`. Its fixed arguments are the existing state root,
operation ID, worker epoch/generation and private stage directory. It refuses an
invalid, stale, expired or uncontained claim before running the existing restricted
Podman rootfs command. It copies locked CAS inputs, preserves bounded merged
stdout/stderr in `diagnostics/rootfs.log` and records a private
`diagnostics/stage-result.json` with operation/input/claim identities.

The record describes only a rootfs stage. `operation_complete` and `unit_reconciled`
remain false even on completion: the controller owns final publication, remaining
image stages and whole-unit stop/reconciliation. The worker never opens a writable
controller database or publishes output references. Loss of claim or deadline
stops its direct process group; uncertain descendants still require controller
unit reconciliation. Startup/input-staging failures precede private diagnostic
creation and are reported through unit stderr/journal. Ownership is checked
before/after synchronous staging and periodically during execution; the owning
unit's deadline must also bound preparation.

The lifecycle owner can now stop and validate/adopt these rootfs results under
its current claim fence. Complete stock-image intent additionally runs runtime,
dracut and assembly in the fixed worker, then signs and publishes through the current
owner. Explicit `serve` worker/signing configuration enables that executor; installed
persistent service setup remains manual. See the [software handoff](stock-recovery-attended.md). Fake execution establishes software behavior,
not actual Podman containment or a deliverable recovery image. Do not replace the
executable directory while an active service uses it. The fixed recovery-rootfs
service retains its separate 4-GiB contract; development kernel builds use their
own 8-GiB bounded starter.

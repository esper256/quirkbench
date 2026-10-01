# Development controller archive

The current development installation uses manual authenticated controller/target
setup. The planned general release requires guided setup/pairing under the
[fresh-user implementation map](installation-to-patch.md). The provisional archive
and worker below remain partial implementation, not evidence of that complete journey. See
[delivery order](product-roadmap.md#delivery-contract) and the
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

For a fresh installation, extract the archive temporarily outside a checkout and
use its launcher to install the original archive:

```sh
./quirkbench-controller-0.1.0/bin/quirkbench controller-install /absolute/output/controller.tar.gz --json
```

The response's `data.runtime_root` is the canonical runtime beneath
`$XDG_DATA_HOME/quirkbench/controller/VERSION-ARCHIVE_SHA256` (default
`~/.local/share/quirkbench/controller`). Run that directory's `bin/quirkbench`
for initial `setup-state`, `setup-check` and the manual service setup below.
The installation is verified before publication, repeatable and never overwritten
with differing bytes. Installing alone selects no service and starts no work.
Archive checksums establish integrity and identity, not publisher authenticity;
these development archives remain unsigned and unqualified.

After manual service/trust setup, or when upgrading an existing installation:

```sh
quirkbench controller-install /absolute/output/controller.tar.gz --activate --json
quirkbench setup-check
```

Activation refuses queued/running work, unreconciled worker units and unresolved
physical attempts. It excludes CLI publication, stops/verifies the existing fixed
unit, checks exclusive controller ownership, updates the service and all configured
workers together, and switches `~/.local/bin/quirkbench`. It retains private settings,
state and old runtime bytes. Readiness must pass before activation reports success.
A private durable rollback record precedes shutdown; a failed activation restores
and verifies the previous setup. After an interrupted activation, explicitly run
`quirkbench controller-install --rollback --json` after reconciling outstanding work.
Setup reports CLI, configured-service, verified active-service and last-advertised
revision identities. A stored advertisement alone is not live readiness.

`setup-state` creates private controller state at `$XDG_STATE_HOME/quirkbench`
(default `~/.local/state/quirkbench`) and records its identity in the user configuration.
The current directory never selects a new `.quirkbench` root. Existing configured
selections remain authoritative; explicit legacy paths remain available for read-only
inspection. New state/build staging inside Git checkouts is rejected.
Use `quirkbench monitor` for the manual terminal dashboard; see [monitoring](monitoring.md).
Without an installed archive, the checkout's `./environments/quirkbench` development
launcher exposes the same commands without relying on a moved virtualenv's shebang.

The commands select durable private state and report actual configured service
readiness. Manual service/trust setup below is implemented; the complete planned
`setup` wizard remains deferred. State selection stays valid if the extracted archive is moved.
Run service checks from the native controller shell. An optional development
container can expose a different service manager and PATH. No lingering,
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
persistent service setup remains manual. See the [software handoff](recovery-operations.md). Fake execution establishes software behavior,
not actual Podman containment or a deliverable recovery image. Use guarded activation to change installations; do not replace the
executable directory while an active service uses it. The fixed recovery-rootfs
service retains its separate 4-GiB contract; development kernel builds use their
own 8-GiB bounded starter.

## Durable build and composition service

The archive supplies `bin/quirkbench-controller-service`, `bin/quirkbench-job-worker`
and `lib/quirkbench/quirkbench-controller.service`. Copy the template to
`~/.config/systemd/user/quirkbench-controller.service` and replace its two absolute
paths. Keep the installed runtime canonical and separate from persistent state;
do not move/replace it while services use it. The service launcher and fixed job
worker must be in the same installed `bin` directory. Native Python 3.11+, systemd
user services, rootless Podman and OSTree publication/signing tools are prerequisites.
No service, host packages, trust, lingering or power settings are installed automatically.

Provision a complete, private `STATE/private/controller-service.json` (mode 0600)
with explicit existing TLS/device credentials. Illustrative paths below must be
replaced; this example creates no keys or default credentials:

```json
{
  "runtime": "/ABSOLUTE/INSTALL/bin/quirkbench-controller-service",
  "job_worker": "/ABSOLUTE/INSTALL/bin/quirkbench-job-worker",
  "host": "127.0.0.1",
  "port": 8443,
  "cert": "/ABSOLUTE/STATE/quirkbench/private/controller.crt",
  "key": "/ABSOLUTE/STATE/quirkbench/private/controller.key",
  "tokens_file": "/ABSOLUTE/STATE/quirkbench/private/device-tokens.json",
  "builder_image_digest": "sha256:REPLACE_WITH_FEDORA_BASE_MARKER_DIGEST",
  "builder_config_digest": "sha256:REPLACE_WITH_ACTUAL_BUILDER_IMAGE_CONFIG_ID",
  "builder_archive_sha256": "REPLACE_WITH_RETAINED_OCI_ARCHIVE_SHA256",
  "repositories": {"lab": "/ABSOLUTE/STATE/quirkbench/repositories/lab"},
  "composition_signing": {
    "home": "/ABSOLUTE/STATE/quirkbench/private/gnupg",
    "fingerprint": "REPLACE_WITH_FULL_PROVISIONED_SIGNING_FINGERPRINT"
  }
}
```

The repository parent must already exist. Composition's input repository alias,
`signing_home` and fingerprint must match this private configuration. The worker
receives an empty signing-home placeholder, never these keys. Optional recovery
coordinator fields are `recovery_worker`, `recovery_signing_home`,
`recovery_public_key` and `recovery_fingerprint`; its resource policy stays separate.
LAN listening remains an explicit `allow_lan` configuration choice with existing TLS
verification. Keep target manual trust/repository provisioning unchanged.

These are three different identities. `builder_image_digest` is the immutable
Fedora base marker also used by existing build provenance; the fixed worker runs
`builder_config_digest`, after verifying its retained OCI archive and layers.
Do not substitute the Fedora base image for the finished builder. Newly admitted
build/compose jobs use argument version 2. Legacy argument records remain readable,
but ambiguous old jobs require explicit resubmission with a new request ID.
`build` and `compose` accept explicit `--builder-archive` and
`--builder-config-digest` overrides when using separately retained inputs.

Service presence refreshes independently of validation, signing and housekeeping.
It establishes a current owner; operation phases and measured output establish
job advancement. One does not imply the other. Run `quirkbench setup-check` to inspect this installation's current readiness.
Investigation-specific setup histories are not product prerequisites.

After provisioning, explicitly run:

```sh
systemctl --user daemon-reload
systemctl --user start quirkbench-controller.service
quirkbench setup-check
```

`setup-check` verifies the current controller epoch/boot/PID, canonical installed
runtime, configured private inputs and actual live user unit. Background submission
is refused until ready. Service restart interrupts lost/queued build work;
`operation resume JOB_ID --request-id NEW_REQUEST_ID` requests reconciliation and
fresh-generation execution. A paused campaign still blocks the next stage.
Logout, sleep and reboot behavior follows the existing reported host settings;
this setup does not change them. These instructions are software setup guidance,
not live service/containment or release qualification evidence.

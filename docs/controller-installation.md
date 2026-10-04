# Development controller archive

The development installation provides authenticated setup and pairing foundations.
Complete installed/native acceptance is tracked in
[GitHub #29](https://github.com/esper256/quirkbench/issues/29), against the
[fresh-user acceptance guide](installation-to-patch.md). The provisional archive
and worker below are not evidence of that complete journey. See
[delivery order](product-roadmap.md#delivery-contract) and the
[storage policy](architecture.md#storage-protection-policy).

For cloud or container **software development**, start with the
[portable development instructions](testing-policy.md#portable-software-development).
Installing the Python package, running software tests and packaging an archive do
not require a running controller or container engine. A controller that runs jobs
uses an explicitly started foreground process and bounded worker containers. No
host systemd manager, user unit, login session bus or lingering is required.
Systemd remains part of the target recovery and experiment operating systems.

## Choosing the controller host

Choose capabilities in the environment where the controller will actually run:
Python 3.11+, Linux cgroups v2, and a local Podman or Docker engine that can enforce
CPU, memory and process limits. Workers need an exact, already available builder
image ID. Rootless Podman is required for managed composition and installroot
ownership mapping; Docker supports software preparation/build workers and the
separate foreground recovery artifact builder. This does not claim every backend
or distribution has been natively qualified.

A Fedora Distrobox shell on Bazzite can use the controller when those capabilities
are available there. The cloud development environment needs software tests and
artifact generation only; it does not need target connectivity or a live deployment.
OpenSSL is needed for TLS setup. OSTree and GnuPG are additional prerequisites for
repository publication. Install tools through the chosen distribution's normal
package mechanisms. Check engine capability in the actual execution environment:

```sh
python3 --version
stat -fc %T /sys/fs/cgroup
podman --remote=false --cgroup-manager=cgroupfs info
# Or, for the Docker-supported paths:
docker info
```

State defaults outside Git checkouts; explicitly selected state may be checkout-local.
Keep installed runtime and signing keys outside source control. Do not
share a live controller state directory between machines. For a separate controller,
transfer the development archive through an authenticated channel and install with
that machine's Python; do not copy a development virtualenv.

Continue with [initial controller setup](#initial-controller-setup), then inspect
`status --json` and `setup-check`. Builder availability, authenticated target
enrollment, target connectivity and exact-attempt approval remain separate facts.

## Signing prerequisites

Production recovery and OSTree composition signing use the explicitly configured
external GnuPG home. Before configuring it, check
`gpgconf --homedir /absolute/private/signing-home --list-dirs agent-socket` and
`gpgconf --homedir /absolute/private/signing-home --launch gpg-agent`.
On Linux, a pathname socket must fit within 107 bytes plus its terminator; a valid
filesystem path can still be too long for the agent. GnuPG may select a shorter
runtime socket directory, so inspect its actual choice. If agent launch fails due
to path length, provision a shorter private signing home or a supported GnuPG socket
directory before signing. Do not relocate keys into worker/output directories.
Release-download verification uses an isolated public keyring with `--no-autostart`
and does not need a signing agent; shortening its input paths is unnecessary.

## Packaging and installation

The controller can now be packaged as an unsigned development archive, with
Python code, target assets, schemas, examples and the agent guide. It runs from
an extracted directory without a checkout or virtualenv. Python 3.11+ must
already be available. Archive installation does not change host packages or services;
initial setup publishes configuration for an explicitly started foreground controller.

Archive input paths may use ordinary ancestor aliases (including a linked home
directory); installation records use canonical paths. The archive itself must be
a regular file, and changing its bytes, leaf or ancestor alias during capture is
rejected. Managed runtime destinations retain their existing no-symlink rules.

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
for initial `setup`, `status` and the foreground setup below.
The installation is verified before publication, repeatable and never overwritten
with differing bytes. Installing alone selects no service and starts no work.
Archive checksums establish integrity and identity, not publisher authenticity;
these development archives remain unsigned and unqualified.

The M1c verification foundation accepts an independently authenticated publisher
key and full fingerprint, a canonical release-set statement and detached signature:

```sh
bin/quirkbench controller-install /downloads/controller.tar.gz --json \
  --release-statement /downloads/release.json --release-signature /downloads/release.sig \
  --release-key /trusted/publisher.asc --release-fingerprint FULL_PUBLISHER_FINGERPRINT
```

All four verification arguments are required together. The key must be outside
the download directory and cannot be adopted from the archive. Native `gpg` uses
an isolated keyring, explicit signer, no ambient options or key retrieval. Expiry,
architecture, API/Python compatibility, archive digest/version are checked before
installation publishes its captured bytes. The distribution receipt authenticates
only the statement/controller bytes unless all three `--release-recovery`,
`--release-builder`, `--release-catalog` paths are supplied; those assets are then
streamed and checked against the signed digests. This verifies bytes, not runtime
compatibility or image qualification. Development `signed`/`qualified` flags remain
false. No publisher key or compatible signed release set is bundled yet, so this is
an explicit trust-input foundation, not the completed `./quirkbench/install` release
journey or automatic key rotation.

After controller/trust setup, or when upgrading an existing installation:

```sh
quirkbench controller-install /absolute/output/controller.tar.gz --activate --json
quirkbench setup-check
```

Activation refuses queued/running work, unreconciled worker identities and unresolved
physical attempts. Stop the foreground controller first. Activation checks exclusive
lifecycle ownership, updates the configured runtime/worker paths together, and switches
`~/.local/bin/quirkbench`. It preserves private settings, state and the old runtime.
A durable rollback record precedes publication; failed publication restores the old
selection. Successful activation reports that controller startup is still required.
After interruption, run `quirkbench controller-install --rollback --json` once
outstanding work has been reconciled. No upgrade or rollback starts a daemon.
Setup reports CLI, configured-service, verified active-service and last-advertised
revision identities. A stored advertisement alone is not live readiness.

`setup-state` creates private controller state at `$XDG_STATE_HOME/quirkbench`
(default `~/.local/state/quirkbench`) and records its identity in the user configuration.
The current directory never selects a new `.quirkbench` root. Existing configured
selections remain authoritative; explicit legacy paths remain available for read-only
inspection. New state/build staging inside Git checkouts is rejected.
Empty sandbox `.git` guards are allowed; linked worktrees and ambiguous metadata
remain blocked. Controller build paths may also use explicitly selected
scratch beneath `/var/tmp` or a build-storage volume under `/mnt` or `/media`.
Create a dedicated directory owned by the executing user first
(for example `/mnt/build-volume/quirkbench`). The volume must already be mounted;
Quirkbench does not mount it or change permissions. The directory must be below the
mount root, contain no nested mounts and have no symlink ancestors. Paths below
it must remain user-owned. Usable user-selected permissions are preserved; no
special umask or mode is required. System trees such as `/var/lib`,
`/dev`, `/etc` and `/usr` remain forbidden. Use home state or a persistent volume
for durable work; temporary storage may be cleaned by the host. These controller
path choices do not change the target's boot-device-only storage policy.
Use `quirkbench monitor` for the manual terminal dashboard; see [monitoring](monitoring.md).
Without an installed archive, the checkout's `./environments/quirkbench` development
launcher exposes the same commands without relying on a moved virtualenv's shebang.

`setup` now journals initial state/resource/connection/logout choices and resumes
interrupted setup. For example, run the installed launcher with
`setup --request-id initial-setup --runtime /absolute/installed/runtime --json`.
Human `setup` generates and prints a retry ID; repeated calls reuse its recorded
choices. Machine calls require that ID. Use `status --json` for independent readiness
without creating state or starting services. A successful setup acknowledgment is
partial: release/setup binding, builder readiness and the M2 enrollment exchange remain pending.
Connection choices do not activate a listener. Historical `existing_linger` records describe
the desired policy; check the observed logout behavior. Existing initialized databases
are inspected without migrations; active owners/work and changed recorded choices
block setup. `setup-state` and `setup-check` remain supported.
State selection stays valid if the extracted archive is moved.
Run capability checks from the controller shell. An optional development
container can expose different engines, cgroups and PATH. No daemon,
firewall or power policy is changed. Build toolchains stay in the isolated builder.

## Installed rootfs worker

For M1b bootstrap, controller service configuration can select
`"credential_registry": true` instead of `tokens_file`. These modes are mutually
exclusive. `serve --credential-registry` starts with zero credentials and denies
all target routes until a complete credential generation exists. Repository
`serve-repository --credential-registry` requires both mutual TLS and a live exact
leaf-certificate fingerprint in the same database. Static manually provisioned
authentication remains supported. Both registry channels inspect revocation for
each request; admitted requests/physical attempts are not cancelled by revocation.
Local record/revoke helpers are administrative foundations, not pairing/retargeting
APIs. No anonymous pairing route or fabricated initial target is supplied. Guided
C4 exchange and private activation remain M2.

Signed installations retain their original authenticated controller archive under
the data home, outside optional download cache. Read-only status rechecks independent
publisher trust, durable signature/statement and exact installed files against that
archive. A matching edited local manifest cannot confer publisher authentication.
Missing or changed required provenance blocks signed readiness.

After signed release installation and foreground controller setup, use
`quirkbench setup --builder-archive /absolute/builder.tar --builder-request-id ID`.
The archive must match release-set v2. Admission returns a durable operation ID;
the existing lifecycle captures/hashes it, reconciles the whole worker, retains its
OCI closure, then imports a private copy into rootless Podman. The bounded launcher
checks the exact image/base marker without networking. Setup builders must have no
OCI Entrypoint. Staging preserves the configured reserve; the required input group
is pinned until explicitly unpinned through maintenance. Status checks current image
availability separately from historical preparation, baseline closure and qualification.
Interrupted work uses the existing explicit operation resume path. Fully configured
manual builder bindings remain supported; partial overrides of a signed builder must
match its exact base/config/archive tuple. No host packages or popup viewer are started.

Controller worker execution uses fixed preparation and payload containers. The
foreground controller records the engine and immutable container identities before
starting them, retains bounded diagnostics, verifies whole-container shutdown, and
only then publishes results through the existing epoch/claim fences. Preparation
can read controller metadata but cannot see configured secret locations. Build
payloads receive captured inputs and output staging, never the controller database,
signing credentials or engine socket. The engine enforces CPU, memory, swap and
process limits; the fixed worker entry point also enforces its deadline.

Interrupted work remains fenced until its recorded containers are reconciled.
A controller reboot alone does not prove an engine container stopped. Old service
worker records remain readable; unresolved same-boot legacy workers need their
original installation's shutdown or a host reboot before migration can proceed.
No old `.service` identity is silently treated as a stopped container.
Before upgrading an old daemon deployment, stop and disable that previously configured
supervisor. The new installation does not manage or restart it. Preserve the old
runtime until legacy workers and any interrupted installation journal are reconciled.

## Foreground build and composition controller

The archive supplies the CLI and fixed worker entry points. The historical
`bin/quirkbench-controller-service` executable remains a foreground compatibility
entry point. No controller unit template is installed. Keep the runtime canonical
and use guarded activation while the foreground controller is stopped.
`setup --configure-controller` publishes local TLS, configuration and the CLI
launcher; start execution separately with `controller-run`.

Provision a complete `STATE/private/controller-service.json` pointing to existing
TLS/device credentials. Keep actual keys and tokens in a private file or secret
store; the configuration record itself has no exact-mode requirement. Illustrative paths below must be
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

The current service's `builder_archive_sha256` is a live retention root, including
across controller activation and restart. Input-history expiry does not delete
that selected archive. Removing or replacing the binding permits ordinary cleanup
when no other owner or pin needs the old bytes. Housekeeping reports an already
missing configured archive; it cannot regenerate or replace bytes under its digest.

These are three different identities. `builder_image_digest` is the immutable
Fedora base marker also used by existing build provenance; the fixed worker runs
`builder_config_digest`, after verifying its retained OCI archive and layers.
Do not substitute the Fedora base image for the finished builder. Newly admitted
build/compose jobs use argument version 2. Legacy argument records remain readable,
but ambiguous old jobs require explicit resubmission with a new request ID.
`build` and `compose` accept explicit `--builder-archive` and
`--builder-config-digest` overrides when using separately retained inputs.

Foreground owner presence refreshes independently of validation, signing and housekeeping.
It establishes a current owner; operation phases and measured output establish
job advancement. One does not imply the other. Run `quirkbench setup-check` to inspect this installation's current readiness.
Investigation-specific setup histories are not product prerequisites.

After provisioning, explicitly run:

```sh
quirkbench controller-run --engine podman --worker-image sha256:EXACT_LOCAL_BUILDER_IMAGE_ID
# In a second terminal:
quirkbench setup-check
```

`setup-check` verifies the controller epoch/boot/PID and the live process's exclusive
lifecycle lock, together with its configured runtime. Submission is refused until
that owner is ready. Stop with Ctrl-C before maintenance or activation. Restart
reconciles interrupted workers; explicit operation resume uses a fresh generation.
A paused campaign still blocks the next stage. Foreground execution does not install
automatic login/boot restart, and sleep pauses execution.

Load a verified builder archive into the selected local engine before first startup
(`podman load --input /absolute/builder.tar`, or `docker load` for supported Docker
paths), then select its exact image config ID with `--worker-image`. The image needs
Quirkbench's Fedora builder tools; a plain Fedora base image is insufficient.
This bootstrap load does not replace the application's signed builder binding or
its later retained import/provenance checks. Configuration may persist `worker_engine`
and `worker_image`; a complete `builder_config_digest` also supplies the image default.

## Signed release acquisition software foundation

Use [publication preflight and the operator runbook](release-publication.md) to
check the signed asset set and selected pinned baseline closure without installing
or publishing. This software check preserves separate production/native gates.

The archive includes an executable `install` beside `bin` and `lib`:

```sh
./install VERSION --request-id INSTALL_REQUEST --json
```

It uses only installed `production-release-trust.json` and its independently supplied
publisher key. No production bundle is shipped yet: this path returns `UNAVAILABLE`
before downloading or creating state. Explicit `--trust-bundle PATH` is for separately
provisioned trust; keys from the unverified download directory are rejected. No test
publisher identity is included. The existing unsigned `controller-install ARCHIVE`
path remains an explicit development option.

Acquisition pins HTTPS location, exact key SHA256/full fingerprint, signed statement
and archive identity. Reusing a request ID with different choices conflicts. Retry
checks actual immutable runtime bytes; the optional download cache can be rebuilt,
while accepted metadata and results remain in private configuration. No service is
activated by release acquisition. Production publisher provisioning, rotation and
release publication are outstanding release acceptance work.

Installer and recovery acquisition share the bounded native HTTPS transport.
DNS resolution runs in a fixed isolated, time-limited child; connection attempts,
TLS, status, headers and body consume the same monotonic deadline. Native hostname
verification remains enabled. Redirects and ambiguous/compressed framing are refused.
Metadata has a 45-second total deadline and bounded size; recovery image streaming
has its separately bounded worker lifetime. These limits supply no publisher trust.

Release-set v2 declares the supported interface versions and authenticates recovery
candidate/manifest metadata plus the existing three assets. For explicit asset
verification, supply both `--release-recovery-manifest` and
`--release-recovery-candidate` alongside recovery image, builder and catalog paths.
The verifier reuses native stock factory/catalog/OCI readers. Structural compatibility,
builder runtime readiness, retained baseline inputs and qualification are separate;
asset paths in receipts must be rehashed before later import/boot. v1 statements and
legacy development archives remain readable.

## Initial controller setup

From a managed installed runtime, initial setup discovers its own immutable runtime:

```sh
bin/quirkbench setup --request-id INITIAL_SETUP --configure-controller --json
```

This creates a private local controller CA/server certificate for the specific
bind IP and publishes the configuration and launcher. It starts no process and
requires no service manager. `--start-service` remains a compatibility alias for
this configuration step. Use the returned `next_command` to start the controller,
selecting a pinned worker image as described above. OpenSSL 3 is required for TLS.
LAN binding still requires `--allow-lan`. No target credentials or execution approval
are created.

Setup preserves exact intent and existing TLS keys across interruption. Differing
or missing committed inputs require explicit maintenance. Version-1 setup histories
are preserved separately when migrated; old unit/start steps never become evidence
of current foreground readiness. A completed setup can be inspected while the owner
runs. Status keeps historical setup, live ownership, pairing, builder availability
and qualification separate.

For a fresh empty-registry installation, explicitly provision an existing private
operator GnuPG home and composition signing key, then configure its first repository:

```sh
# Stop the foreground controller with Ctrl-C before maintenance.
quirkbench publication setup --repository SELECTED_ALIAS \
  --url https://CONTROLLER_IP:REPOSITORY_PORT \
  --signing-home /absolute/private/operator-gnupg \
  --fingerprint FULL_UPPERCASE_SIGNING_FINGERPRINT --request-id PUBLICATION_SETUP --json
quirkbench controller-run  # run status/enrollment commands in another terminal
quirkbench status --json
quirkbench target add SELECTED_NAME --request-id INVITATION --json
```

Use the specific IP already covered by the controller certificate and a separate
repository port. Choose a fresh alias; its repository lives beneath the selected
controller state. This command creates no keys, selects no implicit signing identity,
changes no existing publication/trust, and neither starts the service nor issues a
target credential. Missing OSTree/GPG prerequisites remain actionable
errors; package installation and signing-key provisioning are explicit operator work.

Keep the same request ID and choices after interruption. Private original/new
configuration, public signing-key bytes, TLS identities and repository initialization
progress are retained. Replay accepts only that exact successor configuration;
unrelated maintenance, existing invitations/credentials, changed keys/TLS, unsafe
repository contents or changed ownership block continuation. An interrupted
native initialization without a config must have an empty, unmounted directory;
other partial contents require reconciliation, never automatic deletion. Completed
replay returns the historical acknowledgment and does not establish current readiness.
Ordinary `setup --start-service` retries accept the verified journaled successor.
Existing manually configured publication and endpoint maintenance remain supported.

Starting the configured native service publishes its repository and enrollment listeners under the existing
owner; the repository uses mutual TLS plus exact registered leaf lookup. Setup
status observes a separate, current owner/configuration/TLS capability. Missing
publication remains unavailable. Use `quirkbench target add NAME` to display the
short-lived invitation, endpoint, code ID and full certificate SHA-256; machine
add supplies `--request-id ID --json`. `target show NAME` reads public recorded facts
and does not establish current connectivity or authorize experiments.

### Connected-target software journey

With a compatible authenticated release-set v2 installation, use
`quirkbench recovery download --request-id RECOVERY_ACQUISITION --json` to acquire
the exact factory image/metadata through the existing service. Explicit independently
provisioned publisher trust uses `--trust-bundle /absolute/TRUST_BUNDLE`; the default
production bundle/assets are still a [publisher delivery gate](https://github.com/esper256/quirkbench/issues/41).
Changed or missing pinned bytes are blockers, not permission to substitute a moving
release or rebuild a recovery image. Acquisition reports unqualified retained assets;
it never authorizes flashing. Use a standard image writer only under separate physical
authorization, with the exact externally selected device and acquired image identity.

On supported x86-64/UEFI/USB recovery, explicitly confirm the displayed external disk
GUID/capacity before commissioning; a wrong confirmation or changed/insufficient
capacity leaves storage blocked. See [recovery operations](recovery-operations.md)
for the existing attended screen and resumable journal. Choose **Network**, then
**Connect to controller**. Compare/type the full certificate SHA-256 before entering
the one-use invitation. Lost replies retry the retained request/key. After activation,
**Save selected network connections** retains only explicit private selections, which
later boot restores only for the original target/media/configuration. A changed target
requires explicit retarget maintenance; revocation and endpoint repair retain their
existing commands and authority boundaries.

`target show NAME` and `status` keep recorded enrollment, current owner availability,
recovery reports, candidate readiness and execution approval separate. Successful
pairing or a completed capacity journal establishes none of the other facts. The
installed software journey is tested with disposable trust, synthetic media and native
adapters; real publication, native service survival, recovery boot and hardware behavior
remain [attended commissioning work](https://github.com/esper256/quirkbench/issues/43).

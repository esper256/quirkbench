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
podman --remote=false info
# Or, for the Docker-supported paths:
docker info
```

State defaults outside Git checkouts; explicitly selected state may be checkout-local.

Podman uses its configured `systemd` or `cgroupfs` manager. This is independent of
Quirkbench's foreground lifecycle and does not require a Quirkbench systemd service.
Use `--podman-cgroup-manager systemd|cgroupfs` (or `worker_cgroup_manager` in the
controller configuration) only for a deliberate override. The selected manager is
recorded before creation and replayed for inspection and reconciliation. Missing
delegation or ineffective CPU, memory, zero-swap or PID limits blocks payload
execution. Recorded cgroup descendant population must be empty before completion.
Legacy Podman journals replay `cgroupfs`; an unknown historical cgroup remains
fenced until stronger stop evidence or host reboot. Native support still requires
commissioning in the actual environment; software fixtures do not qualify a host.
Keep installed runtime and signing keys outside source control. Do not
share a live controller state directory between machines. For a separate controller,
transfer the development archive through an authenticated channel and install with
that machine's Python; do not copy a development virtualenv.

Continue with [initial controller setup](#initial-controller-setup), then inspect
`status --json` and `doctor`. Builder availability, authenticated target
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

## Run from a source checkout

On Linux with Python 3.11 or newer, clone and run:

```sh
git clone https://github.com/esper256/quirkbench.git
cd quirkbench
./quirkbench --help
./quirkbench --version
```

The root executable uses the checkout directly. No virtualenv activation, Python
package installation or dependency download is needed: the application runtime uses
Python's standard library. Missing or older Python produces an actionable error.
Help and runtime identity work offline without controller setup. Commands that need
external tools or configured controller state still require those prerequisites;
see the relevant guide. Test and archive-build dependencies are separate development
tools, described in [development setup](testing-policy.md#portable-software-development).

To use `quirkbench` from any directory, optionally create your own symlink (choose
an unused destination and ensure `~/.local/bin` is on your PATH):

```sh
mkdir -p ~/.local/bin
ln -s "$PWD/quirkbench" ~/.local/bin/quirkbench
quirkbench --version --json
```

The launcher follows symlinks to the checkout, including paths containing spaces.
Keep that checkout in place; the command follows its current code as you change
branches or pull updates. Running it creates no home-bin link, selects no installed
runtime and starts no controller service. Ordinary commands use the existing state
selection rules. The older `environments/quirkbench` entry point remains compatible.

`--version` (or `--version --json` for structured output) reports the running CLI's package path,
Python version and checkout commit plus local-change status when Git is available.
An extracted archive reports its manifest hash, and an installed archive also reports
the archive hash from its installation record. Unknown source revisions remain
explicitly unavailable. These are CLI identity observations, not publisher verification,
qualification or proof that a configured controller is running the same code.

## Packaging and installation

For Eric's current local development workflow, use this temporary repository
script from the host, with a clean `main` checkout:

```sh
python3 /var/home/eric/dev/quirkbench/development/update-local-install.py
```

It pulls main, packages the current code using the existing development environment
when present, retains the archive and packaging log outside Git, and installs the
command in `~/.local/bin`. Existing configured installations use guarded activation;
the controller must be stopped and work reconciled. Fresh installation selects the
runtime and command without initializing a database or configuring a controller.
An incompatible database produces a complete copyable explicit reset command;
reset archives an unused controller, never silently deletes state. Rerun the updater
after reset. This developer convenience does not select a future distribution format.
Packaging still requires setuptools and wheel; see the development testing guide.

The controller can now be packaged as an unsigned development archive, with
Python code, target assets, schemas, examples and the agent guide. It runs from
an extracted directory without a checkout or virtualenv. Python 3.11+ must
already be available. Archive installation does not change host packages or services;
initial setup publishes configuration for an explicitly started foreground controller.

Archive input paths may use ordinary ancestor aliases (including a linked home
directory). Installation records retain version and archive identity, not their
location. The archive itself must be
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
./quirkbench-controller-0.1.0/bin/quirkbench dev install /absolute/output/controller.tar.gz --json
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
bin/quirkbench dev install /downloads/controller.tar.gz --json \
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
quirkbench dev install /absolute/output/controller.tar.gz --activate --json
quirkbench doctor
```

Activation refuses queued/running work, unreconciled worker identities and unresolved
physical attempts. Stop the foreground controller first. Activation checks exclusive
lifecycle ownership, updates the configured software identity and local installation
selection together, and switches
`~/.local/bin/quirkbench`. It preserves private settings, state and the old runtime.
A durable rollback record precedes publication; failed publication restores the old
selection. Successful activation reports that controller startup is still required.
After interruption, run `quirkbench dev install --rollback --json` once
outstanding work has been reconciled. No upgrade or rollback starts a daemon.
Setup reports CLI, configured-service, verified active-service and last-advertised
revision identities. A stored advertisement alone is not live readiness.

`setup` creates controller state at `$XDG_STATE_HOME/quirkbench`
(default `~/.local/state/quirkbench`). Defaults are discovered at invocation and are
not copied into configuration. A custom state location is selected once in
`$XDG_CONFIG_HOME/quirkbench/controller.json` (default `~/.config/quirkbench`):
`{"schema_version":1,"state_root":"~/work/quirkbench-state"}`. Absolute selections
are also supported. Explicit `--state` takes precedence over a missing configured
location; a missing configured directory otherwise reports an error.

Selected symlinks resolve through ordinary Unix pathname resolution before work
begins. Files inside state follow the managed layout; their locations are derived
from workspace, operation, request or artifact IDs. An external selected resource
must still be visible to the process and container engine that use it. Explicit
state/build/output selections may be inside a checkout. System trees remain
protected and admission grants no recursive deletion of user-selected output.

The revised development formats require fresh initialization. For an unused
controller, explicitly [reset it in place](#start-over-after-unsuccessful-setup),
then repeat setup. Used or unknown incompatible state must be preserved for explicit
recovery; a new directory is an optional separate fresh start, not an automatic
workaround. There is no conversion or automatic reset.
Current-format backup/restore retains content integrity and leaves scheduling
paused. Restore private configuration and required external selections separately.
Moving stopped managed data does not authorize resuming work or running a target;
recorded active workers must still be reconciled.

Use `quirkbench monitor` for the manual terminal dashboard; see [monitoring](monitoring.md).
Without an installed archive, the checkout's `./quirkbench` launcher exposes the
same commands; see [running from a checkout](#run-from-a-source-checkout).

`setup` now journals initial state/resource/connection/logout choices and resumes
interrupted setup. For example, run the installed launcher with
`setup --request-id initial-setup --runtime /absolute/installed/runtime --json`.
Human `setup` generates and prints a retry ID; repeated calls reuse its recorded
choices. JSON calls follow the same retry rules. Use `status --json` for independent readiness
without creating state or starting services. A successful setup acknowledgment is
partial: release/setup binding, builder readiness and the M2 enrollment exchange remain pending.
Connection choices do not activate a listener. Historical `existing_linger` records describe
the desired policy; check the observed logout behavior. Existing initialized databases
must match the current development format; active owners/work and changed recorded choices
block setup. `setup` and `doctor` remain supported.
State selection stays valid if the extracted archive is moved.
Run capability checks from the controller shell. An optional development
container can expose different engines, cgroups and PATH. No daemon,
firewall or power policy is changed. Build toolchains stay in the isolated builder.

## Installed rootfs worker

For M1b bootstrap, controller service configuration can select
`"credential_registry": true` instead of `tokens_file`. These modes are mutually
exclusive. The configured controller's registry mode starts with zero credentials
and denies target execution routes until a complete credential generation exists.
The configured repository listener requires both mutual TLS and a live exact
leaf-certificate fingerprint in the same database. Start the configured controller
with `quirkbench admin controller run`; raw listener commands are private process
entry points. Static manually provisioned
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
Interrupted work uses the existing explicit admin operation resume path. Fully configured
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
A controller reboot alone does not prove an engine container stopped. Reconciliation
uses recorded engine/container identities, boot IDs and worker generations, then
checks whole-worker shutdown. Missing prior workers are never presumed stopped
because their old directory is unavailable.

## Foreground build and composition controller

The archive supplies the CLI and fixed worker entry points. The historical
`bin/quirkbench-controller-service` executable remains a foreground compatibility
entry point. No controller unit template is installed. Use guarded activation
while the foreground controller is stopped.
`setup --configure-controller` publishes local TLS and controller configuration.
It leaves command installation and any user-selected CLI link unchanged; start
execution separately with `admin controller run`.

Use `setup --configure-controller` to create `STATE/private/controller-service.json`
and its managed TLS identity. The service record contains software version/archive
identity, a TLS setup request ID, connection choices and optional worker/publication
settings. Executable and TLS filenames are derived at startup; do not add `runtime`,
`job_worker`, `cert` or `key` paths to that file. For example, its base configuration
has this shape (substitute identities returned by your own setup):

```json
{
  "software": {"version": "0.1.0", "archive_sha256": "REPLACE_WITH_ARCHIVE_SHA256"},
  "tls_identity": {"kind": "setup", "request_id": "initial-setup"},
  "host": "127.0.0.1",
  "port": 8443,
  "credential_registry": true,
  "reserve_gib": 20
}
```

Use `admin repository configure` for publication. Managed repositories are selected
by alias; their directories are `STATE/repositories/ALIAS`. Composition signing
retains a fingerprint and, only for an external selection, a signing `home`.
Its default is `STATE/private/gnupg`. Absolute and `~/` external selections may use
symlinks. A missing signing home blocks signing rather than unrelated inspection.
Signing keys never enter workers. The worker receives an empty signing-home
placeholder. For manually provisioned authentication, `tokens_file` is an external
local selection instead of `credential_registry: true`.

Optional recovery configuration uses `recovery_enabled: true`,
`recovery_signing_home`, `recovery_public_key` and `recovery_fingerprint`.
The worker executable is derived from the selected verified installation.
Resource policy and LAN `allow_lan` authorization remain separate.

Installation defaults use current XDG directories. Necessary overrides live once
in `installation-settings.json` beside `controller.json`: `data_home`, `cache_home`
and `bin_home`. `installation.json` selects software by version and archive digest.
`builder-input.json`, `recovery-input.json`, `source-selections.json` and
`job-inputs/REQUEST_ID.json` in state hold the corresponding external input choices;
immutable operation records retain their expected content identities, not those paths.
These nonsecret input selections are outside `private/` so trusted preparation
containers can read them while private credentials remain hidden. A location edit
cannot substitute different bytes, trust or authorization. OS launchers outside
managed roots may need regeneration through existing installation/setup tooling.

The current service's `builder_archive_sha256` is a live retention root, including
across controller activation and restart. Input-history expiry does not delete
that selected archive. Removing or replacing the binding permits ordinary cleanup
when no other owner or pin needs the old bytes. Housekeeping reports an already
missing configured archive; it cannot regenerate or replace bytes under its digest.

These are three different identities. `builder_image_digest` is the immutable
Fedora base marker also used by existing build provenance; the fixed worker runs
`builder_config_digest`, after verifying its retained OCI archive and layers.
Do not substitute the Fedora base image for the finished builder. Newly admitted
manual build/compose jobs use argument version 2. Superseded path-bearing records
are rejected; use fresh development state with the current configuration.
`build` and `compose` accept explicit `--builder-archive` and
`--builder-config-digest` overrides when using separately retained inputs.

New composition identities (version 2 in deployment provenance) fingerprint the
exact candidate modules, selected recipes/units and generated runtime settings.
Controller-only modules and unused recovery assets do not change that payload
identity. Composer and installation-policy source hashes are recorded separately;
the final composed tree is audited before signing or publication. Older retained
build identities remain opaque and keep their original meaning; unsigned staged
legacy output must be resubmitted to obtain the current publication checks. Software fixtures
verify these checks; native composition and boot acceptance remain separate.

Foreground owner presence refreshes independently of validation, signing and housekeeping.
It establishes a current owner; operation phases and measured output establish
job advancement. One does not imply the other. Run `quirkbench doctor` to inspect this installation's current readiness.
Investigation-specific setup histories are not product prerequisites.

After provisioning, explicitly run:

```sh
quirkbench admin controller run --engine podman --worker-image sha256:EXACT_LOCAL_BUILDER_IMAGE_ID
# In a second terminal:
quirkbench doctor
```

`doctor` verifies the controller epoch/boot/PID and the live process's exclusive
lifecycle lock, together with its configured runtime. Submission is refused until
that owner is ready. Stop with Ctrl-C before maintenance or activation. Restart
reconciles interrupted workers; explicit admin operation resume uses a fresh generation.
A paused campaign still blocks the next stage. Foreground execution does not install
automatic login/boot restart, and sleep pauses execution.

Start the configured foreground controller before preparing its first builder.
Authenticated status, target communication and evidence remain available when the
local engine or selected image is missing. `status`/`doctor` report
`compute_ready` separately from controller availability and worker reconciliation.
New compute remains blocked until its prerequisites and exact prior-worker stop
are established; an engine outage never proves an uncertain worker stopped.

For a signed release-set v2 installation, admit the matching archive with
`setup --builder-archive /absolute/builder.tar --builder-request-id ID`. The existing
owner captures and verifies signed bytes, imports them through the selected local
engine, then checks the exact image/base marker in a bounded container. This path
requires no pre-existing builder image. Interrupted preparation requires explicit
resume after exact subprocess/container stop reconciliation. Configuration can
still select `worker_engine`, `worker_image` or `builder_config_digest` for development
bindings. A plain Fedora base image is insufficient. Software fixtures establish
the bootstrap boundaries; native backend commissioning remains separate.

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
publisher identity is included. The existing unsigned `quirkbench dev install ARCHIVE`
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
bind IP and publishes the configuration. It leaves PATH commands unchanged, starts
no process and requires no service manager. `--configure-controller` remains a compatibility alias for
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

### Start over after unsuccessful setup

For an **unused controller** (no enrolled targets, attempts, credentials or bound
enrollment), explicitly stop its controller and archive its database/setup records:

```sh
./quirkbench --state /absolute/controller-state admin controller reset \
  --request-id fresh-start-1 --confirm-reset
```

Use a CLI containing this command; an older installed runtime will not have it.
Confirmation also authorizes graceful shutdown of the verified running controller.
The command waits at most 30 seconds for process exit, then checks the existing
locks and worker shutdown proofs. It never force-kills an unknown process. If work
remains unreconciled, the controller may be stopped while reset is still refused;
the database remains available for reconciliation. Other commands/builds holding
locks are named separately.

The result prints the exact archive directory beneath
`STATE/private/controller-resets/`. SQLite and present sidecars, settings, controller
configuration and matching setup journals are retained there. Issued invitations
are invalidated. Runtime installations, the selected state directory, TLS/signing
keys, repositories, images, RPMs and build logs remain in place. Archived CAS objects
remain protected from storage pruning. This archive is local recovery material,
not a portable backup or a supported automatic restore command.

Repeat the **same reset request ID** after interruption. An unfinished reset blocks
new setup/controller database opening until replay completes. A completed replay
returns its receipt and does not reset a later fresh database. Run `setup` afterward
with a **new setup request ID**, your desired connection options and runtime.
Retained installation/publication transactions, unknown/corrupt schemas, substituted
files and active or unreconciled work are refused; do not delete the whole state tree
to bypass these checks. Reset currently supports database schemas 32 through current,
at most 128 MiB per retained file, 10000 CAS objects and 64 local reset archives.
Known older unused databases can be reset without upgrading their format. Missing
safety fields or historical pathname-bearing storage rows require explicit recovery;
unknown schemas remain protected. Stop an incompatible running controller in its
terminal if its live identity cannot be verified by the current CLI.
It does not upgrade an existing database or reset a previously used controller.
Reuse the same state directory for the next setup; a second database is unnecessary.

For a fresh empty-registry installation, explicitly provision an existing private
operator GnuPG home and composition signing key, then configure its first repository:

```sh
# Stop the foreground controller with Ctrl-C before maintenance.
quirkbench admin repository configure --repository SELECTED_ALIAS \
  --url https://CONTROLLER_IP:REPOSITORY_PORT \
  --signing-home /absolute/private/operator-gnupg \
  --fingerprint FULL_UPPERCASE_SIGNING_FINGERPRINT --request-id PUBLICATION_SETUP --json
quirkbench admin controller run  # run status/enrollment commands in another terminal
quirkbench status --json
quirkbench target pair SELECTED_NAME --request-id INVITATION --json
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
Ordinary `setup --configure-controller` retries accept the verified journaled successor.
Existing manually configured publication and endpoint maintenance remain supported.

Starting the configured native service publishes its repository and enrollment listeners under the existing
owner; the repository uses mutual TLS plus exact registered leaf lookup. Setup
status observes a separate, current owner/configuration/TLS capability. Missing
publication remains unavailable. Use `quirkbench target pair NAME` to display the
single-use, explicitly revocable invitation, endpoint, code ID and full certificate SHA-256; machine
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

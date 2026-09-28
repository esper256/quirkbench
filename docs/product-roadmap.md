# Quirkbench: path from qualified infrastructure to a usable debugging lab

This is the authoritative forward implementation plan. It preserves the achieved
M1/M2 infrastructure and [recorded qualification](v1-qualification.md). The work
below is **planned, not implemented**, unless explicitly identified as existing.
CLI examples describe the intended interface, not commands available today.

Planned target-management subcommands extend the existing `target` namespace;
retain compatibility with its current transport-runner flags. Existing `--device`
options and `device_id` fields continue to mean target ID.

Follow [the terminology and scope contract](terminology.md). This roadmap applies
to arbitrary target computer models and form factors. No brief may bake a vendor
or model into controller logic. Current x86-64/UEFI/USB backend limits remain
explicit; new platform support belongs behind build/boot adapters and profiles.

## Decisions

| Question | Decision |
| --- | --- |
| Target discovery | A standalone, read-only Python collector runs on the target's installed Linux and produces a bounded, versioned inventory. |
| Image preparation | The controller resolves that inventory against reviewed hardware profiles, then generates a provisioned `.img` and checksum. Etcher writes it. |
| Deployment | Keep fixed recovery and the six-partition layout; candidate updates remain exact signed OSTree revisions. |
| Watchdog | Hardware reset primarily protects the experimental boot. Keep one systemd owner; recovery handles that same device safely, not a second watchdog architecture. |
| Agent interface | Local CLI with versioned JSON, operation IDs and durable background work. No MCP server in v1. |
| Agent lifecycle | A deterministic controller runner invokes the agent at decision points; no agent remains alive merely to watch a build or experiment. |
| First usable delivery | An attended, provisioned target inventory cycle, followed by one issue-specific session. Overnight operation has additional gates. |
| Crash capture | Logs and qualified reset first. Kdump stays deferred behind an explicit no-kexec policy revision; unavailable dumps are reported honestly. |

## Intended human workflow

Here **target** means the computer being investigated; **controller** means
the separate Linux computer running Quirkbench and the coding agent.

1. On the target's installed Linux, clone a known Quirkbench revision, attach the
   wired adapter/dock intended for debugging, and run the standalone collector:
   `python3 tools/collect-target.py --output target-01-inventory.json`.
   No package installation, privileged setup or service is required. Copy that
   file to the controller by any normal means; it contains no enrollment secret.
2. On the controller, run `quirkbench controller init` once to configure persistent
   state, the rootless Fedora build environment, LAN endpoints and device trust.
   Then run `quirkbench target import target-01-inventory.json --name target-01` and inspect
   `quirkbench target plan target-01`. Missing required observations block preparation
   with specific collection instructions; optional unknowns remain visible.
3. Run `quirkbench target prepare target-01 --output target-01-debug.img`. This returns an
   operation ID promptly. `quirkbench operation status ID --json` and the human
   monitor show progress without agent inference. The worker builds/reuses the
   protected recovery and a matching baseline deployment, publishes the baseline,
   and assembles a private, provisioned image plus checksum and provenance.
4. Flash with Etcher, select USB boot using the owner's normal boot selection,
   and boot with wired networking. First boot commissions storage, installs its
   device configuration and registers in recovery. It does not start experiments
   without an explicit campaign. Secure Boot must already be disabled; Quirkbench
   never changes firmware to make this true.
5. `quirkbench target commission target-01 --attended` runs bounded inventory and one
   baseline round trip, recording identity, evidence acknowledgement and recovery
   arrival separately. Then use `quirkbench session start --device target-01
   --problem problem.md --agent AGENT --attended`, followed by existing monitoring,
   pause and resume concepts. A session is the user-facing workflow for a campaign,
   not a second authoritative copy of campaign state.

Once commissioned, experiments update OSTree objects rather than rebuilding or
reflashing the USB. Unknown hardware can require a profile adjustment and another
image; an inventory cannot guarantee driver support or prove physical reset.
Do not require the long endurance gate before an attended first experiment.

## Target inventory and deterministic image planning

The collector is a standard-library-only script runnable directly from a clone.
Its output is `HardwareInventory` version 1: collector revision, observation time,
platform details, per-observation source/status, and a content hash recorded by
the importer. It is evidence of observations, never executable build instructions.

Collect, where readable:

- Architecture, CPU/platform features, RAM, kernel release/config identity,
  distribution and nonunique DMI model/firmware revision.
- PCI/USB IDs, class, modalias, bound driver, relevant ACPI/I2C/HID relationships,
  USB controller/dock topology and attached network-adapter identity.
- ALSA card/device/driver topology, input device capabilities and camera interfaces;
  loaded modules and available firmware declarations as hints, not proof of all
  required modules or successful firmware loading.
- Storage-controller and transport topology from sysfs, without reading disk
  contents, filesystem discovery or mounting volumes.
- Secure Boot state when safely readable, supported sleep modes, power-management
  driver names and existing watchdog sysfs attributes. Absence of a watchdog node
  means unknown availability, not proof that the hardware lacks one.

All reads and optional allowlisted commands have deadlines and byte limits. No
network calls, device ioctls, active audio/camera capture, key-event recording,
module loading, suspend/reset, firmware writes or opening `/dev/watchdog*`. Opening
a watchdog can arm it, so even a seemingly read-only device probe is prohibited.
Use sysfs/proc metadata directly where possible; optional system tools cannot be
mandatory dependencies. Write only the requested report and bounded temporary
files within its output directory. Cloning and writing that report are the only
intended installed-OS changes.

Default output excludes usernames, hostnames, serial numbers, MAC/IP addresses,
machine IDs, arbitrary kernel command lines, journals, credentials and home files.
An explicit optional privileged collection mode may read only documented missing
metadata; it never installs tools or silently invokes sudo. Raw diagnostic logs
are a separate opt-in evidence operation, not part of initial inventory.

`HardwarePlan` version 1 binds the inventory hash, reviewed profile revision,
source/dependency identities, recovery/candidate Kconfig requirements, firmware and
userspace packages, capacity estimates and unresolved requirements. Import is
strictly validated and bounded; strings cannot become shell commands, arbitrary
paths, package names or download URLs. Known IDs/modaliases resolve through the
selected kernel's metadata and a reviewed mapping. Unknown required hardware
needs a bounded profile task; never guess support from the target computer model name.

HardwarePlan must include target architecture and boot backend separately from
the controller architecture. Dispatch only to an implemented compatible backend;
an unsupported combination returns a precise planning error. Do not pass inventory
strings directly as compiler architecture flags. The existing hardcoded x86-64
build/RPM/GRUB assumptions must be encapsulated as the initial platform adapter
before claiming multi-architecture preparation. Cross-building is an explicit
backend capability, not assumed because both machines run Linux.

P1 fixtures must cover multiple vendors/form factors, different wired adapters,
missing watchdogs, varied storage topology and unsupported architecture/firmware.
P3 must prove profile selection does not reuse another target's credentials,
protection plan or reset evidence. Machine identities are not profile identities.

Build separate recovery and baseline configurations. Recovery needs boot, USB,
wired network, evidence and deployment tools; the baseline additionally needs the
actual audio/input/camera paths and diagnostic packages. Use the installed kernel
as a compatibility clue, not a config to copy wholesale. Choose pinned, available
source and package inputs and record divergence from the production OS. Initially
ship the relevant firmware packages intact instead of inventing fragile firmware
pruning. Include a compact compatibility fallback set for supported USB adapters.

Internal-storage-controller exclusion remains the initial mechanism, reinforced
by positive external-device identity and write allowlists. Validate final Kconfig,
modules and initramfs after dependency resolution. An unsupported driver or a
protection conflict blocks preparation instead of silently relaxing the policy.
Physical readback confirms the resulting profile; source inventory is not proof.

## Provisioning without a second physical transfer

Choose a device-specific provisioned image for v1, avoiding a new remote enrollment
protocol. Reuse the existing bearer-token and repository mTLS mechanisms. The
controller creates per-device/media credentials with read-only repository access
and device-scoped protocol authority; neither permits controller administration.

During regular-file image assembly, put a private bootstrap bundle in a reserved
directory on the initial experiments partition, outside OSTree, library and build
artifacts. It includes controller URLs, public trust, device credentials, expected
media identity and profile references. Bind its manifest to the image identity
and verify it using controller public trust installed in fixed recovery. No AI
credentials, CA private keys or OSTree signing private keys enter the image.

After positively verified commissioning, recovery atomically copies the bundle
into `evidence/control` with restrictive permissions, fsyncs files/directories,
records its bootstrap digest durably, and only then retires the seed. Interrupted
installation is repeatable; an existing different identity/configuration is a
conflict, never overwritten. Lost completion acknowledgement never reformats
storage or replaces credentials. Public factory images may remain unprovisioned.

The provisioned `.img` is a private credential-bearing deliverable, mode 0600,
stored outside the public CAS/repository/evidence export. A checksum is not
encryption; unlinking the seed is not secure erasure. Do not distribute or clone
it onto multiple enrolled devices. Revocation/replacement belongs to local
controller administration; duplicate identity/boot conflicts stop scheduling.
Back up credentials separately from ordinary debugging evidence.

TLS endpoint names/IP SANs, clock plausibility, token validity and repository trust
are checked before claiming readiness; never disable certificate verification to
recover connectivity. Offline recovery waits visibly without rebooting. Recovery
must still upload retained evidence if experiments/library later fail; it cannot
depend on the bootstrap partition after successful provisioning.

## Watchdog: implementation versus qualification

Two different mechanisms already have code:

- The service watchdog detects a stalled Quirkbench supervisor while systemd can
  still run; bounded recipe deadlines catch a responsive but stuck experiment.
- The hardware watchdog may reset the machine when the kernel stops servicing it.
  Requested timeout starts at 120 seconds; record the actual supported timeout.

The existing built kernel has no usable watchdog driver. Configuring systemd alone
does not supply one. Add the driver selected from the hardware profile to the
protected recovery and candidate kernels, then qualify the actual machine. This
primarily protects the experimental OS. On reset, the already-consumed GRUB entry
causes recovery to boot. Recovery handling of the same watchdog provides safe
ownership/handoff and avoids leaving an armed timer unserviced; it is not another
independent hardware failsafe. Controller outages must not trigger recovery resets.

Keep systemd the sole userspace owner. Initial v1 coverage begins where verified
activation actually occurs; no custom GRUB watchdog or promise of early-initramfs
coverage. Qualify handoff, runtime, shutdown and suspend separately. Do not enable
suspend recipes until their mode is approved. A userspace panic test cannot prove
a hardware lockup reset. No supported reset means attended-only operation; do not
automatically repeat an uncertain attempt or pretend software watchdogs suffice.

**Resolve the per-kernel qualification problem explicitly.** Current activation
requires an exact loaded build ID and passing profile (or an attended qualification
run). Keep that default. A new experimental kernel cannot inherit a passing claim.
Otherwise requiring a full destructive qualification for every candidate would
make adaptive kernel development impractical. Implement a separate, versioned
`WatchdogAuthorization` for operator-approved experimental campaigns:

1. An actual platform/baseline qualification remains immutable and bound to its
   hardware, baseline build ID and settings.
2. An explicit campaign opt-in may authorize activation on changing kernels under
   that commissioned platform policy. The controller derives an authorization for
   the exact candidate revision/build ID; it is not a blanket target-side bypass.
3. Each boot verifies hardware, protected config, watchdog identity/settings and
   observed activation. Display **armed; baseline qualified; candidate unqualified**
   where appropriate. Preserve both authorization and evidence in attempt provenance.
4. Known changes to watchdog/reset/boot/power code, relevant config, firmware,
   hardware or suspend policy return to attended review. Dependency checks are a
   screening aid, not proof that an unrelated kernel patch cannot break reset.
5. Unattended experimental operation requires this explicit risk scope and a
   qualified platform; coverage limitations remain visible. Without it, new-build
   trials remain attended. A failure pauses for reconciliation/human reset.

This is a planned authorization extension, not a reinterpretation of current
`RecoveryProfile` pass fields. Older targets must reject it rather than silently
enabling activation. Higher-reasoning review must verify the implementation and
migration before the extension is enabled. Recovery can keep its exact fixed-kernel
profile. Do not make a second destructive watchdog campaign on every recovery boot.

Evidence capture remains independent. Start with sealed live logs, early console
where available and qualified netconsole where the actual adapter supports it.
Leave crash dumps unavailable until a dedicated kdump policy/implementation brief
is accepted. Lack of early evidence is an explicit limit, not a fabricated cause.

## CLI, operations and agent sessions

Use the shell interface available to the controller-side coding agent. Preserve
typed Python application services under the CLI so an eventual MCP adapter can be
a thin wrapper. V1 needs no MCP service, remote administrative HTTP API, SSH
command channel to the target or GUI. Existing device HTTPS remains separate.

Add a versioned local JSON response envelope and bounded output for operations,
sessions, experiment submission and evidence queries. JSON goes to stdout, human
logs to stderr. Mutations have caller-supplied request IDs: exact replay returns
the prior operation, changed reuse conflicts. Exit/error codes distinguish invalid
input, paused/blocked work and infrastructure failure. Commands return durable IDs,
not a success claim for unfinished work. Add an event cursor and evidence selectors
so agents request concise deltas or relevant excerpts instead of entire logs.

New schemas are additive: `HardwareInventory`, `HardwarePlan`, private
`DeviceBootstrap`, `Operation`, `AgentProposal` and `WatchdogAuthorization` start at
version 1 with strict validators and rejection of unsupported versions. Public
inventory/plans never contain private bootstrap bytes. Preserve the existing
Experiment/Result envelopes and historical readers. Link typed records by hashes
and stable IDs; arbitrary JSON metadata cannot grant execution authority.

Operation states are `QUEUED`, `RUNNING`, `WAITING`, `SUCCEEDED`, `FAILED` and
`INTERRUPTED`; campaign pause remains a separate scheduling fence. A worker restart
marks unresolved running operations interrupted and reconciles outputs. It can
reuse verified build stages after explicit resume, but cannot replay physical
execution or agent edits. Waiting carries a reason/deadline; success requires a
durable result reference. Migration/replay tests must cover these boundaries.

The existing SQLite database remains authoritative. Add persistent operation rows
for preparation, build, compose and agent decisions; do not introduce another task
database. Workers run in the managed rootless container under its service manager,
with state/logs outside the container. CLI exit or an agent turn ending cannot kill
the work. Controller restart interrupts work, persists/reconciles its last known stage,
and requires explicit resume. No autonomous scheduling after controller restart.

Use one campaign coordinator and the existing global build resource limits.
Every long operation has a stable ID, immutable inputs, journaled stage transitions,
bounded deadline, last heartbeat, last measurable advance and final manifest. A
duplicate completion or restart publishes once; caches never establish success.
Do not replay an interrupted agent edit blindly: snapshot/quiesce its scoped
worktree, preserve the interrupted decision, and reconcile before another decision.

Support two modes through the same application API: a human/external agent can
drive explicit CLI steps, or the deterministic session runner can invoke a coding
agent at decision boundaries. An agent invocation receives a compact problem,
baseline, ledger, evidence delta and allowed source workspace. It returns a typed
proposal and actual usage accounting. It may edit source in a dedicated worktree;
the runner owns build/compose/submit and validates the proposal before dispatch.
Use a per-invocation identity to prevent double submission; nested autonomous
session runners are forbidden. Checkpoints bind the exact source snapshot actually
built, including uncommitted files, not a branch name that can move.

Default use is attended. Session initialization records the problem, source trees,
baseline deployment, recipe capabilities, hypothesis, physical observation needs,
budget and recovery policy. AI credentials stay in the controller environment.
Authentication expiry, exhausted budgets/storage and persistent infrastructure
failure pause durably. Unknown provider usage must be marked unknown and governed
by time/call limits, never recorded as zero. Session replacement reconstructs
context from evidence/checkpoints rather than relying on previous chat history.

Pause stops new scheduling immediately, lets the active bounded unit finish and
checkpoint, and returns the target through recovery. Results may trigger analysis
while recovery boots, but only acknowledged recovery authorizes another attempt.
No polling agent consumes tokens during a build or physical test. The runner waits
on process completion or target events; the human monitor reads durable progress.

## Bounded implementation briefs and allocation

Implement in this order. The higher-reasoning design is this document; cheaper
models receive one brief plus its contracts and tests. Do not forward the entire
project conversation. Surface contract conflicts instead of redesigning silently.
Proposed test filenames below are deliverables, not existing passing suites.

| Brief | Scope / permitted changes | Software acceptance before handoff | Review and exclusions |
| --- | --- | --- | --- |
| P1 — discovery and profiles | `tools/collect-target.py`, inventory/profile schemas, import/plan CLI, fixture data | `tests/test_inventory.py`, `tests/test_hardware_plan.py`: missing tools, unknown IDs, USB adapters, privacy, bounded output, no device writes or watchdog opens, malicious reports, protection conflicts | Cheaper implementation; higher review of profile/protection decisions. No build, active probing or qualification claim. |
| P2 — durable local operations | Operation journal/migration, CLI JSON, managed worker, scoped source snapshots, events | `tests/test_operations.py`: exit/restart, duplicate requests, stale workers, paused scheduling, crash during publication, source changes, compact outputs | Higher review of restart/ownership semantics; cheaper implementation. No MCP or replacement database. |
| P3 — tailored provisioned images | HardwarePlan to existing builder/composer/image adapters, controller setup, private bootstrap and first-boot import | `tests/test_provisioning.py` plus focused build/image/commission tests: every interrupted import, identity mismatch, no secret export, exact inputs, expired trust and offline recovery | Higher review of privileged writes/credential boundary. Cheaper packaging. No USB writer, firmware changes or in-place old-image conversion. |
| P4 — attended device commissioning | Guided physical inventory and one baseline cycle; actual profile changes if needed | Extend runtime/physical-handoff tests; produce a capability report template with each unobserved item explicit | Human-supervised target run at delivery; distinguish software acceptance from physical evidence. No claim to fix a device issue. |
| P5 — reset policy | Driver/profile requirements, explicit WatchdogAuthorization, coverage display, attended qualification workflow | Watchdog/runtime/controller fixtures: build mismatch, authorization revocation/replay, old-client rejection, healthy waits, deadlines, suspend fences, no-controller recovery | Higher review mandatory; cheap bounded integration. Physical reset trials before unattended use. No kdump or early-boot guarantee. |
| P6 — usable problem-solving sessions | First concrete agent adapter, session CLI, deterministic decision/build/experiment loop, budgets/checkpoints | `tests/test_sessions.py` and agent tests: session replacement, partial edits, auth loss, duplicate decisions, unknown usage, pause/restart, no waiting-agent calls | Higher review of ownership, scoped edit and replay boundaries; cheaper CLI/adapter work. External CLI-driven attended use may precede the autonomous runner. |
| P7 — diagnostics and patch bundles | Input/audio/microphone tools and recipes, physical observation records, baseline/patched/revert comparisons | `tests/test_diagnostic_recipes.py` and patch-report checks: time/exposure accounting, missing observations, negative/revert controls, inconclusive results | Higher experimental design/causal analysis; cheaper recipe implementation. No installed-Bazzite mutation or automatic patch publication. |
| P8 — final release qualification | Freeze a major-version candidate; run the applicable expensive acceptance and >30-hour campaign | Retain source/image/deployment identities and complete release evidence; inspect completion summary, not continuous agent polling | Explicit release authorization only. Hardware availability is an external gate; do not replace it with skips. |

P1/P2/P3 enable inventory → prepare → flash. P4 enables a first supervised physical
cycle. P6 plus one P7 recipe enables a usable attended problem-solving session.
P5 and physical/endurance evidence are additionally required for unattended use.
P5 implementation and P6 software work need not wait for access to the target, but
their hardware claims must wait. Select the first P7 recipe from the investigation
goal and observed target capabilities. Input key-release, microphone routing and
audio-loop stress are initial recipe examples, not a required issue sequence for
every target. Record physical observations and exposure counts appropriate to the
actual failure; headless or non-audio targets need different recipes.

## Validation and non-goals

Follow [the quota policy](testing-policy.md). Each brief uses focused software
fixtures, not a full image rebuild/QEMU cycle. Final major-version qualification
owns the expensive infrastructure suite and endurance run. Actual user-requested
device image production and kernel experiments are product work, not an excuse to
rerun that suite. At the attended delivery checkpoint, validate the particular
device workflow rather than repeating unrelated historical VM gates.

Do not claim all remaining work can be proved without hardware. Inventory is not
qualification; a boot is not a working experiment; a reset is not a dump; and a
passing recipe is not causal proof of a fix. Physical observations must be recorded
through the same evidence pipeline. Keep MCP, GUI, remote shell, direct candidate
chaining, automatic recovery promotion and kdump outside these briefs.

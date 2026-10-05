# Implementation contracts

C0–C8 are the contract identifiers. The first
general release follows fresh controller setup and authenticated pairing with an
attended external-agent investigation through patch export. Exact experiment approval
remains explicit. Managed decisions and unattended grants are optional later modes.
The [acceptance guide](installation-to-patch.md) defines delivery evidence; existing manual
configuration remains supported. Existing wire schemas
remain compatible; permission validation is revised by the file-access policy below. The
[architecture storage policy](architecture.md#storage-protection-policy) governs
recovery separately from candidate restrictions.

This document defines the durable constraints for the [roadmap](product-roadmap.md).
It specifies **planned behavior**, not features already implemented. Its decisions
take precedence where the roadmap was less specific. Do not change the frozen
Experiment/Result envelopes or weaken storage protection to implement these rules.
Use [GitHub tracker #29](https://github.com/esper256/quirkbench/issues/29) and its
child issues for task boundaries, dependencies and current implementation status.

## Single-user installation

Quirkbench is exclusively a single-user application. One person installs a
per-user instance and uses it exclusively. A shared installation, multiple
application users, tenants, accounts, roles, delegated administration, shared
workspaces and collaboration/access-control features are out of scope, not
requirements to anticipate.

Controller, builder, target and coding agent are components acting for that one
user, not separate application users. Multiple worker processes, investigations
or targets do not imply multi-user support. Worker ownership means coordination
and restart fencing within the user's installation, not isolation between users.

Rely on the host OS for the user's account boundary. Do not design against other
local users or the installation owner as adversaries to justify application-wide
permission enforcement. Keep ordinary secret hygiene, authenticated controller/
target communication, explicit experiment approval, and prevention of accidental
data loss or unintended writes. These serve the single user's workflow; they do
not establish a multi-user authorization model.

## File access and permission policy

Owner-approved product decision: [#66](https://github.com/esper256/quirkbench/issues/66).
This policy supersedes blanket mode requirements in earlier contracts, audit
conclusions and issue instructions. Implementation migration is tracked in #66;
this documentation does not claim that existing checks have already been removed.

Quirkbench is a user-installed application. Ordinary controller data, databases,
source workspaces, reports and exports follow normal user-owned filesystem
semantics. Respect usable user-selected permissions. Group/other permission bits
alone are not grounds to reject ordinary data, and every descendant of a protected
root need not independently have mode 0600 or 0700. For example, ordinary 0644
files and 0755 directories inside a private 0700 root are acceptable. Report actual
read/write/traversal failures with actionable errors.

Create actual secrets (authentication tokens, signing/private keys and saved
network credentials) and dedicated secret stores with private defaults, normally
0600 files and 0700 directories. An enclosing private directory can provide the
access boundary; exact leaf-mode equality is not itself the product requirement.
Do not broaden existing secret permissions during migration. Honor native tools'
documented functional requirements, such as those of a signing-key store. Mixed
stores containing secrets need secret-appropriate protection until separated;
this does not make all application data inherently secret.

The target's boot-media control store is distinct from the controller user's home
and contains credentials; provision it with appropriate private defaults. Ordinary
target evidence is not required to have exact 0600 modes merely to prove its
identity or durability. Keep secrets out of public artifacts and exports.

Do not chmod pre-existing user content or recursively normalize permissions to
make an operation pass. Private defaults for newly created app-owned state are
allowed, but they are not an admission rule for all existing ordinary data.
Routine operation must not require users to change their umask or home policy.

Every retained permission-based rejection must name the protected resource, actor
and harmful access, or the concrete functional/native-tool requirement. A reference
to an existing helper or test is insufficient. Prefer a small number of explicit
secret boundaries; do not build a general ACL/ancestor-analysis framework or
repeatedly prove every ordinary path private.

Keep filesystem access separate from logical ownership and integrity. Modes do
not fence same-UID workers, sandbox the trusted local coding agent, prove evidence
bytes unchanged, or contain an experimental kernel. Preserve authenticated approval,
worker generation/locking and stop-before-reuse, correct storage device and write
confinement, atomic publication/durability, source consistency, and prevention of
unintended writes through links. Source/image executable semantics and genuinely
functional mode bits remain meaningful. Preserve existing capture formats and
historical evidence; changing this policy does not authorize silently rewriting
stored manifests, digests or source metadata.

In other sections, "private" work/staging means app-managed scope and a suitable
creation default, not an independent exact-mode rejection requirement. Actual
credentials remain confidential. Boundary review uses this policy when simplifying
implementation; removing an obsolete blanket check does not itself require another
product decision.

### Remaining permission behavior

Ordinary state, setup records, worker directories, locks, source workspaces and
capture outputs, build caches and target evidence have no private-mode admission
rule. Canonical paths, ownership, correct-device checks, locks, immutable digests
and bounded stable reads still serve their original purposes. Atomic updates
preserve an existing regular file's mode; newly created records and the controller
database retain private defaults. The database can contain attempt tokens, so keep
it or its enclosing state directory private when using real targets.

The remaining access-mode checks have specific purposes:

- Managed TLS private keys and drain tokens require a private file or their declared
  enclosing secret store, because they authorize controller/target communication.
  Public certificates and ordinary metadata do not inherit a secret-file mode rule.
- Target control storage contains enrollment keys, device credentials and attempt
  tokens. Its control directory or verified enclosing evidence directory provides
  the private boundary; individual evidence blobs need not be 0600.
- The configured GnuPG signing home keeps a private boundary for publication keys
  outside worker/output mounts. NetworkManager's RAM profile store must be private
  and writable for saved network credentials, and retains its restricted tmpfs.
- Executable bits, source special-mode exclusions, installed archive modes and
  read-only library/build snapshots retain their functional or reproducibility
  meanings. They are not evidence of secret confidentiality or worker authority.

Permission changes on newly created artifacts preserve the required source/image
modes or establish secret defaults. Cleanup may restore owner access only inside
an explicitly disposable, stopped, application-owned snapshot; it never normalizes
user source or ordinary state as a precondition for use.

## C0 — Shared contract and authority rules

Each genuinely new record starts at `schema_version: 1`; a successor to an existing
record uses a new version and explicit compatibility handling. Records require
strict unknown-field rejection, canonical
JSON using the existing `contracts.canonical`, bounded input and matching runtime
validator/JSON Schema. Hash canonical document bytes; never put a document's own
digest inside the bytes it hashes. Optional unknown observations are explicit
`null` or status values, not fabricated empty strings or zeroes. Separate mutable
execution rows from immutable input/result documents. No pickle or executable
configuration, shell interpolation or downloaded code from inventory values.

Reject duplicate JSON keys, nonfinite numbers and nesting beyond 32 levels in new
input records. Do not rely on JSON Schema alone to detect duplicate keys after a
permissive parser has already discarded them. Enforce byte limits before parsing.

Implement schema examples and success/failure fixtures **before** their adapters.
The field lists below are minimum required semantics; represent repeated typed
objects with their own strict schemas. Complete those mechanical schemas within
the owning brief; changed ownership, identity or authorization semantics require
review, not an extra optional field silently added by an implementation agent.

There are three authorities:

| Authority | May do | Must not do |
| --- | --- | --- |
| Local operator configuration | Enroll targets; approve profiles, endpoints, source roots, agent commands and recovery risk scope | Imply unobserved hardware qualification |
| Session runner / controller application | Validate proposals, account usage, publish immutable inputs, authorize attempts | Infer approval from model text, experiment results or timeout |
| Target supervisor | Verify its boot, execute installed bounded recipes, report evidence, return to recovery | Run remote shell text, install arbitrary packages or authorize the next attempt |

The controller-side agent is trusted local software under the agreed local execution
model, not a security sandbox. Restrict its worktree and validate its outputs;
do not claim it is technically unable to access controller credentials. Policy
grants and private credential directories are outside its task workspace and are
never inputs to build snapshots or prompts. Target observations and logs are data,
not instructions that can expand agent authority.

## C1 — Discovery and hardware planning (P1)

`HardwareInventory` fields: version, collector revision, UTC collection timestamp,
platform object, observations array, collection summary. Each observation has a
stable logical key, allowlisted source kind, status (`observed`, `absent`,
`permission_denied`, `tool_missing`, `timed_out`, `truncated`), and a typed value or
null. Do not store arbitrary command output or absolute source paths supplied by
the target. `absent` means an enumerated item was absent, not that its capability
is impossible on that hardware.

Default limits: 8 MiB report, 8,192 enumerated items, 64 KiB per probe, 5 seconds
per optional command and 60 seconds total collection. Exceeding a limit emits an
explicit partial status. Do not silently truncate a value and label it observed.
Version these defaults; tests use smaller injected limits and clocks. Collector
exit codes: 0 complete, 2 usable partial inventory, 1 no valid report. Import of a
partial inventory succeeds, but candidate preparation can remain blocked.

Use explicit sysfs/proc property allowlists, never recursively archive `/sys` or
`/proc`. Sysfs class links may resolve only within the configured sysfs root.
Do not read device nodes, PCI config spaces, firmware table blobs, partition data,
raw journals or arbitrary module parameters. Do not run `blkid`, `udevadm trigger`,
`modprobe`, watchdog query tools or active network/device probes. Missing inventory
tools do not authorize installation or sudo. Collection normally runs in recovery and records that environment explicitly. An
optional installed-OS report uses the same read-only rules and is not a first-boot
prerequisite. The standalone collector may copy
small validation helpers but must not import the installed Quirkbench package.
An absolute interpreter path is not assumed; Python 3.11+ is the documented v1
prerequisite, and an older/missing interpreter is an explicit unsupported setup.

Recovery startup integrates the collector through authenticated registration,
including recovery-only mode, with a 512 KiB report budget and cooperative
10-second deadline. HardwareInventory v2 adds allowlisted modalias/driver,
ACPI/I2C/HID, non-serial model/BIOS and first-logical-CPU identity/features; v1
retains its original property allowlist. Registration preserves the actual boot/media
context and references canonical validated bytes in existing CAS. Missing or partial
collection must not block retained attempt evidence upload. `target inventory`
returns observations and candidate planning blockers without creating attempts,
queueing builds or granting execution authority. See the [current handoff](recovery-operations.md#automatic-first-boot-hardware-report).

Store the exact validated inventory bytes as immutable evidence. A separately
computed hardware fingerprint excludes timestamp and observation ordering; it is
for profile comparison, **not authentication**. Cloned models may have the same
fingerprint. Target identity is the controller's enrolled ID and credentials.
Profile matching is deterministic: exact requirements against a pinned profile
catalog; equally applicable profiles with incompatible policies yield a conflict,
not whichever profile is first in a directory. Optional peripherals do not block
recovery; a missing required boot/network/protection fact does.

`HardwarePlan` fields: version, inventory digest, profile ID/digest, platform
adapter ID, target architecture and boot method, controller architecture, locked
build-input references, recovery requirements, baseline requirements, selected
network device(s), storage protection policy digest, capacity requirements,
blocking reasons and warnings. Required packages/source URLs originate only in
the reviewed catalog, never report strings. Resolve Kconfig dependencies later in
the builder and revalidate the actual config/modules/initramfs; a plan is not proof
that a build satisfies protection.

The initial adapter is x86-64/UEFI/USB. Reject unsupported platform combinations
before queueing a build. P1 does not implement ARM, cross-compilation or a new
bootloader. Its fixtures must still represent those inputs and verify rejection.
The compatibility network-driver set is an explicit list in the profile, not an
instruction to enable all storage/peripheral drivers.

Inventory import stores observations, not enrollment authority or a fabricated
CapabilityReport/boot ID. Enrollment occurs through C4, then the real recovery boot
registers. Reimport is idempotent; different observations produce a new plan without
replacing credentials, qualification or existing evidence. HardwarePlan normally
selects the experimental baseline. Generic recovery is built independently from a
reviewed platform profile with stock kernel packages and boot-device confinement;
passive internal-controller observations are permitted. Candidate exclusions remain
independent. Inventory cannot authorize any weaker storage policy.

Existing HardwarePlan/profile and RecoveryRecipe v1 validators retain their original
exclusion/custom-kernel provenance meaning. P1b/P3a1 now provide recovery recipe,
rootfs-lock and release v2 with distinct package/policy references; candidate
exclusion references are unchanged. See the [software handoff](recovery-operations.md). Retain old readers and artifact identities; do not use a policy digest
to imply that an actual build or boot has passed protection checks.

## C2 — Local operations, ownership and restart (P2)

New CLI mutations use `--request-id`; interactive calls may generate and print one.
Scope uniqueness by controller plus request ID, and hash canonical operation kind,
campaign/target identity and immutable arguments. Exact replay returns the same
operation; different reuse is `CONFLICT`. Normalize local paths before intent is
recorded. Status/query commands never invoke controller startup recovery.

New `--json` responses have `{schema_version, ok, operation_id, data, error}`.
Unused values are null; an error has a stable code, bounded message and retryable
boolean. New command exit codes: 0 accepted/query successful, 2 invalid input,
3 conflict, 4 blocked/paused, 5 infrastructure failure. Legacy commands keep their
current behavior until explicitly adapted. Acceptance of background work is not
completion. Status returns operation state even when that operation failed.

An operation result distinguishes a public CAS reference from a private deliverable
descriptor (credential generation and private-store key). Generic factory images
are public artifacts; credential generations are not. Status can report that an originally successful private deliverable
is unavailable after restore without rewriting the historical terminal state or
silently regenerating credentials.

An operation row records ID, kind, request/input digests, target/campaign, state,
stage, worker epoch, worker unit identity, start/deadline timestamps, last heartbeat,
last measured progress and result/error reference. States/transitions:

| From | Permitted next state and reason |
| --- | --- |
| QUEUED | RUNNING after an atomic claim; FAILED for invalid immutable prerequisites |
| RUNNING | WAITING on a named event; SUCCEEDED after durable publication; FAILED on a definite operation failure; INTERRUPTED on lost worker ownership |
| WAITING | RUNNING on the expected event; FAILED on its bounded deadline; INTERRUPTED on restart |
| INTERRUPTED | QUEUED only after explicit resume and reconciliation of safe stages; FAILED if continuation is impossible |
| SUCCEEDED / FAILED | Terminal and immutable; explicit retry creates a new operation referring to the old one |

Campaign pause is separate: it blocks admission of the **next stage**, not merely
new top-level operations. An already-running build may finish and publish; its
queued compose/submit stages wait. An already-started bounded physical attempt
finishes and returns to recovery. Pause never turns success into failure or erases
queued work. Non-campaign image preparation has explicit admin operation resume after
restart; do not auto-resume it just because it has no campaign.

Unresolved physical execution fences the next controller stage for that operation's
bound target, including its campaign target. An unresolved attempt or missing
recovery return on another target does not block independent source capture,
downloads, preparation, builds or composition. Controller-only preparation has no
target fence. The authoritative worker claim rechecks scope inside its transaction;
the single active compute worker and exact stop/restart fences still apply globally.
Already claimed controller workers may drain, and physical-attempt admission and
exact operator approval remain unchanged. A physical claim may therefore follow
an existing controller claim. This is scoped stage admission, not a promise of
measurement isolation or a quiet controller. Native experiment protocols must
record any workload constraints they require.

One controller lifecycle owner holds an OS file lock for the state directory.
It advances a persistent startup epoch and runs `Controller.startup()` once, before
accepting new scheduling. Repository-serving and read-only CLI processes do not
advance that epoch. Workers capture the epoch and a monotonically increasing claim
generation; every stage/reference publication verifies both in the same SQLite
transaction as its reference commit. Leases alone cannot fence filesystem writes.

Each worker runs in a uniquely named service/cgroup and writes to its private
staging directory. On coordinator restart, fence old workers and stop/reconcile
their complete process groups before letting a replacement use those resources.
PID alone is insufficient because it can be reused. If termination cannot be
established, pause for intervention; never run two agents in the same worktree.
Only the current owner can publish references; stale output can remain unreferenced
for later cleanup. Pure build stages can reuse verified immutable outputs after
resume. Never automatically replay a physical attempt or an interrupted agent edit.

Use short SQLite transactions with the existing durability settings. Never hold a
transaction while compiling, waiting for an agent, hashing an image, making network
requests or rebooting. Serialize schema migrations under a migration lock; refuse
an upgrade while an older active worker can still mutate state. Backup includes
operation rows and all referenced public input/output/source artifacts. Restored running
operations are interrupted and scheduling is paused; no service PID or lock is
restored as live ownership. Private configuration is restored separately.

An operation deadline/failure never releases a physical attempt fence or invents
its terminal result. Use the existing uncertain-attempt/recovery reconciliation
rules for a target that may still be executing, and retain late evidence.

The controller runs in the foreground and has no host service-manager integration.
Daemon packaging is deferred. Target/recovery/experiment systemd remains unchanged.
The owner removed the earlier systemd-only controller requirement in #89/#92. A supported
supervisor must establish worker identity, bounded resources, complete descendant
shutdown and restart reconciliation before accepting background work; a PID alone
is insufficient. Preserve the existing lifecycle lock, epoch/claim fences and
publication transactions independently of the chosen supervisor. Report whether
logout, sleep and reboot stop work; never promise power-loss continuity or silently
change controller power policy, enable lingering, open a firewall or install host
packages. Bind only operator-configured LAN interfaces. No additional scheduler,
database or polling AI process is required.

Foreground artifact generation may run independently of controller services and
state. The recovery builder reuses the fixed stock pipeline in an explicitly pinned
Podman or Docker container with resource limits, a whole-container deadline, isolated
staging, read-only input mounts and verified shutdown. It exports unsigned,
unqualified artifacts only; it has no signing, controller publication, target or
attempt authority. Interrupted builds retain their container identity and diagnostics
for explicit cleanup. This does not advertise unsupported background capabilities.

Progress uses existing Activity/Progress semantics. Default queries return at most
100 events or 64 KiB of summaries; evidence reads use explicit byte ranges and
maximum lengths. Waiting has an event/reason/deadline; output bytes count as output
activity, not percent complete. No second progress stream with incompatible meaning.

Preparation before a target's first boot has no campaign. Store its progress and
events against the operation ID in additive tables in the same database; do not
create a fake target/campaign to satisfy the existing non-null campaign fields.
Reuse progress validation/rendering helpers without altering the frozen target
Progress envelope. Event cursors are explicitly scoped to an operation or campaign;
do not compare unrelated sequence numbers as a global order.

### Home state, read-only monitoring and disposable retention

Current C2 extension: no working-directory state fallback and no popup viewer.
`monitor`, operation queries and existing read-only campaign/inventory/observation
commands read existing state without migrations or lifecycle startup. Queries are
bounded; oversized legacy summaries fail explicitly rather than hiding records.
SQLite queries retain normal locking and current committed WAL visibility. Read-only
means no application database mutations, initialization, migrations or execution
authority. SQLite itself may create or update its `controller.sqlite-wal` and
`controller.sqlite-shm` bookkeeping when a stopped WAL database is opened. These
auxiliary files are not application artifacts or evidence of submitted work. Queries
must not delete them, switch journal mode or mark live state immutable to avoid them.
Advisory worker activity and heartbeat reports use the existing owner's exact claim
fences; they change no deadline, attempt authority or immutable operation outcome.

Housekeeping holds the existing owner/build/cache locks and an exclusive command
publication barrier. Producers share that barrier; read-only queries neither acquire
execution authority nor clean up. Terminal staging needs exact stop proof and verified
publication/error records. Retain bounded diagnostics before disposing of successful
work. Configurable counts retire completed attempt/release/build/input/qualification
owners; pins and active, interrupted, uncertain or resumable dependencies remain live.
Retirement commits before filesystem deletion; retries preserve current shared CAS
closure and avoid reusing paths still pending disposal. Native OSTree refs are pruned
only when no unresolved producer remains. Keep historical rows with expired payloads
unavailable. See [retention settings](local-state-maintenance.md).

Upload ownership is recorded before partial bytes. Pending/resumable or completed
unacknowledged uploads protect their attempt and CAS content; exact evidence
acknowledgements permit count-based expiry. Definitive failures/explicitly abandoned
uploads use failed-staging grace. Legacy unidentified bytes remain protected and
visible until explicit `maintenance abandon-upload ID`, with active/unresolved
attempt checks. Retirement commits before associated-file deletion. Terminal upload,
attempt completion and recovery return persist an idle-owner housekeeping request,
processed after target responses without a timer or additional service.

`build`/`compose` submit fixed durable jobs by default; `--wait` is query-only.
Manual configured controller service readiness is required for admission. Input
capture/hashing happens privately in the first worker stage and is adopted before
compilation. Cache hints are read-only; writable work/proposals are private. Only
the current owner validates stopped output, signs composition and publishes shared
repository/result references. Repository pins precede the short fenced reference
transaction. Explicit resume is a durable request reconciled by that owner with a
fresh generation. See [build and boot](build-and-boot.md)
and [manual installation](controller-installation.md#foreground-build-and-composition-controller).

Failed disposable staging defaults to seven days. Optional cache admission/eviction
uses the configurable 50 GiB default, budget lock and nonblocking lineage locks;
pending entries count and an unavailable budget skips optional publication. Completed
build output caches may collapse to verified CAS references under the build lock.
Ad hoc cleanup additionally requires explicit output retention/abandonment and
whole-service stop verification. Abandonment holds the exclusive publication barrier
before checking stop proof, excluding the acquisition claim-to-launch interval. This
metadata is not an alternative attempt state machine. No cron, timer or new service.

## C3 — Source identity and agent proposals (P2/P6)

Freeze source bytes before build. A checkpoint records approved repository roots,
base commit identities, exact tracked contents plus explicitly allowed untracked
edits, file modes/deletions and any permitted internal relative symlinks. Use a
streaming archive/tree manifest, not the current small in-memory snapshot helper
for an entire kernel tree. Submodules need independently pinned source references;
never silently follow mutable network refs. Escaping symlinks, special files,
credential directories and uncontrolled generated files are excluded or rejected.
Record incomplete snapshots as incomplete, not usable build inputs.

Proposal acceptance returns a durable operation ID promptly; it does not assert
that source capture or validation has completed. A proposal is not a finalized
Experiment. Managed invocations must exit before source capture. External proposals
select a pinned revision in a dedicated private worktree, or an already completed
source-capture operation. Capturing approved dirty/untracked edits requires an
explicit exclusive-writer handoff; it is a separate durable operation. Do not
automatically commit into the user repository or snapshot a changing tree. Reject
mutation during capture, preserve the edits, and do not build partial input.
The controller binds the resulting immutable source identities to execution records.

The agent must exit/quiesce before a final source snapshot. Checkpoint at every
decision boundary and preserve interrupted edits on timeout/restart before another
agent touches the worktree. Periodic snapshots cannot claim a consistent tree while
the agent is writing; use an explicit checkpoint handshake if supporting them.
Build from the immutable snapshot in a separate output tree. A cache key includes
source/config/toolchain/recipe identities; timestamps and branch names are not keys.

`AgentProposal` fields: version, decision ID, campaign ID, input-context digest,
action (`experiment`, `needs_human`, `conclude`), hypothesis/summary, rejected
approaches, approved workspace/base-revision references and change intent, optional
typed experiment proposal and
usage observation. The experiment proposal selects an approved build recipe ID,
installed target recipe ID, bounded parameters/repetitions/deadline and baseline
reference. It cannot contain shell commands, arbitrary artifact URLs, private paths,
watchdog grants, qualification claims or credentials. The runner binds the actual
composed deployment hash when constructing the existing Experiment record.

Persist proposal, usage and the intent to validate/dispatch in one transaction.
Use a durable outbox/reference for the subsequent build/submit stages; replay uses
the same IDs. Existing `run_decision()` records a decision and submits separately;
do not copy that sequence into the autonomous runner without closing the crash gap.
The model returns a proposal; it does not call a nested runner or submit an attempt
on behalf of that same proposal. External CLI-driven investigations may submit
explicitly; their campaign ownership must exclude an autonomous coordinator.

Session metadata records `execution_owner` as `external` or `session_runner` under
an application-level lock; switching requires a paused/reconciled campaign. Default
session admission allows one running investigation per target and one source writer
per worktree. Other targets can have independent campaigns, subject to the existing
global single-build/resource budget. Do not serialize unrelated target evidence
uploads behind a build or hold the build slot while waiting for a target reboot.

Keep the current `CommandAgent` adapter compatible. Add a separate versioned
proposal adapter; do not relax existing response validation to accept arbitrary
provider output. A configured command is an argv list launched without a shell in
the designated worktree, with a bounded timeout and process-group cleanup. V1 uses
one operator-selected installed coding-agent command adapter, not a provider SDK
framework. Its brief must identify and document that concrete adapter and test its
format/auth failure behavior; a scripted fake alone is not a usable integration.

Known token usage is counted exactly once. Unknown usage is null in a new usage
observation, not zero inserted into the old integer ledger. Default behavior pauses
after an unknown-usage invocation; an operator may configure explicit time/call
budgets as fallback. The display distinguishes known totals from incomplete totals.
Bound each invocation (default 600 seconds) and enforce provider output-token caps
where supported; retrospective token accounting is not a guaranteed hard spending
ceiling for an in-flight call. Auth failure is not an automatic retry loop.

Protocol output is at most 1 MiB; raw stdout/stderr are bounded private diagnostic
files (stderr defaults to an 8 MiB limit), never automatically evidence or prompt
content. Enforce limits while draining pipes, not after child exit. Kill/stop a provider that
exceeds its declared output limit instead of filling disk and validating afterward.
Build/test execution remains on the controller; physical recipes remain on the
target. Neither agent suggestions nor build logs may modify protection policy.

## C4 — Recovery setup, enrollment and target binding (P3)

### Guided enrollment and manual compatibility

The M2 fresh-user journey implements the exchange and activation below. Preserve
existing local administrative/target configuration with explicitly provisioned CA/endpoint,
device-scoped authentication, repository trust and a valid target/media binding.
Verify values before activating private state; no HTTP, automatic trust acceptance
or TLS/signature bypass. Keep credentials outside factory images, builds, logs and
public exports. Existing activation/binding checks remain required. Show missing
configuration as blocked, not enrolled. The operator approves the exact candidate
and attempt before arming; proposals/build completion cannot supply this authority.

The pairing exchange, credential lifecycle and retargeting orchestration below
are P3d/e requirements for M2. Manual setup does not claim to implement them.
Safe activation of complete private state and wrong-target boot checks apply to
both paths. Enrollment alone cannot queue experiments or authorize a boot.

Factory image construction has no dependency on inventory, controller endpoints,
private credentials or a baseline deployment. Distinguish image digest, GPT/partition
IDs, per-enrollment media-instance ID and target ID. Identical flashed factory bytes
have identical partition IDs; they must not imply identical enrolled identities.
Manual setup assigns and durably retains a distinct media-instance ID before
authentication. Later automated enrollment also generates a random enrollment
request ID before exchange. Preserve applicable identities across retries.

Recovery setup operates only after boot/protection and evidence-mount verification.
Never scan/mount internal OS partitions for Wi-Fi passwords or hardware discovery.
Use NetworkManager as the only network manager, with nmtui for attended connection
configuration. Disable systemd-networkd in assembled images. Stage connection files
in RAM. Persist only explicitly selected connections in root-only control state;
no log, public inventory, snapshot, OSTree commit or evidence export may contain
passwords, token bytes or private keys. Candidate networking uses a read-only copy
of the selected saved configuration, with runtime changes confined to RAM.

Do not replay saved connections, authenticate under an old target ID or activate
its watchdog profile until target binding is checked. On mismatch, show local setup
and keep previous profiles inactive. The operator may explicitly reuse a network
profile when rebinding, but credentials for the old target never enroll the new one.
Unknown clocks, invalid certificates, bad endpoint SANs and expired credentials are
specific blocking states, not reasons to disable TLS verification. LAN connectivity
is sufficient; target public internet access is optional.

Pairing uses an operator-created short-lived, high-entropy one-use enrollment code
and out-of-band controller certificate fingerprint verification. The recovery screen
must show the endpoint and fingerprint for comparison before transmitting the code.
The TLS bootstrap client may inspect the server certificate without transmitting any
secret; the authenticated exchange must pin that exact approved certificate and then
install controller CA/endpoint trust. No TOFU auto-accept, HTTP enrollment or permanent
verification bypass. Enforce expiry, rate limits, bounded payloads and request IDs.

Before exchange, persist a target-generated keypair and request ID privately. Bind
code redemption to that request and public key in one controller transaction. A lost
reply is retrievable only with proof of the same key and request, including after code
redemption; retries cannot mint another identity or retrieve another target's secrets.
Use maintained TLS/cryptography libraries for proof-of-possession, not custom crypto.
Enrollment result includes the assigned target/media IDs, credential generation,
controller/repository endpoints, pinned CA/OSTree verification key and scoped protocol
and repository credentials. Enrollment does not create a campaign or grant a boot.
Controller authentication configuration must be durable before returning success.

Publish credential generations under evidence/control with private creation defaults
(normally files 0600/directories 0700), applying the file-access policy above:
validate bounded strict records and fixed filenames, reject symlinks/traversal, verify
all files, fsync the staging directory, rename to an immutable generation, fsync the
parent, then atomically publish runtime.json last. Identical retry reuses verified
bytes; a different active generation requires explicit paused maintenance. An ACK
loss never reformats evidence or discards an enrollment key. Repository certificate
revocation must be enforced as well as device token revocation; CA membership alone
is insufficient. Back up private enrollment state separately from public evidence.

TargetBinding v1 contains exactly schema_version and system_uuid (the observed SMBIOS
system UUID). The enclosing enrollment generation binds it to target/media IDs and
may retain separate secondary observations; do not add fields to TargetBinding v1.
UUID is an accidental mismatch guard, not a secret
or attestation. CPUID/model name, network MAC and portable machine-id are not valid
substitutes. All-zero/all-ones, missing or controller-known duplicate identities block
candidate boot. Expose ambiguity; attendance alone does not replace identity. An
alternative gate requires a separately reviewed design, not a v1 fallback;
never silently bypass the early gate or guess the closest enrolled target.

The bootloader checks identity BEFORE loading an experiment kernel. Initial GRUB
support uses SMBIOS type 1 UUID (offset 8); arm_once records a validated expected UUID
in USB one-shot state, alongside candidate identity. GRUB consumes and verifies cleared
state even on mismatch, and selects recovery unless the current UUID matches. Missing
SMBIOS support fails to recovery. Runtime checks the active enrollment binding again
before using its credentials or watchdog profile. Recheck binding at arm/reboot.
The UUID comparison must be qualified against actual firmware/Linux formatting;
platforms without a dependable early identity source remain unsupported for candidate
boot until a separately reviewed identity gate exists. Recovery can still boot for setup and diagnosis.

Retargeting requires recovery, paused/reconciled old work and explicit local operator
confirmation. Clear and verify one-shot state first; fence old controller authorization,
then atomically activate a new binding/credential generation. Partial retargeting
remains paused. Never move old pending chunks into the new target's spool or relabel
attempts. Provide an explicit old-evidence drain using original attribution and scoped
credentials after approval; it cannot register the new hardware as the old target.
New media/target binding invalidates reset qualification and all old attempt grants.

Current implementation is partial: runtime configuration and boot identity guards,
reviewed initial console pairing/exchange and explicit private network-profile
selection/binding-gated RAM replay have software implementations. Native production
commissioning, endpoint maintenance and complete M2 acceptance remain open P3 work.
Explicit revocation, paused retarget/archival, repeated moved-media history and pending
invitation maintenance now have reviewed software implementations. Existing
unbound provisioning remains readable but cannot be activated automatically. Rebuild
older bootloader images; no in-place conversion or silent binding migration.

P3f retained-CA controller staging, explicit expired-source renewal and exact stopped
configuration switch/rollback now have reviewed software implementations. URL-only
target generation provenance uses a separate typed transition record; it preserves
original enrollment documents and every credential/trust/binding byte. Hash labels
alone do not prove an original enrollment anchor. Owned activation must reconstruct
that anchor from independently verified original request/result/generation evidence,
then verify native trust and endpoint reachability before selecting runtime state.

## C5 — Commissioning and physical safety (P4/P7)

Commissioning is an explicit controller workflow while a person can reset the
target. It uses the ordinary attempt/lease/handoff/evidence machinery, not an SSH
shortcut or special direct reboot. Record separate outcomes for external boot,
recovery boot-device confinement, candidate controller exclusions, network/trust, exact baseline identity, live upload,
terminal acknowledgement and subsequent recovery. Tests must exercise the actual
runtime assembly with fake privileged adapters, not just separate protocol classes.

The baseline inventory recipe is bounded and non-stressing. A CLI `--attended`
flag records the operator's declared mode; it cannot prove someone is present.
Fault/suspend/audio stress recipes need explicit recipe eligibility and a visible
warning in their description. Never inject a hang on the installed production OS
or copy QEMU panic injection into the generic collector. If only a reset is observed,
report uncertainty about the cause and surviving logs; do not label it a kernel bug.

Recipes are registry code deployed in the candidate. Parameters cannot be arbitrary
commands/device paths. Resolve hardware selections against observed capabilities
and allowlists. An initial recipe may collect real input events only during an
explicit bounded test window; no permanent keylogger. Audio output defaults to a
bounded duration and conservative level, requires operator-controlled acoustic
observation where needed, and does not increase volume unattended to force failure.
Include recipe version, stimulus hash, exposure counts and limitations in evidence.

Baselines/patched/revert runs compare exact source/deployment/recipe identities and
conditions. A different environment is a recorded confounder, not silently equivalent
to the installed OS. Export inconclusive investigations honestly. No implicit patch
publication or installed-OS modification.

## C6 — Watchdog authorization and revocation (P5)

**Later unattended capability.** These new grants are not an attended-delivery
prerequisite. Initial attempts need an available operator/manual-reset path and
explicit exact-candidate approval; attendance is not hardware reset qualification.
Do not automatically arm an unqualified watchdog or reinterpret an existing grant.
Existing qualified activation checks remain binding when that capability is used.

Keep `RecoveryProfile` exact-build qualification unchanged. Add the capability
`watchdog.authorization.v1` and a **separate** signed `WatchdogAuthorization`.
Do not insert a new field into Experiment v1 or overload its provenance as authority.

Operator policy lives outside the agent workspace. It records campaign/target,
qualified baseline evidence, settings digest, permitted experiment classes and a
monotonic policy epoch. Creating/changing it is explicit local administration;
AgentProposal cannot create it. Known reset/boot/power/watchdog/config/firmware
changes block unattended admission pending review. Unknown diff classification or
an unpinned source tree also blocks it. This screening does not guarantee reset
under an arbitrary kernel patch; that experimental risk remains visible.

Deliver grants through a new authenticated, versioned route
`POST /v1/watchdog-authorization`. Request fields are `schema_version`, `attempt_id`,
`token`, `boot_id`; existing device authentication also applies. Recovery may fetch
after the existing handoff is durably authorized and before arming. Controller
checks attempt ownership, deadline, policy epoch and exact deployment evidence.
Candidate may retrieve the same grant under its authorized next boot. Replays of
the same attempt/policy return the same signed bytes; any identity change conflicts.

Grant fields: version, purpose `quirkbench-watchdog-authorization-v1`, grant ID,
campaign/target/attempt IDs, media ID, origin recovery boot ID, expected next device
generation, deployment manifest digest/revision, kernel build ID, hardware/profile
and settings digests, baseline qualification digest, policy epoch and existing
attempt deadline. Sign canonical bytes with controller trust and verify purpose
as well as signature. No token or private key is part of the signed grant.

Recovery stores it in durable control state with the handoff. At candidate boot,
the runtime verifies it against the local handoff journal, loaded build ID, current
media, observed hardware and settings **before activation**. This ordering matters:
the current runtime activates its configured profile before the normal target loop,
so fetching a grant only after recipe start is too late. A valid cached grant may
enable bounded reset protection offline; it does not authorize recipe execution.
Actual recipe start still needs live candidate adoption/start checks and the current
policy epoch. Missing/expired/mismatched grant permits no unattended recipe; preserve
the reason and request recovery without replay.

Revocation blocks further grants and new controller authorization for arming or
recipe starts. It cannot instantly reach an offline target or prevent an already
issued handoff from completing. Never claim that
distributed revocation undoes physical execution. A started bounded attempt follows
pause/finish/recovery semantics; an already armed watchdog is not abruptly disarmed
on policy expiry, network loss or revocation. A stale grant cannot authorize another
attempt. Recovery uses its own fixed-build profile; candidate grants are invalid in
recovery and cannot turn an offline recovery wait into repeated reboots.

If current-clock plausibility cannot establish the grant deadline, do not start an
experiment. Preserve/handle any already-armed hardware timer through the qualified
systemd path. A device that cannot be safely serviced in recovery is an unsupported
unattended profile, not a reason to invent a second watchdog owner.

Never set coverage to passed when activation succeeds. Report platform baseline
qualification, current kernel identity, authorization and actual armed/timeout
observations separately. Do not substitute an attended flag, software heartbeat,
panic reboot or historical VM result for hardware qualification. Unsupported
pre-userspace hangs remain a manual-recovery limit in v1.

## C7 — Required failure matrix and release evidence

Each owning brief must implement its applicable observable cases with fixtures and
injected clocks/process/storage adapters. Enrollment rows belong to M2; grant and
managed-usage rows belong to their optional M7 packets. A missing physical target
is not a skipped test or evidence of qualification.

| Boundary | Required observable outcome |
| --- | --- |
| Recovery discovers internal controllers | Passive metadata allowed; no internal block opens, filesystem probes/mounts, writes, swap/resume or repair |
| Recovery backing chain ambiguous or duplicate identity | No storage operation; local diagnostic; no first-USB fallback |
| Stock recovery kernel/package mismatch | Refuse staged publication; no custom build fallback |
| Candidate enables internal-storage access | Review/reject before arming; no agent-approved policy change |
| Manual trust/configuration invalid or incomplete | Activation blocked; no verification bypass or fabricated enrollment |
| Operator approval missing or for different bytes/attempt | No arming; build/proposal acceptance is insufficient |
| Unknown/partial inventory | Import retains observations; plan identifies exact blockers; no build queued |
| Inventory contains paths/commands/duplicate JSON keys | Rejected before dispatch or writes outside report/state roots |
| Profile ambiguity or architecture mismatch | Explicit conflict/unsupported result; no fallback to a permissive config |
| CLI exits after submitting intent | Operation survives in managed worker; repeated request returns same ID |
| Old worker completes after restart | Epoch check rejects publication; no duplicate source writer or attempt |
| Pause during build/agent decision | Active bounded unit preserved; no next stage/attempt admitted |
| Crash after proposal persistence, before submit acknowledgement | Same dispatch intent reconciled; usage and experiment not duplicated |
| Partial source snapshot / concurrent edit | Snapshot not publishable/buildable; dirty edits retained for recovery |
| Enrollment redemption/reply/activation interrupted | Same request and key retrieve the same enrollment; only complete private generations activate |
| Generic image/export inspected for secrets | No credentials in factory media, public artifacts or evidence; private state restored separately |
| Armed USB moved to another target | GRUB clears one-shot state and selects recovery before candidate kernel load |
| Binding missing/ambiguous or retarget interrupted | No old credentials, network profiles, watchdog grant or recipe activated |
| Revoked bearer or repository certificate | Both services deny their revoked identity; queued target work pauses |
| Watchdog grant replay on wrong kernel/attempt/media/epoch | Rejected; no claimed qualification and no new recipe |
| Network lost after candidate boot or completion | Bounded finish/reset path; evidence retained; recovery waits without reboot loop |
| Controller restored without private credentials | Readable history; visibly blocked service/dispatch until separate restoration |
| Unknown usage or provider output exceeds limit | Bounded invocation stops/pauses; no fake zero-usage or infinite retries |

Final release evidence still requires the applicable real build/VM/physical and
endurance gates. These fixture tests do not establish hardware support. During
development run only the owning focused suites; do not run expensive acceptance
because a brief mentions its eventual physical outcome.

## C8 — Product workflow and delivery (P0–P8)

The [product interface contract](product-interface.md) is normative for the planned
delivery order, service topology, investigation facade, supported baseline catalog,
recipe extensions, human observations and readiness. Guided setup/pairing gates M2;
backup completeness gates M5. Managed decisions and unattended grants are optional
M7 modes. It extends C0–C7 without replacing the
frozen Experiment/Result envelopes or existing database authority. Implement its
records through additive migrations and versioned schemas. Preview commands are
acceptance targets, not evidence that an implementation exists.

# Product interface contract

Each installation serves one user exclusively. Install per user; do not add shared
instances, accounts/roles, tenants or collaborative workspaces. Controller, targets,
builders and agents act for that user. See the [single-user contract](implementation-contracts.md#single-user-installation).

File access follows the owner-approved [permission policy](implementation-contracts.md#file-access-and-permission-policy).
Ordinary data and workspaces respect usable user-selected permissions; users should
not need chmod rituals or a special umask. Actual secrets receive private defaults.
Implementation migration is tracked in [#66](https://github.com/esper256/quirkbench/issues/66);
this contract update does not claim those runtime changes are already available.

Implement the README's fresh-user installation,
guided authenticated setup/pairing and attended external-agent investigation through
patch export. Fresh controller setup comes first; managed invocation and unattended
target operation are separate optional follow-ons. The
[acceptance guide](installation-to-patch.md) defines evidence;
[GitHub tracker #29](https://github.com/esper256/quirkbench/issues/29) owns current gaps.
Manual authenticated setup remains a supported development/compatibility path, but
does not satisfy the general-release user journey. Exact operator-approved attempts remain mandatory.
Use the [storage policy](architecture.md#storage-protection-policy) for distinct
recovery and experimental protections; no runtime/schema change is claimed here.

This is the planned C8 extension to the [implementation contracts](implementation-contracts.md).
It makes the [preview manual](../README.md) implementable; features below remain
pending until their issue acceptance gates pass. Existing C0–C7 safety and durability rules
continue to apply. There is one controller database and one attempt state machine.

## Release and installation

Publish a user-installable controller archive/launcher, generic recovery image and
signed manifest binding checksums, architecture, runtime assets, API/schema versions
and controller/image compatibility. Setup verifies release signatures against bundled
release trust, then checksums; enrollment trust is separate. Document release-key
rotation and reject unknown/incompatible releases rather than weakening verification.
Manual checksum checks alone detect corruption, not publisher authenticity.

Package runtime units, templates, recipes, migrations and the agent guide as installed
resources. No runtime path may require a source checkout. The current development archive uses existing system Python 3.11+; its guarded
installer manages immutable runtime directories and the launcher. The future signed
installer may supply a private Python environment; users should not construct virtualenvs,
kernel manifests or package recipes to start a supported investigation. Missing host
prerequisites produce specific instructions, never automatic host package changes.
A clean-home installation test uses the built archive without a checkout and exercises
setup with fake container/network adapters. This is a focused software test, not an
image boot or release-qualification run.

## Persistent services and state

Initial registry-mode installations can explicitly run `publication setup --repository
ALIAS --url HTTPS_URL --signing-home EXISTING_PRIVATE_HOME --fingerprint FULL_FINGERPRINT
--request-id ID [--unit PATH] [--json]` with the existing native controller service
stopped and no issued target trust. It initializes a fresh state-owned repository
using existing operator signing trust and preserves an exact private, versioned setup
continuation. It never creates keys, starts services or rotates existing publication.
An exact completed replay is a historical acknowledgment, separate from current
enrollment availability and target/boot authority. See the
[installed sequence](controller-installation.md#initial-controller-setup).

The foreground controller owns the coordinator and bounded container workers. Distrobox is an optional development/agent environment; its disposable
filesystem is not the authority for persistence. Stable private configuration,
credentials, database, source workspaces and retained artifacts live outside it.
Do not reuse the prototype builder's temporary home for agent authentication.

Setup persists the chosen state root under the user's controller configuration.
Commands resolve that identity independently of the current directory. Unconfigured
resolution uses `$XDG_STATE_HOME/quirkbench` (default `~/.local/state/quirkbench`),
never a checkout-local fallback. Setup creates state; queries never initialize it.
New persistent state and build staging must be outside Git checkouts. Preserve
explicit `--state` and low-level commands for development and existing data. Do not
silently migrate or merge an existing `.quirkbench` directory into another identity.
Terminal exit leaves services running. Setup reports logout behavior and offers
explicit instructions for optional lingering; it never enables it implicitly. Sleep
interrupts availability, and reboot restarts reporting/reconciliation with scheduling
paused. No promise of work executing while powered off. Keep C2 ownership epochs,
worker fencing and bounded cleanup; a second CLI cannot become another scheduler.

Manual `monitor` is a read-only TUI client of existing operation/campaign records,
with bounded `--once`/`--json` snapshots and optional recorded development `--run`.
No automatic terminal/browser popup or monitor-owned execution is permitted.
Local `settings show/set` configures retention counts and the optional-cache limit.
Defaults retain five completed physical attempts globally, two recovery releases,
five completed outputs per build category, two input generations and two qualification
runs. Pins and live/uncertain dependencies override counts. Required storage has no
overall byte budget; optional caches alone default to 50 GiB. Mutating commands and
the existing owner trigger housekeeping, with no cron/timer/new service. Read-only
queries remain read-only. `maintenance status/pin/unpin/abandon/prune` exposes local
retention; idle dry-run pruning lists eligible work. Verified publication and stop
proof precede staging cleanup. Failed disposable staging defaults to seven days.
Retired references allow shared-aware CAS/native OSTree reclamation; small historical
records remain, with expired payloads unavailable. See [current settings and coverage](local-state-maintenance.md).

## Public CLI and sessions

**Investigation** is the public organizing concept: problem, target, source workspace,
limits, decisions and accumulated evidence. Its durable identity maps to an existing
campaign and any existing session observation identity in the same database. A chosen
human name resolves to that stable identity; renaming must not rewrite attribution.
Do not reinterpret historical free-standing session strings as complete investigations.
Keep one controller owner and one attempt state machine.

Human CLI, machine JSON, setup wizard and monitor call the same typed application
services for validation, mutation and readiness. Presentation clients do not acquire
worker ownership or introduce independent scheduling/authorization logic. External
mode executes submitted lab work durably but never invokes an agent automatically.

The following forms replace the earlier planned `session start/context/propose`
presentation. They are desired interfaces, not executable-command claims. Preserve
existing campaign commands, implemented `session` observations, `--device`, `--host`,
positional backup/restore, explicit `--state` and current target HTTPS-client flags.
New `target` subcommands must coexist with that flags-only client syntax, with
unambiguous dispatch and compatibility fixtures. Preserve the old specification
parser/fixtures as v1; add a new contract revision before implementing the new facade.

JSON retains C2 versioned envelopes, stable errors and bounded cursor queries.
Machine mutations supply explicit request IDs. Human commands may generate IDs,
but must persist the intent/ID before dispatch and expose them for status/retry;
repeated input after a lost reply resumes the pending intent or requires explicit
new-intent selection. Same ID/content returns the same durable result; changed content
conflicts. A friendly wizard must not turn a lost acknowledgment into another attempt.

M1a implements additive `setup`/`status` syntax in the executable parser; the earlier
product CLI v1 fixture stays unchanged. `setup --json` requires `--request-id`;
human setup generates and prints one and reuses the existing intent on retry.
Optional `--runtime`, `--cache-gib`, `--reserve-gib`, `--host`, `--port`, `--allow-lan`
and `--logout-policy session` choices are normalized before publication.
Omitted retry choices retain the recorded values. The single initial setup journal
refuses a different request or changed intent; later maintenance uses its own APIs.
`status [--json]` does not initialize/migrate state or acquire execution ownership.

Setup progress v1 lives privately in the controller configuration because intent
must precede creation of its database. It is a synchronous setup journal, not a
scheduler or second database. Its schema records state/runtime identity (including
archive and manifest digests), resource/connection/logout choices and ordered completed
steps. Runtime validation additionally checks canonical paths/IPs and digest relationships.
Readiness retains existing service fields and separately reports database, resources,
runtime, release, builder, enrollment and target count. Accepted setup is still partial:
native service startup is available with explicit `--start-service`. Signed release
readiness rechecks retained authenticated archives. `setup --builder-archive` admits
capture/import through the existing worker; read-only builder readiness requires
retained exact OCI inputs and current native image availability. A separately
advertised current-owner publication capability reports enrollment availability;
it requires explicit repository publication and verified current TLS identity.
Full setup acceptance and native commissioning remain pending.

The executable target CLI v2 adds `target add NAME [--request-id ID]
[--ttl-seconds 60..900] [--json]` and `target show TARGET [--json]` alongside the
existing flags-only HTTPS client. JSON add requires an explicit request ID. Human
add retains a name-derived request and original lifetime before dispatch; expired
or redeemed intents need a new explicit ID. The displayed endpoint/full leaf SHA-256
must be compared on the recovery console before transmitting the one-use code.
Show derives names from existing invitation records, or accepts assigned target IDs;
ambiguous names require IDs. Status v1 reports recorded recovery, live credentials
and candidate inputs independently. JSON defaults to this unchanged v1 shape.
`target show TARGET --status-version 2 --json` adds the last authenticated protocol
receipt time and a 30-second advisory contact window; human output uses v2.
Only successful registry-authenticated registration, claim or reconciliation
records a receipt. Current boot/generation and live credentials must still match;
this does not prove continuous connectivity, hardware binding or candidate readiness.
Unattended eligibility remains unknown; pairing/status never grants an attempt approval.

The executable target CLI v4 adds local `target revoke TARGET [--generation ID]
[--request-id ID] [--json]` and `target revoke-code CODE_ID [--request-id ID] [--json]`.
JSON mutation requires an explicit request ID. Human retry retains its target/action
request; another generation requires a new explicit ID. Revocation is available
without a live publication service. Exact generation selection, both-channel denial,
active campaign pauses and the strict target-revocation v1 receipt commit atomically.
Receipt facts describe the decision time, with complete counts and bounded sorted
identity samples. Already paused history retains its reason. COMPLETE invitations
require explicit generation revocation; BOUND pending requests are terminally revoked.
Existing attempts/evidence and worker stop obligations remain separately unresolved;
revocation grants no physical stop, one-shot clearance, drain or retarget authority.

Explicit `target drain-approve TARGET --file PLAN --request-id ID [--ttl-seconds N]
[--json]` grants only the original evidence manifest (at most 128 records/1 GiB)
for one exact revoked generation/attempt/boot/media/binding, after all target work
and worker stops are reconciled. Public output names a private credential file;
tokens stay outside public results/CAS. Grants expire within one hour; exact replay
never extends them. `target drain-revoke TARGET --grant ID [--json]` is terminal.
Only two explicit registry routes accept this separate credential: upload and
evidence acknowledgment. No register/claim/start/heartbeat/handoff/completion,
repository access or physical-state claim is permitted. Fresh owner/scope/time
checks fence I/O and ACK replay; original attempt tokens and attribution remain.
Recovery console choice 7 exports an exact original plan with `plan REQUEST_ID`
and drains its selected records with `drain REQUEST_ID GRANT_ID` after private grant
staging. Original binding/media/runtime/trust and immutable source checks precede
requests and selected ACK saves; retries never widen the plan or complete the attempt.
It reports selected versus additional retained records. Changed hardware stays blocked
pending explicit retarget maintenance.

Explicit `target retarget-code OLD --generation EXACT --new-name NAME --new-uuid
UUID --request-id ID [--ttl-seconds N] [--json]` issues a short-lived invitation
after exact original revocation and all target reconciliation/whole-worker stops.
It preserves original media and binds a different new UUID. Purpose/scope commit
atomically; new ownership and old work fences apply throughout exchange and replay.
Retarget-invitation v1 and authenticated enrollment-result v2 carry exact controller
scope, with no downgrade or old identity inheritance. Initial activation rejects v2.
The invitation alone does not claim local clearance, evidence drain, reset qualification
or boot approval. The reviewed recovery retarget screen now performs explicit stopped
local preparation/activation, preserving bounded linked history and original spools.
Choice 7 accepts explicit archived-plan/drain IDs for any completed linked retarget.
Changed actual hardware, incomplete history or scope changes block secret use. Pending
invitation replacement/resume preserves original keys under the same paused transaction;
remote generation revocation also requires work reconciliation before replacement.

The executable `recovery download [--request-id ID] [--trust-bundle PATH] [--json]`
admits signed factory acquisition on the existing native worker. JSON mutation requires
an explicit request ID; human retry retains the release/archive-derived ID. Current
independent publisher trust and an authenticated compatible v2 installation are
required. The exact signed statement must match the installed release. Completion
retains public factory image, manifest, candidate, statement/signature and the
released-recovery-acquisition v1 index in operation output/CAS. Use the ordinary
operation status/output readers to inspect references; CAS image bytes may be supplied
to a separately operated standard writer. Acquisition neither writes media nor
establishes qualification, builder, baseline or physical execution readiness.
The existing read-only `recovery-images` lists successful acquired sets alongside
prepared images. Additive fields expose publisher statement/signature paths and
fingerprint; old prepared-image fields retain their meanings. Exact retained index,
statement linkage, references and asset sizes are checked within bounded reads.
Publication verification is historical; listing does not rehash whole images or
assert current trust. Missing or inconsistent retained objects are unavailable.

| Commands | Contract |
| --- | --- |
| `setup`, `status` | Resumable controller setup and read-only readiness, including an empty target registry; no fabricated enrollment. |
| `recovery download`, `target add NAME`, `target show TARGET` | Verify compatible released image; C4 interactive enrollment; separate recovery/enrollment/experiment readiness. |
| `investigation start NAME --target TARGET [--problem FILE]` | Persist scope/limits/workspace and supported baseline selection; default external/attended. Preparation never grants boot approval. |
| `investigation brief INVESTIGATION` | Installed guide, workspace, durable context references and ready-to-copy external-agent prompt. |
| `investigation context/recipes/proposal-schema INVESTIGATION --json` | Bounded context, eligible installed recipes and exact supported proposal schema. |
| `investigation propose INVESTIGATION --file FILE --request-id ID` | Accept proposal durably; source validation/freezing and dispatch use C3. |
| `investigation capture-source INVESTIGATION --request-id ID` | Explicit exclusive-writer handoff; return capture operation, not immediate snapshot completion. |
| `investigation submit-baseline INVESTIGATION --compose OPERATION --request-id ID` | Admit one stopped published unmodified baseline to existing jobs; grants no boot/approval authority. |
| `experiment list --investigation INVESTIGATION --json`, `experiment review EXPERIMENT`, `attempt show ATTEMPT --json` | Keep experiment and physical attempt distinct; review exact source/candidate/recipe/risks. Retain current `attempt status`. |
| `attempt approve ATTEMPT` | Human facade supplies durable retry identity; keep existing explicit `--request-id` form and exact approval semantics. |
| `evidence read DIGEST --investigation INVESTIGATION --offset N --length N` | Authorized bounded reads, never private configuration. |
| `operation status ID --json`, `investigation status INVESTIGATION`, `monitor [INVESTIGATION]` | Shared facts and actionable waits; retain current monitor flags. Watching launches no agent. |
| `investigation pause/resume INVESTIGATION`, `target poweroff TARGET` | Reuse pause/reconciliation; coordinated shutdown with local recovery equivalent and explicit uncertainty. |
| `investigation observations INVESTIGATION --json`, `investigation respond INVESTIGATION` | Interactive response uses same durable typed API; machine form takes `--request ID --file FILE --request-id ID`. |
| `investigation report INVESTIGATION`, `investigation export INVESTIGATION --output PATH` | Evidence-linked patch or inconclusive report; public bundle, not a backup. |
| `backup --output PATH`, `restore`, `storage` | Guided completeness, paused restoration and retention services; preserve existing positional/maintenance interfaces. |
| `agent configure`, `investigation driver INVESTIGATION --managed` | M7 optional configured invocation, paused/reconciled writer handoff; never default or implicit. |
| `target qualify TARGET` | M7 separately authorized qualification; bounded unattended authority remains C6, not a consequence of pairing or managed mode. |

Specify exact argument/schema/help fixtures per owning P0 packet; unsupported
commands must not pretend to work. Keep frozen wire names such as `device_id`,
`session_id` and existing `--device` options. No bulk database vocabulary migration.

## New records and compatibility

Use C0 strict validation and explicit versioned readers. Existing schemas, including
Experiment/Result, retain their meanings; this prose does not relax a validator.
Each packet freezes its mechanical schema and failure fixtures before adapters.

| Record boundary | Required identity/semantics | Migration rule |
| --- | --- | --- |
| Signed release set | Exact controller, recovery, builder and baseline/catalog digests; platform/API compatibility, publisher trust and qualification evidence | New/successor distribution record; existing private `unqualified` recovery statements never become qualified by relabeling; development archives remain readable |
| Setup progress | Selected state/runtime, completed configuration steps, operation/request identity, prerequisites and separate readiness facts | New versioned journal; resume and reconcile existing configuration without merging another state root |
| Credential lookup generation | Target/media/generation/system UUID, token digest, exact repository leaf fingerprint and expiry; terminal revocation | Additive controller migration; registry mode opt-in and exclusive with legacy static authentication; no enrollment/attempt authority |
| Controller TLS identity and service continuation | Private local CA/server file identities, exact initial setup intent and verified service publication stages | New v1 records; no target credentials, publisher identity or second execution owner |
| First-publication setup continuation | Exact original/successor configuration, operator public signing key and original controller TLS digests; repository initialization progress | New private publication-setup v1 record; exact setup retry only, no existing trust rotation or live readiness claim |
| Controller release-set statement | Authenticated archive/version, platform/API/Python constraints, expiry and exact recovery/builder/catalog references | Versioned v1/v2 verifier and independent trust loader; v2 structural reader checks stay separate from runtime readiness and qualification |
| Target revocation | Exact terminal generation/invitation decision and immutable bounded work observations | Additive command receipts and admitted attempt generation references in the existing database; no physical completion or drain authorization |
| Released recovery acquisition | Exact installed publisher statement, verified factory image/metadata and retained unqualified asset references | New v1 index in existing operation/CAS records; preserve RPM acquisition specifications and all readiness/approval boundaries |
| Builder preparation | Exact release statement, archive/config/base identities and stopped-worker native import/base-marker proof | New v1 output in existing operation/CAS records; no baseline closure, enrollment or qualification authority |
| Enrollment | C4 expiring request/key binding, durable credential issuance, private generation and revocation | New strict exchange records; retain validated manual generations and existing target protocol readers |
| Investigation | Stable identity/name mapping, campaign/session references, problem digest, workspace/base, limits, driver and execution owner | Additive database migration and explicit reader for legacy session/observation records; no invented historical workspace or source coverage |
| Source workspace/capture | Repository origin and actual Git object ID, distribution source/patch provenance, immutable captured content/config identities, capture completeness | Keep Git commit OIDs distinct from artifact SHA-256; existing proposal-v1 `base_revision` digest is not silently redefined as a Git OID |
| Report/export | Bundle version, base/final source identities, patch series, attributed experiment/attempt/evidence references and limitations | New manifest/readers; missing retained bytes explicit; no secrets or new execution authority |

Any new recipe physical-observation support requires a successor schema and
compatibility reader: existing versions rejecting nonempty physical observations
must not silently begin accepting them. Keep optional unknown values unknown.

## Editable sources and patch results

Baseline selection consumes a distributable immutable catalog and pinned repository,
package, trust and source identities. Existing controller caches cannot be a hidden
installation prerequisite. A missing pinned input blocks with its identity; never
silently substitute an available newer kernel or inherit host repository configuration.

Prepare a separate editable kernel Git workspace using an actual recorded base.
For distribution sources retain the source-package and distribution-patch provenance
and enough reconstruction information to explain the upstream relationship. Existing
user source is an explicit pinned revision or approved dirty capture, copied into
the investigation workspace without modifying the original. C3 streaming capture,
exclusive writers, modes/deletions and immutable build inputs remain mandatory.

Reports distinguish lab commissioning from reproducing the problem and compare
baseline, patched, regression and practical revert attempts with exposure counts,
confounders and missing observations. Export the README layout: `README.md`,
`report.md`, `patches/`, `reproduce/`, `experiments/`, `evidence/`. Produce patches in
`git format-patch` form against the recorded base, preserving author metadata;
do not fabricate authorship or sign-offs. Record exact reconstruction/application
instructions. If final cleanup changes tested content, require another approved
validation or label the exported patch unvalidated. Retain required sources, symbols
and evidence through existing dependency/pin services; expired bytes remain missing,
never reconstructed evidence. A useful inconclusive report is a supported outcome.

## Supported baseline catalog

Inventory selects a supported baseline, not an arbitrary distribution reconstructed
from device IDs. Versioned catalog entries pin kernel sources/configuration, userspace
RPM closure, build/diagnostic recipes, replacement-RPM packaging paths, platform and
protection requirements. HardwarePlan records the catalog identity and deterministic
selection rationale. Native kernel/module and userspace changes must enter the same
composed revision with matching symbols. Custom sources require an explicit supported
recipe. Unknown hardware/package combinations produce a support task; an agent cannot
guess a compatible baseline or relax protection. Recovery inventory is not a report
of the installed OS's configuration.

## Agent decisions and source handoff

Implement a complete external-agent journey first. Managed operation is an optional, explicitly selected capability,
added through one concrete supported command adapter over
the same services. Both modes use C3 immutable source capture and exclusive ownership.
External agents submit pinned source revisions or completed capture references; an
accepted proposal cannot later change because its original worktree changed.

A durable deduplicated decision queue records actionable build/validation failures,
completed bounded batches, deterministic early-stop conditions, and human responses.
Replayed events cannot duplicate a decision or charge recorded usage twice. Define
batch boundaries and early-stop predicates before dispatch; evaluate between physical
attempts. A decision may inspect an uncertain result, but cannot authorize another
attempt until recovery/reconciliation fences clear. Heartbeats and evidence chunks
never trigger model calls. No concurrent source writers. External mode does not launch
an agent or claim to meter unrelated agent spending. Unknown managed usage stays unknown
and applies the configured pause policy. A long operation consumes no waiting agent.

## Recipe extensions and human observations

A versioned recipe manifest identifies its digest/version, approved entrypoint, typed
parameters, runtime/resource limits, hardware/capability predicates, required privileges
and physical observations. Candidate RPM composition installs code and registry metadata
together. Dispatch verifies the installed identity against the authorized recipe;
unknown versions fail explicitly. There is no arbitrary target-shell escape hatch.
New privilege or failure mechanisms require review independent of the proposing agent.

Recovery disables automatic suspend. Candidate suspend is allowed only through an
explicitly eligible bounded recipe with approved sleep mode, attended validation and
recorded reset limitations. Separate recovery/candidate unit policy; do not globally
unmask suspend or assume existing blanket masks permit the README's suspend example.
Hardware watchdog suspend coverage must be checked independently.

Human requests/responses are additive versioned durable records with session, attempt
(if physical), recipe step, request ID, timestamp, operator attribution and deadline.
Distinguish pre-test readiness, live observation and post-test interpretation. Responses
are idempotent; conflicting duplicates fail. Missing observations remain missing, and
late answers remain attributed to their original request without satisfying a newer
step. Human waiting cannot extend the attempt's physical deadline. Offer CLI response
first; interactive monitoring uses the same protocol. Foundational recipe registration
and human-response support precede the complete external-agent journey; expanding the
diagnostic catalog comes later.

## Readiness and safe shutdown

Expose separate facts: recovery booted, enrollment available/complete, experiments
permitted, and unattended qualification. A valid early-boot target UUID and storage
protection remain requirements for candidate authorization. Lack of a watchdog can
permit attended operation; a missing/default/ambiguous UUID cannot be bypassed merely
by attendance. Any alternative identity gate needs its own reviewed design.

Pause status separately reports scheduling stopped, workers draining, target returning,
recovery confirmed and pending/acknowledged evidence. Do not change terminal attempt
outcomes to represent UI state. `Recovery ready` alone is not a safe-eject indicator.
Safe shutdown requires no active attempt/writer, reconciled target state, all produced
evidence durable locally, and orderly sync/unmount/poweroff. Pending controller upload
may remain, but must be visible and cannot be represented as acknowledged. External
editors require explicit quiescence/checkpointing; pause does not terminate them.
An acknowledged shutdown request is not confirmation of physical poweroff. Loss of
contact stays unknown; when software cannot observe final power state, ask the operator
to confirm locally before disconnecting media. Offline recovery offers the same safe
local shutdown sequence without requiring a controller acknowledgment of uploads.

## Endpoint changes and media capacity

**M2 guided setup and maintenance.** Reuse implemented capacity choices and journaled
geometry; do not reimplement them or remove them as premature features. Integrate
the fresh-user screens and explicit paused endpoint maintenance. Local full
boot-device confirmation and capacity refusal remain mandatory before mutation.

Endpoint migration is an operator-guided controller/target setup transaction. A changed
IP may require a new certificate SAN, even when the CA is retained. Validate endpoint
reachability and trust before activating an atomic private configuration generation;
an interrupted switch retains a usable previous generation. Reconfirm fingerprints
when controller trust identity changes. Never bypass TLS or silently replace trust.
Recommend a stable DHCP reservation where available; address changes do not require
reflashing media.

Before the first partition mutation, recovery shows a read-only external-device identity,
capacity/RAM check, proposed geometry and supported advanced sizing choices. Run this
screen from recovery/RAM before evidence exists. Persist the confirmed geometry in the
existing boot-state commissioning journal before changes; retry the same plan, observe
completed steps and never reformat an existing filesystem on a missing acknowledgement.
No credentials are needed for this step. Recheck evidence/log/dump capacity when moved
to a target with different RAM; insufficient capacity blocks affected operations, never
automatically repartitions enrolled media. Later resizing is not a v1 repair shortcut.

## Backup completeness

**M5 guided capability.** Existing backups list their contents and limitations,
including omitted private credentials, uncaptured source edits and possibly
unuploaded target evidence. Keep existing consistent database/CAS/OSTree retention
and restore validation; do not weaken an implemented backup check. Report unknown
coverage as unknown. The comprehensive workflow below gates the main manual's
backup/restore promise, not M1 controller setup.

A guided backup first checkpoints/quiesces approved source writers or reports the
workspace capture incomplete. Preserve unfinished edits, operation inputs, database,
CAS and retained OSTree closures at an identified consistent cut. A completeness report
lists source/checkpoint coverage, active operations, target-only pending evidence and
private credentials that require separate backup. Offline target status is last-known
or unknown, never an invented zero pending count. Controller-complete and whole-session
complete are different claims. Do not label a backup fully resumable while required
sources, credentials or evidence are missing. Restore verifies content and stays paused
until private identity is restored and outstanding execution is reconciled. Public
exports still exclude private credentials and are not resumable backups.

#### Available immutable investigation build/composition facade

`investigation prepare-candidate NAME --request-id ID` derives candidate-rootfs
input v1 from the investigation baseline. `investigation build NAME --capture OP
--candidate OP --request-id ID` binds stopped source/candidate receipts to
investigation-build-input v1. `investigation compose NAME --build OP --repository
ALIAS --request-id ID` binds a completed joined build and configured publication
to investigation-compose-input v1. All accept `--json`, return the existing durable
operation envelope, and use existing build/compose worker kinds with versioned
schema 3 arguments. Manifests are derived inside the adapter, never accepted from
the caller on this path. Schema 2 manual jobs remain compatible.

The reviewed fixed-build-recipe v1 descriptor has exact allowlisted semantics;
unknown descriptors are unsupported. Offline composition consumes only the pinned
RPM snapshot plus the existing generated kernel/userspace replacements. Both worker
and stopped owner verify exact package identities and pinned recipe bytes before
signing. The owner rechecks dependencies and publication configuration after CAS
callbacks before committing success. Every completed joined job retains an
investigation-artifact-link v1 in its public outputs. This is attribution, not
operator approval or native acceptance. Existing expired metadata remains missing;
new workspace publication retains its original preparation intent/input so normal
operation retirement does not destroy that join.

#### Available external proposal admission

`investigation propose NAME --file FILE --request-id ID [--json]` accepts strict
agent-proposal v2. `proposal-schema` returns the installed standalone schema and
current `proposal_scope`/`source_free_scope` receipts. `context` includes the same
proposal scope and known usage totals with incomplete-observation counts.
`investigation proposals NAME [--after CURSOR --limit N] --json` pages retained
decisions and immutable dispatch intents without starting a controller or agent.

V2 `base_oid` is the actual Git object ID. Existing proposal-v1 `base_revision`
remains a digest and its reader is unchanged. `input_context_digest` hashes the
canonical proposal-context v1 receipt supplied in `input_context`; it identifies
the immutable investigation/baseline/capture scope, not all mutable context or the
external agent's full prompt. The controller independently binds that scope.
Experiment proposals require a completed stopped capture and its latest unreleased
QUIESCED writer handoff, exact baseline/build/installed reviewed recipe identities,
and parameters/deadline within the reviewed manifest. Target installation,
peripheral availability and exact-attempt approval remain separate gates.

Source-free `needs_human` and `conclude` proposals may describe selection or
preparation failures; use the returned source-free receipt, null source/base and
null experiment. Acceptance atomically retains proposal, context, source closure,
nullable usage observation and dispatch intent in the existing database. Operation
rows own the sole lifecycle; no second scheduler or outbox claim authority exists.
Replay the same request ID and bytes for a lost response. Different bytes or reuse
of a decision under another request ID conflict. Replay does not reacquire a writer,
reread current source or duplicate usage. Unknown token observations remain null;
known totals do not claim to meter unrelated external spending.

Admission is bounded metadata work, not a full archive rehash or source scan.
Captured bytes were verified at stopped publication and are retained for later
independent execution validation. An unbound operation has the named
`external_loop_pending` reason. `dispatch-proposal NAME --proposal OP
--candidate CANDIDATE_OP --repository ALIAS --request-id ID` binds one strict
proposal-dispatch-input v1 to the original operation. Source-free actions omit
candidate/repository. Exact replay precedes current readiness/source checks;
changed choices or a second binding conflict.

The existing controller owner atomically links deterministic build/compose child
requests and advances the parent. A stopped admitted capture is read from the
proposal-owned retained closure, independently of later writer edits or original
capture-owner retirement. Child claims revalidate the current parent and exact
frozen intent. Pause blocks new stages; already claimed workers drain. Restart
requires explicit parent and interrupted-child resume after termination
reconciliation. Storage/native failures interrupt without automatically retrying.

Experiment submission reuses the published-composition proof, with strict
proposal-experiment-input v1 attribution. It binds the exact recipe, parameters,
repetitions, deadline and candidate deployment; existing experiment/jobs/refs and
parent completion commit atomically after final CAS/native-retention fences.
`proposals` exposes the linked child/experiment IDs. Source-free human/conclusion
actions retain their decision and pause without build or experiment. Parent success
means submission; attempt approval, evidence acknowledgement, recovery and problem
reproduction remain separate. No managed invocation or unattended grant is implied.

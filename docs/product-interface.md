# Product interface contract

**Initial delivery, revised 2026-09-29:** an attended external-agent investigation in
an owner-controlled lab, using manual authenticated setup and exact operator-approved
attempts. The roadmap's delivery tiers govern dependencies. Sections describing
pairing automation, managed decisions, endpoint/advanced capacity wizards and guided
backup completeness specify later capabilities. They do not gate the first journey.
Use the [storage policy](architecture.md#storage-protection-policy) for distinct
recovery and experimental protections; no runtime/schema change is claimed here.

This is the planned C8 extension to the [implementation contracts](implementation-contracts.md).
It makes the [preview manual](../README.md) implementable; features below remain
pending until their handoff gates pass. Existing C0–C7 safety and durability rules
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

The controller's systemd user service manager owns the coordinator and rootless
container workers. Distrobox is an optional development/agent environment; its disposable
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

Freeze only command forms needed by the initial attended journey before their
implementation. Retain already frozen fixtures; the full table also lists later
interfaces and is not a requirement to implement or refreeze everything first. Session records map to existing
campaigns; do not add a parallel execution database. JSON uses the C2 versioned envelope,
stable error codes and bounded cursor queries. Mutations use request IDs; retrying the
same request returns the same durable intent, and changed content conflicts.

| Commands | Contract |
| --- | --- |
| `setup`, `pair`, `targets`, `target qualify TARGET` | Initial manual authenticated setup/readiness; later setup/pairing wizard and separately authorized qualification. |
| `session start --device TARGET [--problem FILE] [--driver external]` | Persist scope/driver/workspace selection; the session owns its initial attended baseline operation. Default is external; managed invocation requires explicit selection. |
| `session context/recipes/proposal-schema SESSION --json` | Bounded context, installed eligible recipes and exact proposal schema. |
| `session propose SESSION --file FILE --request-id ID` | Accept a proposal and return an operation ID; source validation/freezing and execution happen later under C3. |
| `session capture-source SESSION --request-id ID` | Explicit quiescent dirty-source handoff; returns a durable capture operation, not a claim of an immediately complete snapshot. |
| `experiment list --session SESSION --json`, `attempt show ATTEMPT --json` | Preserve the distinction between experiment specification and physical attempt. |
| `evidence read DIGEST --offset N --length N` | Authorized bounded reads, never private configuration. |
| `operation status ID --json`, `session status/watch SESSION` | Same progress facts in machine and human views; watching launches no agent. |
| `session pause/resume SESSION`, `session export SESSION --output PATH` | Existing pause/reconciliation fences; export is a public investigation bundle. |
| `session observations SESSION --json`, `session respond SESSION --request ID --file FILE --request-id ID` | Query and durably answer typed human requests; monitor is a client of this same API. |
| `backup --output PATH`, `restore` | Initial retained backup behavior with explicit contents/omissions and paused restore; later guided completeness. Retain existing positional syntax. |

Specify exact argument/schema/help fixtures in P0; unsupported commands must not
pretend to work. Keep frozen wire names such as `device_id` and `--device`.

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

## Endpoint changes and media capacity

**Later wizards.** Initially document explicit paused endpoint maintenance with
existing validated configuration and current fixed journaled geometry. Local full
boot-device confirmation and capacity refusal remain mandatory before mutation;
advanced sizing UX and automatic migration orchestration do not gate attended use.
The following describes the later operator-guided tooling.

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

**Later guided capability.** Initial backups list their contents and limitations,
including omitted private credentials, uncaptured source edits and possibly
unuploaded target evidence. Keep existing consistent database/CAS/OSTree retention
and restore validation; do not weaken an implemented backup check. Report unknown
coverage as unknown. The comprehensive workflow below is deferred.

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

## Deferred external media

Direct USB storage remains the sole v1 media implementation. The
[external-hardware design](external-hardware.md) reserves post-v1 Pi gadget support
without new public commands or frozen-record changes now. Media export has its own
accessory identity and exclusive ownership; it does not replace target binding,
HTTPS trust, recovery authority or backup completeness. A private backing image is
not a public export or a consistent live backup. Accessory health remains separate
from target readiness and safe-shutdown status.

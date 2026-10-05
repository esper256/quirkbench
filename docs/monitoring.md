# Monitoring

Current monitoring shows build and recovery/experiment state without invoking an
agent. Connection, candidate eligibility and operator approval are separate facts.
Remaining guided integration and investigation views are tracked in
[GitHub #29](https://github.com/esper256/quirkbench/issues/29); the
[roadmap](product-roadmap.md) defines scope.

Run `quirkbench setup` once, then open `quirkbench monitor` manually in an
existing terminal. State defaults to `$XDG_STATE_HOME/quirkbench`, or
`~/.local/state/quirkbench`. The TUI lists operations and investigations, refreshing
bounded summaries every two seconds. Use arrows/j/k to select, Enter for details,
`l` for bounded diagnostic logs, PgUp/PgDn to scroll and `q` to exit. No window or
agent is launched; exiting has no execution effect. `--once` prints a snapshot;
`--json` returns the existing C2 envelope. `--run RUN_ID` views a recorded ad hoc
bounded development build. Development output alone is not measured compile progress.

Use `quirkbench monitor INVESTIGATION` to restrict operation and campaign queries to
one recorded investigation before applying the display limits. `--once` and `--json`
retain that filter; it cannot be combined with `--run`. The view reports admission,
remaining worker units, unresolved target work, recent authenticated recovery contact,
pending human requests and their pagination command separately. A recorded shutdown
preparation adds target-local durability and the unacknowledged upload inventory;
an absent preparation remains unknown. Read-only monitoring works while the controller
service is stopped. It does not initialize, migrate, reconcile or clean the database.
See [attended shutdown](recovery-operations.md#attended-safe-shutdown) for the commands
and the local physical confirmation required before removing media.

Queries open an existing database read-only, without controller construction,
migrations, startup reconciliation or ownership changes. Missing setup, incompatible
schemas and unavailable services are reported. The display separates phase advancement,
heartbeat age, measured counters, waits, deadlines, worker completion and publication.
Stock preparation reports package/runtime installation, initramfs, assembly and owner
validation/signing/publication. Build/compose jobs report input preparation, compilation
or composition, owner validation, signing and publication. Use `experiment status`
or `experiment logs` for a submitted test, and `admin operation show` for low-level
troubleshooting;
test submission returns its request ID and next-step commands immediately. Worker
JSON remains advisory and is accepted only by
the current owner under its exact epoch/generation/claim fence. Malformed advisory
records cannot become execution authorization or terminate the coordinator.

`admin operation list --json` pages at most 100 summaries with `--after`/`--limit`.
`admin operation show ID` prints one recorded status snapshot. Use `monitor` to
follow investigation progress continuously; Ctrl+C stops only that view.
For a bounded ad hoc development build, use `dev monitor RUN_ID [--once] [--json]`.
Output bytes measure activity, not percent complete.
A quiet command is not proof of deadlock. Logs come only from the selected run's
allowlisted directories; reads are bounded, links are rejected and terminal control
characters are filtered in human views.

Storage shows filesystem free space and the last maintenance summary without walking
large trees on every refresh. Configurable retention keeps completed attempts,
releases, builds, inputs and qualification outputs by count; pins and live/uncertain
work remain protected. Required storage has no overall byte budget. Only optional
caches default to 50 GiB of logical file bytes; Btrfs sharing/compression means this
is not exclusive physical usage. Successful disposable staging needs verified
publication and whole-worker stop proof. Failed staging defaults to seven days.
Retired payload references permit shared-aware CAS collection and native OSTree
pruning; database history remains with expired payloads unavailable. `maintenance
prune --dry-run` describes eligible cleanup; `maintenance prune` executes it while
idle. Mutating commands and the existing owner also trigger housekeeping. Read-only
monitoring does no cleanup; no cron/timer or additional service is involved. See
[settings and directory coverage](local-state-maintenance.md).

Run `quirkbench --state /absolute/controller-state watch CAMPAIGN`. A TTY refreshes the same view; redirected output is a timestamped sequence of snapshots. `--once` prints one snapshot, and `--json` provides machine-readable snapshots. The watcher reads the controller directly and spends no agent tokens. `campaign status` remains a concise administrative JSON view. `Controller.events(campaign, after=cursor)` retrieves durable progress events in order.

Each activity reports phase, state, human-readable reason, optional measured completed/total/unit counters, expected report interval, stall threshold, and deadline. IDs and sequences make duplicates harmless. The controller assigns receipt times; replaying the same sequence never refreshes its age. A changed sequence with the same phase/message/count/state proves only that the reporting loop is alive. The immutable deadline cannot slide forward on each heartbeat.

| Display | Meaning and next action |
| --- | --- |
| ACTIVE | Recent reports; inspect measured counters to establish advancement. |
| WAITING | Explicit bounded wait, such as provider response or a physical observation. Unknown percentage is shown literally. |
| REPORTING_LATE | No fresh report within the promised interval; inspect that process or connectivity. |
| SUSPECTED_STALL | Reports continue but no meaningful phase/message/count/state advancement occurred within the threshold. This is a suspicion, not proof of deadlock. |
| OVERDUE | The activity exceeded its original deadline; inspect or recover the responsible component. |
| UNCERTAIN | Physical attempt lost its lease/boot continuity; reconcile before repetition. It is not automatically a kernel crash. |
| COMPLETE / FAILED | Terminal operation report, retained in history. Recipe result and evidence determine experimental outcome. |

Target contact is tracked separately from attempt authorization. A response to registration or heartbeat establishes recent communication; progress reporting does not renew a lease. Attempt views show elapsed time and remaining deadline. Current recipes expose supervisor liveness and bounded execution, but do not yet expose intermediate device-level progress; the display says that progress is not measured. Add counters in the recipe adapter before claiming it can detect a live-but-stuck kernel experiment.

Uploads display controller-durable bytes. Campaign bars count finalized jobs, including inconclusive/failed/explicitly abandoned jobs; they do not mean the underlying issue is fixed. Build and agent adapters use `monitor.Activity`; the build runner reports output activity and object counters where available. Agent waiting reports do not pretend to measure provider tokens as they are generated.

An independent monitor cannot prove that a disconnected machine is alive. Its purpose is to separate known advancement, recent liveness, planned waits and missing information. Absolute sample timestamps reveal a stalled watcher; last-report ages and deadlines reveal stopped producers. Final release checks must qualify these signals under actual network outages, suspend and crashes.

OSTree composition, publication, object transfer and deployment preparation use these same progress semantics. Show measured transferred bytes/objects where the tool exposes them, then deployment preparation and boot handoff as separate bounded phases. A reporting subprocess with no measured advance must not conceal a stuck composition or deploy. Last known commit and attempt identity accompany boot-stage reports; no synthetic percentage or network timeout implies successful boot or a kernel crash.

Current OSTree subprocess monitoring reports bounded phases and output-byte activity, not measured network bytes or a percentage of the OS download. A quiet long-running command can therefore show suspected stall even if it is still working; qualify or add tool-specific counters before claiming precise transfer progress. Composition emits periodic activity and retains logs. Durable physical handoff and boot-stage reporting are implemented; their physical behavior still requires the hardware gate.

## Physical execution and reset observations

The controller monitor includes BOOT_PENDING while waiting for the exact next boot,
and keeps result completion separate from recovery arrival. Target inventory shows
last-observed watchdog identity, armed state, actual timeout, qualified boot stage
and each partition's capacity. These are labeled as last target reports, not live
hardware counters. Unknown timeout/countdown values remain unknown.

The systemd-supervised target loop emits service liveness independently of useful
progress. Live recipe chunks and uploads have measured counts; long library
verification emits hashed-byte progress. A heartbeat cannot extend a phase deadline.
Recovery waits visibly for provisioning/network, while authentication, protocol,
storage and identity errors stop with a preserved human-intervention reason.


## Recovery setup visibility

Before configuration or pairing, the target must provide local status; a controller monitor cannot
report a target it has never contacted. P3 adds explicit phases for storage readiness,
network configuration/link/address, controller reachability, trust confirmation,
enrollment, inventory upload and baseline waiting. Distinguish operator input from
a timed network operation. Never display passwords, enrollment codes or private keys
in diagnostic logs. A changed/ambiguous target binding visibly requires setup while
retaining prior evidence. The existing runtime waits safely; the guided UI is planned.

## Planned product status semantics

The [product interface contract](product-interface.md#readiness-and-safe-shutdown)
separates enrollment, experiment eligibility and unattended qualification. Pause
shows scheduling stopped, draining workers, recovery arrival and pending evidence
individually. Safe shutdown is a separate derived condition, not a synonym for
Paused or Recovery ready. Human-input UI uses durable request/response IDs and
deadlines; late responses cannot satisfy a newer attempt or extend its deadline.

Upload declarations now have durable attempt ownership. Pending/resumable uploads
and completed uploads awaiting acknowledgement protect their bytes and owner
retention. Unidentified legacy uploads are visible in `maintenance prune --dry-run
--json`; `maintenance abandon-upload ID` explicitly retires an unneeded upload,
subject to active/unresolved-attempt checks and configured grace. Terminal uploads,
attempt completion and confirmed recovery return request idle-owner housekeeping.
No monitor cleanup, timer or additional service is involved. See
[upload retention](local-state-maintenance.md#upload-retention).

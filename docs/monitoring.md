# Monitoring is part of the execution contract

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

An independent monitor cannot prove that a disconnected machine is alive. Its purpose is to separate known advancement, recent liveness, planned waits and missing information. Absolute sample timestamps reveal a stalled watcher; last-report ages and deadlines reveal stopped producers. M4 must qualify these signals under actual network outages, suspend and crashes.

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

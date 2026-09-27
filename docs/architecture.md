# Quirkbench: architecture and v1 contracts

This repository is a local, evidence-first laboratory for Linux experiments. The controller owns scheduling and durable state. A booted target reports its capabilities, claims one bounded attempt, performs the privileged recipe locally, and uploads observations. The included `smoke` recipe runs only in simulation mode and demonstrates the protocol without making a kernel claim. The HTTPS transport and image/QEMU build helpers are software components; actual target boot control needs a commissioned implementation and separate qualification.

## Trust and execution boundaries

The controller is authoritative for campaign, job, attempt, lease, and evidence metadata in SQLite. Content-addressed blobs live in an immutable store keyed by SHA-256; their hashes are checked when read and transferred. State changes that authorize work or acknowledge evidence must commit durably before the controller responds. File publication uses a temporary file, flush/fsync, atomic rename, and directory fsync. A backup must capture a consistent database and all referenced blobs; restoring it must recheck the blob hashes. A copied SQLite file alone is not a complete backup.

The target alone runs privileged recipes. The controller can offer artifacts and instructions through the target-facing protocol but cannot run those tasks on its own host. HTTPS uses a server certificate, a device bearer token, bounded request bodies, and a device-scoped artifact endpoint. Administrative operations remain local to the controller. A target journals an outbound operation before sending it; it removes that outbox item only after the server has durably committed and acknowledged it. Retries carry stable identities so a duplicate message is either an idempotent replay or a conflict, never a second execution.

The target's boot mode is an independent fact from experiment outcome. `recovery`, `experiment`, and `simulation` are capability report modes; they are not `PASS` or `FAIL`. A reboot changes the boot identity. Registration of a new boot makes unfinished attempts from an old boot uncertain, while retaining their evidence. An operator must resolve an uncertain attempt explicitly before any new attempt of that work is issued. A lease expiry stops authorization for new execution; it does not erase prior observations or prove that an already-running task stopped.

## State model

| Entity | States | Meaning |
| --- | --- | --- |
| Campaign | `RUNNING`, `PAUSE_REQUESTED`, `PAUSED` | Pause is durable; the controller stops new claims at the request and reaches `PAUSED` after in-flight work is reconciled. Resume is explicit. |
| Job | `QUEUED`, `ACTIVE`, `DONE` | A job is a durable plan for an experiment and its repetitions; attempt outcomes determine its eventual disposition. |
| Attempt | `CLAIMED`, `RUNNING`, `UNCERTAIN`, `RESOLVED`, `COMPLETE` | A claim reserves work for a specific device, boot, and lease. `UNCERTAIN` preserves ambiguity after reboot, lease loss, or lost acknowledgement. An operator moves it to `RESOLVED` with a retry or abandon disposition and a reason. `COMPLETE` records a final result and evidence references. |

The controller startup recovery step marks unfinished claims and runs `UNCERTAIN`, pauses campaigns, and requires reconciliation before resume. A retried resolved job gets a new attempt ID; the old attempt, evidence, and operator note remain. An explicit resume starts a new session only when the device reports recovery or simulation mode and no unresolved attempt remains.

Each session defaults to eight hours and one million recorded decision tokens. Expiry or exhaustion requests a pause, blocking new claims while allowing an already issued attempt to report evidence. Lowering a budget below usage already recorded must trigger that pause immediately. Repeated decision IDs are idempotent; a changed replay is a conflict. These budgets constrain controller scheduling, not the physical duration of an already executing recipe.

`PASS`, `FAIL`, `INCONCLUSIVE`, `INFRA_FAILURE`, and `NEEDS_HUMAN` classify a completed attempt. These outcomes are distinct from lifecycle states. A crash or missing reply is not automatically a recipe failure. Every claim, start, heartbeat, evidence reference, completion, and operator resolution should remain auditable with stable identifiers. The controller is the source of truth for claim eligibility; a target must not interpret an expired lease or a cached job as fresh permission.

## Versioned wire records

The six v1 record shapes have matching runtime validators in `src/quirkbench/contracts.py` and JSON Schema 2020-12 definitions in `schemas/`. They reject unknown fields, unsupported versions, invalid identifiers, and malformed digests. Omitted optional fields take the runtime defaults; when `schema_version` is supplied it must be the integer `1`. Each example in `examples/` is a valid shape illustration, not a certified recipe or outcome. JSON values in arbitrary `parameters`, `inventory`, `measurements`, `notes`, and `provenance` objects still have to be finite, serializable JSON.

| Record | Purpose |
| --- | --- |
| `Experiment` | Hypothesis, recipe, bounded repetitions and timeout, required capabilities, artifact digests, success criteria, and provenance. |
| `Artifact` | Digest and byte size for an immutable blob. |
| `CapabilityReport` | Device and boot identity, boot mode, declared capabilities, and inventory. |
| `Result` | Attempt identity, one outcome, concise interpretation, evidence digests, measurements, and limitations. |
| `Checkpoint` | Campaign identity plus durable artifact references and operator notes. |
| `Progress` | Activity/campaign identity, phase, sequence, measured counters, expected report interval, stall threshold and fixed deadline. |

The v1 field sets and outcome vocabulary are frozen for this milestone. Incompatible changes require a new schema version and migration/replay tests; adding an unknown field to a v1 message is intentionally rejected. This prevents an older target from silently ignoring a control or evidence field.

## Boot and build boundary

The agreed target layout has fixed recovery files, a separate GRUB one-shot state partition, and journaled ext4 experiment/evidence storage. GRUB consumes candidate state before a full boot and defaults to recovery. Recovery is never automatically replaced by a candidate. Only the positively identified external data partition may expand during restartable commissioning. Internal storage controllers are excluded from both recovery and candidate kernels; disable automount, swap/resume, firmware updates, EFI writes and EFI-backed pstore. Secure Boot must be verified disabled. Owner boot selection and complete-hang manual recovery remain explicit boundaries.

The current image module is an unqualified two-partition regular-file prototype, not the final target architecture. Its public API is not frozen. Its recovery shell, one-shot marker and staged kernel policies support future virtual trials, but no hardware deployment or completed boot claim follows from its existence. M2 must finish the separate state/data layout and all commissioning checks.

The QEMU trial uses a copy-on-write USB overlay, a copy of the OVMF variables template, and a regular-file internal-disk sentinel. It compares sentinel/template hashes and also records before/after hashes of the guest's actual mutable firmware-variable copy. A pristine template alone cannot establish firmware preservation. A zero QEMU exit, timeout or unchanged sentinel does not prove guest boot behavior: serial assertions and a qualified firmware-variable baseline are required. Real USB boot, power loss, storage isolation and long duration operation require separate physical validation.

## Human visibility

Progress is a versioned protocol record, not ephemeral terminal output. The controller assigns receipt times, persists event history, and distinguishes last report from last meaningful phase/state/message/counter advancement. Replay cannot refresh liveness, move counters backward or extend the original deadline. Reported progress never authorizes execution or renews a lease. See `docs/monitoring.md` for display semantics and `tests/test_monitor.py` for deterministic stalled/slow/waiting tests. The terminal monitor also shows target contact, job counters, lease/deadline state and controller sample timestamps without invoking an AI agent.

## Verification claims

Unit and integration tests exercise record rejection, durable state transitions, protocol retry and identity rules, target journaling, and local demo flows. The QEMU acceptance path is opt-in because it needs a built image and OVMF fixtures. CI can establish software behavior under its fixtures; it cannot establish a real hardware safety claim or 30-hour endurance result. Any capability report describes what one boot observed or advertises, not a universal property of the device. Keep each result's limitations and evidence alongside the outcome so a later reader can audit the claim.

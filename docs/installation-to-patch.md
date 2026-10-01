# Deliver Quirkbench’s installation-to-patch user journey

**Contract alignment, 2026-10-01.** This is the implementation map for the
[aspirational manual](../README.md). The first general release must support a new
user from installation through an attended investigation and patch export, without
handwritten configuration, build manifests or knowledge of a previous investigation.
Deliver fresh-user setup first. External coding agents are the primary journey;
managed invocation and unattended target operation are separate optional milestones.

This document records plan sections 1 (goal and starting point) and 2 (product
contract). It also maps the remaining milestones so implementation can proceed in
bounded packets. It does not implement their commands or qualify any artifacts.
[C8](product-interface.md) defines interface semantics,
[C0–C7](implementation-contracts.md) retain safety and execution authority, the
[roadmap](product-roadmap.md) sets delivery order, and the
[handoff](implementation-handoff.md) defines packet scope. This revision supersedes
the 2026-09-29 manual-setup-first delivery order, not its safety requirements.

## Starting point

Reuse the existing controller database, operation records, attempt state machine,
content store, systemd worker ownership and target protocol. There is no second
investigation scheduler or database. Development primitives are not a completed
user journey; existing software tests are not release or hardware qualification.

| Area | Existing foundation and evidence location | Integration still needed |
| --- | --- | --- |
| Installation | [Immutable archive installation](../src/quirkbench/controller_install.py), [setup diagnostics](../src/quirkbench/controller_setup.py), [installation tests](../tests/test_controller_install.py) | Signed release distribution, bundled installer, resumable setup, zero-target service startup, unified status |
| Recovery and targets | [Stock recovery](../src/quirkbench/recovery_stock.py), [private provisioning](../src/quirkbench/provisioning.py), [local console](../src/quirkbench/console.py), [capacity tests](../tests/test_capacity_setup.py) | Verified image download, connected setup screens, C4 pairing and credential lifecycle |
| Execution | [Controller](../src/quirkbench/controller.py), [durable jobs](../src/quirkbench/job_coordinator.py), [operator approval](../src/quirkbench/operator_approval.py), [operation tests](../tests/test_operations.py) | Investigation coordinator that joins preparation, proposals, attempts and evidence with crash-safe dispatch |
| Sources and agents | [Catalog](../src/quirkbench/baseline_catalog.py), [agent prototype](../src/quirkbench/agent.py), [contract fixtures](../tests/test_product_contracts.py) | Distributable pinned inputs, editable kernel workspace, full streaming source capture, installed investigation handoff |
| Evidence and use | [Observations](../tests/test_observations.py), [monitor](../src/quirkbench/monitor.py), [maintenance](../src/quirkbench/maintenance.py) | Scientific comparison/report, patch export, investigation views, safe shutdown and guided backup completeness |

The executable [CLI](../src/quirkbench/cli.py) is the evidence for available command
forms. The separate [product CLI parser](../src/quirkbench/product_cli.py) is an older
P0 specification fixture, not the executable interface. Retain its frozen fixtures;
add a new contract revision in the owning implementation packet. Do not rewrite
old fixtures and call that feature completion.

## Delivery milestones and ownership

M numbers identify user outcomes; P numbers remain bounded implementation packets.
Neither numbering replaces C contract identifiers. Select one packet per task.
The order below is integration/delivery order, not a requirement to wait for each
milestone's physical evidence before implementing later software. Use the
[development scheduling rules](implementation-handoff.md#development-scheduling-and-review)
to distinguish actual code dependencies from release dependencies.

| Milestone | Outcome and dependencies | Owning packets/contracts |
| --- | --- | --- |
| M1 — fresh controller | Install verified software, complete resumable setup and report readiness with zero enrolled targets | P0, P2a–d; C0/C2/C8 |
| M2 — connected target | After M1, obtain recovery, confirm external media, configure networking and pair securely; missing support is actionable | P3a–f, P1a/b; C1/C4/C8 |
| M3 — investigation baseline | After M2, select retained immutable inputs, create an editable kernel workspace and complete an explicitly approved baseline round trip | P1c, P4, source portion of P6a, needed P7a/b; C1/C3/C5/C8 |
| M4 — external-agent loop | After M3, hand off context, accept source/proposals durably and connect repeated approved experiments to evidence and observations | P6a/b, P7a/b; C2/C3/C5/C8 |
| M5 — report and everyday use | After M4, export attributable patches/results, monitor/pause/resume, shut down safely, manage storage and explain backup/restore coverage | P6b, P7 diagnostics/exports, P2c/e; C2/C3/C5/C8 |
| M6 — attended general release | After M1–M5, complete the main manual using shipped artifacts; separately authorize and pass the applicable frozen final release gates | P8; C7/C8 |
| M7 — optional modes | After the attended journey, add explicit managed invocation and separately authorized unattended qualification/grants | P6c and P5 independently; C3/C6/C8 |

M2 is the first usable fresh-user milestone. M3 proves the lab round trip, not that
the reported problem reproduces. M4 must support a non-audio investigation without
audio dependencies. M5 must support an honest inconclusive export as well as a fix.
M6 does not promise other platforms: retain the current Fedora/x86-64/UEFI/direct
USB implementation and qualify only the combinations actually checked. No second
image builder, web application, cloud account or MCP service is required.

## Manual implementation checklist

**Status legend:** partial = reusable implementation exists, but the stated manual
behavior remains incomplete; planned = no complete implementation of that behavior.
All rows remain open. Source links above and existing suites below identify where
to inspect evidence, not assertions that a suite was run in this documentation task.
Close a row only with implementation files, exact focused check/results, remaining
limitations and artifact qualification status. New acceptance scenarios below are
requirements to implement, not existing passing tests.
Record software status and qualification separately within the status cell, for
example `software complete; physical commissioning pending`. That permits dependent
software work while keeping the user-facing claim open. A pending release signature,
image build or physical check does not invalidate a passing software result and does
not authorize those operations automatically.

| Manual command or promise | Status and current evidence | Owner | Acceptance evidence required to close |
| --- | --- | --- | --- |
| Download/verify archive; `./quirkbench/install` | Partial: archive/install modules; `test_controller_archive.py`, `test_controller_install.py` | M1/P2d | Clean unrelated home/user and arbitrary archive location; signatures/compatibility checked; repeat install, differing bytes refusal, active-work refusal, failed activation and rollback; one CLI/service/worker identity |
| `setup`; `status`; service survival and actionable dependencies | Partial: `setup-state`, `setup-check`, configured native service; `test_controller_setup.py`, `test_worker_service.py` | M1/P2d | Resumable steps, no manual JSON or temporary credential home, zero targets allowed, missing native tools distinct from container visibility, logout/restart/pause reported, no implicit firewall/lingering changes |
| `recovery download` | Partial: synthesis and unqualified distribution records; `test_recovery_distribution.py` | M2/P3a3 | Authenticated release metadata and exact compatible image; missing/changed bytes rejected; interrupted download resumes or safely retries; no private factory state |
| Standard image writer; local external-drive confirmation/capacity screen | Partial: commissioning, capacity UI/journal; `test_capacity_setup.py` | M2/P3a2/a5 | Join delivered console flow to exact drive confirmation; restart same geometry, preserve existing filesystems, block insufficient capacity; retain existing implementation |
| Recovery **Network** and **Connect to controller**; `target add NAME` | Partial: nmtui/manual provisioning; no pairing exchange; `test_console.py`, `test_manual_provisioning.py` | M2/P3c–e | C4 fingerprint-before-code, expiry, replay/lost reply, request/key binding, activation interruption and private settings across boots; no fake initial target token |
| `target show NAME`; supported/connected/attended states | Partial: authenticated `target-inventory`, binding/planning; `test_hardware_plan.py`, `test_binding.py` | M2/P1/P3b | Separate readiness facts; stale/missing report, wrong target, unsupported platform and missing peripheral explicit; pairing queues no experiment |
| Reassign media, revoke credentials, repair changed endpoints | Partial: immutable private generations; lifecycle orchestration planned | M2/P3e/f | Paused explicit maintenance, old evidence attribution, both-service revocation, new trust reconfirmation and rollback; no reflash as routine connection repair |
| `investigation start NAME --target TARGET [--problem FILE]`; limits/baseline wizard | Planned facade; campaign and P0 records exist | M3/P1c/P4/P6a | Durable problem/limits/target/catalog identity, one active investigation per target; selected immutable inputs only; unavailable pinned input blocks, never substitutes |
| **Use existing source** and editable kernel workspace | Partial: source input capture and small snapshot prototype; `test_agent.py`, `test_build_pipeline.py` | M3/P6a | Separate workspace preserves original tree, actual Git base plus distro patch provenance, full tracked/allowed-untracked capture including modes/deletions; concurrent mutation rejected; reconstruct exact build inputs |
| Initial baseline preparation and approved round trip | Partial: build/compose/attempt primitives and approval tests | M3/P4/P7a/b | Joined application flow with injected adapters; no handwritten manifests; exact approval/rejection and restart; record round-trip readiness separately from problem reproduction; physical commissioning only when requested |
| `investigation brief NAME` and handoff to any external coding agent | Planned generated handoff; installed guide exists | M4/P6b | Correct workspace/state/schema/guide paths, durable hypotheses and history; a fresh agent can continue without original chat; no automatic agent call |
| Agent `investigation context/recipes/proposal-schema/propose/capture-source`; evidence/operation queries | Partial: versioned fixtures, operation queries, recipe registry and source primitives | M4/P6a/b/P7a | Atomic proposal/usage/outbox, prompt durable acknowledgment, replay and lost reply, source writer handoff, bounded context/cursors, eligible recipes only, no target shell escape |
| `experiment review ID`; `attempt approve ID` | Partial: approval exists with required request ID; `test_operator_approval.py` | M4/P6b | Review exact source/candidate/attempt, procedure and risks; human facade supplies durable retry identity; old explicit interface preserved; changed bytes/new attempt require authorization |
| `investigation respond NAME`; physical observations | Partial: `session` request/response commands; `test_observations.py` | M4/P7b | Interactive selection backed by same typed records; late/conflicting/missing replies, restart, request/attempt attribution; recipe schema extension explicitly versioned |
| Baseline/diagnostic/patched/regression/revert comparisons | Partial: immutable experiment/result/evidence records | M4–5/P6b/P7 | Exact identity joins, exposure counts, missing observations and confounders, non-audio and missing-peripheral cases; no unsupported causal conclusion |
| `monitor NAME`; `investigation status/pause/resume NAME` | Partial: monitor/campaign pause/reconciliation; `test_monitor.py`, `test_controller.py` | M5/P2c/P6b | Same service facts, investigation filtering, actionable waits, restart remains paused; distinguish admission stopped/workers draining/recovery/evidence; agent exit does not cancel work |
| `target poweroff NAME`; offline recovery shutdown screen | Planned coordinated lifecycle | M5/P6b/P3 | Reconcile active writers/attempts, locally durable evidence and ordered shutdown; show upload backlog independently; network silence never proves poweroff or safe eject |
| `investigation report/export NAME --output PATH`; documented bundle layout | Planned report and patch exporter | M5/P7 | `git format-patch` against actual recorded base, clean-tree application, exported source matches tested source or explicitly unvalidated; evidence/symbol retention, missing bytes explicit, secrets excluded, inconclusive export supported |
| `backup --output PATH`; `restore` wizard | Partial: positional backup/restore and retained closures | M5/P2e | Preserve positional API; consistent source checkpoint, offline target uncertainty, separate private identity requirements, omissions explicit, restore paused; export is not backup |
| `storage`; cache/retention guidance | Partial: settings/maintenance/build-cache commands | M5/P2c/e | Shared retention services, required/live/pinned data protected; explain usage and eligible cleanup without manual deletion |
| `agent configure`; `investigation driver NAME --managed` | Partial: prototype CommandAgent; durable scheduling planned | M7/P6c | Concrete supported command adapter, paused writer handoff, durable decisions, bounded output/time, auth/unknown-usage behavior; external remains default |
| `target qualify NAME`; authorize bounded unattended plan | Planned scoped grants/qualification integration | M7/P5 | C6 grant before activation, exact candidate/attempt/target/media/epoch, revoke/replay/deadline handling; independently recorded physical reset coverage; no unseen future-patch authorization |

## Completion and handoff rules

Each implementation packet updates its rows and the handoff with: contract/record
versions, files changed, exact validation command and result, outstanding integration
and qualification. Mark a command usable only when the executable parser and shared
application service implement it; help fixtures alone do not count. Leave unsupported
features unavailable with an explicit error, never a success-shaped placeholder.

Use focused software tests and injected failure scenarios while developing. Join
the actual application services in flow tests rather than merely chaining mocked CLI
outputs. Physical commissioning is an explicit product operation; image/QEMU/endurance
qualification remains the explicitly authorized final major-version gate. Changed
bytes retain their unqualified status until corresponding checks are performed.

Obtain the required higher-reasoning review before enabling new storage, enrollment,
source ownership, durable dispatch, shutdown or watchdog boundaries. This documentation
revision grants no new runtime authority. Preserve old records, schema readers,
low-level commands and historical paths; do not recreate discarded artifacts.

The attended release is complete when a new user can follow the main README from
installation to an evidence-linked patch package or clearly inconclusive report
using released artifacts, without intervention from the original developer. Remove
the main aspirational warning only after that evidence exists; separately label
unavailable optional modes. The next implementation packet is M1a in the
[handoff](implementation-handoff.md#next-packet--m1a-resumable-controller-setup-contract).

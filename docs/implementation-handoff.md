# Implementation handoff

Use the [roadmap](product-roadmap.md) and [contracts](implementation-contracts.md).
This file contains bounded work packets, not a record of past implementation.
Names under tests/ below are acceptance suites to create where absent; their listing
does not mean they already exist or pass. Do not run release qualification to finish
a routine packet. Use injected adapters and focused software tests.

| Packet | Dependencies | Permitted scope and acceptance |
| --- | --- | --- |
| P1a — inventory | C0/C1 | Shared recovery collector and optional standalone wrapper; tests/test_inventory.py: limits, partial results, provenance/privacy, no device opens, network calls or installed-OS mutation. |
| P1b — profiles | P1a | Profile catalog/planner and explicit x86-64 platform adapter; tests/test_hardware_plan.py: multiple vendors/form factors, missing peripherals, unsupported architecture, protection conflicts. No automatic protection relaxation. |
| P2a — durable operation records | C0/C2 | Additive DB migrations, request replay, source/input/result references and versioned local JSON. tests/test_operations.py: changed-request conflicts, pause, complete/partial outputs. No new scheduler database. |
| P2b — worker ownership | P2a | Managed worker, epoch/lease fencing, process-group lifecycle and restart reconciliation. tests/test_worker.py: CLI exit survival, stale completions, crashed owners, exclusive source writers. No lingering/firewall/host package changes without operator setup. |
| P2c — operations monitor | P2a/b | Bounded event cursors, progress/error rendering and output queries; no polling agent or fabricated percentages. Reuse monitoring tests. |
| P3a1 — locked recovery recipe | C1/C4; recovery-base.md | Pin Fedora source/config/RPM closure and recipe/schema; extend build-rootfs.sh and build adapter for generic protected kernel/modules/firmware. Focused recipe/build tests: locked replay, missing input, protection conflict, no host-derived configuration. DNF5/dracut are selected; no builder comparison. |
| P3a2 — recovery runtime | P3a1 | systemd unit allowlist, offline console, RAM paths/resolver, NetworkManager/nmtui, explicit recovery-only SELinux disablement. Focused boot/runtime tests: no blocking global network wait, credential-free factory tree, both cold and restored network setup, no candidate-policy leakage. |
| P3a3 — assembly and release record | P3a1/2 | Feed existing image adapter; versioned recovery manifest, signed checksums, capacity checks, explicit cache/input invalidation. Focused image/publication tests: interrupted output, no incomplete publication, no enrolled state in factory media. No KIWI, Lorax, physical writer or live-image remaster. |
| P3a4 — responsive recovery workloads | P2b, P3a2 | Run slow preparation in a bounded worker while the single control authority services evidence/status. Focused runtime tests: stalled worker, network loss, upload progress, restart reconciliation and no duplicate arming. No competing attempt owners. |
| P3b — identity gate | C4 | Extend current pre-kernel UUID guard and runtime check into versioned target/media binding, duplicate detection and mismatch UI. tests/test_binding.py plus boot/image tests: moved armed drive, missing/default UUID, old unbound provisioning, one-shot clear failure. No identity-as-authentication or unguarded fallback. |
| P3c — network and setup | P3a2/P3b | Local console setup, selected private network generations, RAM activation in both environments, visible offline waits. tests/test_setup.py: reboot/retry, secret exclusion, mismatch before profile replay, unavailable experiment/library. Never depend on network-online.target indefinitely. |
| P3d — enrollment service/client | P2a/b, P3b/c | C4 pinned TLS bootstrap, one-use code/request/key binding, device/repository credentials and revocation. tests/test_enrollment.py: lost response, replay, wrong fingerprint, bad clock, expiry, rate limits and both-service revocation. Higher-reasoning review before enabling. |
| P3e — enrollment activation/retarget | P3d | Crash-safe private generation transaction, runtime activation and explicit retarget/evidence drain. tests/test_provisioning.py: every durable boundary, moved drive, old evidence attribution, no inherited authorizations. No credential-bearing factory seed. |
| P4a — commissioning coordinator | P1–P3 | Existing attempt machinery with assembled runtime and fake privileged adapters. tests/test_commissioning_flow.py: inventory → baseline → upload → recovery; no fabricated registration or direct target shell. |
| P4b — attended target validation | P4a, available target | Operator-requested real device round trip, recorded protection/identity/network/boot evidence. Not an automatic release suite or watchdog qualification claim. |
| P5a — watchdog authorization | C6, P2, P3 | Separate signed grants, policy epochs, revocation, exact attempt/build/media binding. tests/test_watchdog_authorization.py: replay/mismatch/expiry, no inheritance of qualification. |
| P5b — runtime activation | P5a/P4 | Verify grants before activation; preserve sole systemd ownership, offline finish and recovery waiting. Focused runtime/watchdog tests; no controller hardware watchdog access. |
| P5c — physical reset coverage | P5b, available target | Operator-controlled matrix in watchdog-qualification.md. Record actual timeouts/stages and surviving evidence separately. No unsupported coverage claim. |
| P6a — proposals/source freeze | C3/P2 | Streaming source snapshots, concrete coding-agent command adapter, bounded pipes, proposal/usage/outbox transaction. tests/test_proposals.py: interrupted edits, output overflow, unknown usage/auth failure. |
| P6b — session coordinator | P6a/P4 | One owner per campaign/worktree; deterministic build/compose/submit; compact evidence context, budgets, pause/restart. tests/test_sessions.py: crash after every dispatch boundary, no repeated physical attempt or usage. |
| P7 — diagnostics and exports | C5/P6 | One bounded recipe per packet; observed device selection, stimuli hashes, physical checks, matched baseline/patched/revert/regression evidence. No vendor branches in core. |
| P8 — final release gate | Stable implementation, explicit release request | Run applicable retained infrastructure/hardware fixtures with predeclared endurance duration/rationale and fault matrix. No agent waits unless results block work. |

Every packet records contract sections, files changed, focused acceptance command,
remaining limitations and whether image bytes/qualification were invalidated. Mark
unimplemented features and unperformed hardware checks explicitly. Preserve existing
Experiment/Result envelopes, campaign readers, evidence and source checkpoints.

Before handoff, test failure behavior as well as success. Source/DB ownership,
controller credentials, target binding, privileged storage writes and watchdog policy
require higher-reasoning review at their boundaries. Surface conflicting contracts;
do not add a bypass flag, reinterpret a qualification field or silently weaken policy.

# Development milestones

The [product roadmap](product-roadmap.md) is the forward plan; the
[handoff packets](implementation-handoff.md) define implementation-sized tasks and
[contracts](implementation-contracts.md) define their boundaries. The
[implementation progress log](implementation-progress.md) records completed
software packets without treating them as image or hardware qualification.

| Milestone | Current scope and remaining gate |
| --- | --- |
| M1 — durable foundation | Achieved: contracts, controller state machine, simulation, evidence, pause/restart/retry tests and monitoring. Preserve these contracts. |
| M2 — build and boot | Build/composition/image infrastructure exists. Generic recovery compatibility, NetworkManager setup integration and portable-media boot binding need completion and qualification against final image bytes (P1/P3). |
| M3 — physical execution | HTTPS, exact-revision handoff, live evidence and reconciliation exist. Complete enrollment, target binding, durable operations and attended commissioning (P2–P4). |
| M4 — adaptive operation | Watchdog integration and agent adapter primitives exist. Complete scoped activation authorization, actual hardware coverage and durable session orchestration (P5/P6). |
| M5 — investigations | Add bounded diagnostics and evidence-supported patch exports (P7). No fixed vendor, peripheral or list of bugs defines product completion. |

P3a follows the selected [Fedora recovery synthesis pipeline](recovery-base.md);
tool selection is settled. Implement locked inputs, runtime integration, publication
and responsive upload workers as separate bounded packets.

The next usable product path is generic media → recovery setup → secure pairing →
inventory → exact baseline → attended investigation. A standalone collector is
optional. Fixed recovery and evidence remain independent of candidate state.

Milestone completion needs observable behavior, not merely a passing unit suite.
Software tests provide development feedback. Expensive composition/VM/endurance
qualification runs only on an explicitly requested stable major-version candidate,
following [testing policy](testing-policy.md). Keep changes unqualified until matching
release evidence exists; prior results never qualify changed bytes.

Release acceptance verifies controller package/boot preservation, target internal
storage/firmware preservation, complete attributed artifacts, retry convergence,
pause/restart durability, recoverable storage/auth failures and all attempts accounted
for as evidence, explicit failure or uncertainty. Declare the endurance duration and
rationale before running it; evaluate fault coverage and bounded resource use, not
an arbitrary universal number of hours. Hardware reset and surviving diagnostics
are independently observed. Missing hardware is an unmet gate, never a passing skip.

Every task includes human visibility: phase, contact/heartbeat, measurable advance,
waiting reason, deadline and real counters. Long work may be healthy; heartbeats do
not prove progress, and progress does not authorize a lease or another attempt.

The product delivery gate also includes the [C8 interface contract](product-interface.md):
installable releases without a checkout, persistent controller services, supported
baseline selection and the public session facade. Complete an external-agent session
before adding managed scheduling. Foundational recipe/human-observation records come
before that journey; broad diagnostics and unattended qualification follow. Readiness,
safe shutdown and backup completeness must report distinct facts, not a single green
status. See the handoff for packet dependencies; no milestone is advanced by this plan.

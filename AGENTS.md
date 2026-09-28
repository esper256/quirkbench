# Quirkbench agent instructions

Use [the terminology and scope rules](docs/terminology.md): **controller** builds
and runs investigations; **target** boots experiments and produces evidence.
Quirkbench is hardware-generic, not tied to one laptop/vendor. Keep platform quirks
in reviewed profiles/adapters. Preserve public wire names such as `device_id` and
technical names such as the network `--host` option; do not break compatibility
for vocabulary changes. Never equate generic design with universal tested support.

Preserve agent quota. Read [the testing policy](docs/testing-policy.md) before
choosing validation. This policy applies to delegated work as well.

Follow [the authoritative forward plan](docs/product-roadmap.md), briefs P1–P8.
Inventory/provisioning/session commands in that plan are proposed, not implemented.
Use a bounded brief and focused tests; request higher-reasoning review for storage,
watchdog authorization or durable execution boundary changes. No MCP service in v1.

- For ordinary edits, run the smallest relevant software tests. Documentation-only
  changes need link/consistency checks, not pytest or an image rebuild.
- Real composition/image/transfer/backup qualification, QEMU boot cycles and
  hardware endurance are **final major-version release gates**.
  Do not run them after routine edits, at every milestone, or merely because a
  related source file changed. A development task is not release authorization.
  User-requested image production or scientific kernel experiments are product
  operations, not automatic infrastructure tests; do not attach the release suite
  to them. Attended device commissioning validates that specific delivered device.
- The release Make targets require `RELEASE_QUALIFICATION=1`. Set it only for an
  explicitly requested final major-version qualification. Do not bypass this
  policy by invoking the underlying Python scripts or CLI directly.
- Do not wait for long tests unless their results are critical to the next
  development decision. Continue independent work or return control to the user
  with the run's durable status/log location and an honest pending result.
- Never spend an agent turn repeatedly polling logs, sleeping and narrating
  unchanged progress. Do not spawn an agent to watch tests. Prefer a completion
  event; inspect a compact result once it is available. If a critical result
  requires waiting, use bounded event waits and inspect only actionable changes.
- Record commands, source/artifact identities, logs and completion status so work
  can resume without repeating expensive runs. Reuse matching evidence; label
  changed artifacts unqualified until the release gate, rather than implying old
  qualification applies to new bytes.
- After a release-gate failure, use focused reproductions and software regressions
  to develop the fix. Rerun only the affected release checks when the candidate is
  stable; rerun the complete gate only if a dependency invalidates that evidence.

These rules change test scheduling, not acceptance standards. Never hide a failing
gate with a skip or mark an unrun qualification complete.

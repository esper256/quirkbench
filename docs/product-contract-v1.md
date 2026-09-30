# P0 product contract fixture

**2026-09-29 delivery clarification:** existing fixtures remain frozen and readable;
this revision changes delivery dependencies, not their schemas or runtime dispatch.
Only added interfaces needed for the attended journey need initial freezing.
Pairing, managed scheduling, wizards and guided backup completeness are later work;
manual authenticated setup and exact-candidate operator approval remain required.
See [delivery tiers](product-roadmap.md#delivery-contract).

This packet freezes planned argument forms in
[`product_cli.py`](../src/quirkbench/product_cli.py), and four additive documents in
[`product-contracts.v1.schema.json`](../schemas/product-contracts.v1.schema.json).
The examples under `examples/` are accepted fixtures; `test_product_contracts.py`
checks both runtime validation and JSON Schema. These commands are **planned**:
the current executable does not dispatch them. P2/P6/P7 own implementation.

The existing `Experiment` and `Result` envelopes, `--device`, `device_id`,
positional `backup`, and service `--host` network-address option are unchanged.
The future `target qualify TARGET` syntax needs explicit disambiguation from the
current low-level `target --url ...` process before adding it to the executable.

## Authority and ownership decisions

- `SessionIntent` maps one public session to one existing campaign and records the
  driver and execution owner. The application must hold the ownership lock when
  changing that row. A document alone never starts or authorizes an attempt.
- `AgentProposal` carries an immutable source reference and a typed experiment
  intent. A revision digest is accepted only after the approved workspace and
  source-capture machinery verify it. Recipe IDs and parameters are checked against
  an installed, reviewed registry in P7a; this P0 validator only rejects structural
  command/path/authorization fields. It does not grant a new privilege.
- `ObservationRequest` keeps its issued and deadline times. A response is a
  separate attributed document. P7b must join responses by exact request ID,
  enforce idempotency/conflict rules, and retain late answers without extending a
  physical attempt deadline or satisfying another request.
- A target supervisor may report observations but cannot choose a proposal, recipe
  privilege or next attempt. Controller services own the durable operations and
  rootless workers under the user service manager. A CLI process is a client, not
  a replacement lifecycle owner.
- Recovery booted, enrollment, experiment eligibility and unattended qualification
  are separate facts. Pause, draining workers, recovery return, local evidence and
  controller acknowledgement are separate facts. No single status string implies
  safe shutdown or backup completeness.

All four records require `schema_version: 1`, reject unknown fields, and use strict
JSON parsing with a 1 MiB document cap, duplicate-key rejection, finite numbers and
maximum depth 32. The canonical digest covers the document bytes without a
self-digest field. `null` usage means unknown; it is never recorded as zero usage.
This is contract evidence, not a passing implementation of source capture, service
ownership, enrollment, recipe dispatch or observations persistence.

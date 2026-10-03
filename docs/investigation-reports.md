# Investigation reports

`quirkbench investigation report INVESTIGATION` reads committed state without
initializing the controller, scheduling work or granting approval. Human output
and `--json` show the same version 1 facts. Successful recipe execution does not
establish problem reproduction or a patch effect; an inconclusive report is useful.

```sh
quirkbench investigation report input-device-investigation --json
quirkbench investigation report input-device-investigation --comparison comparison.json --json
quirkbench investigation report input-device-investigation --after 12 --limit 2 --json
quirkbench investigation report input-device-investigation --experiment patched-input-check --attempt-after 25 --attempt-limit 1 --json
```

Use `next_cursor` for experiment pages and each item's `next_attempt_cursor` for
that exact experiment's attempt pages. Counts describe **all recorded attempts of
that experiment**, not only the displayed page. A changed retry has its own attempt
identity; jobs/repetitions and attempts are separate counts. Started-attempt counts
record protocol adoption/start, not proven recipe stimulus exposure. Stimulus
exposures and reproduction counts remain unknown unless a future reviewed typed
recipe supplies attributable measurements; arbitrary measurement keys cannot fill
those fields. Evidence availability, terminal outcomes, observations, late answers
and recovery return remain separate. No answer transfers to another attempt.

A retained attended baseline supplies its baseline role. Other comparison roles
require an explicit [comparison declaration](../examples/investigation-comparison.json)
with the exact investigation/experiment IDs. Baseline, diagnostic, patched,
regression and revert are declared groups, not effects inferred from names, passing
results or current target inventory. Missing/mixed source identities remain
unavailable. Recorded metadata comparisons identify differences from a unique
baseline; historical environment/peripheral equivalence remains unknown. No audio
peripheral or recipe is required for a non-audio investigation.

The [versioned schema](../schemas/investigation-report.v1.schema.json) describes
comparison input, report and retention records. Reports verify bounded stored
metadata joins: investigation, immutable source/base capture, build/composition,
actual candidate deployment, installed recipe manifest/version and stimulus identity
(the canonical recipe manifest identity plus parameters). Exact candidate adoption
requires the original attempt's matching handoff revision, start and result. It
still does not establish reproduction. Large source archives, symbols and evidence
objects are listed by essential retained identity and safe file presence; additional
RPM/dependency references contribute to `required_object_count` with
`required_objects_truncated`, without invalidating the exact metadata joins; their bytes are
**not verified** by a report. Missing, expired or corrupt bytes are never invented.
No private controller/target credentials, raw measurement dictionaries or current
mutable inventory are exported through this allowlisted view.

Each page holds one read-only database snapshot. Separate invocations may observe
newer committed facts. The export service must consume these same report facts
under one shared snapshot, preserving comparison declarations and missingness;
report is not a second truth or a controller backup.

## Preserve evidence

Read-only reports never insert pins. To preserve the currently recorded owners:

```sh
quirkbench investigation report-retain input-device-investigation --note 'Keep this inconclusive comparison' --request-id retain-comparison-01 --json
quirkbench maintenance status
```

This explicit command atomically uses existing retention pins for the investigation,
experiments and attempts, preserving their dependency references to source, symbols
and evidence. It reports retired/unknown owners and missing object identities;
inserting a pin cannot restore missing bytes or validate them. The request ID freezes the selected owners and receipt; retry returns that exact
receipt, including its original missing-object observations; it is not fresh
byte verification. New attempts added later require a new request ID. Human calls derive a
stable name/note identity; machine input requires `--request-id`. Limits are 100 experiments, 1,000 attempts
and 16,384 direct object references; oversized scope is refused before new pins
commit. Existing [storage maintenance](local-state-maintenance.md) owns pin removal and
collection. Pin only the evidence you intend to keep.

Native/physical qualification remains a separate attended operator gate. A report
cannot authorize a boot, shutdown, publication, release or unattended operation.

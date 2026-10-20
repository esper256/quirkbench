> **Legacy reference — not current requirements or agent instructions.**
> This document was archived on 2026-10-07. Consult [current documentation](../README.md).
> Commands and implementation claims below may be obsolete.

# Using Quirkbench with a coding agent

Quirkbench owns reproducible experiments and evidence. The agent investigates the
problem, edits source and interprets results. Neither a proposal nor a successful
build authorizes target execution. Native commissioning remains separate from
software fixture coverage; the README describes the intended complete product.

## Configure the lab

Run `quirkbench setup --help` for explicit controller choices. Commands never prompt.
Use `quirkbench doctor` to diagnose requirements and `quirkbench status` to inspect
readiness. Setup configures the controller; start it in its own terminal with
`quirkbench admin controller run`. See [installation](controller-installation.md)
for signed installation, signing prerequisites and foreground worker requirements.

Download compatible recovery media with `quirkbench recovery download`, inspect it
with `quirkbench recovery list`, and follow the [recovery guide](recovery-operations.md)
to prepare external media. `quirkbench target pair target-01` creates an invitation;
compare the full controller fingerprint on the target before completing enrollment.
Pairing does not mean the target has connected or may execute a candidate.

## Start and prepare an investigation

```sh
quirkbench investigation start first-fix --target target-01 --problem problem.md
quirkbench investigation source prepare first-fix --request-id source-001
quirkbench investigation resume first-fix
quirkbench investigation status first-fix
quirkbench monitor first-fix
quirkbench investigation brief first-fix
```

Start selects an explicit supported baseline, or fails when a choice is ambiguous.
Source preparation uses that exact distribution and builder. Wait for completed
source preparation before submitting a baseline or editing the workspace. An
investigation starts paused; resuming it permits preparation, not target execution.
`brief` supplies the workspace, current context, installed resources and copyable
commands. Edit only the reported workspace when its writer state permits editing.
The currently supported distribution builder requires distribution-prepared source;
manual Git source preparation does not imply another working platform adapter.

## Submit and follow a test

Create a file using the installed
[submission schema](../../schemas/experiment-submission.v1.schema.json). For example:

```json
{
  "schema_version": 1,
  "hypothesis": "The changed driver completes the same bounded system observation.",
  "source": {"mode": "workspace", "quiesced": true},
  "recipe": {
    "id": "system-observation",
    "parameters": {},
    "repetitions": 1,
    "timeout_seconds": 120
  }
}
```

Stop all writers before acknowledging `quiesced`. For an unmodified baseline test,
replace `source` with `{"mode":"baseline"}`; it uses retained pristine bytes without
resetting edited source. Choose an eligible reviewed recipe with
`quirkbench investigation recipe list first-fix`. Supply `repository` when more than
one repository is configured. Recipe choices, source, baseline, builder and signing
identity are retained with the request; retries do not adopt changed defaults.

```sh
quirkbench experiment submit first-fix --file experiment.json --request-id test-001
quirkbench experiment status first-fix --request-id test-001
quirkbench experiment logs first-fix --request-id test-001
quirkbench experiment list first-fix
```

Submission returns after saving the request. The existing controller captures
source, prepares the candidate filesystem, builds, assembles and signs the system,
and records the immutable experiment. No experiment ID is reported before that
proof exists. Follow the request ID during preparation. Editing may resume only when
status reports it; later edits cannot alter the captured test.

Logs default to the active or failed stage. Use `--stage build` or `--stage system`
to select another stage. When multiple logs exist, choose a returned `--selector`;
`--offset/--length` page through its bytes. `--after/--limit` page recorded events.
Missing or retired output is not reconstructed or inferred.

After a controller restart, inspect status, reconcile stopped workers and explicitly
resume the investigation before continuing its interrupted submission:

```sh
quirkbench investigation resume first-fix
quirkbench experiment resume first-fix --request-id test-001 --resume-request-id resume-001
```

Repeat the same request IDs to recover lost replies. Different submission input with
the same ID conflicts. Terminal failure requires a corrected new submission. Neither
resume command approves a target run or bypasses changed publication identity.

## Approve a run and inspect evidence

```sh
quirkbench experiment show EXPERIMENT_ID
quirkbench run list first-fix
quirkbench run show RUN_ID
quirkbench run approve RUN_ID
quirkbench investigation observation list first-fix
quirkbench investigation observation answer first-fix --request QUESTION_ID --file observation.json --request-id answer-001
quirkbench investigation evidence list first-fix
quirkbench investigation evidence read DIGEST first-fix --offset 0 --length 4096
quirkbench investigation results show first-fix
quirkbench investigation results export first-fix --output results.tar
```

Approval applies to one exact prepared run. Repetitions need their own approval.
A completed run is not proof that the problem reproduced or that a patch fixed it.
Keep execution results, human observations, recovery arrival and durable evidence
separate. Before exporting dirty changes as a patch, stop writers and run:

```sh
quirkbench investigation source capture first-fix --quiesced --request-id export-source
```

Wait for that capture in investigation status. The exporter verifies whether these
bytes match a tested experiment; it never labels later edits as tested.
Export includes attributable patches and results; supplying `--author`
may be necessary to represent dirty captured changes. See [export](investigation-export.md).

## Deliberate manual work and administration

Investigation `source`, `baseline`, `build` and `proposal` actions expose individual
stages when needed. Prefer `experiment submit` for routine testing. Proposals also
retain requests for observations and conclusions; submission creates its own exact
experiment proposal, so do not submit the same test twice.

Use `admin operation` only for troubleshooting internal work. It is unnecessary for
the normal human or agent journey. `admin storage`, `admin settings`, `admin backup`
and `admin restore` manage retained local data. `dev recovery` produces unsigned
recovery artifacts without requiring controller setup; `dev install` is explicitly
for unsigned development archives. These operations do not qualify a release or
activate a live installation implicitly.

## Machine calls

`--json` works before or after the command path. Commands use the one locally
configured controller; no public `--state` override exists. Human and machine
calls use the same validation and authority. Retain returned request IDs and cursors;
never infer success from missing errors, recent contact or a completed build. Help,
version, malformed inputs and removed commands do not create controller state.

Structured responses have schema_version, ok, data, error and operation_id fields.
Read product references from data; internal operation IDs are diagnostic. Errors
include a code, message and retryability. Monitor JSON is one snapshot per call.
The --version --json command returns the offline source identity.

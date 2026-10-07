> **Legacy reference — not current requirements or agent instructions.**
> This document was archived on 2026-10-07. Consult [current documentation](../README.md).
> Commands and implementation claims below may be obsolete.

# Make Quirkbench’s CLI clear, consistent and task-oriented

Status: owner-approved product plan; implementation is not claimed by this document.
Prepared 2026-10-05 against checkout `67cb55b`. Reconcile current main before coding.
This is the complete replacement for the earlier CLI redesign proposals.

This document defines the destination, implementation decisions and acceptance
criteria. GitHub issues and PRs remain the execution/status ledger; do not add a
parallel checklist of completion claims here. Follow [CONTRIBUTING](root-CONTRIBUTING.md)
and [the testing policy](testing-policy.md).

## 1. Product decisions

Quirkbench’s CLI should describe investigating a Linux problem and testing a patch,
not the controller’s internal record types.

- One CLI for humans and agents, calling the same application services.
- Explicit investigation names and exact experiment/run identifiers. No implicit
  current investigation, working-directory inference or global selected context.
- No interactive prompts or wizards, including when stdin is a terminal. Missing
  or ambiguous inputs produce an actionable error and an example.
- One resumable experiment submission coordinates preparation, stopping before
  target execution for exact-run approval.
- Everyday work is separate from administration and developer tools.
- Superseded commands disappear immediately at interface cutover. No aliases,
  deprecation period or compatibility-only public-format readers are required.
- Reuse the existing controller, SQLite database, operation/attempt state machines,
  supervisor and application services. No new scheduler, daemon, database or plugin
  framework.
- Preserve authentication, source and evidence attribution, worker coordination,
  storage protection and exact-run approval. Single-user scope and the approved
  file-access policy remain in force.

The owner's explicit approval supersedes repository instructions that would require
keeping old CLI spellings or unused public JSON formats. Update those instructions
as part of implementation; do not ask for the same compatibility decision again.
This is not permission to rewrite historical evidence, reinterpret stored hashes,
delete existing user state or weaken internal protocol validation.

### No everyday `job` or `operation` command

Users follow the investigation or experiment they requested. Preparation status,
logs, failures and safe continuation must be accessible through those objects.
Simply hiding the old operation command is insufficient.

Keep low-level execution records under `admin operation` for troubleshooting. Both
humans and agents must complete the ordinary journey without it. Internal operation
IDs may appear in diagnostic details, but must not be the normal next step.

## 2. Approved root help

```text
Quirkbench — investigate Linux hardware problems and test patches.

Usage: quirkbench [OPTIONS] COMMAND ...

Getting started
  setup           Configure this controller
  doctor          Check requirements and explain problems
  status          Show controller readiness and work needing attention
  recovery        Download and inspect target recovery images
  target          Pair and manage target computers

Investigating a problem
  investigation   Manage a problem, its source workspace and findings
  experiment      Prepare tests and follow their progress and results
  run             Review and approve individual target runs
  monitor         Watch investigation progress

Specialist tools
  admin           Manage installation, connections, storage and backups
  dev             Build recovery images and maintain releases

Options
  --json          Return structured output
  --version       Show the running version and source
  -h, --help      Show help

Examples
  quirkbench setup --help
  quirkbench investigation start first-fix \
    --target target-01 --problem problem.md
  quirkbench investigation brief first-fix
  quirkbench monitor first-fix

Use 'quirkbench COMMAND --help' for actions and examples.
Commands never prompt; provide required choices as arguments or input files.
```

Root help lists families, not every specialist action. Family help lists every
action, with routine actions before specialist ones. Do not produce argparse’s
enormous inline enumeration in the root usage line.

| Term | Meaning |
| --- | --- |
| Investigation | One problem, its target, workspace, reasoning and evidence |
| Experiment | A test question, exact source and reviewed test procedure |
| Run | One execution of an experiment, with its own approval and results |
| Recovery image | The bootable fallback environment for a target |

Descriptions lead with purpose. Replace “admit,” “closure,” “controller cut,” and
“private CAS” with concrete actions/results. Explain storage deletion, unsigned
inputs, approval and shutdown consequences in the relevant action help. Do not
bury important consequences, but do not repeat every internal invariant everywhere.
Every action needs a description, argument explanations and a representative example.
Help and version reporting work offline without configured state or image tools.

## 3. Canonical command families

### Investigations

```text
investigation start / list / show / status / pause / resume
investigation brief / context / history
investigation source prepare / show / capture / release
investigation baseline prepare / show
investigation build prepare / kernel / system
investigation proposal schema / add / list / submit
investigation recipe list
investigation observation list / show / answer
investigation evidence list / read
investigation results show / retain / export
```

- `show` describes the investigation; `status` describes activity and blockers.
- `brief` is the agent handoff: workspace, relevant context and copyable commands.
- Source/baseline preparation appears in investigation status and monitor.
- Source and build actions expose individual stages for diagnosis and deliberate
  manual work. Their help points to `experiment submit` as the normal path.
- `build prepare/kernel/system` means candidate filesystem preparation, captured
  kernel compilation, and assembly/signing of the experimental system respectively.
- Experimental builds remain investigation-scoped. Do not create disconnected
  `dev build` or `dev candidate` workflows.
- `proposal` retains recommendations, including requests for human observations and
  conclusions. Experiment submission handles its necessary proposal admission and
  dispatch internally; users do not manually submit the same test twice.
- `results export` produces patches, report and evidence. It must not translate
  successful execution into proof of reproduction or a successful fix.

Keep names/IDs explicit. List commands use bounded pagination. Do not make a plain
listing verify every retained artifact or recursively traverse all investigations.

### Submit and follow experiments

```sh
quirkbench experiment submit first-fix \
  --file experiment.json --request-id test-001
quirkbench experiment status first-fix --request-id test-001
quirkbench experiment logs first-fix --request-id test-001
quirkbench monitor first-fix
```

The input records the hypothesis, reviewed recipe and its parameters, run limits,
and source selection. Baseline submissions explicitly select the recorded unmodified
baseline. Editable-source submissions explicitly acknowledge that writers stopped.
The first implementation step below freezes the exact schema and normalized defaults
once; later CLI implementers must use that contract rather than design another one.

Submission coordinates validation, exact capture, missing candidate inputs, build,
system assembly and experiment admission through the existing controller. It returns
promptly after durable admission; it does not run a pipeline in the CLI process.

```text
Experiment preparation accepted for first-fix.
Request: test-001
Stage: Capturing source

Status: quirkbench experiment status first-fix --request-id test-001
Logs:   quirkbench experiment logs first-fix --request-id test-001
Watch:  quirkbench monitor first-fix

A target run will require approval after preparation completes.
```

Use the request ID while preparation is underway. Return the immutable experiment ID
only when its required inputs are established. A submission is not a runnable
experiment; status must not invent an experiment record or imply readiness early.

`experiment list` is investigation-scoped and distinguishes preparing submissions
from their resulting experiments. `experiment show EXPERIMENT_ID` reviews the exact
prepared test. If one submission produces an experiment, show them together rather
than as two apparently unrelated tests.

Status includes stage, measured progress when available, last activity, blockers,
capture/writer status, and the next action. No invented percentage across unrelated
stages. Follow linked operations automatically; do not require manual ID translation.

Logs return relevant public preparation/build output. Choose the active stage, or
the failed stage when blocked; allow explicit `--stage`. If that stage has multiple
outputs, list safe log selectors and require a selector rather than guessing. Expose
bounded reads and machine pagination, not an arbitrary artifact-digest reader. Keep
existing output ownership checks, byte budgets and safe terminal rendering.

### Resume interrupted preparation

```sh
quirkbench experiment resume first-fix \
  --request-id test-001 --resume-request-id resume-001
```

Resume continues that submission through existing owner/reconciliation checks. It
does not create another experiment, resume the whole investigation, approve a run,
or restart terminal failed work. Explain when a new submission is required.
An identical resume request replays its result; conflicting input is rejected.

Investigation pause/resume and submission continuation remain distinct. No preparation
command silently resumes a paused investigation. If resume cannot proceed while
paused, return that fact and the explicit investigation command.

### Review, approve and collect results

```sh
quirkbench experiment show EXPERIMENT_ID
quirkbench run show RUN_ID
quirkbench run approve RUN_ID
quirkbench investigation observation answer first-fix \
  --request REQUEST_ID --file observation.json
quirkbench investigation results show first-fix
quirkbench investigation results export first-fix --output results.tar
```

`run` provides list, show, approve, reject and resolve. Approval applies to one exact
prepared execution. Resolving an interruption does not approve another execution.
Status and monitor keep preparation, approval, execution, recovery arrival and
received evidence distinct. Unobserved outcomes remain unknown.

### Targets

```text
target pair / list / show / inventory
target pairing cancel
target access revoke
target reassign
target evidence approve / revoke
target shutdown request / status / cancel
```

Pairing and reassignment report the invitation they create; they do not claim the
target has already connected or changed identity. Preserve fingerprint verification,
authenticated enrollment, evidence-upload authorization and shutdown reconciliation.
This plan removes controller CLI prompts, not necessary physical target confirmation.

## 4. Administration, specialist tools and removal map

| Current entry point/capability | Destination or disposition |
| --- | --- |
| `setup`, `setup-state` | `setup`; state selection is a configuration choice |
| `setup-check`, `doctor` | `doctor` with controller/build/optional VM checks |
| `version` | `--version`; `--version --json` reports structured runtime identity |
| `attempt`, `resolve` | `run` actions |
| `session` observation actions | `investigation observation` |
| `evidence` | `investigation evidence` |
| `operation` | `admin operation list/show/events/output/resume` |
| `target-inventory` | `target inventory` |
| `controller-run` | `admin controller run` |
| `endpoint` | `admin connection`; replace wizard with complete explicit inputs |
| `publication` | `admin repository` |
| `release-install` | `admin install` for signed controller installation |
| `controller-install` | `dev install`, explicitly unsigned development installation |
| `recovery`, `recovery-images` | `recovery download/list/show` |
| `recovery-bundle` | `dev recovery plan/prepare/verify/export/import/build` |
| `recovery-inputs` | `dev recovery inputs` for necessary specialist actions |
| `recovery-image` | `dev recovery submit` for controller-managed production |
| `recovery-image-build` | `dev recovery build-recipe`; use the portable bundle `dev recovery build` path normally |
| `recovery-image-cleanup` | `dev recovery cleanup` |
| `build`, `candidate-rootfs`, `compose` | Investigation-scoped build stages only |
| `image`, `qualify-image` | `dev image assemble/qualify` |
| `release-check` | `dev release check` |
| `storage`, `maintenance`, `build-cache` | `admin storage` |
| `settings`, `backup`, `restore` | `admin settings/backup/restore` |
| `library-maintenance` | `admin recovery maintenance` |
| `monitor`, redundant `watch` | Keep `monitor`; remove public `watch` |
| Development `monitor --run RUN_ID` | `dev monitor RUN_ID`; reuse the existing read-only development view |
| `campaign`, `register`, `artifact`, `snapshot`, `agent-step`, `demo` | Remove public prototypes; retain useful services/fixtures |
| Raw `serve`, `serve-repository`, flags-only target client, `target-service` | Remove user CLI facades; preserve actual packaged process entry points |

`admin operation` is troubleshooting, not the default next step for ordinary work.
Controller-wide and developer commands can reference it for detailed diagnosis.
Do not add a public `job` family as another spelling.

Recovery production stays usable without controller setup. Foreground build and
controller-managed submit have different owners: their shared grouping must not
pretend otherwise. Retain a necessary explicit recipe-based diagnostic build as a
specialist `dev recovery` action if it has unique supported inputs; do not duplicate
the assembler or silently translate an unsigned output into a signed release.

Storage help separates inspection, pruning, pinning and interrupted-work abandonment.
Prune must retain the existing eligibility rules; admission to a user directory does
not authorize recursive cleanup. Restore still targets separate state and leaves
scheduling paused. Retention must not silently pin every experiment forever.

Remove `--start-service`: it configures rather than starts the controller. Retain
explicit configuration inputs; `admin controller run` starts the existing foreground
lifecycle. Do not add daemon packaging or require host systemd for this work.

Removed commands fail before opening state, starting workers or downloading inputs.
A concise replacement suggestion is allowed; forwarding execution is not. Test both
the obvious prototypes and previously undocumented parser actions.

## 5. Implementation guidance and fixed tradeoffs

### Read these seams, not the whole repository

These references describe the inspected checkout, not guaranteed future filenames:

| Concern | Existing implementation to reuse |
| --- | --- |
| Parser, dispatch, readiness and maintenance routing | `src/quirkbench/cli.py` |
| Source/lifecycle facade | `src/quirkbench/investigation_sources.py` |
| Source handoff and capture ownership | `src/quirkbench/source_workspace.py`, `src/quirkbench/source_capture.py` |
| Source/candidate/build/composition joins | `src/quirkbench/investigation_pipeline.py` |
| Proposal scope, admission and existing durable progression | `src/quirkbench/external_proposals.py`, `src/quirkbench/proposal_dispatch.py` |
| Baseline admission and prepared experiment proofs | `src/quirkbench/attended_baseline.py` |
| Investigation progress | `src/quirkbench/investigation_monitor.py`, `src/quirkbench/investigation_context.py` |
| Bounded record/output reads | `src/quirkbench/state_reader.py` |
| Owner startup, restart fencing and operations | `src/quirkbench/controller.py`, `src/quirkbench/controller_service.py` |
| Controller-free recovery workflow | `src/quirkbench/recovery_bundle_cli.py` |

#### CLI organization

Use small family parser builders and handlers. Attach handlers/typed action identities
to parsed commands; call application functions directly. Do not implement new command
names by rewriting argv into old command strings and recursively calling `main`.
Temporary private adapters into existing function arguments are acceptable while
refactoring, but old spellings must not remain an executable public command tree.

Keep one lightweight definition of help/routing metadata; no generic plugin framework
or exhaustive declarative CLI language. Move validation out of a root parser where
it belongs to a specific action. Inspect root command-name checks carefully: names
currently select readiness, read-only treatment and maintenance locking, so renaming
without transferring those policies is unsafe.

Support `--json` before or after the command path without child-parser
defaults overwriting already supplied values. Put resource/repository overrides on
the relevant actions or configured settings, not the short root help. Use `--target`
for public target selection; retain network `--host` where it really means an address.

#### Noninteractive behavior and output

Human and JSON calls validate the same normalized request. Rendering must not decide
what can execute. An approval command with an exact run ID is itself an explicit
action; do not add a confirmation wizard or a broad `--yes` authority bypass.

Reuse the existing response envelope where sensible, with command-specific data,
structured errors and public next steps. Preserve established exit categories rather
than inventing different exit codes in each family. Extend parse-error rendering so
`--json` failures are machine-readable too; stdout must contain no diagnostic prose.
Human errors go to stderr. Long-running `monitor` is an explicit observer, not part
of submission; JSON monitor output must have a documented streaming format.

#### Identity and automatic choices

Resolve an omitted choice only from an unambiguous recorded investigation choice.
Freeze the selected baseline, repository/signing identity, recipe, limits and source
selection in the accepted intent. Never pick an arbitrary first repository, silently
substitute newer packages, or resolve a retry against newly changed defaults.
Unknown required choices are errors, even for humans.

The existing proposal API requires an already completed source capture. Consequently,
the new pre-capture submission cannot just invoke that API early or fill its capture
fields with placeholders. It needs a durable pre-capture intent tied to the existing
owner and eventual proposal/experiment. This is the first high-reasoning task below.

For a dirty workspace, acknowledgement of stopped writers must be coupled to the
existing handoff/ownership mechanism. An acknowledgement made at submission cannot
be treated as proof that arbitrary live files are unchanged hours later. Do not
automatically release source ownership until the retained capture is safe to use.
Baseline mode must obtain the unmodified recorded baseline without resetting or
overwriting an edited investigation workspace.

#### Retry and restart rules

Persist intent before admitting children. Link child admission and parent state
atomically where existing service transaction hooks permit. Reuse a deterministic
child request identity for each stage; after a crash, find/replay that child rather
than creating another one. A cached human message is not the durable result.

All query views must be reconstructible from retained state after the CLI exits and
the controller restarts. No process-local progress map, second JSON status journal
or filesystem directory scan as an alternative authority. Readiness queries must
not drive reconciliation or advance a stage.

Resume first applies the existing ownership/reconciliation checks. Distinguish an
interrupted stage that can continue from a terminal failed stage requiring a new
submission. Do not retry compilation indefinitely or reset terminal states merely
to make the friendly command succeed. Binding/trust changes remain blockers.

#### Scope of format changes

Public `run` terminology can map to stored attempt IDs; do not rename database tables
for cosmetic consistency. Change public JSON fields when it removes real ambiguity,
not through a repository-wide replacement of `device`, `attempt` or `operation`.
Existing target protocols and authenticated serialized content remain intact unless
a separately reviewed functional need requires a change.

Adding narrowly scoped records to the existing SQLite database for submission/link
identity is allowed. It is not permission for a second database or separate scheduler.
Preserve old evidence bytes and readers needed by retained state. No migration of
unused public CLI formats is required.

## 6. Recommended first step: higher-effort submission foundation

**Do this before broad parser edits.** Use higher reasoning for one bounded PR that
implements and tests the durable submission foundation with injected workers. Do
not build images, run a target, or redesign the whole product in this step.

Deliver these concrete artifacts in that PR:

1. A submission input schema and normalized service request covering baseline versus
   editable source, hypothesis, recipe/limits, acknowledgement and selected defaults.
   Include one neutral worked example and a schema-validation regression.
2. Durable pre-capture intent and links to existing preparation/proposal operations
   under the current controller owner. Record exact transaction boundaries, child
   request identities, required retained references and restart handling in the
   relevant interface/implementation contract. Reuse current operation states; a
   user-facing stage is a projection, not a competing execution state machine.
3. A narrow shared service interface for submit, status, logs and resume. A useful
   implementation home is `experiment_submissions.py`; split query/rendering code
   only when it actually makes review easier. CLI modules must not duplicate this
   logic. Typed return values must expose the public request reference before an
   immutable experiment exists.
4. Software tests of admission, writer handoff, child linkage and crash/replay using
   existing fixtures. At least one representative preparation path must exercise
   the service through a completed child, rather than just testing a new schema.
5. Required independent higher-reasoning review of the exact foundation diff. Record
   reviewer, scope, findings and resolution in the PR. No simulated review approval.

The first implementer must choose and freeze the exact minimal record layout after
checking the current owner/transaction hooks. That decision belongs to this bounded
high-reasoning step; subsequent lower-cost implementers must not each invent a parent
record, retry rule or source-lock strategy. The approved constraints above are fixed.
If this requires a new execution owner or weaker source/approval protections, stop
and present the concrete gap rather than implementing a workaround.

This changes delivery order, not product scope: establish the risky service contract
first, then perform the largely mechanical command cutover. Keep the old public CLI
until the replacement has its required progress coverage; that temporary development
state is not a compatibility-alias promise in the delivered interface.

## 7. Remaining implementation sequence

After the foundation is reviewed and merged with session authorization, implement
the following bounded work against the established services. Reconcile task claims
and shared files before editing. Do not start a dependent task against an unmerged
assumed contract. Without merge authorization, leave the PR reviewable and report the
dependency rather than silently merging it.

### A. Complete unified preparation and user-level progress

Connect source/candidate preparation and existing proposal dispatch through prepared
experiment admission. Add submission status/log/resume projections and investigation
status/monitor integration. Preserve explicit pause, exact-run approval, evidence
access and source-release rules. Required boundary review applies to changed owner
and continuation behavior, not every wording edit.

### B. Cut over the public command tree

Implement the approved root/family help and all mappings. Deliver useful progress
access in the same cutover; never remove the old everyday operation command while
users still need it to follow normal submissions. Remove old spellings, prompts and
misleading options. Route process launchers through retained proper entry points.
Use focused command-family tests; do not make every parser test construct a complete
controller and target fixture.

### C. Align instructions and verify the journey

Update generated brief/next-step commands, installed agent instructions, active guides,
examples, schemas and focused CI mappings. Align the README's aspirational command
examples without removing its warning about unproven end-to-end/native behavior.
Remove obsolete compatibility-only tests and examples; retain their meaningful
behavioral assertions under the replacement interface.

Update AGENTS.md, C8 and terminology rules to state the approved CLI break explicitly.
Search for old commands in launchers, Python argv arrays, shell scripts, target
packaging, docs and tests; classify remaining references as internal names or actual
historical provenance rather than imposing a blanket word ban.

A full-chain software fixture must call the public interface from setup through
export and after interruption. It must not call `admin operation` to bridge an
unfinished product interface. Fixture success is not native qualification.

## 8. Focused acceptance and efficient validation

| Area | Required evidence |
| --- | --- |
| Discovery | Compact root help; every retained family/action documented; no old aliases |
| No side effects | Help/version, malformed input and removed commands leave state absent and launch nothing |
| Automation | Missing inputs never prompt; human/JSON enforce the same rules; parse failures are structured in JSON mode |
| Entry points | Checkout, manual symlink and packaged launcher expose the same commands |
| Identity | Same request/content replays; changed content conflicts; chosen defaults stay frozen; no early experiment ID |
| Source | Missing acknowledgement rejected; changed source/ownership blocked; later edits cannot change captured inputs; baseline does not overwrite edits |
| Crash points | Before/after parent admission, child admission/linkage, child publication and experiment publication do not duplicate work |
| Pause/resume | Paused investigation stays paused; repeated resume is idempotent; unresolved owner/trust and terminal failures are not bypassed |
| Progress | State/logs remain attributable across stages/restarts; multiple submissions stay distinct; no invented progress |
| Logs | Stage selection/pagination work; inaccessible, missing or retired outputs remain explicit; no arbitrary private output access |
| Approval | Preparation never authorizes a boot; one exact run approved; retries/reconciliation do not grant another run |
| Lifecycle | Renamed internal invocations work through start/interruption/restart; low-level repair remains available |
| Recovery | Standalone portable image preparation needs no controller; existing production/qualification gates remain |
| Product journey | Non-audio setup-to-export fixture, normal and interrupted, needs neither `job` nor `admin operation` |
| Documentation | Returned commands are parseable; guides/examples agree; links and diff checks pass |

Use the existing testing policy: smoke as needed and the focused cases invalidated
by the change. Do not run the full suite after every rename or repeatedly rerun slow
fixtures. Parser/help tests should be cheap; reserve full-chain software fixtures
for service integration and final interface acceptance. Reuse prior valid evidence.

Use real service/SQLite behavior for identity and restart tests with injected native
workers; mocking the entire service does not prove durable correctness. Inject faults
at transaction boundaries instead of sleeping or running real compiles. Record exact
commands, tested source and required review; leave missing native evidence explicit.

No image builds, QEMU campaigns, physical execution, production publication or release
qualification are authorized by this redesign. Do not change the live installation
automatically. A later explicit activation must use existing stopped-work checks and
must update all runtime references coherently.

## 9. Handoff and stop rules

Lower-cost implementers should treat the command structure, terminology, noninteractive
behavior, absence of public aliases and no-`job` decision as settled. Make ordinary
local implementation choices without asking the owner about spelling, module splits
or fixture arrangement already covered here.

Proceed through missing helpers, routine bugs and focused test failures. Do not turn
every stage into another design review. Request the required review when changes
actually affect source/worker ownership, durable execution, trust, storage, shutdown
or approval; pure help/rendering changes do not automatically need that review.

Pause the affected work for a new scheduler/database/service, incompatible unplanned
stored semantics, weakened safeguards, an unavailable required review, or an actual
conflict with the reviewed foundation. Record the evidence and smallest correction
in the issue. Do not invent production credentials or treat software fixtures as
permission to boot hardware.

Completion means a new human or agent can discover and follow the supported journey,
including interruption and log inspection, without learning internal operation IDs
or choosing among obsolete interfaces. Report unfinished acceptance honestly rather
than presenting parser renaming alone as completion of this plan.

Owner follow-up #157 removes public state overrides: use the one controller selection
in local configuration; offline restore accepts an artifact `--output` destination.

# Installed attended investigation

Use this guide with an installed controller, its executable help and the returned
operation/experiment/attempt identities. Every command below is available; uppercase
names are placeholders to replace with actual IDs, paths and operator choices.
Select the same controller state with `--state /absolute/controller-state` before
the command, or use the state selected during setup. Keep state, output and editable
workspaces outside Git checkouts. No command assumes an audio problem or target model.

The software journey uses the existing controller services. Running it on a real
host requires [controller prerequisites](controller-installation.md#choosing-the-controller-host),
independently provisioned publisher trust, compatible signed recovery/builder assets,
a complete pinned baseline and an explicitly supplied supported target. A cloud
coding container is a development environment. This guide grants no physical
execution, credential provisioning, image production or release authorization.

## Install, set up and pair

Follow [authenticated acquisition](controller-installation.md#signed-release-acquisition-software-foundation)
and the [publisher runbook](release-publication.md) for independent trust and precise
asset availability. The development archive installer remains explicitly unsigned
and unqualified; it does not become a signed release through setup or pairing.
An acquired controller is not automatically activated. From its installed runtime,
with the native prerequisites and operator choices available:

```sh
quirkbench --help
quirkbench setup --request-id INITIAL_SETUP --runtime /absolute/installed-runtime --start-service --json
quirkbench status --json
```

Initial setup creates the private state/TLS identity and starts the existing native
user service with zero enrolled targets. Choose the bind address/logout/storage
policy explicitly when defaults do not fit; use `setup --help`. Keep the request ID
and exact choices for continuation after a lost reply. Setup does not install host
packages, enable lingering, change firewall/power settings or authorize a boot.

Follow [first repository setup](controller-installation.md#initial-native-user-service-setup)
with an already provisioned operator signing key; stop/start the existing user
service as that guide directs. Repository publication and enrollment availability
are separate from execution readiness. Then:

```sh
quirkbench recovery download --request-id RECOVERY_DOWNLOAD --json
quirkbench operation status RECOVERY_OPERATION --json
quirkbench target add TARGET_NAME --request-id INVITATION --json
quirkbench target show TARGET_NAME --json
```

Use the downloaded exact compatible recovery with a standard writer, explicitly
confirm the external device/capacity, and complete the recovery console's network
and fingerprint comparison before entering the private invitation code. Follow
[connected-target setup](controller-installation.md#connected-target-software-journey).
The console preserves the original pending enrollment key/request after a lost
accepted response. Successful pairing assigns the target identity; it grants no
experiment approval. Wait for an actual recovery report; use the returned `device_id`:

```sh
quirkbench target-inventory TARGET_ID --json
quirkbench investigation start INVESTIGATION --target TARGET_ID --problem /absolute/problem.txt --workspace WORKSPACE --request-id START --json
quirkbench investigation baseline INVESTIGATION --json
```

Missing inventory, ambiguous baselines or unavailable pinned bytes remain blockers.
Prepare the authenticated builder through `setup --builder-archive` as described in
[controller installation](controller-installation.md). Do not substitute an arbitrary
OCI tag or omit missing packages. A running service is not builder readiness.

## Prepare and attend a baseline

```sh
quirkbench investigation prepare-distribution INVESTIGATION --request-id PREPARE_SOURCE --json
quirkbench investigation resume INVESTIGATION
quirkbench operation status PREPARATION_OPERATION --json
quirkbench investigation source INVESTIGATION --json
quirkbench investigation capture-source INVESTIGATION --quiesced --request-id BASELINE_CAPTURE --json
quirkbench operation status CAPTURE_OPERATION --json
quirkbench investigation prepare-candidate INVESTIGATION --request-id CANDIDATE --json
quirkbench operation status CANDIDATE_OPERATION --json
quirkbench investigation build INVESTIGATION --capture CAPTURE_OPERATION --candidate CANDIDATE_OPERATION --request-id BUILD --json
quirkbench operation status BUILD_OPERATION --json
quirkbench investigation compose INVESTIGATION --build BUILD_OPERATION --repository REPOSITORY_ALIAS --request-id COMPOSE --json
quirkbench operation status COMPOSE_OPERATION --json
quirkbench investigation submit-baseline INVESTIGATION --compose COMPOSE_OPERATION --request-id BASELINE --json
quirkbench experiment review BASELINE_EXPERIMENT --json
quirkbench investigation resume INVESTIGATION
quirkbench attempt show BASELINE_ATTEMPT --json
quirkbench attempt approve BASELINE_ATTEMPT --request-id BASELINE_APPROVAL --json
quirkbench attempt show BASELINE_ATTEMPT --json
```

Each accepted operation runs through the existing service; proceed only after its
required predecessor is `SUCCEEDED` with stopped worker ownership. Yield while work
runs, using persisted operation output/events or a manually opened monitor. No
handwritten build manifest is required. Stop every source writer before declaring
`--quiesced`; pause does not stop an external editor. Preserve the unmodified captured
baseline. Review exact source, deployment, recipe, binding and risks before approving
the claimed attempt. `attempt reject` denies a boot. Approval while paused does not
make handoff effective; a new attempt always needs its own exact approval.

Candidate request, candidate adoption, recipe result, recovery arrival and durable
evidence are separate facts. A PASS observation is not problem reproduction. Missing
recovery/evidence or an uncertain attempt needs explicit reconciliation; neither
contact loss nor a watchdog grants another attempt. Retry accepted commands with
identical arguments and request IDs; conflicting replay fails. Failed work needs a
new decision/request; interrupted work uses the existing `operation resume` flow.

## External proposal and original human observations

A fresh external agent starts from the installed brief, not a previous chat:

```sh
quirkbench investigation brief INVESTIGATION --json
quirkbench investigation context INVESTIGATION --json
quirkbench investigation recipes INVESTIGATION --json
quirkbench investigation proposal-schema INVESTIGATION --json
quirkbench investigation release-source INVESTIGATION --json
```

Edit only the assigned private workspace with one writer. Stop the writer, then
capture the new bytes. After the capture reports stopped `SUCCEEDED`, refresh
context/schema and create `proposal.json` from the installed v2 example and that
new immutable context/scope receipt; select the actual captured Git base and reviewed
recipes. A receipt read before editing cannot admit the new capture. The [agent guide](agent-guide.md#submit-an-external-proposal)
describes the exact fields and source-free human/conclusion decisions.

```sh
quirkbench investigation capture-source INVESTIGATION --quiesced --request-id PATCH_CAPTURE --json
quirkbench operation status PATCH_CAPTURE_OPERATION --json
quirkbench investigation context INVESTIGATION --json
quirkbench investigation proposal-schema INVESTIGATION --json
quirkbench investigation propose INVESTIGATION --file /absolute/proposal.json --request-id PROPOSAL --json
quirkbench investigation dispatch-proposal INVESTIGATION --proposal PROPOSAL_OPERATION --candidate CANDIDATE_OPERATION --repository REPOSITORY_ALIAS --request-id DISPATCH --json
quirkbench investigation proposals INVESTIGATION --json
quirkbench operation status PROPOSAL_OPERATION --json
quirkbench experiment review PATCHED_EXPERIMENT --json
quirkbench attempt show PATCHED_ATTEMPT --json
quirkbench attempt approve PATCHED_ATTEMPT --request-id PATCHED_APPROVAL --json
quirkbench attempt show PATCHED_ATTEMPT --json
quirkbench investigation observations INVESTIGATION --json
quirkbench session request INVESTIGATION --campaign INVESTIGATION --file /absolute/question.json --json
quirkbench investigation observation INVESTIGATION --request QUESTION_ID --json
quirkbench investigation respond INVESTIGATION --request QUESTION_ID --file /absolute/answer.json --request-id ANSWER --json
```

Dispatch builds/composes/submits one experiment through existing workers; acceptance
is not completion or approval. A paused investigation needs explicit resume before
the next stage. Preserve prior capture attribution when making a further edit.
If no relevant question exists, the external operator/agent prepares `question.json`
using the installed observation-request schema in `product-contracts.v1.schema.json`
and explicitly records it with `session request` above. Use the exact session and
original attempt ID, unique request ID, recipe step/kind, prompt and UTC issue/deadline
timestamps. This records a question; it does not invoke a managed agent or start a
recipe, extend a physical deadline or ask the controller to invent an observation.
An answer addresses that existing typed question and its original attempt, using the
installed observation-response schema. Late/uncertain answers remain explicit; they
do not extend deadlines or authorize another boot. Managed invocation and unattended
modes are deferred; closing the external agent does not cancel submitted work or
promise automatic reinvocation.

## Compare, retain, export and stop

Create a version 1 comparison declaration from the installed example, substituting
the actual investigation and experiment IDs for baseline/diagnostic/patched/regression/
revert roles. Observe all report cursors when inspecting larger histories.

```sh
quirkbench investigation report INVESTIGATION --comparison /absolute/comparison.json --json
quirkbench investigation report-retain INVESTIGATION --note 'Keep this comparison' --request-id RETAIN --json
quirkbench investigation export INVESTIGATION --comparison /absolute/comparison.json --output /absolute/new-public.tar --author 'Actual Author <actual@example.org>' --json
quirkbench investigation pause INVESTIGATION --json
quirkbench backup --output /absolute/new-backup
quirkbench --state /absolute/new-restored-state restore --input /absolute/new-backup
quirkbench target poweroff TARGET_ID --request-id SHUTDOWN --json
quirkbench target poweroff-status TARGET_ID --json
```

Retain before expiry; pins cannot recover missing bytes. Follow [report semantics](investigation-reports.md)
and [export reconstruction](investigation-export.md). `tested-source-match` proves
attributed captured bytes were adopted by a terminal attempt; it does not establish
reproduction, stimulus exposure, a causal fix or native qualification. Missing
evidence remains missing, even if source reconstruction succeeds. The export
representation author is explicit; no historical author or signoff is invented.
Exports use a new public filename and never publish partial output as complete.
Review public source/log content before sharing. Export is not a resumable backup.

[Backup coverage](backup-and-restore.md) describes the SQLite/public-artifact/native
commit cut. Private identities/settings/editable Git and target-only backlog are
excluded or unknown. Interrupted backups without a final manifest are incomplete.
Restore verifies the cut and stays paused; restore private identity separately and
reconcile outstanding execution before explicitly resuming. Old workspace inode
records do not authorize a newly restored writer.

Controller shutdown stops admission and waits for unresolved workers/attempts; the
activated recovery runtime retains the exact intent, seals evidence and acknowledges
local preparation before requesting native poweroff. Recovery also supports its
[local shutdown flow](controller-installation.md). Prepared/requested poweroff and
safe eject are separate; contact loss never proves physical shutdown or safe removal.

## Software evidence and remaining gates

`tests/test_installed_journey.py` installs a complete development archive into a fresh
home, then runs outside Git with `python -I` and an unusable ambient `PYTHONPATH`.
Every application import and fixture resource resolves into the installed runtime.
The installed target `runtime.main` reads the activated provisioning/binding, takes
configuration ownership, constructs its actual agent and resolves installed recipes
through the same candidate/recovery and shutdown journey. One controller database
and one actual enrolled identity join the services above;
installed launchers also query the resulting state and export it. Tiny native input
fixtures, the initial enrollment transport, systemd/cgroup/RPM/compiler/OCI/OSTree/GPG/TLS-command
kernel-log command output and boot/poweroff adapters are injected. Attempts and shutdown use real authenticated
loopback HTTPS with the enrolled credential. They create no real image, kernel campaign,
QEMU run, production identity or physical target action. Application admissions,
records, approvals, source capture, report/export and backup verification are real.

The interrupted case covers lost enrollment response with preserved keys, exact
build/approval replay, queued pause, rejected and paused handoff followed by a new
separately approved attempt, uncertain original
human interpretation, missing attributed evidence, interrupted export/backup and
paused restore. The comparison stays inconclusive. The default smoke suite remains
quick; this joined test belongs to focused product checks and the existing full
software matrix at larger milestones.

Production publisher trust/signing/delivery and native pinned-input availability,
real controller service/worker containment, manual recovery commissioning, target
binding/boot/recovery/evidence, native backup and safe shutdown require separately
authorized operator evidence in [#43](https://github.com/esper256/quirkbench/issues/43).
General-release image/endurance qualification remains [#44](https://github.com/esper256/quirkbench/issues/44).
The README remains aspirational until shipped-artifact acceptance supports its claims.

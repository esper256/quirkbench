# Focused software CI and retained evidence

The [testing policy](testing-policy.md) owns validation tiers and release boundaries.
The [contribution workflow](../CONTRIBUTING.md) explains review and issue closure.
No workflow here grants physical, storage, worker or unattended execution authority.

## Selecting and reproducing suites

[ci/suites.json](../ci/suites.json) is the small, reviewable path-to-suite manifest.
`python -m ci.select --base origin/main` compares HEAD against its actual merge base.
Actions compares the PR head with the event's actual base SHA, including both names
of a rename and deleted paths. The tested checkout is GitHub's PR merge commit;
metadata records that SHA separately from the PR head/base. No topic push duplicates
PR tests. Superseded PR runs are cancelled, and focused jobs have 15-minute limits
with a 12-minute inner timeout to leave time for artifact upload.

```sh
python -m ci.select --paths src/quirkbench/distribution_source_worker.py
python -m ci.select --paths tests/test_retarget_evidence.py
python -m ci.select --suites 'source-workers evidence'
make test-suite SUITE=source-workers EVIDENCE=/tmp/qb-source-attempt-1
make test-suite SUITE=endpoint-control EVIDENCE=/tmp/qb-endpoint-attempt-1
```

Each EVIDENCE directory must be new and outside the checkout. Use `PYTHON=...` to
select an alternate virtualenv. For exact focused CI reproduction use Python 3.13
and `umask 077`; local metadata records the actual interpreter and mask. Automatic
smoke and manually dispatched full software checks retain Python 3.11/022 and
Python 3.13/077. Focused checks initially use only 3.13/077 to control cost.

| Suite | Deliberately bounded coverage |
| --- | --- |
| `source-workers` | Distribution import, worker and preparation; original source operations; claims/workers |
| `endpoint-control` | Probe, preflight, history, generation, activation and controller endpoint |
| `endpoint-retarget` | Joined endpoint retarget and activation |
| `evidence` | Archived validation, retarget evidence, drain client/target and release HTTP |
| `filesystem` | Storage admission, runtime destinations, revocation, read-only setup/SQLite lifetime and recovery listing |
| `ci-tooling` | Selection, redaction/bounds, disposable pytest outcomes and artifact plumbing |
| `recovery-integration` | Joined portable boot, failure-handler, storage and packaging regressions |
| `recovery-native` | Pinned Fedora generator/unit verification and packaged failure entry point; isolated userspace only, 60-second deadline |

The native suite requires a separately prepared dependency cache; see
[fast recovery integration](testing-policy.md#fast-recovery-integration-no-boot-or-image-build).
CI acquires/caches it before the timed test step; ordinary Python suites remain portable.

Selectors deduplicate suites/files; separate suite jobs keep their own evidence.
Shared contracts, schemas and fixtures deliberately select all named focused suites.
Unmapped non-documentation changes also select all focused suites **and report the
unmapped paths**. This is a coverage aid, not complete dependency analysis. Review
warnings and add affected file/node checks where needed. Documentation-only changes
select no focused jobs; selection summaries list unselected suites accurately.
Smoke remains separate. No selection automatically triggers full software or release
campaigns. Full software still requires explicit milestone dispatch.

Request additional suites with Actions → **focused software tests** → **Run workflow**,
choosing the desired ref and space/comma-separated manifest names. For example:

```sh
gh workflow run focused-tests.yml --ref feature/my-change -f suites='source-workers evidence'
```

Names are validated against the manifest, passed as arguments and never evaluated
as shell code. A manual ref run is distinct evidence from a PR merge run. Review
`metadata.json` and its exact command, not just the badge. Broad integration may
justify a full milestone; the fallback does not silently provide one.

## Bundle contract and retrieval

All three software workflows use the same [composite action](../.github/actions/software-tests/action.yml).
Initialization precedes Python/dependency setup, so setup failures normally leave
an `incomplete` metadata record. `always()` summary/upload steps attempt to retain
partial evidence without replacing the original failing step's status. Each retry
uses a new run/attempt name; local reuse is rejected.

Artifacts are named `software-<tested-SHA>-<run-id>-<attempt>-<suite>-<python>-<umask>`,
retained **14 days**, and linked from the job summary. Download through the run's
Artifacts section or:

```sh
gh run download RUN_ID --name EXACT_ARTIFACT_NAME --dir /tmp/qb-downloaded-attempt-1
```

Retain needed evidence elsewhere before expiry, according to the project's data
policy; expired/deleted artifacts cannot be recovered through the run page.

- `metadata.json` (256 KiB limit): [versioned v1 schema](../ci/metadata.schema.json), tested and PR
  source identities, dirty flag, workflow/run/job/attempt, exact argv/selection,
  interpreter/build/platform, dependency versions, umask, timestamps, elapsed time,
  outcome and exit code. This is the shared identity/outcome format for future
  performance reporting, not a second performance-specific collector.
- `tests.jsonl`: bounded per-phase test outcomes and durations; failed phases also
  include short tracebacks and stdout/stderr. These distinguish setup, call and
  teardown failures. Truncation is explicit. The `ci-tooling` bundle also retains
  `diagnostic_probe` records with the disposable inner worker causes/statuses;
  expected failing probes pass the parent check only when their failure is verified.
- `console.log`: at most 1 MiB of redacted combined stdout/stderr, including pytest's
  delayed traceback where emitted. `results.xml`: redacted JUnit up to 2 MiB;
  oversized or absent JUnit is recorded instead of silently treated as success.
- `workers/*.json`: at most 128 records / 2 MiB, capturing only string `state` and
  `error` from allowlisted `diagnostics/stage-result.json` (64 KiB input limit).
  Worker result trees, inputs, controller databases, private directories, arbitrary
  logs and credential/key files are never copied. Missing/partial/linked/oversize
  diagnostics get an explicit status record. The overall bundle stays below 8 MiB
  for the supported fixed-size reports and bounded metadata.

The opt-in `ci.diagnostics` plugin scans only a failing test's disposable `tmp_path`,
with a 2,000-directory budget, before fixture teardown. It does not inspect home
or arbitrary controller state. Fixtures that remove their own stages sooner (or
use `TemporaryDirectory`) must capture before that cleanup:

```python
@pytest.fixture
def disposable_worker(tmp_path, retain_diagnostics):
    stage = tmp_path / 'stage'
    try:
        yield stage
    finally:
        retain_diagnostics(stage)  # before deleting stage
        shutil.rmtree(stage)
```

The fixture is available when running through `ci.run` (or loading
`-p ci.diagnostics` with `QUIRKBENCH_CI_BUNDLE` pointing at a private output directory).
No bespoke upload code belongs in tests. Workers already removed before capture
cannot be reconstructed. Empty worker evidence means no available/registered stage,
not proof of worker success. Unknown data fields are discarded. Output redaction
covers private-key PEM blocks, common labeled secrets/auth headers, credential URLs
and GitHub tokens. It is defense in depth, not a guarantee that arbitrary unlabeled
secrets can be recognized: run only disposable software fixtures, never live private
state or credential-bearing commands through this collector. No environment dump
is recorded, and normal unprivileged `pull_request` jobs receive no project secrets.

## Failures and cost

The runner returns pytest's failure/collection/setup status; timeout returns 124,
with available evidence retained. Missing diagnostics never makes a failed run
pass. Concise failure output appears in the job log. Do not overwrite evidence or
rerun to seek green; diagnose the first failing attempt with focused checks.
Crashes may omit JUnit; cancellation, SIGKILL and hard runner/job termination can
prevent even `always()` steps. Missing upload links and incomplete metadata remain
visible where finalization runs; no artifact is promised after abrupt termination.

Use per-phase durations and run elapsed time to measure suite cost before expanding
this matrix. Runner setup/install/upload adds time beyond the recorded pytest
process. Representative implementation measurements are recorded with the PR's
source identity and evidence; they are observations, not portable deadlines or
full-software/hardware qualification.

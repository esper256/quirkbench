# Testing and agent quota

## Portable software development

Editing, software tests, the simulated demo and unsigned controller packaging can
run on a Linux development host or cloud container. They do not require Bazzite,
Fedora on the host, Distrobox, a systemd user manager, Podman, OSTree or access to a
target computer. Use Python 3.11+, Git and the test dependencies below. Some native
tool checks also use OpenSSL/GnuPG; unavailable tools must be reported as such.
The test extra includes setuptools and wheel for archive packaging; pip's isolated
build-system dependencies alone do not install these into a development virtualenv.

```sh
make bootstrap
# Or choose an interpreter and environment explicitly:
python3 environments/bootstrap.py --python python3.13 --venv .venv
```

Bootstrap installs the checked-in `development/constraints.txt` stack over the
existing `.[test]` extra and ends with smoke. It never starts a controller or
runs the full suite. Missing Git/Make or distro venv/ensurepip support produces
an actionable failure; install those prerequisites through your package manager.
An existing incompatible or incomplete environment is preserved: choose a new
`--venv` destination and remove the old one yourself when no longer needed.

Each attempt retains capped phase logs, exit statuses and a report in a new
evidence directory; its path is printed even on failure. With `--report`, that
directory is created beside the report so CI uploads failed setup evidence too.
The latest successful nonsecret `<venv>/bootstrap-report.json` records the source, interpreter/build,
dependency versions, native-tool availability and elapsed time. Download caching is
keyed by interpreter/platform, project metadata and constraints; virtualenvs are
never moved between interpreters. Use `--cache-dir /writable/cache` on restricted
hosts and `--report /existing/directory/report.json` to retain another copy.
Optional OpenSSL/GnuPG coverage is reported separately from smoke readiness.

`--check-archive` exercises unsigned controller packaging in ordinary temporary
storage with the selected virtualenv. `--diagnose-runtime` explicitly runs the
bounded stdlib/isolated-pytest diagnostic once and retains its logs and exit statuses;
a passing probe does not resolve a historically intermittent runtime fault (#8).
Neither option runs images or hardware. CI calls this same bootstrap with
`--skip-smoke` before its separately selected existing test suite.

For a deliberate dependency update, create a fresh virtualenv, install the current
`.[test]` ranges, record all resolved development dependencies in
`development/constraints.txt`, then run fresh bootstrap, smoke and archive checks on
the supported CI interpreters. Review the dependency diff and retained evidence in
a PR. Constraints define the tested development stack; the package's Python 3.11+
compatibility and public dependency ranges remain unchanged.

Keep temporary test state and build staging outside the checkout. Pytest's normal
temporary directory is suitable, including a sandbox parent with an empty `.git`
guard. Real repositories, linked worktrees and ambiguous Git metadata remain
excluded. For a custom location, create a private directory and pass
`--basetemp=/absolute/private/directory/run` to pytest; pytest deletes that run
directory, so never point it at existing state. Tests should work with either
`umask 022` or `umask 077`; do not change a cloud host's permissions to appease a
fixture. CI exercises both settings.
Native signing fixtures allocate short private homes separately from pytest's
temporary root because GnuPG agent sockets have a pathname limit. They use only
disposable test keys and stop their own agent before removing the home.

This development environment is separate from the machine running the controller.
The controller runs in the foreground with bounded container workers. A host service
manager is not required. Actual engine/cgroup capabilities determine which native
workloads are available; software tests need no running controller.
Missing runtime capabilities do not prevent software development and must not be
reported as ready. See [controller installation](controller-installation.md).

## Focused cloud checks: archived evidence

Controller-prepared media regressions are selected by the existing
`recovery-integration` and `recovery-native` gates. Native cache preparation includes
hash-pinned mtools, e2fsprogs and gdisk (including their conversion/runtime libraries);
test execution never downloads dependencies. `integration/test_preparation_native.py`
exercises FAT completion and copied-filesystem/GPT adapters on disposable regular
components, without assembling an image or accessing a device. It fails when the
verified cache or Bubblewrap prerequisites are unavailable. Prepare a fresh cache
when its package inventory changes; retain older evidence/cache identities.

[Issue #7](https://github.com/esper256/quirkbench/issues/7) reported an endpoint
archive case taking 219.26 seconds across export, drain and replay, each with its
own unchanged absolute deadline. At checkout `83a364f`, individual `_source` reads
called `full`, whose `_capture_source` read callbacks called `basic`; each `basic`
rebuilt history/completion and private bundles. The correction keeps live binding,
ownership, deadlines and exact retained bytes, moves semantic reconstruction to
full boundaries and pins each reader's identity to the immutable archive. No global
cache, timestamp trust, longer deadline or short-state-path requirement is added.
Higher-reasoning source review approved; runtime validation is **deferred to cloud**.

The focused regression creates both shallow and eight-level configured paths,
executes the original joined endpoint export/drain/replay case, prints separate
phase counts and asserts no recursive capture inside individual source reads or
history/completion reconstruction inside capture callbacks. Other cases retain
late private-byte/link mutations, exact attribution, later current
endpoint history, scoped upload authorization and original-deadline checks.

Run from the checkout with the test extra installed. Every `--basetemp` below is a
new disposable child; do not substitute existing state. The deep fixture supplies
path depth independently of the temporary parent. This is focused software work:

```sh
TASK_TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/quirkbench-archived-evidence.XXXXXXXX")"
(umask 022; .venv/bin/python -m pytest -q -s \
  tests/test_archived_evidence_validation.py tests/test_retarget_evidence.py \
  tests/test_retarget_endpoint.py::test_new_target_endpoint_history_can_coexist_with_immutable_original_archive \
  tests/test_retarget_endpoint.py::test_repeated_retarget_after_completed_retarget_endpoint_keeps_both_origins \
  tests/test_retarget_endpoint.py::test_owned_context_never_runs_native_callback_after_final_private_source_fence \
  tests/test_retarget_endpoint.py::test_archived_identity_and_snapshot_are_rechecked_after_capture_callbacks \
  tests/test_evidence_drain_client.py tests/test_release_http.py \
  --basetemp="$TASK_TEST_ROOT/evidence" >"$TASK_TEST_ROOT/evidence.log" 2>&1)
```

Earlier work for [issue #19](https://github.com/esper256/quirkbench/issues/19)
caches bounded, immutable ancestor/`.git` path layouts, while repeating the remaining
filesystem, ownership and checkout checks. Blanket mode checks described in the
historical measurements were subsequently removed under the file-access policy. Record reads use a fresh strict
canonical-root traversal without constructing another resolved `Path`; symlink
loops fail with `ContractError`, and missing/inaccessible roots still fail closed.
Drain verification removes one adjacent full reconstruction because each private
source reader already checks before access and after capture. Request, journal,
per-file mutation and deadline guards remain active.

For the two-record joined endpoint regression on Debian 13/Python 3.13, the
profile changed from **106,561,944 to 64,409,757 calls**, with full archive
validations reduced from **189 to 147**. A same-session unprofiled comparison
measured test-call time at **17.11 versus 11.73 seconds**. The regression retains
its behavior checks and originally budgeted at most 150 reconstructions; the previous code
fails that budget at 189. These measurements describe one case, not a new full
suite result. Entry rejection
and post-capture mutation tests verify that no client exchange or archived
journal write follows a failed reader boundary.

The subsequent #19 correction reconstructs archived proof at each owned operation's
entry and exit. Between those boundaries it rereads the exact immutable bytes and
the managed-directory, presence, reader-specific ownership/single-link and bounded
namespace observations made by the existing semantic readers. This includes the
transitive endpoint and retarget enrollment origins, not just the selected archive.
Callbacks run before the pure dependency fence. The old journal remains freshly
validated with only ACK/offset progress permitted; the immutable snapshot and new
journal retain their stable ownership/single-link checks. Selected sealed blobs,
scoped upload credentials, deadlines and source-identity checks remain live.

Paused new-key preparation likewise reuses unchanged proof inside its ownership.
Activation can legitimately publish records and relocate the original namespaces;
a changed observation triggers the existing full phase reconstruction and must
still produce the exact initially prepared source. Final reconstruction remains
mandatory. No dependency survives its operation, no reads are served from a cache,
and timestamps never establish validity. Ordinary file permissions remain governed
by the existing file-access policy.

On Python 3.13.5, the two-record endpoint export/drain/replay profile at `b4d248f`
compared with the unchanged production baseline at `562b3e4` measured:

| Work count | Before | After |
| --- | ---: | ---: |
| Full archived reconstructions | 147 | 6 |
| All original-source captures, including preparation | 217 | 25 |
| Endpoint selected-file reconstructions | 788 | 73 |
| Bounded file reads | 85,234 | 32,944 |
| Filesystem stat/lstat calls | 2,149,611 | 829,244 |
| Profiled function calls | 64,871,349 | 28,822,374 |

The unprofiled joined test took **17.57 versus 7.62 seconds** in the same cloud
environment (test call **16.12 versus 6.09 seconds**). These are examples, not
portable timing thresholds; the executable regression limits work counts.

The joined regression budgets at most six archive reconstructions and twenty
paused-source reconstructions (seventeen measured, versus sixty-eight previously).
The latter allows legitimate activation phases rather than fixing their exact
count. Shallow/deep cases retain behavior assertions and a per-operation budget.
Fault tests require source/namespace/availability changes after client creation to
prevent uploads and journal writes, and activation-stage changes to prevent further
key/runtime publication. Profiling adds overhead; these counts and one journey's
timing do not establish full-suite or native readiness.

## Validation scope

Routine development uses a tiny smoke suite as needed, plus focused software
regressions for changed code. Full software CI runs only at larger integration
milestone boundaries, not for every small bugfix or change. Expensive end-to-end qualification runs only
for an explicitly requested final release of a major version, against a stable
candidate. Milestone acceptance descriptions specify the eventual evidence needed;
they do not instruct agents to rebuild and qualify after each implementation task.

| Work | Validation |
| --- | --- |
| Documentation or policy | Inspect links, consistency and `git diff --check` |
| Development sanity check | `make smoke` (also the default `make test`) |
| Local implementation change | Smoke as needed, plus relevant pytest files or test cases |
| Larger software integration milestone | `make test-full` or manually dispatch the full software CI matrix |
| Final major-version candidate | Explicit release qualification, with recorded inputs and retained results |

### Smoke, focused and full software checks

See [CI suites and evidence](ci-evidence.md) for automatic subsystem selections,
local reproduction, bounded diagnostics, and artifact retrieval/expiry.

```sh
make smoke                                  # routine sanity check
make test                                   # same smoke selection
make test TESTS=tests/test_monitor.py         # affected feature
make test TESTS=tests/test_cli.py::test_invalid_command_fails_without_system_changes
make test-full                              # explicit milestone, all software tests
```

The smoke list lives in `SMOKE_TESTS` in the root Makefile. It reuses existing
contract/schema, artifact publication/upload, state-path, controller lifecycle
and setup tests, plus a simulated CLI demo and independent monitor. Setup checks foreground ownership and publishes configuration without starting a daemon. Explicit file/node paths
avoid importing every integration module as `pytest -m smoke` would. All smoke
cases remain part of the full suite. No tests or assertions are removed or skipped
by this split. Smoke checks basic functionality; they do not replace regressions
for the code being changed.

The initial selection ran **79 tests in 0.87 seconds** on Debian 13/Python 3.13;
the previous full cloud suite took **18 minutes 19 seconds**. These are measured
examples, not deadlines on arbitrary machines. Keep smoke around one second on
that baseline (a few seconds on slower hosts); additions need representative
coverage and measured cost. Keep long reconstruction, subprocess timeout and
network integration scenarios in focused/full runs rather than growing smoke.
The #19 measurements concern the affected application journey; full-suite runtime
must be assessed separately at an integration milestone.

PR updates and pushes to `main` automatically run smoke on Python 3.11/umask 022
and Python 3.13/umask 077. Topic-branch pushes do not duplicate PR jobs; superseded
smoke runs are cancelled. Runner startup and dependency installation add CI time
beyond pytest's elapsed time.

At a milestone, use Actions **full software tests** → **Run workflow**, select
the branch/tag to validate, and enter the milestone description. Alternatively:

```sh
gh workflow run full-tests.yml --ref main -f milestone='Integration milestone description'
```

Both interpreter/umask combinations run the entire `tests/` suite, retain JUnit
results and print the tested commit and slowest 25 cases. Record the run URL and
commit when reporting milestone acceptance; ordinary smoke CI is not full-suite
evidence. A new milestone run is appropriate when integration changes invalidate
prior evidence. Do not launch it merely because a PR or small fix was merged.
There is no automatic full run on pushes, PRs or a schedule.

`make acceptance-m1` remains a full software gate, and release aggregates retain
that full prerequisite. Neither `make test-full`, `make acceptance-m1` nor any
CI workflow invokes real image, VM, composition or hardware campaigns. Keep real
qualification fixtures outside pytest's normal `tests/` collection and out of CI.

Release targets (`acceptance-m2`, `acceptance-v1-image`, `acceptance-qemu`,
`acceptance-standard-image` and `acceptance-ostree-*`) require
`RELEASE_QUALIFICATION=1`, in addition to their documented inputs. For example:

```sh
make acceptance-v1-image RELEASE_QUALIFICATION=1 IMAGE=... OVMF_CODE=... OVMF_VARS=... WORK_DIR=...
```

The opt-in is a safeguard, not permission to run these automatically. Invoking
qualification scripts or using build/image commands to perform qualification
directly follows the same release-only policy. Hardware endurance is also release-only; the inexpensive commands that
validate an existing hardware or patch report do not themselves run a campaign.

Distinguish infrastructure qualification from using the product. An explicitly
requested recovery image or scientific kernel experiment necessarily builds its
artifacts; that is not a request to run the release qualification suite. Likewise,
the attended device commissioning step in [the product plan](product-roadmap.md)
checks that delivered device and baseline. Do not attach full VM/repository/endurance
tests to each profile edit, image generation or experimental kernel. This exception
does not authorize heavy tests during ordinary repository development.

## Long-running work

When an authorized long run is needed, capture its exact command, source/artifact
identities, process or container identity, log location and eventual exit status
in a durable run directory. Use an execution mechanism that survives the agent
turn if the run will continue after returning control. Do not leave a process
attached to a tool session that will terminate it when the turn ends.

If the result does not block development, continue independent work or finish the
turn with a concise pending-status handoff. Do not create a polling automation or
delegate a monitoring agent merely to keep waiting. Do not promise a notification
without an actual completion mechanism. A later status request should read the
saved result, not rerun the test.

Wait only when the result determines the next necessary development step and
there is no independent useful work. Prefer completion notifications over polling;
read compact status or failure excerpts, not repeated full logs. Continuous
progress remains available to humans through existing logs and monitoring without
requiring agent commentary to relay every phase.

At release time, freeze implementation inputs before launching the expensive
gates. Preserve successful evidence and failed-run diagnostics. Diagnose with
focused checks, then repeat only invalidated gates on a stable candidate. Clearly
distinguish historical qualification, current software tests and pending release
qualification. Quota savings never justify silently reusing mismatched evidence.

## Assert requirements, not incidental implementation details

Use the revised [file-access policy](implementation-contracts.md#file-access-and-permission-policy)
when assessing permissions. Existing mode guards and the historical #49 audit are
not independent evidence of a requirement. Remove tests that require blanket
privacy or exact modes for ordinary data when the corresponding behavior is
simplified in #66. Keep focused coverage for ordinary usable permissions (including
0644 files/0755 children under a private parent), actual secret defaults/access,
functional executable bits, meaningful source changes and unintended writes.
Do not make an exhaustive mode matrix or new permission-testing framework.
Permission-specific negative tests must identify the real access or functional
violation; unrelated storage tests must exercise the intended guard. Validate
affected paths under 022 and 077 without changing the host's global umask.

For each new assertion, identify the behavior or contract it protects and a real
defect it should reject. Preserve privacy, exact attribution/identity, mutation
rejection, ownership and ordering where those implement a documented boundary.
Make fault preconditions explicit: prove a permission change actually changes the
relevant bit, and snapshot preserved values instead of assuming an ambient umask.
Use complete native API results when overriding individual metadata fields; compare
unordered directory membership as such. Read-only SQLite allows its exact WAL/SHM
bookkeeping while forbidding application/schema/authority changes. Prefer stable
error categories and actionable fragments to freezing entire diagnostic prose.
See the [first assertion audit](test-assertion-audit.md) for reviewed examples and
retained safety coverage; boundary-sensitive test changes still require AGENTS.md's
higher-reasoning review.

## Fast recovery integration (no boot or image build)

`recovery-integration` joins the existing portable boot, watchdog, storage,
packaging and staging regressions. `recovery-native` checks the actual pinned
Fedora generator and systemd unit verifier in a device-free Bubblewrap sandbox.
It consumes current GRUB arguments, service masks, the installed generator wrapper
and the packaged failure-handler command. It never invokes PID 1, dracut, a disk
image builder, mount services, QEMU, flashing or compilation.

Keep downloads/extraction separate from test execution. On Linux x86-64 with
Bubblewrap, user namespaces and `rpm2archive` available:

```sh
# One-time dependency preparation. Uses the existing candidate's exact RPM hashes.
PYTHONPATH=src .venv/bin/python -m ci.native_recovery prepare \
  --cache /path/to/native-recovery-cache
# Optional: add --rpms /path/to/already-acquired/rpms to avoid downloads.
export QB_NATIVE_RECOVERY_CACHE=/path/to/native-recovery-cache
make test-recovery-native
```

The fixture contains only userspace dependencies from the supported Fedora
implementation; it is not a recovery rootfs or another image builder. RPM scriptlets
are never run. Koji acquisition uses exact versions and verifies the repository's
reviewed package hashes. No publisher trust is invented. Cache contents and executable
bits are rechecked before use; a changed cache requires preparation at a fresh path.
Missing packages/tools or unavailable namespaces fail explicitly, never silently skip
or substitute host systemd. Ordinary Python development needs none of these tools.

The native suite has a **60-second process-group deadline**, including cache
verification, and a target below 30 seconds with dependencies present. The initial
six-case run took **1.46 seconds** locally. Cache preparation and CI provisioning
are separate costs; no network is used during test execution. CI caches the fixture
by package snapshot and preparation code, selects the suite for producer/consumer
changes, and retains commands, package identities and verifier diagnostics in its
normal evidence bundle. A selected native lane must pass; a setup failure is not
coverage. Local execution prints the evidence directory.

The tests demonstrate both rejection and usable configuration: the stock generated
mount conflicts with the actual fsck mask, the reviewed adapter passes systemd's
verifier, an unrelated required masked service fails, forbidden staging writes fail,
and failing producers never publish usable partial mounts. The packaged failure
handler also runs with its boot prerequisite absent and cannot import missing
modules from the developer checkout.

These checks establish userspace integration only. They do not test dracut assembly,
actual mounts, device discovery, firmware, kernel behavior or successful boot.
Continue using explicitly requested end-to-end operations for those properties.
When adding coverage, connect real producers and consumers and substitute only the
physical/expensive effect; do not grow a general boot simulator or a full version matrix.

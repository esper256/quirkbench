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
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
make smoke
```

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
The currently supported durable controller uses native systemd user services,
with rootless Podman for build workers. A Linux VM with those capabilities is a
possible deployment route; a container without a user manager is development-only.
Missing runtime capabilities do not prevent software development and must not be
reported as ready. See [controller installation](controller-installation.md).

## Focused cloud checks: archived evidence and delayed traceback

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
TASK_TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/quirkbench-issues-7-8.XXXXXXXX")"
(umask 022; .venv/bin/python -m pytest -q -s \
  tests/test_archived_evidence_validation.py tests/test_retarget_evidence.py \
  tests/test_retarget_endpoint.py::test_new_target_endpoint_history_can_coexist_with_immutable_original_archive \
  tests/test_retarget_endpoint.py::test_repeated_retarget_after_completed_retarget_endpoint_keeps_both_origins \
  tests/test_retarget_endpoint.py::test_owned_context_never_runs_native_callback_after_final_private_source_fence \
  tests/test_retarget_endpoint.py::test_archived_identity_and_snapshot_are_rechecked_after_capture_callbacks \
  tests/test_evidence_drain_client.py tests/test_release_http.py \
  --basetemp="$TASK_TEST_ROOT/evidence" >"$TASK_TEST_ROOT/evidence.log" 2>&1)
```

Further work for [issue #19](https://github.com/esper256/quirkbench/issues/19)
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
its behavior checks and budgets at most 150 reconstructions; the previous code
fails that budget at 189. These measurements describe one case, not a new full
suite result. Repeated reconstruction remains tracked in #19. Entry rejection
and post-capture mutation tests verify that no client exchange or archived
journal write follows a failed reader boundary.

[Issue #8](https://github.com/esper256/quirkbench/issues/8) contains one reported
exit 139 during pytest's 30-second faulthandler dump on Debian 13, CPython 3.12.14,
pytest 9.1.1, ending in `python3.12/pathlib.py`. Only that excerpt is available here;
there is no core/native stack or complete crash artifact to assign a cause. The
local editing runtime is CPython 3.14.7 (GCC 15.3.1, Fedora/glibc 2.42), pytest
9.1.1; it is not the reported runtime. Inspection of the installed pytest hook
shows it calls `faulthandler.dump_traceback_later` around the test protocol and
cancels it afterward. No local reproduction was attempted and no defect is assigned
to Quirkbench, pytest, CPython or the supplied runtime build.

The bounded stdlib-only probe prints runtime/build identity and forces a delayed
dump during path resolution. Its paired test leaves timer setup to pytest. Run
each once on the affected cloud runtime, retain both logs and statuses:

```sh
TASK_PYTHON_STATUS=0
timeout 10s .venv/bin/python -I tests/faulthandler_probe.py \
  --path "$TASK_TEST_ROOT" --duration 1 --delay .25 \
  >"$TASK_TEST_ROOT/python-dump.log" 2>&1 || TASK_PYTHON_STATUS=$?
printf '%s\n' "$TASK_PYTHON_STATUS" >"$TASK_TEST_ROOT/python-dump.exit"
TASK_PYTEST_STATUS=0
timeout 10s .venv/bin/python -m pytest -q -s tests/test_faulthandler_probe.py \
  -o faulthandler_timeout=0.1 -o faulthandler_exit_on_timeout=false \
  --basetemp="$TASK_TEST_ROOT/pytest-dump" \
  >"$TASK_TEST_ROOT/pytest-dump.log" 2>&1 || TASK_PYTEST_STATUS=$?
printf '%s\n' "$TASK_PYTEST_STATUS" >"$TASK_TEST_ROOT/pytest-dump.exit"
```

A crash in the Python-only probe narrows the scope beyond pytest/Quirkbench; a
pytest-only crash isolates integration for further investigation, without proving
which component is faulty. Two successful probes do not resolve the original
fixture-specific report. Preserve its diagnostic form (the original joined case
with `-o faulthandler_timeout=30`) and recorded exit 139. A faster #7 case that
never fires that timer provides no #8 crash evidence. These commands leave product
diagnostics enabled and do not skip the evidence regression or request a runtime
matrix, image build or qualification campaign.

### Cloud results and isolated runtime diagnosis

On Debian 13 with the supplied Clang-built CPython 3.12.14 and pytest 9.1.1,
the Python-only probe completed its delayed dump, but the pytest probe exited
139 during its dump. An isolated test importing only `pathlib` and `time` also
exited 139, with no Quirkbench imports, repository configuration, fixtures or
third-party pytest plugins. The same isolated test and the existing pytest probe
both completed their dumps on Debian's GCC-built CPython 3.13.5 with pytest 9.1.1.
These results exclude Quirkbench application code from the minimal reproduction;
they do not distinguish a CPython defect, runtime-build defect or pytest interaction,
nor establish that every Python 3.12 build is affected. Issue #8 remains open.
One later run of the copied probe also completed on the supplied Python 3.12
runtime, so a single successful run there does not establish that the fault is
gone. Retain both successful and crashing results rather than retrying to obtain
a passing diagnostic.

To reproduce independently of the repository, choose the interpreter being
investigated, with pytest installed, and run the copied probe once:

```sh
TASK_PROBE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/quirkbench-runtime-probe.XXXXXXXX")"
cp tests/faulthandler_minimal.py "$TASK_PROBE_ROOT/test_minimal_dump.py"
TASK_PROBE_STATUS=0
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 timeout 10s .venv/bin/python -m pytest \
  -c /dev/null -q -s "$TASK_PROBE_ROOT/test_minimal_dump.py" \
  -o faulthandler_timeout=0.1 >"$TASK_PROBE_ROOT/dump.log" 2>&1 \
  || TASK_PROBE_STATUS=$?
printf '%s\n' "$TASK_PROBE_STATUS" >"$TASK_PROBE_ROOT/dump.exit"
```

Disabling third-party plugin discovery above isolates the diagnostic; it is not
the normal application test command. One diagnostic run with `--assert=plain`
also completed on the supplied Python 3.12 build. That narrows investigation to
the runtime/pytest execution context but is not proof that assertion rewriting
causes the fault. Keep normal test assertions and product diagnostics enabled.

If the selected interpreter crashes in this diagnostic, retain its build identity,
log and exit status and use a supported Python 3.11+ interpreter that passes it
for development. Recreate the virtualenv using that interpreter and reinstall
`.[test]`; virtualenvs and native extensions cannot be moved between interpreters.
For example, on a host providing a working Python 3.13:

```sh
python3.13 -m venv .venv-python313
.venv-python313/bin/python -m pip install -e '.[test]'
make test PYTHON=.venv-python313/bin/python TESTS=tests/test_faulthandler_probe.py
```

Install the distribution's venv/ensurepip support if its Python packages split
those components. This interpreter choice changes no application platform checks,
deadlines, storage rules, assertions or dependency declarations. A passing alternate
runtime is a development route while the original crash remains under investigation.

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
and setup tests, plus a simulated CLI demo and independent monitor. Setup uses
injected service responses, not a live systemd manager. Explicit file/node paths
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
Underlying full-suite performance is still tracked separately in issue #19.

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

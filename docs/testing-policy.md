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
make test
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
late private-byte/permission/link mutations, exact attribution, later current
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

## Validation scope

Agent tokens per useful finding are the primary optimization. Routine development
uses focused software regressions. Expensive end-to-end qualification runs only
for an explicitly requested final release of a major version, against a stable
candidate. Milestone acceptance descriptions specify the eventual evidence needed;
they do not instruct agents to rebuild and qualify after each implementation task.

| Work | Validation |
| --- | --- |
| Documentation or policy | Inspect links, consistency and `git diff --check` |
| Local implementation change | Relevant pytest files or test cases |
| Broad software integration | `make test` when justified; push/PR CI runs the software suite |
| Final major-version candidate | Explicit release qualification, with recorded inputs and retained results |

For example, `make test TESTS=tests/test_monitor.py` runs one software suite.
`make test`, `make acceptance-m1` and ordinary CI never invoke real image, VM,
composition or hardware campaigns. Keep expensive fixtures outside pytest's normal
`tests/` collection and out of push/PR CI.

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

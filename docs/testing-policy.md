# Testing and agent quota

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

## Fast development loop

Pick the smallest changed behavior and its failure cases, implement them, then run
the owning tests. After those pass, add adjacent compatibility tests only where the
change crosses that boundary. Do not rerun an unchanged passing selection at the end
of every turn or run the full suite merely to complete a packet. Full software CI
still runs on push/PR; required CI failures must be resolved before claiming readiness.
Do not run the same matrix locally and in CI by habit.

Collect timing information during a needed run with `--durations=10`; do not start
a benchmark campaign before development. Aim for a repeatable edit/check selection
under 30 seconds and a focused packet check under two minutes. These are engineering
targets, not measured current timings, deadlines, skip rules or reduced acceptance.
If a necessary test exceeds them, keep its coverage and use its timing to decide
whether setup reuse or an injected boundary would help. Report actual elapsed time
alongside the normal result; no separate performance-reporting system is needed.

Test domain logic with real validation, SQLite transactions, temporary files and
small source repositories. Inject clocks, network failures, service managers and
expensive build/boot operations at their existing adapter boundaries. Expiry and
retry tests should advance a fake clock rather than sleep for production deadlines.
Keep bounded real subprocess tests when they verify termination, pipes or descendant
ownership; a sleeping child killed by a short deadline is not a long-running build.
Do not mock away the transaction or reconciliation behavior under test.

Build one small joined flow incrementally as the services land: setup → enrollment
→ investigation → proposal → approval → evidence → export. Exercise real application
services and versioned records with injected external effects. Reuse its fixtures;
do not construct a separate simulator, fake scheduler or generic testing framework.
Small signed archives and miniature Git histories can test distribution and source
semantics without publishing a release, fetching a kernel or rebuilding recovery.
They do not qualify real image bytes or full-size resource behavior.

Use the configured test interpreter and existing dependencies. For tests exercising
state/build path guards, choose a disposable test base outside Git checkouts and
forbidden system paths, with the same access semantics the test requires. Account
cache paths worked in the recorded boundary audit; `/tmp` and `/var/tmp` did not in
that local environment. This is a fixture-location issue, not a universal requirement
on users' paths. Never weaken production guards to accommodate a test directory.
If using pytest `--basetemp`, select a dedicated disposable child directory: pytest
deletes it. Never point it at a cache root, product state, another run or credentials.
Do not share that directory across concurrent runs.

Diagnose tool availability, fixture paths and sandbox restrictions once and retain
the working command in the packet handoff. Do not reinstall environments or move
between native/container execution after every failure without evidence it is needed.
Archive construction tests are appropriate when packaging changes; native activation,
service migration and production-state mutation are not routine test prerequisites.
Do not add parallel pytest workers until timings justify them and state/port/temp
isolation has been checked. Parallelism is not a substitute for smaller selections.

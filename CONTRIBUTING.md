# Contributing to Quirkbench

## Choose and claim a task

Start with [first-usable tracker #29](https://github.com/esper256/quirkbench/issues/29).
GitHub issues and linked PRs own task status; the [roadmap](docs/product-roadmap.md)
and [acceptance guide](docs/installation-to-patch.md) describe product scope.
Read [AGENTS.md](AGENTS.md), then only the contracts relevant to the chosen issue.

Reconcile current main, the issue's dependencies and any open PR/claim before
coding. Reuse existing foundations. Choose one ready product issue; supporting
infrastructure is lower priority unless it blocks that task or is explicitly requested.
Do not select deferred managed/unattended work or operator gates as cloud work.

Claim the issue in a comment with your worker/session identity, intended branch,
scope and likely shared files. Assign yourself if supported. Re-read the issue to
detect a concurrent claim; comments are coordination, not a lock. Coordinate an
overlap rather than both changing the same CLI/schema/service. Reclaim an apparently
abandoned task only after checking its PR/branch and recording the handoff.

## Implement through a PR

Fetch current main and create a topic branch (for example,
`feature/30-candidate-job`) or isolated worktree. Preserve unrelated edits and
never overwrite another worker's branch. If shell GitHub access is unavailable,
use the authenticated connector; report missing capabilities instead of blocking
on repeated inaccessible commands.

Implement the bounded issue outcome through existing services. Keep interfaces,
records and compatibility rules intact. Update installed help and user documentation
when behavior becomes available. Open a linked draft PR early for substantial work.

Use [C0–C7](docs/implementation-contracts.md), [C8](docs/product-interface.md) and
the storage policy as durable constraints. Obtain the higher-reasoning review
required by AGENTS.md for storage, trust/binding, source/worker ownership, durable
execution, shutdown or watchdog boundaries. Record reviewer/scope/findings and the
resolution in the PR; unavailable review is a blocker, not an implied approval.

## Validate and report

Follow [testing policy](docs/testing-policy.md):

- `make smoke` or default `make test`: quick sanity check.
- `make test TESTS=tests/test_feature.py`: affected software regressions.
- `make test-full` or manual full CI: larger integration milestones, not every fix.
- Documentation: link/consistency checks and `git diff --check`.
- Real image/QEMU/composition/endurance qualification: explicit final major-version
  request and existing release guard. An issue assignment does not authorize it.

Record source SHA, exact focused commands/results, relevant CI links, required
review and remaining limitations. Preserve first-failure diagnostics; diagnose with
focused checks instead of rerunning until green. Smoke alone does not prove a changed
subsystem or the native journey. Retrieve completed CI results once; do not occupy
an agent polling jobs or launch a full suite simply because a PR merged.

## Merge, close and continue

Keep PRs limited to one issue or a clearly explained inseparable slice. Use
`Fixes #N` only if merging will satisfy the issue's entire bounded acceptance;
use `Refs #N` for partial work. A plan, open PR or passing smoke run is not closure.

Merge only when session authorization covers merging, relevant checks/review pass
and no blocking feedback remains. Branch protection is not being introduced here.
Without merge authorization, leave a reviewable PR and proceed to another independent
ready issue when the session permits. Never push directly to main as the normal
development workflow.

After merge, confirm the issue's acceptance, record evidence, close it and update
tracker #29's checklist/dependency readiness. Preserve separately open native/operator
gates. Start the next ready issue from current main; do not implement a dependent
task on an unmerged assumed interface.

## Architectural obstacles and operator gates

Make routine implementation decisions within existing contracts. Pause the affected
task when it needs a new database/scheduler/service, incompatible wire/storage
semantics, a weaker trust/storage/approval boundary, conflicting contracts or a
material change in first-usable scope. Record the evidence, alternatives, recommended
option and specific decision needed. Do not quietly redesign to keep a queue moving.

Production credentials/signing/publication, physical target operations and release
qualification require their own explicit authorization and inputs. Cloud development
does not need to run a real controller.

Continue independent ready work if a bounded task is blocked. Stop and ask the owner
when the obstacle changes shared architecture, required review is unavailable for
all remaining work, or no safe ready tasks remain. Leave branch/PR, commands, evidence
and the exact next step in the issue before ending a session.

For a reusable consecutive-task instruction, see the [cloud worker prompt](docs/cloud-worker-prompt.md).

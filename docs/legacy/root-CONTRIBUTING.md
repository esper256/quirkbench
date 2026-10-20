> **Legacy reference — not current requirements or agent instructions.**
> This document was archived on 2026-10-07. Consult [current documentation](../README.md).
> Commands and implementation claims below may be obsolete.

# Contributing to Quirkbench

## Choose and claim a task

Start with [first-usable tracker #29](https://github.com/esper256/quirkbench/issues/29).
GitHub issues and linked PRs own task status; the [roadmap](product-roadmap.md)
and [acceptance guide](installation-to-patch.md) describe product scope.
Read [AGENTS.md](root-AGENTS.md), then only the contracts relevant to the chosen issue.

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

Inspect `git status --short` first. From a clean checkout:

```sh
git fetch origin main
git switch -c feature/issue-24 origin/main
```

For an occupied checkout, use `git worktree add -b feature/issue-24
../quirkbench-issue-24 origin/main` and work there. Substitute the agreed issue/base;
do not reset, stash or clean somebody else's work. Reconcile base changes
intentionally before final validation.

Fetch current main and create a topic branch (for example,
`feature/30-candidate-job`) or isolated worktree. Preserve unrelated edits and
never overwrite another worker's branch. If shell GitHub access is unavailable,
use the authenticated connector; report missing capabilities instead of blocking
on repeated inaccessible commands.

Implement the bounded issue outcome through existing services. Keep interfaces,
records and compatibility rules intact. Update installed help and user documentation
when behavior becomes available. Open a linked draft PR early for substantial work.

Use [C0–C7](implementation-contracts.md), [C8](product-interface.md) and
the storage policy as durable constraints. Follow AGENTS.md's campaign review policy:
one independent **gpt-6-astra / medium** review of the accumulated changes at the
end of a substantial refactoring or feature milestone, before merging the campaign.
Include affected authority and execution boundaries in that review. Individual
packets and routine fixes use implementer review and focused tests, without reviewer
subagents. Resolve findings in the same review cycle; another review requires a
material change to the reviewed design or guarantees. Record reviewer, scope,
findings and resolutions in the PR. An unavailable campaign review blocks that
campaign's merge, not independent development. Architectural decisions still
require the owner's input as specified in AGENTS.md.

Commit only intended paths, push the topic branch and open the draft:

```sh
git add CONTRIBUTING.md
git commit -m 'Document the contribution workflow'
git push -u origin feature/issue-24
gh pr create --draft --base main
```

Use the [PR template](../../.github/pull_request_template.md); remove prompts that add no
reviewer value. A short documentation PR can be brief. These are contribution
conventions, not required approvals or branch-protection enforcement.

## Validate and report

Follow [testing policy](testing-policy.md):

- `make smoke` or default `make test`: quick sanity check.
- `make test TESTS=tests/test_monitor.py`: affected software regressions.
- `make test-full` or manual full CI: larger integration milestones, not every fix.
- Documentation: link/consistency checks and `git diff --check`.
- Real image/QEMU/composition/endurance qualification: explicit final major-version
  request and existing release guard. An issue assignment does not authorize it.

Install dependencies using the [portable setup](testing-policy.md#portable-software-development).
[Focused suites and retained evidence](ci-evidence.md) use the same selection
locally and in CI. Review selected/unselected suites and unmapped-change warnings.

Record source SHA, exact focused commands/results, relevant CI/evidence links,
required review and remaining limitations. For boundary-sensitive changes record
reviewer/model identity, reviewed SHA, findings and their disposition; resolve
findings and re-review affected changes. Tests alone do not satisfy that review. Preserve first-failure diagnostics; diagnose with
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

Record final PR head, tested merge SHA/CI URL and merged commit identity (squash
merges produce a different SHA). Close only when the delivered change and applicable
validation support closure.

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

For a reusable consecutive-task instruction, see the [cloud worker prompt](cloud-worker-prompt.md).

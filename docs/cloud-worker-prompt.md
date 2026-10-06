# Consecutive cloud worker prompt

Copy the following into a cloud worker session. It authorizes ordinary software
implementation and issue/PR updates. The final paragraph makes merge authority
explicit; omit that paragraph if the worker should leave PRs for human merge.

> Work through Quirkbench's first-usable product backlog:
> https://github.com/esper256/quirkbench/issues/29.
> Read AGENTS.md, CONTRIBUTING.md and the chosen issue's relevant contracts.
> GitHub issues/PRs own task status; do not reconstruct tasks from historical plans.
>
> Continue through ready product issues one at a time, starting with #30 if it is
> still ready and unclaimed. Reconcile current main, dependencies, claims and open
> PRs before each selection. Claim the issue with your session identity, branch and
> scope, then recheck for competing claims. Coordinate shared files and preserve
> other workers' changes. Do not pick supporting infrastructure ahead of product
> work unless it is an actual blocker.
>
> This is a single-user, per-user installation used exclusively by its owner.
> Do not add multi-user/shared-instance, account/role or collaboration machinery.
> Multiple targets and workers do not change that scope.
>
> Apply the owner-approved file-access policy in implementation-contracts.md (#66).
> Respect usable ordinary-data permissions; preserve actual secret protection and
> storage/worker integrity. Existing mode checks/tests are not proof of a product
> requirement. Do not turn another umask failure into a fixture-only fix without
> checking the revised contract. Coordinate #65/#66 with the integration owner.
>
> Implement the complete bounded outcome on a topic branch using existing services.
> Reuse working foundations, preserve compatibility and authority boundaries, and
> update executable help/docs as needed. Run affected regressions and smoke as
> appropriate; retain failures and record exact source/commands/results. Use
> implementer review during development. At the end of a substantial refactoring
> or feature milestone, obtain one independent gpt-6-astra review at medium
> reasoning effort of the accumulated changes before merging the campaign.
> Do not launch reviewer subagents for individual packets or routine fixes.
> Resolve findings in the same review cycle; request another review only for a
> material change to the reviewed design or guarantees. Record review evidence
> under AGENTS.md's authoritative campaign review policy.
>
> Open/update a linked PR with the problem, resulting behavior, validation and
> limitations. Close an issue only after merged work and evidence satisfy its scope.
> Update #29's completion/readiness, sync current main and move to the next ready
> issue. If a PR awaits review/CI, choose independent ready work rather than polling
> continuously or assuming its interface has landed.
>
> Make routine decisions autonomously. Pause for a major architectural obstacle:
> conflicting contracts, a new scheduler/database/service, incompatible stored/wire
> semantics, weakening safety/trust/source ownership/approval rules, or a material
> change to first-usable scope. Record evidence, options, your recommendation and
> the decision needed. Continue unrelated ready work only if safe; otherwise stop
> and ask the owner. Missing required review is a blocker, not approval.
>
> This is cloud software development. Do not run real image/kernel/QEMU/hardware
> campaigns, commission targets, create production credentials or publish releases
> without separate explicit authorization. Full software CI is for a larger
> integration milestone, not every issue. Managed invocation and unattended modes
> remain deferred. Do not claim native readiness from injected tests.
>
> Continue until no unclaimed ready software task remains, a shared architectural
> decision blocks progress, or the session must end. Leave a durable issue/PR handoff
> with completed work, pending checks, blockers and the next concrete action.
>
> You may merge your bounded implementation PRs after relevant checks and required
> review pass and blocking feedback is resolved. Do not bypass failed checks, merge
> someone else's work without coordination or push directly to main. If merge
> permissions are unavailable, leave the PR ready and continue independent work.

The prompt does not grant physical execution, production publication or release
qualification. Those issue gates remain separate even if all software dependencies close.

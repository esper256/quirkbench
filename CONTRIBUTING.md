# Contributing

Start with an issue describing the concrete problem and expected behavior. Read
[AGENTS.md](AGENTS.md) for architectural boundaries and review requirements, and
[the testing policy](docs/testing-policy.md) for authoritative validation tiers.
The [documentation index](docs/README.md) points to implementation contracts.
These are contribution conventions, not branch protection or required-approval settings.

## Checkout to review

1. Inspect `git status --short` and preserve unrelated work. Fetch the intended
   base; do not assume a stale local `main` is current. From a clean checkout:

   ```sh
   git fetch origin main
   git switch -c feature/issue-24 origin/main
   ```

   For an occupied checkout, use an isolated worktree instead:

   ```sh
   git fetch origin main
   git worktree add -b feature/issue-24 ../quirkbench-issue-24 origin/main
   cd ../quirkbench-issue-24
   ```

   Substitute the agreed base/issue. Do not reset, stash, clean or force-push
   somebody else's work. Reconcile base changes intentionally before final checks.
2. Install the test extra using the [portable setup](docs/testing-policy.md#portable-software-development).
   Implement a bounded change with affected regressions. Inspect the diff and run
   `git diff --check`. Documentation-only edits need link and consistency checks.
3. Choose appropriate validation. `make test` defaults to smoke, just like
   `make smoke`; `make test TESTS=tests/test_monitor.py` runs an explicit selection.
   [Focused suites and retained evidence](docs/ci-evidence.md) are reproducible
   locally. Run `make test-full` or dispatch `full-tests.yml` only at a larger
   software milestone. Real-system release qualification requires the explicit
   authorization described in the testing policy. Smoke is never full-suite or
   hardware evidence.
4. Commit only intended paths, push the topic branch and open a draft PR early:

   ```sh
   git add CONTRIBUTING.md
   git commit -m 'Document the contribution workflow'
   git push -u origin feature/issue-24
   gh pr create --draft --base main
   ```

   Use the [PR template](.github/pull_request_template.md). Explain the problem,
   resulting behavior, related issue, exact checks and results, material limits
   and evidence links. Delete inapplicable prompts; a short docs PR can be brief.
5. Review the final diff and CI results, including selected/unselected suites and
   selection warnings. Follow AGENTS.md's higher-reasoning review requirement for
   boundary-sensitive changes. Record reviewer/model identity, reviewed source SHA,
   findings and their disposition in the PR; resolve findings and re-review affected
   changes. Passing tests alone do not satisfy that review. Ordinary docs/test-tool
   changes do not require inventing a boundary approval.
6. On failure retain the first attempt's evidence, diagnose using focused checks
   and fix the cause. Do not rerun just to obtain green or overwrite failed evidence.
   Record any unavailable validation and its consequence. Mark the PR ready once
   the change and applicable review/validation are complete, then merge under the
   owner's normal workflow. Record final PR head, tested merge SHA/CI URL, and the
   merged commit identity (a squash produces a different SHA).

Close the linked issue only when the delivered change and applicable validation
support closure. `Closes #24` in a PR schedules closure on merge to the default
branch; an open draft or green smoke run alone does not complete the issue.

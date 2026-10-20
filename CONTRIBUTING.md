# Contributing

Use plain English and the [glossary](docs/glossary.md), including in reviews and
progress updates. Explain an action directly instead of inventing process jargon.

Read [requirements](docs/requirements/product.md) and the relevant [design](docs/README.md).
Requirements changes need human approval. Design changes do not, except massive
complexity increases or substantial losses of capability, performance or usability.

Inspect current issues/PRs and working-tree state before coding. Use a topic branch
or isolated worktree, coordinate shared files and preserve unrelated work.
Current designs guide the rewrite; old issue wording does not override them.
Do not close unmerged work.

Follow [development guidance](docs/designs/development.md): large coherent implementation stages,
focused tests, one review of the completed work, honest limitations. Update current help and
designs as implementation evolves. No new migrations or compatibility machinery.
Do not modify live state or perform physical operations without authorization.

Use `uv sync --locked` to acquire dependencies, then `uv run --no-sync pytest -q`
for the fast integration suite. It uses temporary files and local sockets, without
root, downloads, image builds or hardware operations. Shared helpers live in
`tests/support`; keep tests focused on user-visible outcomes.
Do not run full suites or real image builds after every edit. Preserve failure
logs and diagnose before retrying. Documentation-only work needs link checks and
`git diff --check`.

PRs explain the problem, changed behavior, relevant verification and limitations.
Report the required review and remaining work. Push/merge only with session
authorization. Archived documentation is reference material, never a second contract.

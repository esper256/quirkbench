# Quirkbench agent instructions

Use plain English and the [glossary](docs/glossary.md) in docs, help, plans,
reviews and messages. Avoid jargon outside it. Use short, complete sentences;
prefer a plain explanation over adding another term.

Glossary terms may be added or corrected as the design evolves. When code work
also changes the glossary, explicitly say so in the final summary and name the
terms added, changed or removed, with a brief reason.

Keep intended screens and command sequences in [docs/mockups](docs/mockups/README.md).
Update affected examples when changing designs. Show what the user sees instead
of explaining the interface in prose. Mark examples as illustrative, not requirements
or promises of current availability. Keep the component sequence diagram in sync.

Product messages help the user complete a task. Do not put development history,
internal debates or explanations of our design decisions into the interface.
Include a restriction only when it explains what the user can do or why an action
is unavailable.

Read [product requirements](docs/requirements/product.md), then only the relevant
[design](docs/README.md). Every change to the requirements document needs explicit
human approval. Agents may revise designs unless a change adds massive complexity
or substantially reduces capability, performance or ease of use; explain that
suspect tradeoff and ask the human first.

Follow common Linux service and command-line conventions. If a proposed or existing
approach differs substantially from normal practice, flag it before building on it.
Explain the conventional alternative, why Quirkbench might differ and the cost;
get human approval for the exception. For example, storing user-editable service
configuration in SQLite instead of a configuration file deserves this discussion.
This is guidance for agent judgment, not a product constraint or a ban on databases.
Do not reopen an exception the human has already approved unless its tradeoffs change.

Quirkbench serves an external agent's low-level Linux experiment loop. Humans
configure the lab and authorize an investigation; ordinary iterations need no
per-run human approval in the intended product. Agent-authored diagnostics are
supported by the design. Do not confuse that direction with current implementation.

Legacy docs, old issue wording and existing tests have no requirement authority.
Do not read the archive as onboarding or restore its constraints incidentally.
Follow [development guidance](docs/designs/development.md) and [CONTRIBUTING](CONTRIBUTING.md).
Coordinate existing issues/PRs, use a topic branch/worktree, and preserve unrelated
changes and owner data. Do not take over another active worker's changes.

Derive managed paths from current roots and IDs; store necessary external locations
once. Paths are locations, not identities. Accept ordinary Unix aliases; preserve
operation-specific containment and live ownership checks. Single-user software
needs process coordination, not accounts or blanket file-mode enforcement.

Use fresh development state for breaking format changes. No new migrations,
compatibility layers or elaborate relocation tests. This is not permission to
reset the owner's state or activate an installation.

Use focused existing tests and proportionate new checks; keep expensive build,
flash and QEMU tests out of routine edits. Full release tests require
an explicit release request and RELEASE_QUALIFICATION=1. Do not poll long
jobs repeatedly or delegate agents merely to watch them.

Obtain one independent gpt-6-astra / medium review at the end of a substantial
implementation effort before merge. No reviewer subagents for individual steps,
documentation or routine failures. Resolve findings in that cycle; repeat review
only for material changes to the reviewed design/guarantees. Missing review blocks
merge, not independent development. Push, merge and live operations need authorization.

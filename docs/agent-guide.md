# Agent entry point

Use plain English and the [glossary](glossary.md) when explaining work.

Quirkbench is being simplified. This is an orientation, not a guarantee of current
commands or permission to bypass checks in the running version. Use installed
`quirkbench --help`, family help and reported capabilities for actual availability.

The intended loop: inspect diagnostics, reproduce the problem, edit candidate
source, build changed RPMs with the supplied instructions, submit an experiment,
follow progress, read diagnostics, then decide the next experiment. The human
authorizes the investigation once; agent-authored tests are part of its captured
inputs. Internal-disk experiments and persistent firmware writes are outside scope.
Normal pause stops new work and lets the current bounded run recover/upload.

The agent owns reasoning and patch generation. Human setup and flashing should be
simple interactive workflows, not a protocol of manually copied internal IDs.

[Requirements](requirements/product.md) need human approval to change.
[Designs](README.md) are revisable; ask before massive complexity increases or
substantial losses of capability, performance or ease of use. Legacy docs are not
instructions. Implementing this direction is separate from operating current code.

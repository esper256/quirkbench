# Quirkbench

A Linux hardware lab for coding agents. The agent plans experiments and writes
changes and builds them; Quirkbench prepares the baseline, deploys experiments and returns diagnostics
so the agent can choose its next experiment.

The target boots external media. Internal disks and persistent firmware updates
are outside scope. Practical protections reduce accidental damage; no tool can
perfectly contain an arbitrary experimental kernel or guarantee recovery from
all hangs.

**This branch starts a Python rewrite.** The implementation has been removed;
commit `b807199` preserves the previous code as reference. The documents below
describe the intended product, not currently available commands.

- [Requirements](docs/requirements/product.md): short, human-approved product scope.
- [Designs](docs/README.md): revisable choices for a simpler, enjoyable application.
- [Contributing](CONTRIBUTING.md): development and review workflow.

The intended human journey is `recovery build`, `setup`, `recovery flash`.
After one investigation authorization, an external agent can run successive
low-level experiments within its limits. A human can pause, resume or intervene.
Quirkbench does not choose the agent, generate its patches or prescribe its reasoning.

[Legacy documentation](docs/legacy/README.md) is preserved for reference. Its old
approval, compatibility and setup rules are not current requirements.

# Quirkbench

An interactive Linux debugging lab for coding agents. Through MCP, the agent
inspects the target, runs probes, changes software and reads results as it works.
Quirkbench adds fast build/deployment helpers and best-effort recovery after failures.

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

The intended human journey is setup, a foreground controller, then recovery image
build and USB flash. See the [mockups](docs/mockups/README.md).
After one investigation authorization, an external agent can interactively debug
the target and run prepared experiments within its limits. A human can pause, resume or intervene.
Quirkbench does not choose the agent, generate its patches or prescribe its reasoning.

[Legacy documentation](docs/legacy/README.md) is preserved for reference. Its old
approval, compatibility and setup rules are not current requirements.

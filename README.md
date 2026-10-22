![Quirkbench: Refinement through questionable means.](docs/assets/quirkbench-banner.png)

# Quirkbench

An interactive Linux debugging lab for coding agents. Through MCP, the agent
inspects the target, runs probes, changes software and reads results as it works.
Quirkbench adds fast build/deployment helpers and best-effort recovery after failures.

The target boots external media. Internal disks and persistent firmware updates
are outside scope. Practical protections reduce accidental damage; no tool can
perfectly contain an arbitrary experimental kernel or guarantee recovery from
all hangs.

**Quirkbench is in active development.** Fast integration tests cover the
application journey. Recovery and Bazzite have booted on physical hardware,
connected to the controller, run an MCP command and returned diagnostics.
New images and changes still need their own boot checks. See [local installation](docs/controller-installation.md).
The design documents describe the full intended product; later features are not
yet available.

- [1.1 changelog](CHANGELOG.md): features and fixes since the first agent investigation.
- [Agent guide](docs/agent-guide.md): available tools and suggested routes.
- [Requirements](docs/requirements/product.md): short, human-approved product scope.
- [Designs](docs/README.md): revisable choices for a simpler, enjoyable application.
- [Contributing](CONTRIBUTING.md): development and review workflow.

The intended human journey is setup, a foreground controller, then recovery image
download and external-drive flash. Developers can explicitly build locally. See the [mockups](docs/mockups/README.md).
After one investigation authorization, an external agent can interactively debug
the target and run prepared experiments within its limits. A human can pause, resume or intervene.
Quirkbench does not choose the agent, generate its patches or prescribe its reasoning.

[Legacy documentation](docs/legacy/README.md) is preserved for reference. Its old
approval, compatibility and setup rules are not current requirements.

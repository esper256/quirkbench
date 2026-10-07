# Build environment

Use `make bootstrap` for local software development and `./quirkbench --help` for
current commands. Build tools in this directory support the implementation; they
do not define product requirements.

Follow [development guidance](../docs/designs/development.md). Use existing bounded
container execution for requested builds, with visible resources and whole-worker
shutdown. No long kernel/image build is required for routine software changes.

The [archived environment guide](../docs/legacy/environments-README.md) preserves
specific commands and historical configuration for reference, not as prerequisites.

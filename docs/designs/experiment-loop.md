# The agent's investigation loop

Revisable design supporting the [requirements](../requirements/product.md).
See [interactive debugging](interactive-debugging.md) for the architecture and
[agent mockup](../mockups/agent-experiment.md) for the intended interaction.

A human authorizes the target, investigation goals and allowed changes once.
One investigation may watch for several suspected bugs simultaneously. Run compatible
probes together and examine shared evidence for all relevant symptoms. Keep findings
distinguishable; fixing one bug does not close the others. Separate investigations
are optional, not required for each bug. The user's agent
connects to Quirkbench through MCP and reads context, current software, connection
status and existing evidence. It edits candidate software and tests, not Quirkbench.

Begin with target information, diagnostics and reproduction. Choose a candidate
close enough to the reported environment. The first supported baseline is
[Bazzite stable](candidate-deployment.md). Boot it for live investigation. The agent can inspect it, start
stress programs, ask new questions, send small binaries, install packages and change
runtime settings while it remains running. A complete prewritten test plan is optional.
Prepared tests and candidate hooks remain useful for early-boot and unattended work.

A run starts from an identified candidate and includes subsequent commands and live
changes until returning to recovery. Keep its initial candidate identity unchanged;
record later actions separately. Changed files do not invalidate unrelated work.
A baseline that does not reproduce the problem is not a fix.
New deployments start with fresh mutable OS files. The controller may boot a
retained deployment again, keeping persistent edits but assigning a new run ID.
Plain `ostree admin unlock` edits disappear on reboot. For a reusable candidate,
prepare selected packages/files explicitly; a retained disk is not a saved candidate.

For software changes, follow [build guidance](experiment-builds.md) and choose the
appropriate [deployment](candidate-deployment.md). Small probes and suitable
userspace changes can run live. Replacing the running kernel normally needs a new
boot. Report when a reboot or service restart is needed rather than doing it silently.

MCP tools return useful bounded output, current mode/run, next actions and references
to full saved results or instructions. Tool descriptions explain how to use them;
context links to the agent guide. The agent finds matching build instructions
using candidate package and build information. Do not flood agent context with raw logs. Save long-command output
durably and wake a waiting agent on completion, an agreed deadline or an error,
without periodic model polling. Other commands can run concurrently. Each starts
with its own explicit working directory and environment; files and services remain
shared. See the [command rules](interactive-debugging.md#command-behavior).
The controller and target dashboards show current work; CLI status remains available.

The agent chooses independent command timeouts; runs have no overall deadline.
Investigation pause stops new work, lets bounded active work finish, then returns
to recovery and uploads evidence. Force stop attempts
immediate interruption. An uncertain command is never blindly replayed after a
lost reply, disconnect or reboot. Evidence remains readable while paused.

The agent evaluates evidence and produces the investigation patch with its usual
coding tools. Quirkbench helps reproduce selected findings but does not reason
about the bug or guarantee a fix. Measure acceleration, including failures and
human intervention.

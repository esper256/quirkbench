# Enjoyable to use

Revisable design, not additional [requirements](../requirements/product.md).

See [example screens and commands](../mockups/README.md) and the
[component sequence](component-sequence.md).

- The external agent reasons and writes patches. MCP is its primary Quirkbench interface for live inspection and control. Humans use the CLI, recovery TUI and monitor. Quirkbench supplies tools, not a model runner.
- Borrow [Omarchy's discoverable menus and direct keyboard actions](https://omarchy.org/manual/navigation/), not its desktop stack. Favor clear defaults, spacing, restrained color, useful shortcuts and one obvious next step.
- Setup uses short terminal questions with defaults; selectors are useful for flashing. Keep output readable. JSON/noninteractive modes use the same services without prompts. Humans should not transcribe hashes or internal IDs.
- Keep deployment-tool administration out of the everyday flow. The agent can inspect, execute, copy files and change a running candidate. Build/deploy helpers accelerate changes that need them; every probe does not require a prepared experiment.
- Progress shows phase, elapsed time, recent activity and useful counters, with logs/stop readily available. Don't invent percentages or mistake a heartbeat for progress.
- Errors explain the failed action and next step. Put technical details in logs. Avoid surprise windows and walls of warnings.
- Keep maintenance out of the main journey. Useful source/evidence export is enough; the agent judges success and prepares patches with its own tools.

The first candidate uses Bazzite stable on x86-64/UEFI. This is a starting platform,
not the product's identity. An example investigation never becomes a universal requirement.

`controller run` shows the [controller console](../mockups/controller-console.md)
when attached to an interactive terminal. It shows target contact age, active work
and recent controller/MCP activity. Connections shows addresses and token-file
location, never the token. The dashboard reads existing records; it starts no
additional service and sends no target commands. Stop confirms before using the
normal controller shutdown. Ctrl+C and `controller stop` remain available.
Redirected output, `--json`, unsupported terminals and `--no-tui` retain plain
output. Terminal resize and exit restore a usable terminal. File logging continues.

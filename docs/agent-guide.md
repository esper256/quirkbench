# Agent entry point

Use plain English and the [glossary](glossary.md). The human configures the MCP URL
and installation token once. Address the authorized target by its friendly name;
you do not need its private pairing credential.

Read investigation context and the available MCP tool descriptions first. They
report current target mode, work, results and next actions. Inspect the target and
try to reproduce the problem before changing software. Never edit Quirkbench to
perform an investigation. Internal disks and persistent firmware are excluded.

## Available workflow

- `ssh_recovery_inspect` reads recovery diagnostics; `emergency` avoids launching
  external programs. Recovery provides no unrestricted agent shell.
- `rpm_ostree_prepare_candidate` prepares a candidate. `recovery_deploy_ostree`
  transfers it and prepares its files without booting.
- `recovery_boot_ostree` requests one candidate boot. `ssh_command_start` runs
  commands there with explicit arguments, working directory and environment.
- `ssh_command_read`, `ssh_command_wait` and `ssh_command_stop` follow commands.
  Output is saved independently of reading it. Each command has its own timeout;
  runs have no overall deadline. Processes have separate environments but share
  files and services. `cd` and `export` do not carry into another command.
- `recovery_return` requests return and diagnostic upload. `run_results` lists
  received files; `saved_output_read` reads bounded portions.
- Repeat boot keeps persistent edits but receives a new run ID. Plain
  `ostree admin unlock` edits disappear on reboot. `recovery_discard_ostree`
  removes retained files explicitly before a fresh deployment.

Use the returned quiet-wait command through your local process tool for long
preparations. MCP waits are bounded; do not spend tokens repeatedly asking for
unchanged progress. Commands continue through controller/network loss. After a
lost reply, retrieve existing work rather than blindly replaying it. Reboot does
not restart commands automatically. Check boot identity and mode before acting.

Candidate networking reads saved connection settings; experimental changes do not
update recovery settings. Save reusable software changes in a prepared candidate,
not just a retained disk. The agent owns reasoning, code edits and patch generation.

The [mockups](mockups/agent-experiment.md) also show intended later features,
including interactive terminal input, SFTP transfer and live package helpers.
Check the actual tool list instead of assuming those examples are available.
See [installation and observed checks](controller-installation.md) for build status.

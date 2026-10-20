# Agent entry point

Use plain English and the [glossary](glossary.md) when explaining work.
This rewrite branch has no runnable application yet. These are intended interfaces.

The user configures the local MCP URL and installation token once in their agent
client. The user's agent then accesses the authorized target by its friendly
reference; it does not need the target's private pairing credential.
Context names the applicable tools: `ssh_command_start` for target commands,
`sftp_upload` for files, `rpm_overlay_install` for supported live package changes,
and `rpm_ostree_prepare_candidate` for saved candidates.
Use the returned next-tool names; do not assume a universal `run` or `deploy`.
Read investigation context, inspect the target and try to reproduce the problem.
Run commands, send probes, install packages or modify candidate software live.
Use incremental build/deploy helpers when larger changes need them. Prepared tests
remain available, but every inspection does not require a new experiment or reboot.

Tools return bounded output, saved results and instructions for the next action.
Start long commands, do other work, then wait for completion, an agreed deadline
or an error without periodic polling. Output is saved independently of reading it.
Commands have separate process environments but share the target's files and
services. Set working directory and environment explicitly; `cd` and `export` do
not carry into another command. Do not replay uncertain work after a lost
connection. Observe the current target boot and mode. The agent owns reasoning,
code edits and patch generation; never edit Quirkbench to perform an investigation.
Ordinary target commands continue through controller/network loss and keep output
locally. Reconnect to retrieve their status; interactive terminal reattachment is
not guaranteed. The target enforces independent command timeouts. There is no
overall run deadline; waiting or compiling elsewhere does not expire the run.

The human authorizes scope once. Internal disks and persistent firmware remain
excluded. Pause stops new work and allows bounded work to recover/upload. Force
stop is best effort. See the [interactive design](designs/interactive-debugging.md)
and [agent mockup](mockups/agent-experiment.md). The first candidate uses rpm-ostree. Live changes belong to one run; prepare a
named candidate from explicit packages/files to retain them. Recovery selects
the next boot. Candidate networking starts from read-only saved connection
settings; experimental network changes do not update recovery settings.

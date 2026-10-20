# Interactive debugging through MCP

Quirkbench makes the user's agent faster at investigating low-level Linux problems.
The normal interaction is inspect, try something, read what happened and choose
what to do next on the same running target. A prepared experiment is one useful
tool in that interaction, not a requirement before every probe.

## What should make Quirkbench valuable

Provide useful MCP tools, a debugging-ready base system, quick improvised tests
and the highest practical chance of returning to a connected system after failure.
Build and transfer acceleration support that experience. Measure successful
reconnections and surviving diagnostics across representative failures; a configured
watchdog is not proof of recovery. Keep the limits honest without abandoning useful
software measures.

The base should include a curated set of command-line inspection, tracing, profiling,
debugger and evidence tools with working instructions. Add specialized tools easily.
Do not bundle every possible tool or enable intrusive tracing by default.

That base is a convenient starting point, not the only test environment. Distro
package versions, configuration, services, permissions and boot behavior can be the
bug. Support tailored candidates close to where it was reported, with only the
needed control connection and evidence support added. Missing reproduction is not
evidence of a fix. Keep the dedicated recovery system separate from that candidate.

## Agreed direction

- The user's agent runs on the controller computer first. Quirkbench supplies a
  local MCP server. Remote agent access can follow later.
- The human authorizes an investigation once. The agent can execute commands,
  use interactive debuggers, transfer files and make live changes in the candidate,
  including root commands, within that scope. No fixed list of permitted probes.
- Live changes include package installation and replacement of installed OS files.
  Recovery exposes inspection tools only. Its local human terminal remains unrestricted.
- Keep practical internal-disk and firmware protections. Ordinary permissions and
  clear instructions help cooperative code; a modified kernel cannot be contained.
- Build/deployment helpers accelerate larger changes. Do not require an RPM, full
  experiment description or reboot just to run a new diagnostic program.

Run one foreground controller process. It serves MCP, HTTPS and incoming target
connections. Use the official Python MCP SDK with Streamable HTTP on a local-only
address; the agent connects by URL. No separate MCP process or controller daemon.
Human CLI and TUI use the same application behavior. Builds and commands may run
as child processes without becoming separate services. Reconnecting an agent must
not restart commands or runs.

Use [mechanism-specific tool names](controller-api.md#name-mcp-tools-by-mechanism):
SSH command tools, SFTP file transfer and rpm-ostree preparation. Different
mechanisms can coexist as separate tools without changing what an old name means.

## Command behavior

The agent can start a command, receive output, send more input, stop it and retrieve
saved output. Support overlapping work: a stress program may run while another
command inspects logs. Normal commands return directly when short. Longer commands
return a command ID with bounded output and a quiet wait action. An interactive
terminal is available when needed by a debugger, rather than required for every call.

### Long commands and quiet waiting

Starting a long command returns promptly. It keeps running independently of an
MCP call, so the agent can do other work. Save output to files as it arrives;
do not hold the only copy in memory or require the agent to read it.
Save completion and received output before announcing that results are ready.
Retain them across agent disconnects and controller restarts. After a crash,
report partial output and unknown outcomes honestly; recent unwritten bytes may
be lost. A recording failure, including a full disk, is an error to report.

Ordinary commands survive a network disconnect or controller restart. The target
owns their processes, deadlines and saved output; it records request IDs before
launch and returns existing work on retry. If launch outcome is uncertain, report
that rather than launching again. Reconnect queries this record and retrieves
missing output. The target enforces each command timeout without the controller.
Interactive terminal reattachment is not guaranteed initially; report a lost
session and stop its process where possible. No process survives a target reboot.

The desired experience is one wait, then an agent wake-up only when:

- A command finishes and its saved output is ready.
- The agreed command deadline or wait deadline is reached.
- A failure needs attention, including a confirmed reboot that ends the command.

Wait for one command or the first relevant event among several. Do not wake the
model for new log lines, keepalives or periodic “still running” replies. Reading
partial output and interacting with a terminal remain explicit agent actions.
A wait deadline ends waiting; a command deadline stops the command.
Neither should be confused with a network request timeout.

Use a pending tool response or supported MCP task completion handling. Whether
an event can wake an idle agent depends on its client; MCP alone does not guarantee
that behavior. Prefer client-managed waiting/reconnection without model calls.
Document and test the supported client behavior; do not silently replace this
goal with repeated model polling or add another controller service to achieve it.
Saved results remain retrievable if a notification is missed.

Connection loss is an error worth reporting, not proof that the process exited.
Once a new boot is confirmed, end waits for commands from the old boot promptly.
Return available saved output and say whether recovery is still uploading more
evidence; do not wait indefinitely for files that may never arrive.

### Parallel commands and their environment

Each execution starts a separate process with its own SSH command channel.
Accept explicit arguments, working directory, environment overrides and user.
Show the resolved working directory, user and base environment in context; make
the effective environment available without inserting it into every reply.
Do not inherit the controller computer's environment accidentally.
Report the candidate's actual user/session context and required services. A probe
using a desktop service needs that session's environment; root access is not a
substitute for it. Report unavailable services instead of inventing working defaults.

Direct argument execution does not expand shell expressions or load shell startup
files. The agent can explicitly launch a shell when wanted. A `cd` or `export`
in one command cannot change another command's environment. Input sent to an
existing interactive shell does preserve that shell's state until it exits.
Identify that session by its command ID; never silently reuse it for a new command.

Separate processes are not isolated machines: they share the candidate's files,
services, devices and resources. Package installs and service restarts affect
other commands. Document this directly in tool instructions. Allow concurrent
inspection and stress programs; serialize only operations that require exclusive
access, and explain any wait.

Record the run, boot, command, working directory, privilege choice, timing, exit
status and available output. A request ID prevents a repeated tool call after a
lost reply from executing twice. A connection loss leaves execution status
unconfirmed until the target reports it, not automatically failed or retried.
Identify a changed boot before accepting further input.
Do not replay terminal input after an uncertain disconnect. No command can remain
attached to a process across a reboot.

Each command has its own agent-selected execution timeout within human limits.
There is no overall run deadline and no sum of command durations. A healthy target
stays available while the agent thinks or builds elsewhere. Wait timeouts return
control without stopping work. Boot/reconnect waits are bounded and report failure.
Separately, the [candidate startup deadline](recovery.md#boot-run-return) returns a
new candidate to recovery if it never connects. Neither limits the run's lifetime
after connection succeeds. No arbitrary memory cap.

Investigation pause stops new commands/builds/deployments, lets active bounded work
finish, then returns to recovery; if idle, return immediately. `ssh_command_stop`
stops one command and its children without ending the run. Explicit `recovery_return`
stops remaining run commands, allows bounded output flushing and reboots. Force stop
attempts immediate interruption. Read access and uploads remain available while paused.
After the first connection, controller loss neither cancels commands nor reboots
a healthy target. Commands
keep their timeouts; once done the target waits for reconnection. Any future policy
for prolonged disconnection must be explicit, not a hidden overall deadline.

Live changes are attributed to the current run and reported as changes to its
starting candidate. Keep command history, explicit transferred files and useful
outputs. Do not claim to capture every effect of arbitrary root commands. A repeatable
result uses retained source/build output and an explicit test or saved procedure;
Quirkbench does not automatically turn a shell history into a reliable experiment.

## Connection and persistence

Use the [controller connections](controller-api.md): reverse-direction AsyncSSH
for commands and file transfer, HTTPS for evidence. The target initiates both;
the controller needs reachable HTTPS and SSH ports served by its single process.
Quirkbench connects SSH requests to local processes and real terminals, handling
input, resize, stop and exit. Reconnection identifies the boot and queries saved
command records before accepting more work.

Keep target control separate from evidence receipt. Use one saved command-output
format for live reads, reconnect retrieval and recovery upload, with copies on the
target and controller. Reuse completed file segments and the evidence upload helper;
do not build independent logging systems that must reconcile each other.
After a crash, recovery uploads surviving files and deletes confirmed copies.
Interactive command records and uploaded files contribute to the same run's results.
A live-output stream is not proof that all requested evidence has arrived. Commands
may name expected evidence files relative to the evidence directory. Upload completed
files using the same HTTPS helper: command completion triggers upload of its
declared evidence files; completed output segments may upload earlier. Recovery
retries surviving files after interruption. A command result
reports whether those files reached the controller; `https_evidence_wait` waits for
named files and returns local paths only after receipt, or reports missing files/errors.
Command completion alone must not imply evidence receipt.

## First implementation

Use [rpm-ostree candidate preparation](candidate-deployment.md), fresh mutable files
per run and explicit persistence. Recovery owns boot selection. Begin with the
kernel and PipeWire build paths; other OS support can reuse the same command,
connection and evidence code. Start with latest stable Bazzite, record its exact
version when preparing the baseline and disclose differences from the reported environment. Available debugging tools do not substitute for reproducing the problem.

## Judge acceleration

Compare the same agent/model, target, problem, starting software and success criterion
with and without Quirkbench. Use competent ordinary Linux tooling as the comparison.
For a completed task, report speedup as comparison elapsed time divided by Quirkbench
elapsed time. Report failed/incomplete attempts too; never count a quick failure as
speedup. Use repeated tasks before drawing broad conclusions.

Also report time to first useful evidence, agent tool calls/tokens, build/transfer/
reboot time and human interventions. Include cold setup and warm repeat work
separately. Include an ad-hoc probe, a live userspace change and a kernel replacement.
Use those results to choose tools, not the appeal of a particular architecture.
These measurements are occasional development work, not costly routine CI.

References: [MCP connections](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports),
[MCP tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools),
[MCP task support](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks),
[AsyncSSH reverse-direction connections](https://asyncssh.readthedocs.io/en/latest/#reverse-direction-example).

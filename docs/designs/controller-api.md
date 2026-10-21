# Controller connections

MCP provides [interactive debugging](interactive-debugging.md). Targets connect
outward through SSH for commands and HTTPS for pairing, evidence and downloads.

## One controller process

`quirkbench controller run` runs one foreground process with three interfaces:

- Local MCP through the official Python SDK's Streamable HTTP support. The user's
  agent connects to its URL for context, commands, file transfer, build/deploy
  helpers, progress and results. Keep this listener local-only in the first version.
- HTTPS for pairing, evidence and software downloads.
- AsyncSSH `listen_reverse()` for connections initiated by targets.

These share application behavior and one asynchronous event loop. Bind MCP to
loopback, enable the SDK's Host/Origin checks and require one local installation
access token, configured once in the agent client. No per-investigation tokens.
This protects local access without claiming isolation from malicious same-user code.
Human CLI and monitor use the same application behavior. No separate MCP server
process, SSH daemon, tunnel or service manager.
Child processes perform commands and builds; blocking work must not stall connections.
An MCP client disconnect does not end the investigation.

Investigation context links to the agent guide and explains deployment readiness
separately from controller preparation. The guide offers possible routes, not a
mandatory experiment sequence. Supported operations, not matching source hashes,
determine whether an older recovery can be used.

For checkout development, packaging uses loaded Python instructions and files on
disk. If the runtime file list, package builder or candidate builder changed after import, preparation
must fail before reusing or building software with mixed instructions. The operator
coordinates a controller restart; investigation agents do not restart it routinely.
Ordinary target file edits with an unchanged packaging mechanism can be rebuilt.
This check neither updates recovery nor blocks existing target command access.
Avoid editing packaging sources during a build; this is not a hot-reload system.

MCP, SSH and HTTPS use the same run records. Target mode, boot and command status
come through the SSH connection, including after reconnection.

Recovery and candidate code use AsyncSSH `connect_reverse()`. The target initiates
the network connection and acts as the SSH server; the controller acts as the SSH
client. Commands, interactive terminals and file transfers use standard SSH.
Pairing records the target's SSH public key and supplies the controller's public
key. Keep the target's private pairing identity stable across reboots; use library
key checks to recognize it. A friendly target reference and human authorization
select agent access, without exposing the target secret to the agent. Boot IDs
identify restarts, not new pairing identities. No per-run keys. Candidate root can
read its credentials and shared USB storage; a reported recovery identity is not
proof against malicious candidate software.

The target connects SSH requests to local processes and terminals. Quirkbench
restricts recovery diagnostics to inspection and checks investigation authorization.
Recovery also performs the explicit candidate deployment and boot actions.
Scope guides the agent and tool actions; it cannot contain arbitrary candidate
root code.
Reconnect identifies the current boot and queries target-owned command records.
Ordinary commands continue through controller/network loss; old-boot commands
and lost interactive terminals are not automatically recreated.

Command responses distinguish target mode, boot, run, command ID, output and exit
status. Repeated start requests identify existing work; uncertain commands or input
are not replayed. A disconnected target cannot receive queued commands intended for
its previous boot. Current recovery/candidate status and control availability are
visible to both the human and agent.

Deployment, boot, discard and return admission commits the retry request, any run
changes and the dispatchable status in one SQLite transaction. Dispatch happens
after commit. An interrupted transaction leaves none of those changes; a committed
request with an uncertain dispatch is reconciled against target records, never
automatically replayed. Deployment includes the expected recovery boot ID; a
restarted recovery rejects it before writing candidate files. Older recovery images
without this check need an update before deployment.

Target status reports `target_source` (installed runtime identity),
`recovery_base` (selected package-set ID and platform), and `operations` supported
in the current mode. The controller checks operations before dispatch, including
file transfer; recovery still validates requests. Different source or base IDs
are diagnostic facts, not grounds for rejecting an otherwise supported operation.
A missing operation produces an update instruction; do not guess support from a
version string or add a compatibility framework.

## Name MCP tools by mechanism

Callable MCP actions are **tools**. A skill is a guide for an agent using tools,
not another name for a callable action. Name both for the workflow or mechanism
they actually support.

Use names such as these in the [mockup](../mockups/agent-experiment.md):

| Tool | Specific purpose |
| --- | --- |
| `investigation_context` | Read the investigation and discover applicable tools. |
| `ssh_recovery_inspect` | Read bounded hardware, network, storage or boot-log output through recovery's SSH connection. |
| `ssh_command_start/read/wait/stop` | Separate tools for starting, reading, waiting for and stopping SSH commands. Interactive input is later work. |
| `sftp_upload` / `sftp_download` | Transfer a probe to the candidate or retrieve completed experiment evidence. |
| `rpm_ostree_prepare_candidate` | Prepare an OSTree candidate from an explicit base and changes. |
| `recovery_cancel_ostree` | Request cancellation by deployment work ID; wait for target-confirmed stop and cleanup. |
| `recovery_deploy_ostree` | Transfer and prepare fresh files without rebooting. |
| `recovery_boot_ostree` | Boot a prepared or retained deployment once. |
| `recovery_discard_ostree` | Discard retained files after diagnostics receipt. |
| `run_results` / `saved_output_read` | Find received results and read bounded portions of saved files. |
| `recovery_return` | Stop remaining commands, flush available output and return to recovery. |
| `controller_work_wait` | Wait for already accepted controller work; never select or start a mechanism. |

Avoid universal `run`, `execute`, `build` or `deploy` tools with a growing mechanism
selector. A different execution or deployment mechanism gets a separately named
tool, which may coexist with the existing one. Do not change an old name to invoke
something materially different. Ordinary implementation fixes do not need new names;
use `ssh`, not the library name or version, when SSH behavior is what matters.

Context and tool descriptions identify the recommended tools for the current
candidate, their effects, persistence and next actions. If mechanism-specific skill
guides are added, use names such as `rpm-ostree-experiments`, not `run-experiment`.
No plugin registry, compatibility aliases or stored-record migrations follow from
this naming choice. Tools share the existing run records and application code.
Retire unused mechanisms rather than accumulating permanent alternatives.
Human CLI commands may remain task-oriented.

## Recovery tools in milestone 1

`ssh_recovery_inspect` takes a target, a named view and bounded options such as
the number of log lines. Recovery maps these to fixed diagnostic commands and
returns their actual output, exit status and saved-output location. Do not accept
shell text, arbitrary executables or arbitrary file paths. Reuse ordinary command
and output handling rather than building a second diagnostic framework.

Recovery exposes this tool alongside candidate preparation, deployment, progress
and return controls. The first candidate command tools are `ssh_command_start`,
`ssh_command_read`, `ssh_command_wait` and `ssh_command_stop`. They execute explicit
arguments as root, with separate working directories and environment overrides,
individual timeouts and durable output. They do not accept commands in recovery.
Candidate file transfer is available through `sftp_upload` and `sftp_download`.
Interactive input, user-session selection and live package helpers remain later work. No OS preparation code belongs in these command tools.

## Keep simple HTTP services

Pairing, confirmed evidence receipt and recovery reports remain ordinary HTTPS.
The paired credential identifies the caller, so target routes need no target ID.
These routes handle file transfer and pairing; SSH carries interactive control.
Human CLI and monitor requests use a local controller interface, not target routes.

| Method and path | Purpose |
| --- | --- |
| `POST /api/v1/pair` | Bind an invitation to a target. |
| `PUT /api/v1/runs/{run}/evidence/{path}` | Store one completed evidence file. |
| `PUT /api/v1/runs/{run}/result` | Report the run outcome and expected evidence files; the controller tracks which files it received. |
| `PUT /api/v1/reports/{report}` | Store recovery's own debugging report without an investigation. |

OSTree content uses normal read-only repository HTTPS paths on this controller. All paired targets
may read shared candidate content; the assignment determines what runs. Reading
content is not approval to execute it. Run uploads must belong to the calling target.

Pairing uses the controller address/trust and single-use, non-expiring invitation
prepared on USB. Save target credentials before the request; an identical retry
after a lost reply returns the same binding. No image/evidence prerequisites or
credential-renewal machinery. Revoked credentials fail clearly.

Stream evidence into ordinary temporary files, finish storage before acknowledgement,
then let recovery delete its copy. Identical retries succeed without duplicating
results. Retry an interrupted file from the beginning. Keep unfinished uploads
separate, handle full disks and reject filenames escaping the run directory.
Use standard HTTP libraries, not a second transfer framework.

Keep a local file/receipt list until the run's final report is acknowledged. Live
command output already stored on the controller and later USB evidence belong to
the same run; neither alone proves collection is complete. Reports distinguish
finished, interrupted and unknown outcomes, and identify missing evidence. Closed
log segments may upload during execution. Never rewrite a completed file with a
growing log. Recovery reports use the same upload helper but separate storage.

Use clear normal HTTP errors and simple JSON. The controller writes its shared
SQLite records as described in [internals](internals.md); no client generator is
needed. MCP tool inputs follow the
[mockup](../mockups/agent-experiment.md); SSH carries target commands and streams.
Exercise both through the real Python application in fast integration tests.

For long controller work, responses include a copyable `investigation wait` CLI
command. It waits quietly and prints once, allowing an agent's process tool to
wait without repeated model calls. Short MCP waits remain available for clients
with short request deadlines; automatic wake-up depends on the agent client's
process/task support. A timed-out wait never cancels the work.

Run summaries distinguish evidence pending from missing output. Uploaded command
records update controller status. Software identity, target capabilities and
candidate base/target-tools identity are visible before deployment.

Live evidence: `sftp_download(run_id, file)` reads a completed file relative to
`QUIRKBENCH_EVIDENCE_DIR` through the existing SSH connection. It returns a local
controller path, byte size and digest. Final recovery upload still controls target
cleanup. Recovery advertises `kernel-args` before optional debugging arguments may
be sent; candidates advertise `sftp-download` before live evidence may be read.
Command reads distinguish `connection-lost` from a confirmed `interrupted` boot.
`saving_pending` keeps quiet waits open until output and completion are saved.

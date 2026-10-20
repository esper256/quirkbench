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

These share application behavior and one asynchronous event loop. Use the MCP
SDK's normal local-access checks. Human CLI and monitor use the same application
behavior. No separate MCP server process, SSH daemon, tunnel or service manager.
Child processes perform commands and builds; blocking work must not stall connections.
An MCP client disconnect does not end the investigation.

MCP, SSH and HTTPS use the same run records. Target mode, boot and command status
come through the SSH connection, including after reconnection.

Recovery and candidate code use AsyncSSH `connect_reverse()`. The target initiates
the network connection and acts as the SSH server; the controller acts as the SSH
client. Commands, interactive terminals and file transfers use standard SSH.
Pairing records the target's SSH public key and supplies the controller's public
key. Use library key checks to associate a connection with the paired target.
No inbound target connection or custom remote-command protocol is needed.

The target connects SSH requests to local processes and terminals, enforcing
recovery's inspection-only access or the candidate's investigation limits.
Reconnect identifies the current boot and queries target-owned command records.
Ordinary commands continue through controller/network loss; old-boot commands
and lost interactive terminals are not automatically recreated.

Command responses distinguish target mode, boot, run, command ID, output and exit
status. Repeated start requests identify existing work; uncertain commands or input
are not replayed. A disconnected target cannot receive queued commands intended for
its previous boot. Current recovery/candidate status and control availability are
visible to both the human and agent.

## Name MCP tools by mechanism

Callable MCP actions are **tools**. A skill is a guide for an agent using tools,
not another name for a callable action. Name both for the workflow or mechanism
they actually support.

Use names such as these in the [mockup](../mockups/agent-experiment.md):

| Tool | Specific purpose |
| --- | --- |
| `investigation_context` | Read the investigation and discover applicable tools. |
| `ssh_command_start/input/read/wait/stop` | Separate tools for starting, interacting with, waiting for and stopping SSH commands. |
| `sftp_upload` | Transfer a file over the existing SSH connection. |
| `controller_build_script_start` | Execute the supplied build script on the controller computer. |
| `rpm_ostree_install_live` | Apply supported RPM changes to the running candidate. |
| `rpm_ostree_prepare_candidate` | Prepare an OSTree candidate from an explicit base and changes. |
| `recovery_boot_ostree` | Have recovery deploy an OSTree candidate and select its one-time boot. |
| `recovery_return` | Stop remaining commands, flush available output and return to recovery. |
| `https_evidence_wait` | Wait for named evidence files to reach the controller through HTTPS upload. |
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

Use clear normal HTTP errors and simple JSON. No database format or client generator
is required by this design. MCP tool inputs follow the
[mockup](../mockups/agent-experiment.md); SSH carries target commands and streams.
Exercise both through the real Python application in fast integration tests.

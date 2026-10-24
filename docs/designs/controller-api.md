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
client. Commands and file transfers use standard SSH; interactive commands use the same command records and SSH connection.
Pairing records the recovery drive's SSH public key and supplies the controller's
public key. The drive also reports the current computer's stable hardware identity.
The controller records computers separately and routes commands by computer, never
by the drive's previous target name. Investigations stay attached to their computer.
A drive reconnects without another invitation; moving it registers or selects the
new computer before SSH commands or evidence handling. Boot IDs identify restarts.
No per-run keys. Candidate root can
read its credentials and shared USB storage; a reported recovery identity is not
proof against malicious candidate software.

The target connects SSH requests to local processes. Quirkbench
restricts recovery diagnostics to inspection and checks investigation authorization.
Recovery also performs the explicit candidate deployment and boot actions.
Scope guides the agent and tool actions; it cannot contain arbitrary candidate
root code.
Reconnect identifies the current boot and queries target-owned command records.
Ordinary commands continue through controller/network loss; old-boot commands
and lost interactive terminals are not automatically recreated.

A disconnected target cannot receive queued commands intended for its previous
boot. Repeated starts identify existing work; uncertain commands are not replayed.

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
| `investigation_context` | Read current investigation and target context; MCP discovery lists tools. |
| `ssh_recovery_inspect` | Read bounded hardware, network, storage or boot-log output through recovery's SSH connection. |
| `ssh_terminal_input` | Send bounded input or resize one interactive command. |
| `ssh_target_notify` | Show a human request, with an optional single short chime. |
| `ssh_command_start/read/wait/stop` | Separate tools for starting, reading, waiting for and stopping SSH commands. Optional interactive commands accept `ssh_terminal_input`; input is never replayed. |
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

## Recovery inspection

`ssh_recovery_inspect` accepts a target, named view and bounded options. It maps
these to fixed diagnostic commands and returns actual output, exit status and a
saved-file reference. It accepts no shell text or arbitrary paths. `services`
means failed units; `emergency` avoids external programs and shared-storage reads.
Candidate commands remain separate and run as root. See the
[agent guide](../agent-guide.md) for available workflows.

## Keep simple HTTP services

Pairing, confirmed evidence receipt and recovery reports remain ordinary HTTPS.
The paired credential identifies the caller, so target routes need no target ID.
These routes handle file transfer and pairing; SSH carries interactive control.
Human CLI and monitor requests use a local controller interface, not target routes.

| Method and path | Purpose |
| --- | --- |
| `POST /api/v1/pair` | Pair the drive and identify its current computer. |
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

Keep a local file/receipt list until the run's final report is acknowledged. Live downloads and later USB evidence belong to the same run; neither alone
proves collection is complete. Reports distinguish
finished, interrupted and unknown outcomes, and identify missing evidence. Do not overwrite a completed evidence file with a growing log. Recovery reports use the same upload helper but separate storage.

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

Live evidence: `sftp_download(run_id, evidence_file)` reads a completed file relative to
`QUIRKBENCH_EVIDENCE_DIR` through the existing SSH connection. It returns a local
controller path, byte size and digest. Final recovery upload still controls target
cleanup. Recovery advertises `kernel-args` before optional debugging arguments may
be sent; candidates advertise `sftp-download` before live evidence may be read.

## Preparation follow-ups and investigation controls

`rpm_ostree_prepare_candidate(after_prepare="stop"|"deploy"|"boot")` and
`recovery_deploy_ostree(boot=false|true)` reuse the existing operations.
Defaults stop for inspection. Work results expose each phase and its work ID.
Save intent and stable child request IDs before dispatch. Failed or uncertain
phases stop; restart never automatically continues physical work. Cancellation
stops future phases and cancels owned preparation/deployment, without undoing boot.
The generic sequence tools and adapters are removed.

`investigation_control` offers pause, force-stop and resume through the same service
as the CLI. Pause stops new work, finishes bounded work, returns to recovery and
waits for evidence. Force stop also cancels preparation/deployment and stops commands.
Resume permits new work; it does not boot or continue interrupted follow-ups. Read access and
uploads remain available. A disconnected or frozen target can leave stopping or
return pending. Recorded command stop requests are sent after reconnection.

`investigation_problem_update` maintains named problems with optional references
and open/resolved/not-reproduced status. No mandatory reasoning form. Timeline
records controller-observed work stages, connections and evidence receipt with
one clock; external agent builds are not timed automatically.

Every successful MCP tool returns both JSON text and structured content. File and
work references stay outside bounded output previews. Quiet-wait commands use the
controller's Python executable, so a missing shell PATH entry does not break them.

Repeated controls preserve completed outcomes. Pause cannot downgrade a force stop.

## MCP arguments and results

- `ssh_command_start` accepts exactly one of `argv` or `script`. Scripts use
  `shell -c` (default `/bin/sh`), not a persistent session. `timeout_seconds`
  bounds execution; `wait_seconds` briefly waits for a result (default 0.5,
  maximum 5). Zero returns immediately. Changing the wait does not change retry
  identity. Long commands retain the same read/wait/stop tools.
- Work and command replies expose `complete`, `outcome` and `needs_attention`.
  Preserve detailed `status`. Lost connection means unknown outcome; an interrupted
  boot can be complete with an unknown outcome. Evidence receipt remains separate.
  `saving_pending` keeps command completion false. Output uses byte `offset`,
  `next_offset`, `output_bytes` and `more` in live and saved reads. `next_tool`
  is an optional suggestion; it is null when no continuation is needed.
- File arguments identify their location: upload `controller_path` and `target_name`,
  download `evidence_file`, read `saved_file`. Replies use `target_path`,
  `controller_path`, and controller-relative `saved_file`. Preparation `log_file`
  is also controller-relative and directly readable. Paths remain locations.
- Context reports current work and a guide pointer. `investigation_runs` pages
  through summaries, newest first. A stable `deployment_id` names retained files;
  each boot attempt has its own `run_id`. Boot replies return both. No new storage
  service or deployment mechanism is introduced.
- Schemas publish fixed choices, existing bounds and common result fields.
- `controller_mcp_activity` reports call counts, elapsed time, successful response
  text bytes, errors and unchanged status/output positions. It includes read/wait
  calls and schema rejections, excludes internal polling, and persists in existing
  events. Normal investigation timelines exclude this traffic. No command text,
  environment or output copy is stored. Omit investigation to see unscoped calls.
  Calls are recorded on return/cancellation; abrupt process loss can omit an
  in-flight call. An unchanged reply is a measurement, not proof of wasted effort.

The controller TUI derives readable action summaries from existing work records.
`ssh_command_start.description` is optional human-readable intent, not command
verification. Keep arguments and environment values out of automatic previews;
retain all MCP timing data while filtering routine successful polls from the TUI.

Recovery `console-snapshot` support adds inspection view `console`: the last rendered
TUI frame, bounded and stored in RAM. Report age, dimensions and truncation. Do not
capture external programs or claim a stale frame is live. Use existing report storage
and output-reading tools; add no remote terminal service.

## 1.2 evidence and interaction

`run_results` defaults to a compact receipt/count summary. Its files, commands,
transfers and collections views page details; only the files view lists file paths.
`saved_output_read` supports a bounded tail. Inventory distinguishes process exit
codes from partial reads; collection records identify environment and boot when known.

`ssh_login_sessions` inspects logind sessions and inhibitors. Optional
`ssh_command_start.login_session` selects an active user explicitly, using the
user-manager display environment, normal process credentials and separate user-owned
work/evidence directories. No account creation, display switching or sleep changes.
Ambiguous graphical sessions require explicit ordinary commands instead.

`sftp_upload_batch` uses existing single-file publication, stops at a failure and
reports each outcome. Earlier files stay published. It is not an atomic batch.


`ssh_human_request` records prepare/ready/cancel states with existing controller
storage. The agent decides readiness. The human answers through the target menu or
controller CLI; the same authenticated controller handles both. Responses after
cancellation or a changed boot are late. Requests do not switch desktops, replay
commands, or interpret measurements. A new request ID rearms a test.


## Incident capture, observation and optional desktop control

`ssh_incident_configure/capture/status` use an ordinary bounded candidate command,
existing evidence files and SFTP. The controller saves the incident identity,
run/boot, origin and collector configuration before dispatch. Initial snapshot
and manifest remain separate from later analysis. Receipt does not imply successful
collection. Partial originals remain eligible for final recovery upload. Lost
replies reconcile existing commands; collectors are never automatically replayed.
The human CLI uses the same capture service. Export includes incident records.

`ssh_command_wait(reconnect=true)` observes the same command within the caller's
wait deadline. Malformed SSH replies and saved metadata return observation errors
with phase and identities, never experiment-success claims. A new boot ends old
process observation.

`ssh_human_request(login_session=...)` optionally delivers through libnotify on
the selected user's session bus. Persist each delivery attempt before dispatch;
only explicit redelivery tries again. Readiness/acknowledgment remain separate.
No notification daemon of our own; unavailable desktops keep CLI/menu fallback.

`ssh_keep_awake` starts, observes or releases an ordinary bounded command running
`systemd-inhibit --what=idle:sleep`. Its suspend-aware deadline is target-owned.
Only its own inhibitor is released. No persistent power settings or global timeout.

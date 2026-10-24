# Interactive debugging through MCP

Quirkbench accelerates the user's agent: inspect a target, try a probe, read the
result and choose the next action. A prepared procedure is optional. Keep the
candidate close enough to the reported environment to reproduce the problem;
a convenient debugging base that cannot reproduce it is not a fix.

The [agent guide](../agent-guide.md) describes available use. The
[controller API](controller-api.md) owns tool and connection details; the
[candidate design](candidate-deployment.md) owns deployment and persistence.

## Command behavior

Candidate commands run as root within the authorized investigation. Recovery
provides named inspections and managed deployment actions, not an agent shell.
The human recovery terminal remains available.

Accept exact arguments or an explicit script, working directory, environment,
execution timeout and output limit. Shell expressions expand only in a shell.
Commands have separate environments but share files, services, devices and resources.
There is no fixed parallel-command cap. Serialize only actions needing exclusive
access. A desktop service may require a real user session; root is not a substitute.

A short command can finish in its initial call. Longer commands return an ID for
read, wait or stop. The target owns execution, deadlines and output files; agent
or controller disconnection does not stop them. A retry ID identifies the same
work, not permission to run it again. Confirmed reboot ends old processes.
A disconnected target leaves their outcome unknown until observed again.

Save output in bounded chunks, independently of reads. The default output limit
is 16 MiB; report recording limits and failures. `saving_pending` means output is
not safely saved yet. Completion, success and final evidence receipt are different
facts. Use the same command records for live reads and recovered results.

### Long commands and quiet waiting

Execution deadlines stop commands; wait deadlines only return control. There is
no summed run deadline. A healthy target can wait while the agent thinks or builds.
Neither command deadlines nor network waits wake a suspended computer.

MCP waits last at most 50 seconds and return on completion, connection loss or
attention-worthy failure. For long controller work, return a copyable quiet CLI
wait command. The agent's process tool can wait without repeated model calls.
Automatic wake-up depends on the client; do not promise MCP notifications that
it cannot deliver. Prefer completion waits over repeated status reads.

The longer-term goal is one wait for relevant completion or failure, including
across several commands. Interactive commands use a terminal and `ssh_terminal_input` for text, control characters
and size changes. Input is never replayed. Target-process restart cannot reattach
a terminal. Existing-session selection uses explicit logind properties and reports inspection failures. These features must not change ordinary
command semantics or require another persistent service.

Investigation pause finishes bounded work and returns to recovery. Force stop
attempts interruption; stopping one command does not end the run. Read access and
uploads remain available. Resume permits work without replaying it. See
[operation controls](controller-api.md#ordered-operations-and-investigation-controls).

## Files and evidence

Uploads go to `/var/tmp/quirkbench`; commands default to `/var/tmp`. Those files
survive repeat boots until deployment discard. Plain `ostree admin unlock` edits
are temporary. Each boot has a new run ID even when persistent files are retained.

Commands and startup probes receive `QUIRKBENCH_RUN_ID`,
`QUIRKBENCH_EVIDENCE_DIR` and `QUIRKBENCH_WORK_DIR`. Evidence is separate from
credentials and command records. Scratch files are deleted after final upload.
Finish evidence files with an atomic rename before live SFTP download. A changed
file or boot prevents publishing an incomplete download. Live downloads do not
delete target evidence; recovery deletes it only after confirmed final receipt.

Record actions and transferred files without claiming to capture every effect of
root commands. The agent retains source, build output and procedures for repeatable
results. Quirkbench does not turn shell history into a reliable experiment.

## Keep control responsive under load

The root connection service uses `Nice=-5` and `CPUSchedulingResetOnFork=yes`;
experiment commands and external build/deployment processes start at normal priority.
Do not add real-time scheduling, CPU reservations or blanket throttling.

Keep file operations off the connection event loop, with bounded chunks and
backpressure. Stop the process group promptly even if saving output stalls; report
unfinished saving separately. Stops allow `stop_grace_seconds` (default 10 seconds)
for the process group to flush its own output before SIGKILL; collection continues
during that interval. These choices help under load but cannot overcome
kernel hangs, memory exhaustion, saturated networks or failed devices.

## Judge acceleration

Compare the same agent, target, problem and starting software with competent
ordinary Linux tools. Measure time to useful evidence, build/transfer/reboot time,
tool calls, tokens where available and human interventions. Separate cold setup
from repeated work. Report unsuccessful attempts too; a quick failure is not speedup.

`investigation_timeline` records controller-observed work. `controller_mcp_activity`
records tool traffic, including reads and waits. Unchanged output is a clue to
avoidable calls, not proof of waste. External agent builds and token usage are not
automatically measured. Use occasional representative investigations, not expensive
routine benchmarks, to choose the next acceleration.

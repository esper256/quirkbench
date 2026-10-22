# Agent entry point

Use plain English and the [glossary](glossary.md). The human configures the MCP URL
and installation token once. Address the authorized target by its friendly name;
you do not need its private pairing credential.

Read investigation context and the available MCP tool descriptions first. They
report current target mode, work, results and next actions. Inspect the target and
try to reproduce the problem before changing software. Never edit Quirkbench to
perform an investigation. Internal disks and persistent firmware are excluded.

## Choosing a starting point

These are suggested routes, not a required sequence. Use investigation context and
actual tool availability to choose the next useful action. A debug kernel, fresh
candidate, or recovery update is not required for every investigation.

- In recovery, inspect available diagnostics. To run arbitrary diagnostic programs,
  boot a suitable prepared candidate; recovery's inspection-only interface does not
  restrict candidate command access.
- On a suitable running candidate, inspect and run bounded probes with SSH commands;
  use `sftp_upload` and `sftp_download` for files. No new deployment is needed merely
  to collect more data.
- When a different OS or persistent software change is needed, prepare a candidate
  with the desired RPM inputs, wait for success, then deploy and explicitly boot it.
  If replacing retained run files, first retrieve needed results and wait for final
  diagnostics receipt, then discard those files before deployment. Preparation can
  finish while the previous deployment remains available.
- To reuse retained persistent changes, boot that deployment again instead of
  discarding it. Temporary `ostree admin unlock` changes do not survive reboot.

| Observed state | Meaning for the next action |
| --- | --- |
| Different controller/target source identities, or a newer unflashed image | Not by itself a blocker. Check support for the requested operation. |
| `ready_for_deploy: false` | Fresh deployment is unavailable now. Controller preparation is independent; inspect target mode and current work. |
| Retained run | Boot it again, or discard before fresh deployment after required diagnostics receipt. It does not prevent preparation. |
| Recovery has no unrestricted shell | Use named inspections there; ordinary commands and file transfer run on candidates. |
| Packaging or candidate build instructions changed in a running controller's checkout | Preparation reports a maintenance error. Ask the operator to coordinate a restart; do not interrupt another investigation. Existing inspection/command access is unaffected. |

A prepared candidate records a fixed OSTree commit. With the current RPM mechanism,
provide kernel replacements as RPMs before preparation; raw kernel files are not
accepted package inputs. Later edits to input files do not change the prepared
candidate. Make another preparation request for different software.

## Available workflow

- `ssh_recovery_inspect` reads recovery diagnostics; `emergency` avoids launching
  external programs. Recovery provides no unrestricted agent shell.
- `rpm_ostree_prepare_candidate` prepares a candidate. `recovery_deploy_ostree`
  transfers it and prepares its files without booting.
- `recovery_boot_ostree` requests one candidate boot. `ssh_command_start` runs
  commands there with explicit arguments or a script, working directory and environment.
- `ssh_command_read`, `ssh_command_wait` and `ssh_command_stop` follow commands.
  Output is saved independently of reading it. Each command has its own timeout;
  runs have no overall deadline. Processes have separate environments but share
  files and services. `cd` and `export` do not carry into another command.
- Set `interactive: true` when a command needs a terminal. `ssh_terminal_input`
  sends a line, control character or size change. Continue reading the same command
  ID. Do not retry input after a lost reply: delivery is uncertain.
- `stop_grace_seconds` gives a collector time to finish after TERM (default 10).
  Output continues saving until exit; SIGKILL follows if it does not stop.
- `ssh_target_notify` can display a human request. A chime is optional, short and
  may be unavailable; it does not change volume or imply the human has responded.
- `sftp_upload` transfers candidate files; `sftp_download` retrieves completed
  experiment evidence while the candidate runs.
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

The [agent examples](mockups/agent-experiment.md) illustrate these tools.
Interactive terminal input, user-session selection and dedicated live-package
helpers are not available. Other mockups may show future screens; check the running
controller's tool list. See the [1.1 changelog](../CHANGELOG.md) for recent changes.
See [installation and observed checks](controller-installation.md) for build status.

## Evidence files

Candidate commands and startup probes receive `QUIRKBENCH_RUN_ID`,
`QUIRKBENCH_EVIDENCE_DIR` and `QUIRKBENCH_WORK_DIR`. Put completed measurements in
the evidence directory, not beside credentials or Quirkbench's command records.
Close and rename a finished file into place before `sftp_download`. Live downloads
keep the target copy; recovery deletes it only after final confirmed upload.
Scratch files in the work directory are removed then too. Keep reusable source and
build output on the controller. Startup probes can save evidence before networking.

## Optional accelerators

Use `investigation_problem_update` to track several symptoms together, and
`investigation_timeline` to inspect observed delays. Neither requires a particular
reasoning format. `ssh_candidate_inventory` reports tools, kernel/build files and
storage without implying that every listed facility works. Internal disks are
intentionally excluded from experiments; unmounted does not prove never modified.
The recovery `services` view lists **failed units**, not every service.

Candidate preparation includes a small debugging toolkit by default; set
`debugging_tools: false` when environment fidelity calls for omitting it.
`repository_packages` selects packages from the candidate's repositories.
`packages` and `replacements` still accept controller-local RPM files. Preparation
records the requested changes and resolved packages. No tracing or stress tool is
started automatically. Context reports whether Podman is visible to the running
controller, separately from target availability and source versions.

When the next actions are already known, `controller_sequence_start` can perform
an explicit ordered list of supported tools. Start collectors and probes separately,
then explicitly wait or stop them. Each command keeps its own deadline. Sequences
stop on errors; inspect progress before explicitly resuming after a controller
restart. Ordinary individual tool calls remain equally supported.

Humans can use `quirkbench investigation pause NAME`, `force-stop NAME`, and
`resume NAME`; agents use `investigation_control`. Pause finishes bounded work and
returns to recovery. Resume permits work but never boots or resumes a sequence.
Review `run_results` collection status: received files do not establish that all
requested diagnostics were available or that the experiment succeeded.

### Optional load utility

Candidates with the debugging toolkit offer `quirkbench-load --help`. Use it only
when a synthetic workload helps the investigation. `--seconds` is required;
`--gpu` adds headless Vulkan rendering and `--audio rain` adds generated rain-like
playback. Both are off by default. Playback needs the human's authorization and a
working ALSA device; `--audio-device` selects it without changing mixer settings.
The seed makes workload choices repeatable, not execution timing or failures.
GPU output identifies the device. A missing device, failed or skipped render, or
playback failure is an error. The tool timestamps reported audio underruns, other playback messages, delayed
feeding and late CPU frames, plus failures and counts. JSON lines go to stderr,
which Quirkbench retains with command output and collects after return. Use
`ssh_command_read` or `ssh_command_wait`, then retained run results. UTC observation
time and monotonic time help correlate other logs; observation time can lag the
fault under load. `--late-ms` adjusts the CPU frame tolerance. Delayed feeding can
also mean ordinary backpressure. These observations do not prove an audible glitch
or certify graphics correctness; an unreported fault is still possible.

### Efficient tool calls

`ssh_command_start` accepts either `argv` or `script`, not both. Scripts run in a
new shell (`/bin/sh` by default); exports do not persist into another command.
The execution deadline is `timeout_seconds`. Its optional `wait_seconds` (default
0.5) returns short-command output directly; zero starts without waiting. Long
commands return the same command ID to read, wait or stop. `ssh_command_wait`
waits at most 50 seconds; a wait deadline never stops the process. Reuse the request ID
when retrying unchanged work; changing only the brief wait does not launch again.

Use `complete` and `outcome`, not a status word alone. `complete` does not promise
that evidence arrived. `more` and `next_offset` continue output without rereading
it. File inputs are `controller_path`/`target_name` for upload, `evidence_file`
for download (relative to `QUIRKBENCH_EVIDENCE_DIR`) and `saved_file`
for saved-output reads. Use returned references
without guessing path conversions.

Context stays focused on current work. `investigation_runs` pages through history;
`deployment_id` identifies retained files while each boot gets its own `run_id`.
Optional `controller_mcp_activity` shows call counts, time, response size and
unchanged reads/waits. It is useful for evaluating a workflow, not a required step.

Deployment accepts an explicit `selinux` policy. Default `disabled` differs from
normal Bazzite; use inventory to check the actual environment. Enabled policy needs
correct candidate labels and may affect boot. It is not changed by ordinary commands.

After discarding retained deployment files, `recovery_prune_ostree_cache` explicitly
removes downloaded candidate content from idle recovery. Use it when space is
needed; later deployments will download that content again. It keeps diagnostics
and connection settings. Follow completion with `controller_work_wait`.

## Optional rsync root images

`rpm_ostree_prepare_candidate` accepts explicit `backend="rsync"`; omitted means
OSTree. Use `recovery_deploy_candidate`, `recovery_boot_candidate`,
`recovery_discard_candidate` and `recovery_cancel_deployment` for either backend.
The candidate's recorded backend selects deployment; unsupported Recovery rejects
it without fallback. Rsync needs its executable on the controller and an updated
Recovery image. No persistent rsync service is used.

Rsync runs retain an ordinary writable root, including `/usr`, across repeat boots.
They use additional image storage and full copies on ext4; live rpm-ostree
transactions are unavailable. Interrupted image preparation may require explicit
discard of retained files before retry. See [independent rsync deployment](designs/rsync-deployment.md)
for guarantees, costs and outstanding full Bazzite boot qualification.

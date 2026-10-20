# Agent debugging through MCP

> **EXAMPLE ONLY — intended design, not implemented tools or a frozen API.**
> Tool names, programs and the audio problem are illustrative. MCP tools are shown
> as calls and results, not as shell commands. This shows the first rpm-ostree
> implementation; the example does not limit future candidate OS support.

## Discover the investigation

```text
Tool: investigation_context
Input: {"investigation":"microphone-dropout"}

Investigation: microphone-dropout · Authorized
Problem: Microphone capture stops after several minutes.
Target: lab-laptop · Connected · Recovery system
Allowed: Kernel, audio services and diagnostic programs
Excluded: Internal disks, persistent firmware, edits to Quirkbench

Workspace: /home/sam/quirkbench-work/microphone-dropout
Build guides: BUILD.md, DEPLOY.md (relative to Workspace)
BUILD.md: Matching Bazzite package sources, patches, pinned build container and incremental commands.
DEPLOY.md: Required RPMs and symbols, compatible package versions and file layout.
Prepare and edit source using these guides. Local build scripts are optional helpers.
Relative controller paths use Workspace. SFTP destinations use the target working
directory; evidence names use its evidence directory.
Target facts: target.json
Candidate OS: Bazzite stable · rpm-ostree
Selected upstream reference: ghcr.io/ublue-os/bazzite:stable
Baseline not yet prepared. Preparation records the exact version in baseline.json.
Available now: target information, logs, rpm_ostree_prepare_candidate

Tools for this target:
  recovery_boot_ostree          Deploy an OSTree candidate and boot it once
  ssh_command_start            Execute on the connected candidate through SSH
  ssh_command_input            Send input to an existing SSH command
  ssh_command_read             Read saved SSH command output
  ssh_command_wait             Quietly wait for SSH commands
  ssh_command_stop             Stop one command without ending the run
  https_evidence_wait          Wait for named files to arrive on the controller
  sftp_upload                  Copy a file over the existing SSH connection
  controller_build_script_start  Run the supplied local build script
  rpm_overlay_install          Install RPMs in this run’s temporary writable layer
  rpm_ostree_prepare_candidate  Prepare a saved candidate from selected changes
  recovery_return              End this run and return to recovery
  controller_work_wait         Quietly wait for accepted controller work

Prepare the selected baseline, then boot it to inspect live behavior.
Within a run you can execute commands, send input to interactive programs,
copy files, install packages and change software. Use root when needed within the authorized scope.
Choose an execution timeout for each command. Commands have independent timers.
The run stays available while you think or build. There is no overall run deadline.
On a new boot, startup_connect_timeout_seconds returns the candidate to recovery
if it never connects. That timer ends at the first successful connection.
A wait timeout returns control without cancelling the command.
ssh_command_stop stops one command. recovery_return stops remaining commands,
flushes available output and returns to recovery. Pause lets active commands finish.
Tools return bounded output and a reference to complete saved output.
Declared evidence files upload when their command finishes; completed log segments
may upload sooner. Command records and output are saved on USB.
Long commands run independently. Start other commands while they run, then use
ssh_command_wait for completion, an agreed deadline or an error. No periodic polling.
Use ssh_command_read only when you want to inspect output before completion.

Each ssh_command_start starts a new process. Set cwd and env for that command.
Arguments run directly; launch a shell explicitly for shell syntax.
cd and export affect only that process and its children. An interactive shell
keeps its state only when you send input to that same command ID.
All commands share this target's files, services and devices.
Ordinary commands keep running during controller/network loss and save output
on the target. Reconnect reads their status without rerunning them. Interactive
terminal reattachment is not guaranteed. The target enforces command timeouts.
Each new run starts with fresh configuration, application data and home directories.
Live edits last for this run. Prepare a named candidate with selected packages/files
to retain changes. Package helpers report whether a fresh boot is needed.
Recovery controls the next boot. Do not change boot entries from the candidate.
```

## Prepare the first baseline

```text
Tool: rpm_ostree_prepare_candidate
Input: {"investigation":"microphone-dropout","base":"ghcr.io/ublue-os/bazzite:stable","name":"baseline","request_id":"prepare-baseline"}
Output: {"work_id":"baseline-preparation","status":"preparing","stage":"Resolve Bazzite stable, download and add target RPM and debugging tools","next_tool":"controller_work_wait"}

Tool: controller_work_wait
Input: {"work_id":"baseline-preparation"}
Output: {"status":"ready","candidate_id":"baseline-1","base_record":"baseline.json","instructions":"Exact Bazzite version saved for subsequent runs. Baseline prepared. Use recovery_boot_ostree to start it."}
```

## Boot once, investigate interactively

```text
Tool: recovery_boot_ostree
Input: {"investigation":"microphone-dropout","candidate":"baseline-1","startup_connect_timeout_seconds":300,"request_id":"baseline-1"}
Output: {"work_id":"work-1","status":"starting","next_tool":"controller_work_wait"}

Tool: controller_work_wait
Input: {"work_id":"work-1"}
Output: {
  "status":"ready",
  "run_id":"run-1",
  "boot_id":"boot-7",
  "software":"baseline-1",
  "control":"interactive",
  "mutable_files":"Fresh from baseline-1 defaults",
  "network":"Connected using saved recovery settings, supplied read-only",
  "live_changes":{"packages":"supported RPM transactions in a temporary writable layer","direct_files":"temporary writable layer; unsupported package transactions require a prepared candidate"},
  "tools_available":["journalctl","dmesg","gdb","strace","perf","pw-cli","arecord"],
  "evidence_directory":"/run/quirkbench/evidence",
  "working_directory":"/run/quirkbench/work",
  "evidence_storage":"Shared USB storage mounted at evidence_directory",
  "user_session":{"user":"quirkbench","uid":1000,"status":"ready","services":["pipewire","pipewire-pulse","wireplumber"]},
  "command_defaults":{"user":"quirkbench","cwd":"/run/quirkbench/work","env":{"PATH":"/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin","HOME":"/home/quirkbench","LANG":"C.UTF-8","XDG_RUNTIME_DIR":"/run/user/1000","DBUS_SESSION_BUS_ADDRESS":"unix:path=/run/user/1000/bus","QUIRKBENCH_EVIDENCE_DIR":"/run/quirkbench/evidence","QUIRKBENCH_WORK_DIR":"/run/quirkbench/work"}},
  "instructions":"The default user/session is ready for this audio investigation. as_root requests a separate root command, not a change of the audio session. Commands receive QUIRKBENCH_EVIDENCE_DIR and QUIRKBENCH_WORK_DIR. Evidence survives run cleanup. Working files and OS edits are disposable. Network edits affect this run only. Leave recovery settings, boot selection and Quirkbench files alone."
}

Tool: ssh_command_start
Input: {"run_id":"run-1","argv":["pw-cli","info","all"],"timeout_seconds":10,"request_id":"inspect-1"}
Output: {"command_id":"command-1","status":"finished","exit_code":0,"output":"…device and stream information…","output_file":"results/run-1/commands/command-1.txt"}
```

The agent writes `tests/record.py` locally. It records through the selected audio service, writes the named files to `QUIRKBENCH_EVIDENCE_DIR`, and closes them before exiting.

```text
Tool: sftp_upload
Input: {"run_id":"run-1","source":"tests/record.py","destination":"record.py","request_id":"copy-1"}
Output: {"path":"/run/quirkbench/work/record.py","status":"copied"}

Tool: ssh_command_start
Input: {"run_id":"run-1","argv":["python3","record.py","--seconds","600"],"cwd":"/run/quirkbench/work","env":{"PYTHONUNBUFFERED":"1"},"timeout_seconds":660,"evidence_files":["capture.json","recording.wav"],"request_id":"record-1"}
Output: {"command_id":"command-2","status":"running","output":"Recording microphone…","output_file":"results/run-1/commands/command-2.txt","next_tool":"ssh_command_wait","instructions":"Output is saved while this command runs. Other commands can run now. env overrides the displayed target defaults for this process only."}

Tool: ssh_command_start
Input: {"run_id":"run-1","argv":["journalctl","--user","-u","pipewire.service","--follow","--no-pager"],"timeout_seconds":600,"request_id":"logs-1"}
Output: {"command_id":"command-3","status":"running","output":"…service logs while recording continues…","output_file":"results/run-1/commands/command-3.txt"}

Tool: ssh_command_stop
Input: {"command_id":"command-3","request_id":"stop-logs-1"}
Output: {"status":"stopped","output_file":"results/run-1/commands/command-3.txt","instructions":"Log command stopped. The recording and run continue."}
```

## Improvise without rebooting

```text
Tool: rpm_overlay_install
Input: {"run_id":"run-1","packages":["lsof"],"request_id":"package-1"}
Output: {"status":"installed","method":"RPM installation in temporary writable layer","output":"lsof is available in this run.","persistence":"this run only","boot_selection":"unchanged"}

Tool: ssh_command_start
Input: {"run_id":"run-1","argv":["lsof","/dev/snd/pcmC0D0c"],"as_root":true,"timeout_seconds":10,"request_id":"inspect-2"}
Output: {"command_id":"command-5","status":"finished","exit_code":0,"output":"pipewire 913 quirkbench …"}

Tool: ssh_command_wait
Input: {"command_ids":["command-2"],"wait_timeout_seconds":900}
Output: {"command_id":"command-2","status":"finished","exit_code":1,"output":"Capture stopped after 347 seconds.","output_file":"results/run-1/commands/command-2.txt","output_saved":true,"evidence_upload":"pending","next_tool":"https_evidence_wait"}

Tool: https_evidence_wait
Input: {"run_id":"run-1","files":["capture.json","recording.wav"],"wait_timeout_seconds":900}
Output: {"status":"received","controller_directory":"results/run-1/evidence/","files":["capture.json","recording.wav"]}

Tool: ssh_command_start
Input: {"run_id":"run-1","argv":["gdb","-q","-p","913"],"as_root":true,"terminal":true,"timeout_seconds":120,"request_id":"debug-1"}
Output: {"command_id":"command-6","status":"running","output":"Attaching to process 913…\n(gdb) ","next_tool":"ssh_command_input"}

Tool: ssh_command_input
Input: {"command_id":"command-6","text":"thread apply all bt\n","request_id":"input-1"}
Output: {"status":"running","output":"…thread backtraces…\n(gdb) ","output_file":"results/run-1/commands/command-6.txt","instructions":"Available output only; Quirkbench does not interpret debugger prompts or know when this input has finished."}

Tool: ssh_command_input
Input: {"command_id":"command-6","text":"detach\nquit\n","request_id":"input-2"}
Output: {"status":"finished","exit_code":0,"output":"Detaching…"}
```

The agent reads the received files, follows BUILD.md to prepare matching PipeWire source and edits it locally. The investigation build script below runs the documented build; Quirkbench does not diagnose or fix compilation errors.

## Build and try changed software

```text
Tool: controller_build_script_start
Input: {"investigation":"microphone-dropout","component":"pipewire","request_id":"build-1"}
Output: {"work_id":"work-2","status":"running","command":"./build-pipewire.sh","log":"builds/build-1.log","next_tool":"controller_work_wait"}

Tool: controller_work_wait
Input: {"work_id":"work-2"}
Output: {"status":"finished","exit_code":0,"build_id":"build-1","files":"output/pipewire/","next_tools":["rpm_overlay_install","rpm_ostree_prepare_candidate"],"instructions":"Live installation changes this run. Restart the affected service when ready. A prepared candidate gives a clean repeatable starting point."}

Tool: rpm_overlay_install
Input: {"run_id":"run-1","build_id":"build-1","request_id":"install-1"}
Output: {"status":"installed","software":"baseline-1 with live changes","restart_needed":["pipewire"],"persistence":"this run only","evidence_preserved":true}

Tool: ssh_command_start
Input: {"run_id":"run-1","argv":["systemctl","--user","restart","pipewire.service","pipewire-pulse.service","wireplumber.service"],"timeout_seconds":20,"request_id":"restart-1"}
Output: {"command_id":"command-7","status":"finished","exit_code":0}

Tool: ssh_command_start
Input: {"run_id":"run-1","argv":["python3","/run/quirkbench/work/record.py","--seconds","600","--output-name","after-change"],"timeout_seconds":660,"evidence_files":["after-change.json","after-change.wav"],"request_id":"record-2"}
Output: {"command_id":"command-8","status":"running","output_file":"results/run-1/commands/command-8.txt","next_tool":"ssh_command_wait"}

Tool: ssh_command_wait
Input: {"command_ids":["command-8"],"wait_timeout_seconds":900}
Output: {"command_id":"command-8","status":"finished","exit_code":0,"output":"Recorded 600 seconds without interruption.","output_file":"results/run-1/commands/command-8.txt","output_saved":true,"evidence_upload":"received","controller_directory":"results/run-1/evidence/","files":["after-change.json","after-change.wav"]}
```

The agent uses quiet waiting, inspects the new evidence and decides whether a clean comparison is needed.

### If live installation is unavailable

```text
Tool: rpm_overlay_install
Input: {"run_id":"run-1","build_id":"build-1","request_id":"install-alternative"}
Output: {"status":"reboot_required","changed":false,"reason":"These RPMs require an installation step the live helper cannot perform.","next_tool":"rpm_ostree_prepare_candidate","instructions":"Prepare baseline-1 plus build-1, then return to recovery and boot that candidate. The current run remains available."}
```

## Retain a change for clean repeats

```text
Tool: rpm_ostree_prepare_candidate
Input: {"investigation":"microphone-dropout","base":"baseline-1","build_id":"build-1","name":"pipewire-fix","request_id":"save-fix"}
Output: {"work_id":"work-save","status":"preparing","instructions":"Preparing baseline-1 plus the selected PipeWire RPMs. Live lsof installation, configuration edits and application data are not included."}

Tool: controller_work_wait
Input: {"work_id":"work-save"}
Output: {"status":"ready","candidate_id":"pipewire-fix","instructions":"Start this candidate through recovery for a clean repeat. Each run gets fresh mutable files."}
```

## When a new boot is needed

If a later step needs a kernel change, the agent prepares and edits matching kernel
source using BUILD.md.

```text
Tool: controller_build_script_start
Input: {"investigation":"microphone-dropout","component":"kernel","request_id":"kernel-build-2"}
Output: {"work_id":"kernel-work-2","status":"running","log":"builds/kernel-build-2.log","next_tool":"controller_work_wait"}

Tool: controller_work_wait
Input: {"work_id":"kernel-work-2"}
Output: {"status":"finished","exit_code":0,"build_id":"kernel-build-2","files":"output/kernel/","instructions":"Kernel and matching modules are ready. Prepare a candidate and boot it to test this kernel."}

Tool: rpm_ostree_prepare_candidate
Input: {"investigation":"microphone-dropout","base":"pipewire-fix","build_id":"kernel-build-2","request_id":"candidate-2"}
Output: {"work_id":"work-3","status":"preparing","current_run":"run-1","instructions":"The current run remains available. Wait for the candidate, then finish the run and start it explicitly."}

Tool: controller_work_wait
Input: {"work_id":"work-3"}
Output: {"status":"ready","candidate_id":"candidate-2","reboot_required":true}

Tool: recovery_return
Input: {"run_id":"run-1","request_id":"finish-1"}
Output: {"work_id":"work-4","status":"stopping_commands","instructions":"Stop remaining run commands, flush available output, then reboot to recovery and upload evidence.","next_tool":"controller_work_wait"}

Tool: controller_work_wait
Input: {"work_id":"work-4"}
Output: {"status":"finished","evidence":"received","target":"connected in recovery","results":"results/run-1/","cleanup":"Run files discarded. Prepared candidates, recovery settings and controller results retained."}

Tool: recovery_boot_ostree
Input: {"investigation":"microphone-dropout","candidate":"candidate-2","startup_connect_timeout_seconds":300,"request_id":"run-2"}
Output: {"work_id":"work-5","status":"starting","stage":"Recovery is deploying candidate-2 with fresh mutable files and saved connection settings.","next_tool":"controller_work_wait"}

Tool: controller_work_wait
Input: {"work_id":"work-5"}
Output: {"status":"ready","run_id":"run-2","boot_id":"boot-8","working_directory":"/run/quirkbench/work","instructions":"Fresh run. Upload your local test program again; previous working files were discarded."}

Tool: sftp_upload
Input: {"run_id":"run-2","source":"tests/record.py","destination":"record.py","request_id":"copy-2"}
Output: {"path":"/run/quirkbench/work/record.py","status":"copied"}
```

The agent repeats selected probes using the reported user/session context and creates the investigation patch with its usual coding tools.

## If the target crashes while a command is running

```text
Tool: ssh_command_wait
Input: {"command_ids":["command-9","command-10"],"wait_timeout_seconds":900}
Output: {
  "status":"interrupted",
  "reason":"Target rebooted into recovery. Commands from boot-8 cannot continue.",
  "commands":["command-9","command-10"],
  "target_mode":"recovery",
  "boot_id":"boot-9",
  "saved_output":"results/run-2/commands/",
  "output_complete":false,
  "evidence_upload":"in progress",
  "instructions":"Read the saved output now. Remaining recovery evidence will be reported separately. Neither command was retried."
}
```

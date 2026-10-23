# Milestone 1: recovery inspection and candidate boot

> **EXAMPLE ONLY — illustrative screens and responses, not an exact transcript.** Names, output,
> addresses and timing are illustrative. Any compatible agent can use these tools.

The user completes [setup, recovery build and USB flash](installation-to-patch.md),
boots the target and connects recovery to the controller.

```text
Copying live image to RAM...
Done copying live image to RAM.
```

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ QUIRKBENCH / RECOVERY                                                        │
├────────────────────────┬─────────────────────────────────────────────────────┤
│ MENU                   │ CURRENT STATUS                                      │
│   N  Network           │ Target          lab-target (7c2a91)                 │
│   C  Controller        │ Network         Workshop Wi-Fi                      │
│ > A  Activity          │ Target IP       192.168.1.81                        │
│   L  Logs & reports    │ Controller      Connected                           │
│   I  Target details    │ Last contact    1s ago                              │
│   T  Open terminal     │ Controller URL  https://192.168.1.20:7443           │
│   P  Power             │ Recovery OS     Read-only                           │
│                        │ Data free       21 GiB                              │
│                        │ Current work    Idle                                │
├────────────────────────┴─────────────────────────────────────────────────────┤
│ RECOVERY & MCP ACTIVITY                                           Live log   │
│ 14:31:52  RECOVERY  Shared USB storage ready                                 │
│ 14:31:54  RECOVERY  Connected to Workshop Wi-Fi                              │
│ 14:31:55  RECOVERY  Controller connected                                     │
│ 14:32:01  MCP       Read hardware / received                                 │
│ 14:32:02  MCP       Read hardware / finished                                 │
│ 14:32:04  MCP       Read boot log / finished                                 │
├──────────────────────────────────────────────────────────────────────────────┤
│ [------------] No operation running                                          │
├──────────────────────────────────────────────────────────────────────────────┤
│ ↑↓ Select   Enter Open   A Activity history   T Terminal   Esc Back          │
└──────────────────────────────────────────────────────────────────────────────┘
```

[Activity and connection screens](recovery-console.md).

```console
$ quirkbench investigation start first-boot --target lab-target
What problem do you want to investigate?
> Check recovery diagnostics and boot the Bazzite baseline.

Allow the agent to prepare and boot candidate software on this USB? [Y/n]
Investigation authorized. Internal disks and persistent firmware are excluded.

Add this MCP server to your agent client:
  URL: http://127.0.0.1:7442/mcp
  Authorization header: Bearer <contents of ~/.quirkbench/mcp-token>

Give your agent this request:
  Use Quirkbench for first-boot. Inspect recovery, prepare and boot the baseline,
  then return to recovery and check that it reconnects.
```

The user configures their agent client and gives it the displayed request.

If replacing a retained deployment, the interaction can instead include:

```text
Investigation context: Recovery · Retained run run-old · ready_for_deploy: false
Preparation remains available. Retained files must be discarded before deployment.

rpm_ostree_prepare_candidate → controller_work_wait
Candidate replacement-1 ready. Existing deployment unchanged.

run_results(run-old)
Diagnostics received. Files: /home/sam/.quirkbench/results/run-old/

recovery_discard_ostree(run-old) → controller_work_wait
Retained run files removed. Saved controller results kept.

recovery_deploy_ostree(replacement-1) → controller_work_wait
Deployment ready. Still in recovery; boot when needed.
```

The following example starts without a retained deployment.

```text
Tool: investigation_context
Input: {"investigation":"first-boot"}

Investigation: first-boot · Authorized
Target: lab-target · Recovery · Boot boot-1
Recovery OS: Read-only
Workspace: /home/sam/quirkbench-work/first-boot
Baseline: Not prepared · Bazzite stable

Available tools:
  ssh_recovery_inspect          Read hardware, network, storage or boot logs
  rpm_ostree_prepare_candidate  Prepare a candidate on the controller
  recovery_cancel_ostree        Cancel deployment before boot
  recovery_deploy_ostree        Prepare fresh files; stay in recovery
  recovery_boot_ostree          Boot prepared or retained files once
  recovery_discard_ostree       Remove retained run files
  controller_work_wait         Wait for preparation, boot or return
  recovery_return              Return a connected candidate to recovery
  ssh_command_start            Start a bounded command on the candidate
  ssh_command_read/wait/stop    Read output, wait or stop that command

Agent guide: docs/agent-guide.md
For this example, start with recovery inspection. Other investigations may reuse
an existing candidate. Diagnostic output is saved on the controller.
Fresh deployment readiness does not restrict controller-side preparation.
Recovery has no agent shell, file-upload or package-install interface.
```

```text
Tool: ssh_recovery_inspect
Input: {"target":"lab-target","view":"boot-log","max_lines":80}
Output: {"status":"finished","exit_code":0,"output":"…recovery boot messages…","controller_path":"/home/sam/.quirkbench/reports/lab-target/boot-1/boot.log"}

Tool: ssh_recovery_inspect
Input: {"target":"lab-target","view":"storage"}
Output: {"status":"finished","exit_code":0,"output":"Recovery filesystem: read-only\nShared data: mounted read-write\nNo internal disks mounted","controller_path":"/home/sam/.quirkbench/reports/lab-target/boot-1/storage.txt"}

Tool: ssh_recovery_inspect
Input: {"target":"lab-target","view":"report"}
Output: {"status":"finished","output":"=== processes ===\n…\n=== services ===\n…\n=== Current work and connection ===\n…","controller_path":"/home/sam/.quirkbench/reports/lab-target/boot-1/report.txt"}

Tool: rpm_ostree_prepare_candidate
Input: {"investigation":"first-boot","base":"ghcr.io/ublue-os/bazzite:stable","name":"baseline","request_id":"prepare-1"}
Output: {"work_id":"prepare-1","status":"preparing","stage":"Download selected Bazzite version and add target connection support","next_tool":"controller_work_wait"}

Tool: controller_work_wait
Input: {"work_id":"prepare-1"}
Output: {"status":"ready","candidate_id":"baseline-1","base_record":"baseline.json","instructions":"Selected version recorded. Use recovery_deploy_ostree, then recovery_boot_ostree."}

Tool: recovery_deploy_ostree
Input: {"investigation":"first-boot","candidate":"baseline-1","request_id":"send-1"}
Output: {"work_id":"send-1","run_id":"run-1","status":"starting","stage":"Downloading missing candidate content","quiet_wait_command":"quirkbench --json investigation wait first-boot --work send-1"}

$ quirkbench --json investigation wait first-boot --work send-1
{"status":"ready","run_id":"run-1","target_mode":"recovery"}

Tool: recovery_boot_ostree
Input: {"run_id":"run-1","startup_connect_timeout_seconds":300,"request_id":"boot-1"}
Output: {"work_id":"boot-1","status":"starting","stage":"Reboot requested. Temporary /usr edits do not survive reboot."}

Tool: controller_work_wait
Input: {"work_id":"boot-1"}
Output: {"status":"ready","target":"lab-target","target_mode":"candidate","run_id":"run-1","boot_id":"boot-2","software":"baseline-1","instructions":"Candidate connected. Use ssh_command_start for a diagnostic command, then recovery_return."}
```

```text
Quirkbench · Candidate system

Target        lab-target
Software      baseline-1
Controller    Connected
Next boot     Recovery

Boot check ready. Return through your controller when finished.
```

```text
Tool: ssh_command_start
Input: {"run_id":"run-1","request_id":"inspect-kernel","argv":["uname","-r"],"timeout_seconds":10}
Output: {"command_id":"command-1","status":"accepted","run_id":"run-1","boot_id":"boot-2","output":""}

Tool: ssh_command_wait
Input: {"command_id":"command-1","timeout_seconds":25}
Output: {"command_id":"command-1","status":"finished","exit_code":0,"output":"6.x-example\n","next_offset":12}

Tool: recovery_return
Input: {"run_id":"run-1","request_id":"return-1"}
Output: {"work_id":"return-1","status":"returning","stage":"Save available boot diagnostics and reboot to recovery.","next_tool":"controller_work_wait"}

Tool: controller_work_wait
Input: {"work_id":"return-1"}
Output: {"status":"finished","target":"lab-target","target_mode":"recovery","boot_id":"boot-3","evidence":"received","results":"results/run-1/","instructions":"Recovery reconnected. Persistent run files retained. Read run_results, boot again, or recovery_discard_ostree."}

Tool: ssh_recovery_inspect
Input: {"target":"lab-target","view":"storage"}
Output: {"status":"finished","exit_code":0,"output":"Recovery filesystem: read-only\nShared data: mounted read-write\nNo internal disks mounted","controller_path":"/home/sam/.quirkbench/reports/lab-target/boot-3/storage.txt"}
```

## Cancel a superseded deployment

```text
Tool: recovery_cancel_ostree
Input: {"work_id":"send-1"}
Output: {"work_id":"send-1","status":"cancelling","stage":"Cancellation requested; waiting for target confirmation","next_tool":"controller_work_wait"}

Tool: controller_work_wait
Input: {"work_id":"send-1","timeout_seconds":25}
Output: {"work_id":"send-1","status":"cancelled","stage":"Deployment cancelled; recovery ready"}

Tool: recovery_deploy_ostree
Input: {"investigation":"first-check","candidate":"replacement-1","request_id":"boot-replacement"}
Output: {"work_id":"boot-2","status":"starting","stage":"Deploying candidate on target","next_tool":"controller_work_wait"}
```

## Inspect, repeat or start clean

```text
Tool: run_results
Input: {"run_id":"run-1"}
Output: {"evidence":"received","files":["results/run-1/candidate-journal.log"],"next_tool":"saved_output_read"}

Tool: saved_output_read
Input: {"saved_file":"results/run-1/candidate-journal.log","limit":4096}
Output: {"output":"…","next_offset":4096,"more":true}

Tool: recovery_boot_ostree
Input: {"run_id":"run-1","request_id":"repeat-1"}
Output: {"run_id":"run-2","status":"starting","files":"Persistent edits retained; temporary /usr edits lost on reboot"}
```

After run-2 returns and its diagnostics arrive:

```text
Tool: recovery_discard_ostree
Input: {"run_id":"run-2","request_id":"discard-1"}
Output: {"work_id":"discard-1","status":"starting"}

Tool: controller_work_wait
Input: {"work_id":"discard-1"}
Output: {"status":"finished","stage":"Run files discarded; downloaded candidate content retained"}
```

## Optional independent rsync deployment

Illustrative MCP sequence; complete converted-image boot qualification is pending.
OSTree remains the default when `backend` is omitted.

```text
rpm_ostree_prepare_candidate(investigation="first-boot", request_id="image-1", backend="rsync")
controller_work_wait(work_id=<preparation work ID>)
recovery_deploy_candidate(investigation="first-boot", candidate=<candidate ID>, request_id="deploy-1")
controller_work_wait(work_id=<deployment work ID>)
recovery_boot_candidate(run_id=<prepared run ID>, request_id="boot-1")
controller_work_wait(work_id=<boot work ID>)
recovery_return(run_id=<run ID>, request_id="return-1")
controller_work_wait(work_id=<return work ID>)
recovery_discard_candidate(run_id=<run ID>, request_id="discard-1")
```

Use `recovery_cancel_deployment(work_id=<deployment work ID>)` to stop an active
transfer. Partial files remain available for inspection and retry. Illustrative
userspace update after return, retaining target fstab and boot artifacts:

```text
recovery_deploy_candidate(investigation="first-boot", candidate=<next candidate ID>, request_id="sync-2", destination_run_id=<retained run ID>, rsync_options={"mode":"delta", "checksum":true, "exclude":["etc/fstab"], "update_boot":false})
controller_work_wait(work_id=<sync work ID>)
recovery_boot_candidate(run_id=<new prepared run ID>, request_id="boot-2")
```

For forced full transfer, use `{"mode":"full", "ignore_times":true}`. For a dry
run into a retained experiment use `{"dry_run":true, "update_boot":false}`.
Deletion is opt-in with `delete`; exclusions also protect destination paths.

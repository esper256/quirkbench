# Milestone 1: recovery inspection and candidate boot

> **EXAMPLE ONLY — intended design, not current functionality.** Names, output,
> addresses and timing are illustrative. Any compatible agent can use these tools.

The user completes [setup, recovery build and USB flash](installation-to-patch.md),
boots the target and connects recovery to the controller.

```text
Quirkbench · Recovery system

Target        lab-laptop
Controller    Connected
Recovery OS   Read-only
Shared data   Available

Ready. Start an investigation on your controller computer.

N Network    C Connection    L Logs    T Terminal    P Power
```

```console
$ quirkbench investigation start first-boot --target lab-laptop
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

```text
Tool: investigation_context
Input: {"investigation":"first-boot"}

Investigation: first-boot · Authorized
Target: lab-laptop · Recovery · Boot boot-1
Recovery OS: Read-only
Workspace: /home/sam/quirkbench-work/first-boot
Baseline: Not prepared · Bazzite stable

Available tools:
  ssh_recovery_inspect          Read hardware, network, storage or boot logs
  rpm_ostree_prepare_candidate  Prepare a candidate on the controller
  recovery_boot_ostree          Deploy to shared USB data and boot once
  controller_work_wait         Wait for preparation, boot or return
  recovery_return              Return a connected candidate to recovery

Start with recovery inspection. Diagnostic output is saved on the controller.
Recovery has no agent shell, file-upload or package-install interface.
```

```text
Tool: ssh_recovery_inspect
Input: {"target":"lab-laptop","view":"boot-log","max_lines":80}
Output: {"status":"finished","exit_code":0,"output":"…recovery boot messages…","output_file":"/home/sam/.quirkbench/reports/lab-laptop/boot-1/boot.log"}

Tool: ssh_recovery_inspect
Input: {"target":"lab-laptop","view":"storage"}
Output: {"status":"finished","exit_code":0,"output":"Recovery filesystem: read-only\nShared data: mounted read-write\nNo internal disks mounted","output_file":"/home/sam/.quirkbench/reports/lab-laptop/boot-1/storage.txt"}

Tool: rpm_ostree_prepare_candidate
Input: {"investigation":"first-boot","base":"ghcr.io/ublue-os/bazzite:stable","name":"baseline","request_id":"prepare-1"}
Output: {"work_id":"prepare-1","status":"preparing","stage":"Download selected Bazzite version and add target connection support","next_tool":"controller_work_wait"}

Tool: controller_work_wait
Input: {"work_id":"prepare-1"}
Output: {"status":"ready","candidate_id":"baseline-1","base_record":"baseline.json","instructions":"Selected version recorded. Use recovery_boot_ostree to deploy and boot it."}

Tool: recovery_boot_ostree
Input: {"investigation":"first-boot","candidate":"baseline-1","startup_connect_timeout_seconds":300,"request_id":"boot-1"}
Output: {"work_id":"boot-1","status":"starting","stage":"Recovery is downloading candidate content and preparing fresh run files.","next_tool":"controller_work_wait"}

Tool: controller_work_wait
Input: {"work_id":"boot-1"}
Output: {"status":"ready","target":"lab-laptop","target_mode":"candidate","run_id":"run-1","boot_id":"boot-2","software":"baseline-1","instructions":"Candidate connected. Use recovery_return to finish the boot check."}
```

```text
Quirkbench · Candidate system

Target        lab-laptop
Software      baseline-1
Controller    Connected
Next boot     Recovery

Boot check ready. Return through your controller when finished.
```

```text
Tool: recovery_return
Input: {"run_id":"run-1","request_id":"return-1"}
Output: {"work_id":"return-1","status":"returning","stage":"Save available boot diagnostics and reboot to recovery.","next_tool":"controller_work_wait"}

Tool: controller_work_wait
Input: {"work_id":"return-1"}
Output: {"status":"finished","target":"lab-laptop","target_mode":"recovery","boot_id":"boot-3","evidence":"received","results":"results/run-1/","instructions":"Recovery reconnected. Pairing and network settings retained. Read the results or inspect recovery again."}

Tool: ssh_recovery_inspect
Input: {"target":"lab-laptop","view":"storage"}
Output: {"status":"finished","exit_code":0,"output":"Recovery filesystem: read-only\nShared data: mounted read-write\nNo internal disks mounted","output_file":"/home/sam/.quirkbench/reports/lab-laptop/boot-3/storage.txt"}
```

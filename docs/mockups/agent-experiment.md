# Live investigation through MCP

**Illustrative example, not a restriction on agents, hardware, operating systems or
investigation goals. Paths and IDs below stand for values returned by the tools.**

```text
Tool: investigation_context
Input: {"investigation":"intermittent-fault"}

Output includes:
{"investigation":"intermittent-fault","mode":"candidate",
 "current_run":{"run_id":"run-1","deployment_id":"run-1","candidate_id":"candidate-1"},
 "agent_guide":"/home/sam/dev/quirkbench/docs/agent-guide.md",
 "working_directory":"/var/tmp","history_tool":"investigation_runs"}
```

The agent reads the returned guide, inspects the environment, prepares a bounded collector and a probe using
its usual tools, and builds changed software if the findings call for it.

```text
Tool: sftp_upload
Input: {"run_id":"run-1","controller_path":"/home/sam/investigation/collector.py","target_name":"collector.py"}
Output: {"target_path":"/var/tmp/quirkbench/collector.py","next_tool":"ssh_command_start"}

Tool: ssh_command_start
Input: {"run_id":"run-1","request_id":"collect-1","argv":["python3","/var/tmp/quirkbench/collector.py"],"timeout_seconds":300}
Output: {"command_id":"command-1","status":"running"}

Tool: sftp_upload
Input: {"run_id":"run-1","controller_path":"/home/sam/investigation/probe","target_name":"probe"}
Output: {"target_path":"/var/tmp/quirkbench/probe","next_tool":"ssh_command_start"}

Tool: ssh_command_start
Input: {"run_id":"run-1","request_id":"probe-1","argv":["sh","-c","chmod +x /var/tmp/quirkbench/probe && /var/tmp/quirkbench/probe"],"timeout_seconds":120}
Output: {"command_id":"command-2","status":"running"}

Tool: ssh_command_wait
Input: {"command_id":"command-2","timeout_seconds":50}
Output: {"command_id":"command-2","status":"finished","exit_code":0,"output":"Probe complete. Saved trace.bin in QUIRKBENCH_EVIDENCE_DIR.\n","saving_pending":false}

Tool: sftp_download
Input: {"run_id":"run-1","evidence_file":"trace.bin"}
Output: {"controller_path":"/home/sam/.quirkbench/results/run-1/downloads/file-1/trace.bin","size":4096,"sha256":"<digest>","instructions":"Local copy ready. Target evidence remains until final recovery upload is confirmed."}
```

The probe closes its output and renames the completed file into place before the
agent downloads it. The agent examines the binary locally and chooses the next test.

```text
Tool: ssh_command_stop
Input: {"command_id":"command-1"}
Output: {"status":"stopping","saving_pending":true,"message":"Stop requested; waiting for output saving and cleanup."}

Tool: ssh_command_wait
Input: {"command_id":"command-1","timeout_seconds":50}
Output: {"status":"stopped","saving_pending":false}

Tool: recovery_return
Input: {"run_id":"run-1","request_id":"return-1"}
Output: {"work_id":"return-1","status":"starting"}

Tool: controller_work_wait
Input: {"work_id":"return-1","timeout_seconds":50}
Output: {"status":"finished","results":"/home/sam/.quirkbench/results/run-1"}
```

If a change requires a new boot, the agent builds matching RPMs and an optional
startup probe with ordinary build tools. The probe receives the same run environment
variables before networking starts; its standard output goes to the boot journal.

```text
Tool: rpm_ostree_prepare_candidate
Input: {"investigation":"intermittent-fault","request_id":"prepare-2","name":"instrumented","replacements":["/home/sam/build/changed-component.rpm"],"startup_probe":"/home/sam/build/early-probe","startup_probe_timeout_seconds":60}
Output: {"work_id":"prepare-2","status":"preparing"}

Tool: controller_work_wait
Input: {"work_id":"prepare-2","timeout_seconds":50}
Output: {"status":"ready","candidate_id":"candidate-2"}

Tool: recovery_discard_ostree
Input: {"run_id":"run-1","request_id":"discard-1"}
Output: {"work_id":"discard-1","status":"starting"}

Tool: controller_work_wait
Input: {"work_id":"discard-1","timeout_seconds":50}
Output: {"status":"finished"}

Tool: recovery_deploy_ostree
Input: {"investigation":"intermittent-fault","candidate":"candidate-2","request_id":"deploy-2","kernel_args":["ignore_loglevel","log_buf_len=8M"]}
Output: {"work_id":"deploy-2","status":"starting"}

Tool: controller_work_wait
Input: {"work_id":"deploy-2","timeout_seconds":50}
Output: {"status":"ready","run_id":"run-2"}

Tool: recovery_boot_ostree
Input: {"run_id":"run-2","request_id":"boot-2"}
Output: {"work_id":"boot-2","status":"starting"}

Tool: controller_work_wait
Input: {"work_id":"boot-2","timeout_seconds":50}
Output: {"status":"ready","run_id":"run-2","target_mode":"candidate"}
```

The agent repeats selected measurements and writes the investigation patch using its
own coding tools. A completed command alone does not establish a fix.

## Optional ordered operations

**Another example, not a required investigation sequence.**

```text
Tool: controller_sequence_start
Input: {
  "investigation":"intermittent-fault", "request_id":"prepare-and-boot-1",
  "steps":[
    {"name":"prepare", "tool":"rpm_ostree_prepare_candidate",
     "arguments":{"name":"baseline","repository_packages":["perf"]}},
    {"name":"deploy", "tool":"recovery_deploy_ostree",
     "arguments":{"candidate":{"from_step":"prepare","field":"candidate_id"}}},
    {"name":"boot", "tool":"recovery_boot_ostree",
     "arguments":{"run_id":{"from_step":"deploy","field":"run_id"}}}
  ]
}
Output: {"work_id":"sequence-1","status":"running","next_tool":"controller_work_wait",
 "quiet_wait_command":"/usr/bin/python3 -m quirkbench.cli --data-dir /home/sam/.quirkbench --json investigation wait intermittent-fault --work sequence-1 --timeout 3600"}

Tool: controller_work_wait
Input: {"work_id":"sequence-1","timeout_seconds":50}
Output: {"work_id":"sequence-1","status":"finished","stage":"All ordered operations finished",
 "steps":[{"name":"prepare","status":"finished","result":{"candidate_id":"candidate-1"}},
          {"name":"deploy","status":"finished","result":{"run_id":"run-1"}},
          {"name":"boot","status":"finished","result":{"run_id":"run-1","target_mode":"candidate"}}]}

Tool: ssh_candidate_inventory
Input: {"run_id":"run-1"}
Output: {"run_id":"run-1","boot_id":"boot-1","output":"OS, kernel and build files...",
 "output_truncated":true,"saved_file":"results/run-1/inventory-boot-1.txt","next_tool":"saved_output_read"}

$ quirkbench investigation pause intermittent-fault
Investigation status: pausing

$ quirkbench investigation show intermittent-fault
Investigation status: paused
Mode: recovery

$ quirkbench investigation resume intermittent-fault
Investigation status: active
Mode: recovery
```

## Optional synthetic load

The agent may use the bundled utility instead of writing a probe. This example
adds headless graphics load; it does not require a desktop or enable sound.

```text
Tool: ssh_command_start
Input: {"run_id":"run-1","request_id":"load-1","argv":["quirkbench-load","--seconds","60","--gpu","--seed","72"],"timeout_seconds":75}
Output: {"command_id":"command-5","status":"running"}

Tool: ssh_command_wait
Input: {"command_id":"command-5","timeout_seconds":50}
Output: {"command_id":"command-5","status":"running","output":"Load started: seconds=60 seed=72 audio=off gpu=True\nVulkan device 1: <reported GPU> (Integrated GPU); offscreen rendering\nElapsed 5s: {'frames': 290, 'gpu_batches': 12, 'audio_bytes': 0}\n"}
```

```text
Tool: ssh_command_read
Input: {"command_id":"command-5"}
Output includes a JSON line when a CPU frame is late:
{"event":"late_frame","observed_utc":"2026-10-21T23:30:02+00:00","monotonic_ns":5210000000,"run_id":"run-1","late_ms":84.2,"expected_monotonic_ns":5125800000,"interpretation":"Scheduling or CPU delay; not proof of a graphics or audio glitch"}
```

## Short command and optional call review

```text
Tool: ssh_command_start
Input: {"run_id":"run-1","request_id":"inspect-kernel","script":"uname -r; cat /proc/cmdline","timeout_seconds":10,"wait_seconds":1}
Output: {"command_id":"command-6","status":"finished","complete":true,"outcome":"succeeded","needs_attention":false,"output":"<kernel and boot arguments>\n","more":false,"next_tool":null}

Tool: investigation_runs
Input: {"investigation":"intermittent-fault","limit":10}
Output: {"runs":[{"run_id":"run-2","deployment_id":"run-1","candidate_id":"candidate-1","status":"running","boot_id":"boot-2","evidence_received":false}],"more":false,"next_cursor":"run-2"}

Tool: controller_mcp_activity
Input: {"investigation":"intermittent-fault","limit":10}
Output includes totals: [{"tool":"ssh_command_wait","calls":3,"elapsed_seconds":25.2,"response_text_bytes":1800,"unchanged":1}]
```

## Optional interactive command

```text
Tool: ssh_command_start
Input: {"run_id":"run-2","request_id":"inspect-interactively","argv":["python3","-i"],"interactive":true,"timeout_seconds":120}
Output: {"command_id":"command-7","status":"running","complete":false,"output":">>> "}

Tool: ssh_terminal_input
Input: {"command_id":"command-7","text":"print(2 + 2)\n","rows":24,"columns":100}
Output: {"command_id":"command-7","status":"running","complete":false}

Tool: ssh_command_read
Input: {"command_id":"command-7"}
Output includes: "4"

Tool: ssh_target_notify
Input: {"investigation":"intermittent-fault","message":"Please reconnect the external test peripheral, then tell your agent.","chime":false}
Output: {"displayed":true,"chime":"not-requested"}
```

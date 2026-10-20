# How the parts work together

Revisable [mockup](../mockups/README.md). Quirkbench combines the binary, controller
and MCP interface here. One controller process serves MCP, HTTPS and target SSH
connections. The target can run a clean fallback or an investigation's
candidate. Recovery remains separate from the writable candidate.
The first candidate uses rpm-ostree; see [candidate deployment](candidate-deployment.md).

## Setup and connection

```mermaid
sequenceDiagram
    actor Human
    participant Quirkbench
    participant USB as USB drive
    participant Target
    Human->>Quirkbench: Configure and run the foreground controller
    Human->>Quirkbench: Build recovery image and flash selected USB
    Quirkbench->>USB: Write bootable software, settings and shared storage
    Human->>Target: Boot USB and connect network
    Target->>Quirkbench: Pair over HTTPS, then open reverse-direction SSH connection
    Quirkbench-->>Human: Target ready and local MCP URL/token-file instructions
    Human->>Quirkbench: Describe problem and authorize investigation
```

## Milestone 1: recovery inspection, deployment and return

```mermaid
sequenceDiagram
    participant Agent as User's agent
    participant Quirkbench
    participant Target
    Agent->>Quirkbench: ssh_recovery_inspect
    Quirkbench->>Target: Run selected recovery diagnostic through SSH
    Target-->>Quirkbench: Command output and exit status
    Quirkbench-->>Agent: Bounded output and saved file
    Agent->>Quirkbench: rpm_ostree_prepare_candidate, then controller_work_wait
    Quirkbench-->>Agent: Prepared baseline with target connection support
    Agent->>Quirkbench: recovery_boot_ostree
    Quirkbench->>Target: Supply candidate content to recovery
    Target->>Target: Deploy on shared data, prepare fresh files and select one-time boot
    Target->>Quirkbench: Candidate reconnects with same identity and new boot ID
    Quirkbench-->>Agent: Candidate connected
    Agent->>Quirkbench: recovery_return
    Quirkbench->>Target: Save diagnostics and reboot
    Target->>Quirkbench: Recovery reconnects and uploads surviving diagnostics
    Quirkbench-->>Agent: Recovery available and results ready
```

Recovery OS files stay read-only; managed actions write shared data and boot selection.
An initial candidate connection failure follows the startup fallback in the recovery design.

## Later milestones: interactive investigation

```mermaid
sequenceDiagram
    participant Agent as User's agent
    participant Quirkbench
    participant Target
    Agent->>Quirkbench: investigation_context, then rpm_ostree_prepare_candidate if needed
    Quirkbench-->>Agent: Baseline preparation progress, then completed candidate
    Agent->>Quirkbench: recovery_boot_ostree
    Quirkbench->>Target: Supply prepared OSTree candidate to recovery
    Target->>Target: Deploy with fresh run files and read-only connection settings
    Target->>Target: Recovery selects one candidate boot, keeping recovery the default
    Note over Quirkbench,Target: Return to recovery if candidate startup never connects before its deadline
    Target->>Quirkbench: Connect with stable paired identity and new boot ID
    Quirkbench-->>Agent: Current software, available tools and instructions
    loop Inspect and improvise on the same boot
        Agent->>Quirkbench: Start command, send input or upload a probe
        Quirkbench->>Target: Execute through SSH, save output independently
        Quirkbench-->>Agent: Command ID, available output and next action
        Note over Agent,Target: Other commands can run concurrently; each has its own timeout
        Agent->>Quirkbench: ssh_command_wait
        Note over Agent,Quirkbench: Wait without periodic model calls
        Target-->>Quirkbench: Command output and completion, or interruption
        Quirkbench-->>Agent: Saved output and status
        Agent->>Quirkbench: https_evidence_wait for named files
        Target->>Quirkbench: Upload completed evidence files
        Quirkbench-->>Agent: Received files or missing evidence reported
    end
    opt Software change
        Agent->>Agent: Edit source
        Agent->>Quirkbench: controller_build_script_start
        Quirkbench-->>Agent: Build output or errors to fix
        Agent->>Quirkbench: rpm_overlay_install or rpm_ostree_prepare_candidate
        Quirkbench->>Target: Transfer changes, new candidate boots go through recovery
        Target-->>Quirkbench: Current software and connection status
        Quirkbench-->>Agent: Ready for more inspection
    end
    Agent->>Quirkbench: recovery_return
    Quirkbench->>Target: Stop remaining commands, flush output and return to recovery
    Target->>Quirkbench: Upload remaining evidence
    Quirkbench-->>Agent: Return status and received evidence
    Agent->>Agent: Assess findings and create investigation patch
```

## Failure, return and pause

```mermaid
sequenceDiagram
    actor Human
    participant Agent as User's agent
    participant Quirkbench
    participant Target
    Target--xQuirkbench: Connection lost during experiment
    Quirkbench-->>Agent: Command status unknown, not replayed
    alt Same boot reconnects
        Target->>Quirkbench: Report command status and saved output
        Quirkbench-->>Agent: Continue the existing run
    else Target crashed and restarted
        Note over Quirkbench,Target: Panic reboot or watchdog may help; a hard hang may need manual reset
        Target->>Target: Boot recovery when possible
        Target->>Quirkbench: Reconnect and send surviving evidence
        Quirkbench-->>Target: Confirm stored files
        Target->>Target: Delete confirmed evidence and finished run files, keep settings
        Quirkbench-->>Agent: Interrupted commands, results and current availability
    end
    Human->>Quirkbench: Pause investigation
    Quirkbench->>Target: No new commands or runs, finish bounded active work
    Target->>Quirkbench: Return safely and upload remaining evidence
    Quirkbench-->>Human: Paused, with any missing evidence reported
```

Recovery collects diagnostics and deploys the next prepared candidate. Interrupted
commands do not survive a reboot.
Force stop attempts immediate interruption; a frozen target may need manual reset.

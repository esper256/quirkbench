# How the parts work together

Revisable [mockup](../mockups/README.md). Quirkbench combines the binary, controller
and MCP interface here. One controller process serves MCP, HTTPS and target SSH
connections. The target can run a clean fallback or an investigation's
candidate. Recovery remains separate from the writable candidate.
The first candidate uses rpm-ostree; see [candidate deployment](candidate-deployment.md).

## Setup and connection

Development uses `./dev quirkbench` to run the same application on the host.
The launcher handles Distrobox forwarding and private Python dependencies before
this sequence; neither belongs in the running controller.

```mermaid
sequenceDiagram
    actor Human
    participant Quirkbench
    participant USB as USB drive
    participant Target
    Human->>Quirkbench: Configure and run the foreground controller
    Human->>Quirkbench: Flash recovery onto selected external drive
    Note over Quirkbench: Download the release recovery or explicitly select a local build
    Quirkbench->>USB: Write bootable software, settings and shared storage
    Note over Quirkbench,USB: Later compatible recovery updates keep shared data and pairing
    Human->>Target: Boot USB and connect network
    Target->>Target: Copy compressed recovery OS to RAM with dracut
    Target->>Target: Keep boot logs on tty1; display recovery menu on tty2
    Target->>Quirkbench: Pair over HTTPS, then open reverse-direction SSH connection
    Note over Target: Menu and connection status use RAM; storage work runs separately
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
    Target->>Target: Show received action and progress in recovery Activity
    Note over Quirkbench,Target: Check advertised operations; identities need not match
    Target-->>Quirkbench: Command output and exit status
    Quirkbench-->>Agent: Bounded output and saved file
    opt Human requests Send report in recovery menu
        Target->>Quirkbench: HTTPS report upload, independently of SSH
        Quirkbench-->>Target: Confirm report stored
    end
    Agent->>Quirkbench: rpm_ostree_prepare_candidate, then controller_work_wait
    Note over Quirkbench: Reuse local image layers, import into OSTree and record reuse and timings
    Quirkbench-->>Agent: Prepared baseline with target connection support
    opt Replacing retained run files
        Agent->>Quirkbench: Retrieve needed results; confirm diagnostics receipt
        Agent->>Quirkbench: recovery_discard_ostree, then wait for completion
        Quirkbench->>Target: Remove retained run files
    end
    Agent->>Quirkbench: recovery_deploy_ostree
    Quirkbench->>Target: Supply candidate content to recovery
    Target->>Target: Deploy on shared data with fresh /etc and /var
    Quirkbench-->>Agent: Prepared, still in recovery
    Agent->>Quirkbench: recovery_boot_ostree
    Quirkbench->>Target: Select one-time boot
    Target->>Target: Arm startup fallback independently of target service
    Target->>Quirkbench: Candidate reconnects with same identity and new boot ID
    Target->>Target: Disarm startup fallback for this boot
    Quirkbench-->>Agent: Candidate connected
    Agent->>Quirkbench: recovery_return
    Quirkbench->>Target: Save diagnostics and reboot
    Target->>Quirkbench: Recovery reconnects and uploads surviving diagnostics
    Quirkbench-->>Agent: Recovery available and results ready
```

Recovery OS files stay read-only; managed actions write shared data and boot selection.
An initial candidate connection failure follows the startup fallback in the recovery design.

## Interactive investigation

```mermaid
sequenceDiagram
    participant Agent as User's agent
    participant Quirkbench
    participant Target
    Agent->>Quirkbench: investigation_context and optional candidate inventory
    Note over Agent,Quirkbench: Optional ordered list calls the same tools; stop on failure
    Note over Agent,Target: Candidate boot follows the deployment diagram above, when needed
    loop Inspect and improvise on the same boot
        Agent->>Quirkbench: Start command or sftp_upload a probe
        Quirkbench->>Target: Execute through SSH, save output independently
        Quirkbench-->>Agent: Short command result or running command ID, output cursor and completion
        Note over Quirkbench: Record MCP call time and response size without copying command text
        Note over Agent,Target: Other commands can run concurrently; each has its own timeout
        Note over Target: Optional quirkbench-load uses ordinary command control; no separate service
        opt Interactive command
            Agent->>Quirkbench: ssh_terminal_input
            Quirkbench->>Target: Input or resize, without replay
        end
        Agent->>Quirkbench: ssh_command_wait
        Note over Agent,Quirkbench: Bounded MCP wait; quiet CLI wait for long controller work
        Target-->>Quirkbench: Command output and completion, or interruption
        Quirkbench-->>Agent: Saved output and status
        Agent->>Quirkbench: sftp_download a completed evidence file
        Quirkbench->>Target: Read file over existing SSH connection
        Target->>Quirkbench: Completed binary or text file
        Quirkbench-->>Agent: Received files or missing evidence reported
    end
    opt Software change
        Agent->>Agent: Edit source
        Agent->>Agent: Build changed software with ordinary tools
        Agent->>Quirkbench: rpm_ostree_prepare_candidate with captured package changes
        Note over Agent,Target: Return, discard, deploy and boot through recovery as shown above
        Target-->>Quirkbench: Current software and connection status
        Quirkbench-->>Agent: Ready for more inspection
    end
    Agent->>Quirkbench: recovery_return
    Quirkbench->>Target: Stop remaining commands, flush output and return to recovery
    Target->>Quirkbench: Upload remaining evidence
    Quirkbench-->>Agent: Return status and received evidence
    Agent->>Quirkbench: Boot retained files again, or explicitly discard
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
        Target->>Target: Delete confirmed evidence; retain run files until explicit discard
        Quirkbench-->>Agent: Interrupted commands, results and current availability
    end
    Human->>Quirkbench: Pause investigation
    Quirkbench->>Target: No new commands or runs, finish bounded active work
    Target->>Quirkbench: Return safely and upload remaining evidence
    Quirkbench-->>Human: Paused, with any missing evidence reported
    Human->>Quirkbench: Resume investigation
    Note over Quirkbench: Permit new work; no automatic boot or sequence resume
```

Recovery collects diagnostics and deploys the next prepared candidate. Interrupted
commands do not survive a reboot.
Force stop attempts immediate interruption; a frozen target may need manual reset.

## Cancel before boot

```mermaid
sequenceDiagram
    participant Agent as User's agent
    participant Quirkbench
    participant Target as Target recovery
    Agent->>Quirkbench: recovery_cancel_ostree(work_id)
    Quirkbench-->>Agent: Cancellation requested
    Quirkbench->>Target: Cancel deployment (retry after reconnect if needed)
    Target->>Target: Stop process, clear boot selection, clean unfinished run
    Target-->>Quirkbench: Cancelled; recovery ready
    Agent->>Quirkbench: controller_work_wait(work_id)
    Quirkbench-->>Agent: Cancellation confirmed
```

## Recovery drive failure

```mermaid
sequenceDiagram
    actor Human
    participant Recovery
    participant Quirkbench
    participant Kernel
    Note over Recovery: Storage work is stalled; menu and connection remain available when possible
    Quirkbench->>Recovery: Read emergency diagnostics
    Recovery->>Kernel: Read bounded kernel information
    Recovery-->>Quirkbench: Return diagnostics without external programs
    Human->>Recovery: Confirm force power off
    Recovery->>Kernel: Power off without waiting for disk cleanup
```

## Delete a finished investigation

```mermaid
sequenceDiagram
    actor Human
    participant Quirkbench
    participant Files
    Human->>Quirkbench: Delete a paused investigation
    Quirkbench->>Quirkbench: Refuse unfinished work or retained target deployment
    Quirkbench-->>Human: Preview removal
    Human->>Quirkbench: Confirm
    Quirkbench->>Quirkbench: Stop accepting work for this investigation
    Quirkbench->>Files: Remove its diagnostics and unused software
    Note over Quirkbench,Files: Keep software referenced by other investigations; prune OSTree with its own tools
    Quirkbench->>Quirkbench: Remove investigation records
    Quirkbench-->>Human: Deletion complete
```

## Explicit rsync candidate

```mermaid
sequenceDiagram
    participant Agent
    participant Controller
    participant Appliance as Optional image-building appliance
    participant Recovery
    Agent->>Controller: Prepare candidate with backend rsync
    Controller->>Appliance: Export exact commit as PAX; assemble and label root image
    Appliance-->>Controller: Root image, kernel, matching live initramfs
    Controller->>Controller: Record artifact hashes and build provenance
    Agent->>Controller: Full transfer or delta update; optional retained destination
    Controller->>Recovery: Exact bundle identity; require supported backend
    Controller->>Controller: Open read-only source filesystem view on demand
    Recovery->>Controller: Authenticated rsync file-tree stream
    Recovery->>Recovery: Create backing filesystem once; sync files into it
    opt Explicit boot-artifact update
        Recovery->>Recovery: Verify kernel/initramfs; publish boot entry
    end
    Agent->>Controller: Explicit boot request
    Controller->>Recovery: Select one-time entry using shared boot lifecycle
```

See [rsync deployment](rsync-deployment.md) for interrupted preparation and pending
complete-image boot qualification. Merely booting Recovery starts no backend work.

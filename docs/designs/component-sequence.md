# How the parts work together

Revisable design for the [example walkthrough](../mockups/README.md).
Arrows show responsibilities, not new services or required wire formats.
Build responsibilities follow the [experiment build design](experiment-builds.md).

```mermaid
sequenceDiagram
    actor Human
    participant CLI as Quirkbench binary
    participant Controller as Quirkbench controller
    participant Worker as Preparation worker
    participant Fedora as Official Fedora repositories
    participant USB as USB drive
    participant Recovery as Recovery system
    participant Candidate as Candidate on target
    participant Agent as Quirkbench user's agent

    Human->>CLI: setup
    CLI->>CLI: Save controller config and connection credentials
    CLI-->>Human: Configuration saved; run quirkbench controller run
    Human->>CLI: controller run in a separate terminal
    CLI->>Controller: Run in foreground with saved config
    Human->>CLI: recovery build
    CLI->>Worker: Use matching cached image or build
    Worker-->>CLI: Progress, then complete recovery image
    Human->>CLI: recovery flash
    CLI->>Controller: Obtain pairing credentials
    CLI-->>Human: Select USB and confirm erasure
    Human->>CLI: Confirm selected USB
    CLI->>USB: Write generic recovery and final partitions; save settings on shared data
    CLI-->>Human: USB ready
    Human->>Recovery: Boot target from USB
    Human->>Recovery: Connect network if needed
    Recovery->>Controller: Pair using credentials from USB
    Controller-->>Recovery: Target connected

    Human->>CLI: Describe problem; approve investigation scope and limits
    CLI->>Controller: Save investigation and authorization
    Controller-->>CLI: Workspace and INVESTIGATION.md with instructions
    CLI-->>Human: Guide path to give the agent
    Human->>Agent: Investigate the problem
    Agent->>Agent: Read guide, build instructions and candidate hook examples
    loop Until agent finishes or investigation pauses
        Agent->>CLI: Read investigation and existing evidence
        CLI->>Controller: Read saved records and files
        Controller-->>Agent: Limits, candidate source, results
        Agent->>Agent: Inspect code; design test and time limit; edit candidate source
        opt Changed software or compiled test programs
            Agent->>Agent: Incrementally build changed software; package completed output as RPMs
        end
        Agent->>CLI: Submit package changes, custom RPMs and test
        CLI->>Controller: Save submission and retry ID
        Controller-->>Agent: Accepted; next command and instruction path
        Controller->>Controller: Check deployment requirements; capture submitted files
        Controller-->>Agent: Capture complete; editing may resume
        Controller->>Worker: Prepare package selection with cached baseline and custom RPMs
        opt Stock packages not cached
            Worker->>Fedora: Download selected versions and dependencies
            Fedora-->>Worker: Stock RPMs
        end
        Worker->>Worker: Assemble with rpm-ostree; reuse persistent caches
        Worker-->>Controller: Candidate recorded in OSTree
        Controller->>Controller: Check investigation still permits execution
        Agent->>CLI: Wait quietly for this submission
        CLI->>Controller: Wait for results or required intervention
        Recovery->>Controller: Request candidate and experiment instructions
        Controller-->>Recovery: Exact candidate identifier, test and time limit
        Recovery->>Controller: Fetch missing OSTree content
        Controller-->>Recovery: Candidate content
        Recovery->>USB: Prepare candidate on shared data; assign evidence and working directories
        Recovery->>USB: Set one-time boot request
        Note over USB,Candidate: Bootloader clears request before starting candidate; next boot defaults to recovery
        Recovery->>Candidate: Boot candidate once
        Candidate-->>Controller: Progress and live evidence when connected
        Candidate->>USB: Save evidence locally
        alt Test finishes or failure permits restart
            Candidate->>Recovery: Restart into recovery system
            Recovery->>Controller: Upload saved evidence for this run
            Controller-->>Recovery: Evidence stored durably
            Recovery->>USB: Delete uploaded evidence; clear finished run temporary files
            Controller-->>Agent: Results, missing evidence and next instruction path
        else Target stops responding
            Controller-->>Human: Last contact, partial evidence, unknown outcome
            Human->>Recovery: Restart target manually if needed
            Recovery->>Controller: Upload any surviving evidence
            Controller-->>Recovery: Evidence stored durably
            Recovery->>USB: Delete uploaded evidence; clear interrupted run temporary files
            Controller-->>Agent: Interrupted result and next instruction path; do not blindly repeat
        end
    end
    Agent->>Agent: Assess evidence and create investigation patch with Git
    Agent-->>Human: Investigation patch, results and limitations
```

```mermaid
sequenceDiagram
    actor Human
    participant Controller as Quirkbench controller
    participant Candidate as Candidate on target
    participant Recovery as Recovery system
    Human->>Controller: Investigation pause
    Controller->>Controller: Stop starting new work
    Candidate->>Recovery: Finish current bounded run and restart
    Recovery->>Controller: Upload evidence
    Controller-->>Recovery: Evidence stored durably; USB copy can be deleted
    Controller-->>Human: Paused; report any missing evidence
    Human->>Controller: Resume with existing limits
    Controller->>Controller: Allow new work
```

Force stop attempts immediate interruption instead. A frozen target may need a
manual reset. Recovery-system reports use normal pairing for upload; local
collection and preview do not need a connection.

# How the parts work together

Revisable design for the [example walkthrough](../mockups/README.md).
Quirkbench represents the binary and controller together.
Arrows show responsibilities, not new services. Target requests follow the
[controller API](controller-api.md).
Build responsibilities follow the [experiment build design](experiment-builds.md).

## Setup and pairing

```mermaid
sequenceDiagram
    actor Human
    participant Quirkbench
    participant USB as USB drive
    participant Recovery as Recovery system

    Human->>Quirkbench: setup
    Quirkbench->>Quirkbench: Save controller config and connection credentials
    Quirkbench-->>Human: Configuration saved#59; run quirkbench controller run
    Human->>Quirkbench: controller run in a separate terminal
    Human->>Quirkbench: recovery build
    Quirkbench->>Quirkbench: Use matching cached image or build
    Quirkbench-->>Human: Progress, then complete recovery image
    Human->>Quirkbench: recovery flash
    Quirkbench-->>Human: Select USB and confirm erasure
    Human->>Quirkbench: Confirm selected USB
    Quirkbench->>USB: Write generic recovery and final partitions#59; save settings on shared data
    Quirkbench-->>Human: USB ready
    Human->>Recovery: Boot target from USB
    Human->>Recovery: Connect network if needed
    Recovery->>Quirkbench: Pair using credentials from USB
    Quirkbench-->>Recovery: Target connected

```

## Investigation and experiment loop

Build preparation is shown within Quirkbench, and USB writes within the
system performing them, to keep the diagram focused on the experiment flow.

```mermaid
sequenceDiagram
    actor Human
    participant Agent as Quirkbench user's agent
    participant Quirkbench
    participant Recovery as Recovery system
    participant Candidate as Candidate on target

    Human->>Quirkbench: Describe problem#59; approve investigation scope and limits
    Quirkbench-->>Human: Workspace and agent guide path
    Human->>Agent: Investigate the problem
    Agent->>Agent: Read guide, build instructions and candidate hook examples
    loop Until agent finishes or investigation pauses
        Agent->>Quirkbench: Read investigation and existing evidence
        Quirkbench-->>Agent: Limits, candidate source, results
        Agent->>Agent: Inspect code#59; design test and time limit#59; edit candidate source
        opt Changed software or compiled test programs
            Agent->>Agent: Incrementally build changed software#59; package completed output as RPMs
        end
        Agent->>Quirkbench: Submit package changes, custom RPMs and test
        Quirkbench-->>Agent: Accepted#59; next command and instruction path
        Quirkbench->>Quirkbench: Check deployment requirements#59; capture submitted files
        Quirkbench-->>Agent: Capture complete#59; editing may resume
        Quirkbench->>Quirkbench: Prepare package selection with cached baseline and custom RPMs
        opt Stock packages not cached
            Quirkbench->>Quirkbench: Download selected stock RPMs and dependencies from Fedora
        end
        Quirkbench->>Quirkbench: Assemble with rpm-ostree#59; record candidate in OSTree
        Quirkbench->>Quirkbench: Check investigation still permits execution
        Agent->>Quirkbench: Wait quietly for this submission
        Recovery->>Quirkbench: Request candidate and experiment instructions
        Quirkbench-->>Recovery: Exact candidate identifier, test and time limit
        Recovery->>Quirkbench: Fetch missing OSTree content
        Quirkbench-->>Recovery: Candidate content
        Recovery->>Recovery: Prepare candidate on shared data#59; assign evidence and working directories
        Recovery->>Quirkbench: Confirm prepared run may start
        Quirkbench-->>Recovery: Saved start decision under investigation authorization
        Recovery->>Recovery: Record attempt#59; set one-time boot request
        Note over Recovery,Candidate: Bootloader clears request before starting candidate#59; next boot defaults to recovery
        Recovery->>Candidate: Boot candidate once
        Candidate-->>Quirkbench: Progress and live evidence when connected
        Candidate->>Candidate: Save evidence locally
        alt Test finishes or failure permits restart
            Candidate->>Recovery: Restart into recovery system
            Recovery->>Quirkbench: Upload saved evidence for this run
            Quirkbench-->>Recovery: Evidence stored durably
            Recovery->>Recovery: Delete confirmed uploaded files
            Recovery->>Quirkbench: Final outcome and evidence list
            Quirkbench-->>Recovery: Result received
            Recovery->>Recovery: Clear finished run temporary files
            Quirkbench-->>Agent: Results, missing evidence and next instruction path
        else Target stops responding
            Quirkbench-->>Human: Last contact, partial evidence, unknown outcome
            Human->>Recovery: Restart target manually if needed
            Recovery->>Quirkbench: Upload any surviving evidence
            Quirkbench-->>Recovery: Evidence stored durably
            Recovery->>Recovery: Delete confirmed uploaded files
            Recovery->>Quirkbench: Interrupted outcome and evidence list
            Quirkbench-->>Recovery: Result received
            Recovery->>Recovery: Clear interrupted run temporary files
            Quirkbench-->>Agent: Interrupted result and next instruction path#59; do not blindly repeat
        end
    end
    Agent->>Agent: Assess evidence and create investigation patch with Git
    Agent-->>Human: Investigation patch, results and limitations
```

## Pause and resume

```mermaid
sequenceDiagram
    actor Human
    participant Quirkbench
    participant Candidate as Candidate on target
    participant Recovery as Recovery system
    Human->>Quirkbench: Investigation pause
    Quirkbench->>Quirkbench: Stop starting new work
    Candidate->>Recovery: Finish current bounded run and restart
    Recovery->>Quirkbench: Upload evidence
    Quirkbench-->>Recovery: Evidence stored durably#59; USB copy can be deleted
    Quirkbench-->>Human: Paused#59; report any missing evidence
    Human->>Quirkbench: Resume with existing limits
    Quirkbench->>Quirkbench: Allow new work
```

Force stop attempts immediate interruption instead. A frozen target may need a
manual reset. Recovery-system reports use normal pairing for upload; local
collection and preview do not need a connection.

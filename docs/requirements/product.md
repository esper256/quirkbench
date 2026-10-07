# Quirkbench product requirements

**Human approval required for every change to this document.** Agents may propose changes but must obtain explicit human approval before editing it. This initial document records the owner's agreed product direction. Design documents do not add requirements by implication.

## Purpose

Quirkbench enables an external coding agent to investigate a Linux computer and ultimately produce fixes, including kernel patches, autonomously. The agent reasons, plans experiments, edits code and judges results. Quirkbench is the tool that builds/prepares, deploys and runs those experiments and returns their requested diagnostics and debugging logs. Quirkbench does not itself perform the agent's reasoning or patch generation.

## Product requirements

1. **Complete the agent's experiment loop.** Once a human authorizes an investigation, the agent can build and run experiments within its agreed limits until paused or human intervention is needed. It can supply new diagnostic/stress-test code as part of an experiment, with time/resource limits and practical disk protections. Routine iterations do not require human approval for each run.
2. **Support low-level Linux investigation.** Kernels and other low-level components may be modified both to fix problems and to stress hardware/software to expose rare failures. General userspace application debugging is outside scope. The tool must not be tied to one vendor, laptop, peripheral or example investigation.
3. **Aim to leave the target's persistent installed state untouched.** Internal disks and persistent BIOS/device-firmware changes are outside scope. Use practical protections to make accidental access/modification difficult. Experiments involving storage drives or their surrounding subsystems are outside Quirkbench's domain. Booting from external media, stressing the target and crashing are accepted activities. Arbitrary privileged experimental code cannot be perfectly contained; that residual risk does not justify unlimited protective complexity.
4. **Make human use painless and enjoyable.** Installation, configuration and media preparation should be ordinary, approachable Linux application tasks. Show useful progress during long operations so a person can judge whether intervention is needed.
5. **Allow pause and resume.** Normal pause stops new work, lets the current bounded target run finish and recover/upload, then waits for resume. Offer a separate stop-now action.
6. **Recover when possible.** After a failed experiment, make a practical effort to return the target to a known-good environment and upload diagnostics. Recovery and diagnostic survival cannot be guaranteed for all failures.
7. **Keep investigations separate from Quirkbench development.** The Quirkbench user's agent must not modify Quirkbench itself to carry out an investigation. Changes belong in candidate software and investigation test programs. Quirkbench must support this work without requiring edits to its own code.

## Authority

Only this document defines product requirements. Designs are revisable proposals for fulfilling them, not additional human mandates. Agents may change designs during implementation without approval unless the change would add massive complexity or substantially reduce capability, performance or ease of use; those suspect changes require human confirmation. Any requirement change still requires approval, even if an agent considers it beneficial.

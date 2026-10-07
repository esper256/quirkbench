# The experiment loop

Revisable design supporting the [requirements](../requirements/product.md).

The Quirkbench user's agent edits candidate source and investigation test programs,
not Quirkbench. Adding a diagnostic or stress test must not require changing
Quirkbench's code.

The user's agent first collects target system information and diagnostics, then
tries to reproduce the reported problem. Early experiments may only observe or
add diagnostic code. A baseline that does not reproduce the problem is not a fix;
the agent decides what to investigate next before proposing a correction.

A human authorizes the investigation's target, scope and limits once. The agent then submits successive experiments without per-run approval. Out-of-scope actions require a human decision; pause/revocation stops new dispatch.

The user's agent chooses each experiment's time limit to suit its test, within any
limits the human explicitly set. Do not ask humans to guess test duration during
setup. Do not impose a fixed experiment memory cap by default: memory pressure may
be the intended test. Apply resource controls where they serve the experiment or
protect the controller computer, rather than one arbitrary cap for all runs.

Experiments can modify kernel/modules/initramfs and low-level services/libraries, and include agent-authored diagnostic or stress programs. Agents can write new tests. Capture test code with its inputs and apply practical time/resource limits. Run it in the experimental environment, not the fixed recovery process. Persistent firmware changes and internal-disk experiments remain excluded.

Use a small submit/status/logs/results interface. [Quirkbench prepares stock software; the user's agent builds changed RPMs](experiment-builds.md). Capture completed build output and test programs, with their code and build settings. Quirkbench uses rpm-ostree to assemble the package selection and OSTree to deploy it. Later workspace edits do not alter submitted work.

Provide live logs where possible and stored files afterward. Explain missing/truncated output; don't reject a useful new measurement merely because a built-in test did not request it. The agent interprets results and creates patches.

Each agent-facing response includes a next action and a path to short instructions
for that step, including failures and pending work. Results point to both evidence
and instructions for using it. Include the same pointers in JSON output.

Minimize agent tokens spent waiting. Provide a quiet `experiment wait` command
that waits for results or required intervention and returns one concise response.
A wait timeout reports pending work; it does not cancel or repeat the experiment.
Allow waiting again for the same submission after interruption. Use existing
controller notifications or inexpensive internal checks, not repeated agent
status calls, narrated sleeps or continuous log output. Human monitoring remains
separate. This is a design goal, not an extra product requirement.

Aim for fast repeat experiments. Reuse unchanged builds and files already on the
target; transfer and write only changed content where practical. A small code
change should not require reflashing the USB or resending an entire system.
Measure preparation and transfer time. Prefer existing caching and transfer tools
over a new synchronization system; more elaborate optimizations need a measured
benefit. Reuse must still produce the exact submitted candidate and preserve the
recovery system and saved evidence. This is a design goal, not a requirement.

Investigation pause stops new work, lets the current bounded run recover/upload, then waits for resume. Force stop attempts interruption; a hard hang may require manual reset. Ask humans for physical observations only when needed.

Proposed disconnect default, not an owner mandate: finish the bounded run, retain diagnostics, return to recovery and wait for reconnection. Reconcile uncertain runs rather than blindly repeating them. Reuse existing operation state instead of creating another workflow engine.

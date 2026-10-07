# Who builds experiment software

Quirkbench 1.0 uses **rpm-ostree to assemble candidates and OSTree to deploy them**.
This is the chosen design, not an added [product requirement](../requirements/product.md).
Implementation details can change as we learn.

| Quirkbench | Quirkbench user's agent |
| --- | --- |
| Prepare the workspace, baseline and build instructions | Edit candidate source and test programs |
| Acquire stock RPMs; build unchanged software when needed | Build changed software into replacement RPMs; fix errors |
| Assemble candidates with rpm-ostree and deploy with OSTree | Supply completed build output and interpret evidence |

Use official Fedora repositories plus local custom RPMs. Record exact selected
versions and dependencies; do not silently update unrelated packages. Cache stock
packages and prepared results. Keep the original baseline available for comparison.
Quirkbench is not a Fedora mirror or a new package manager.

The experiment description names the baseline, stock package changes, custom RPM
replacements and test instructions. Keep rpm-ostree's configuration internal; do
not expose its entire format as Quirkbench's interface. Use its existing dependency
and installation handling. Do not build a parallel arbitrary-file OS assembler.
Small diagnostic programs and configuration files have a supported place without
requiring the agent to create a package for every script.

Supply ordinary build scripts that preserve incremental build directories and
package completed output without unnecessary recompilation. The agent runs and
may adjust them. Quirkbench does not rebuild changed code. Deployment requirements
cover matching kernel/modules, explicit replacements and necessary boot files.
Keep source records and debugging symbols on the controller unless the test needs
them. A successful experimental boot is not an admission condition.

Publish completed output separately from files still being built. Record the code
and settings used; copy completed output for submission before it can be replaced.
No `--writers-stopped` assertion or control over arbitrary editors. Report detected
incomplete or changing output with a useful retry instruction.

Provide `INVESTIGATION.md` with the problem, scope, file locations and exact next
commands. Link build instructions, deployment requirements and editable candidate
hook examples. Hooks run at documented points in candidate startup and shutdown;
earlier instrumentation is a candidate source change. No Quirkbench edits needed.

Candidate hooks receive `QUIRKBENCH_EVIDENCE_DIR` and `QUIRKBENCH_WORK_DIR` for
the current run. Document these in the supplied agent guide and test examples.
Evidence is returned to the controller; working files are disposable after the
run. Tell agents to leave recovery settings, boot selection and managed candidate
files alone. See [recovery storage and cleanup](recovery.md).

Recovery image builds remain a separate Quirkbench responsibility.
See [deployment and storage](candidate-deployment.md).

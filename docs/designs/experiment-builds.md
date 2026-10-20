# Build software for an investigation

Builds support [interactive debugging](interactive-debugging.md). Quirkbench prepares
the baseline, workspace and build instructions. The user's agent edits software,
chooses builds and resolves compilation errors. No agent edits to Quirkbench are needed.

Expose the supplied incremental build scripts through an MCP helper as well as
ordinary local execution. The helper runs the selected script, returns useful logs
and retains completed output. It does not diagnose or fix code. Retain useful build
directories and do not rebuild unchanged software or repeat a successful agent build.

Provide context and short guides for the supported kernel/userspace builds, required
output, debugging symbols and deployment choices. Include matching source/package
references and build dependencies so the agent can prepare source without guessing.
Quirkbench's supported build helpers supply `BUILD.md`, `DEPLOY.md` and incremental
scripts using a pinned build container. Resolve sources and patches from the actual
selected Bazzite packages, including custom repositories; Fedora sources alone may
not match. The agent prepares and edits source and fixes compilation errors.
Package output as RPMs where useful for OS assembly or installation. A standalone probe or file change needs no RPM.
Use official packages and cache selected versions without becoming a distribution
mirror. Record resolved packages when they affect the experiment.

Publish completed output separately from active build files. Record relevant source
and build settings. Copy completed output before deployment; later edits must
not change retained output. Do not require control over the agent's editor.
Keep symbols and source on the controller unless needed on the target.

Candidate hooks support startup/shutdown tests. They are optional alongside
live commands, not the only way to run diagnostic code. Supply evidence and working
directories to both. Never require the agent to predict every future inspection at
build time. Earlier boot instrumentation remains a candidate-source change.

Before producing the first bootable image, record whether the initial path supports
Secure Boot and how experimental kernels meet that choice. Never automatically
change firmware settings or enroll keys. Ask for any required human action.

Recovery image builds remain Quirkbench's responsibility. Start with matching
kernel/modules and PipeWire RPM builds for the selected rpm-ostree base. The agent
chooses and fixes builds; the controller assembles completed outputs into a
[candidate](candidate-deployment.md). Saved candidates name their base, package
changes and selected files explicitly. No universal build system or automatic
capture of every live edit.

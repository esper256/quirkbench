# Build software for an investigation

Quirkbench prepares unchanged baselines and assembles completed outputs into a
[candidate](candidate-deployment.md). The user's agent edits software, runs ordinary
build tools and fixes compilation errors. Investigations never require edits to
Quirkbench. Specialized module-build or driver-reload MCP tools are unnecessary.

Build against the running candidate's actual source, patches, configuration and
symbol versions. Similar package names do not establish a match. Use package
metadata to find sources and dependencies, including distribution-specific sources.
Keep source, symbols and useful incremental build directories on the controller.

Publish completed output separately from active build files. Record relevant source
and build settings. Quirkbench captures supplied files before preparing a candidate;
later edits must not change that candidate. No control over the agent's editor is
required. Additions and replacements may be RPMs for the first OS assembly mechanism.
A standalone probe or live file change needs no RPM.

For suitable modules, use ordinary upload and command tools to stop users, unload,
reload and verify changed code. Do not force unloading or bypass signature checks.
Built-in drivers, incompatible interfaces and startup changes need a new candidate
boot. Live changes are disposable unless explicitly incorporated into a candidate.

Candidate hooks are optional alongside live commands. A startup probe receives
run, evidence and scratch directories before networking and has an independent
execution timeout. Earlier boot instrumentation remains a candidate-source change.
Hardware-specific probes and failure detectors belong in the investigation workspace.

Recovery image builds remain Quirkbench's responsibility. The first image is unsigned
and requires Secure Boot disabled; Quirkbench does not change firmware settings or
enroll keys. No universal build system or automatic capture of every live edit.

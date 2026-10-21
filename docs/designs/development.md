# Develop by simplifying

Follow the [glossary and writing guidance](../glossary.md) in docs and discussions.

[Requirements](../requirements/product.md) need human approval to change. Agents may revise designs unless a change adds massive complexity or substantially reduces capability, performance or ease of use; explain such a tradeoff and ask first.

Read relevant code, not the entire archive. Coordinate issues/PRs and preserve others' work. Archived contracts, old issues and existing tests do not override current requirements.

Use [interactive debugging](interactive-debugging.md) as the main workflow.
Use one controller process and AsyncSSH reverse-direction target connections.
Implement rpm-ostree first, with fresh mutable files per deployment and recovery owning
boot selection. Verify that integration early. Keep OS-specific preparation small
and separate from shared command/evidence code. Measure repeat-loop cost.
This does not authorize image builds or hardware runs.

## Milestone 1: bootable recovery and agent-driven deployment

Build and flash a USB, boot recovery, connect to the controller, inspect recovery
through MCP, and deploy a Bazzite candidate that boots once and returns to recovery.
See the [recovery-to-candidate mockup](../mockups/recovery-to-candidate.md).

Deliver this as one coherent working journey:

- Setup and one foreground controller, with local MCP, pairing, reverse SSH and
  HTTPS downloads/uploads. Keep credentials stable across reboots.
- Recovery image build/cache and USB flashing with progress; recovery TUI for
  networking, connection status, diagnostics and the human terminal.
- Read-only recovery OS files, with settings, downloaded candidates and diagnostics
  in shared data. MCP offers specific inspection actions, not an unrestricted shell.
- Controller preparation of the selected Bazzite baseline. Recovery deploys it on
  shared data with fresh writable directories and GRUB one-time boot selection.
- Minimal candidate connection, boot identification, startup connection deadline
  and return-to-recovery control. The next proof also includes bounded candidate MCP
  commands, saved output and disposable `/usr` edits through `ostree admin unlock`.

Completion means an actual built/flashed USB boots recovery, an agent retrieves
useful recovery command output through MCP, provisions and boots a candidate, then
requests return and observes recovery reconnect with the same target identity and
a new boot ID. Read-only recovery and preserved settings/diagnostics are checked.
Fast tests alone cannot establish this result. Perform the separate image/physical
checks with the required authorization; if they remain unperformed, say the milestone
is not yet demonstrated.

Start with the [fast recovery/deployment journey](testing.md#confront-the-hardest-part-first)
while implementing this result. Use real application processes, connections and
files; replace expensive or privileged OS work in routine tests. Do not build a
separate simulated product. Confirm the actual mount/boot arrangement early rather
than finishing every screen before the first boot check. Record the initial Secure
Boot choice and measured USB space before declaring supported setup instructions.

The current implementation and outstanding real checks are described in
[local installation](../controller-installation.md). Software test success does not
complete the physical milestone.

## After milestone 1

2. **Interactive candidate debugging.** Add arbitrary candidate commands,
   concurrent sessions, interactive input, durable output, quiet waiting, file
   transfer, command stop and investigation pause/resume. Exercise controller
   restart and candidate interruption through evidence receipt and cleanup.
3. **Changed software and the patch journey.** Add kernel/PipeWire build helpers,
   explicit saved candidates and live-change helpers; complete the investigation
   screens. Verify temporary writable-layer package changes without disturbing
   recovery boot selection. Keep general live replacement and patch generation
   out of milestone 1's completion criteria.

## Agent decisions and when to ask

Agents may choose ordinary Python libraries, record schemas, test helpers,
supported OSTree commands/layout and container build tooling. Use the agreed
SQLite controller store, temporary writable-layer direction and first GRUB
arrangement; verify their integration rather than treating them as proven.
The initial base is latest stable Bazzite, resolved once per baseline as described
in [candidate deployment](candidate-deployment.md).
Document differences and ask for missing target facts only when they affect reproduction.
Choose one supported path, keep OS-specific code small and continue through routine
failures without asking for approval at each step.

Stop the affected approach and ask before patching upstream deployment tools,
building a competing boot manager, adding a persistent controller service, or
substantially weakening clean resets, recovery, quiet waiting or environment fidelity.
Also ask before an unusual Linux architecture or a large new subsystem becomes
necessary. Explain the concrete obstacle, simplest alternative and cost; continue
independent work. Do not replace a blocked design with a silent downgrade.

Requirement changes, push/merge and live operations still need their stated approval.
The deliberately limited live-change combinations and lack of guaranteed terminal
reattachment are accepted design choices, not reasons to stop again.

## Verification and scope

Build shared test tools alongside each milestone. Run the relevant fast integration
tests after a substantial piece works; diagnose failures instead of repeatedly
running suites. Follow the [testing strategy](testing.md), including real recovery/deployment tests, later command interruption tests and
separate image checks. Start with integration
tests, not unit tests. Documentation needs link checks and `git diff --check`.

The rewrite branch already excludes the previous implementation; commit `b807199`
preserves it for specific reference questions. Use fresh development state for
breaking changes, without migrations or compatibility layers. Preserve owner data.

[AGENTS.md](../../AGENTS.md) governs review, push/merge and live-operation approval.
No image builds, flashing, QEMU or kernel compilation for routine edits. Obtain one
independent review after substantial implementation, before merge; documentation
does not need reviewer agents. Do not repeat reviews or full suites per small step.

Designs describe intended behavior. Documentation edits do not establish working
features or authorize live installations and hardware operations.

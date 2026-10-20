# Develop by simplifying

Follow the [glossary and writing guidance](../glossary.md) in docs and discussions.

[Requirements](../requirements/product.md) need human approval to change. Agents may revise designs unless a change adds massive complexity or substantially reduces capability, performance or ease of use; explain such a tradeoff and ask first.

Read relevant code, not the entire archive. Coordinate issues/PRs and preserve others' work. Archived contracts, old issues and existing tests do not override current requirements.

Use [interactive debugging](interactive-debugging.md) as the main workflow.
Use one controller process and AsyncSSH reverse-direction target connections.
Implement rpm-ostree first, with fresh mutable files per run and recovery owning
boot selection. Verify that integration early. Keep OS-specific preparation small
and separate from shared command/evidence code. Measure repeat-loop cost.
This does not authorize image builds or hardware runs.

Implement in three large, coherent stages, with focused verification:

1. **Real interactive connection.** Build the single foreground controller and
   target command handling. Exercise an actual MCP client, reverse SSH, concurrent
   local processes, durable output, quiet waits and reconnection after controller
   restart. Start with the [command-to-recovery test journey](testing.md#confront-the-hardest-part-first),
   including evidence receipt and cleanup, before finishing the UI or deployment.
   No image is needed. Plan the latest stable Bazzite import and concrete
   fresh deployment, per-run writable directories and GRUB one-time boot arrangement.
   Plan the early real check; local tests do not establish that the OS arrangement works.
2. **Bootable investigation environment.** Implement baseline preparation, recovery
   TUI, setup/flashing, deployment, evidence upload/cleanup and fresh run files.
   Use the real controller from stage 1. Check preparation, live modification,
   clean reset and recovery-owned boot selection together. Fast tests mock expensive
   OS work; actual boot confirmation needs a separately authorized image/boot check.
   Record that gap rather than claiming mocks prove it.
3. **Complete the patch journey.** Add kernel/PipeWire build helpers, explicit saved
   candidates, live-change helpers and finish the human/agent screens. Exercise the
   full investigation journey, including pause, failures and evidence retrieval.

Stage 1 is complete when a supported agent client can use the local controller
to run concurrent commands on a separate local target process, wait quietly, and
retrieve output after controller restart. The fast integration journey must also
exercise candidate interruption, real recovery upload/cleanup and fresh run files.
Use real connections and processes, replacing only privileged OS work. This first
working result needs no USB image and makes no boot or hardware claim.

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

Build shared test tools alongside each stage. Run the relevant fast integration
tests after a substantial piece works; diagnose failures instead of repeatedly
running suites. Follow the [testing strategy](testing.md), including the real
command-to-recovery journey and separate image checks. Start with integration
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

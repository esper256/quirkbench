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

Use `./dev quirkbench ...` for host execution of edited code, including from
Distrobox. The launcher manages private Python dependencies; it installs no host
RPMs and does not start a controller automatically. It uses normal user data by
default. Test-only dependencies remain in the test environment. See the
[development example](../mockups/development.md).

## Milestone 1: bootable recovery and agent-driven deployment

The original journey was build/flash, recovery connection and inspection,
candidate preparation/deployment, one-time boot and return with diagnostics.
It has been exercised on hardware; later image changes need their own checks.
See [installation](../controller-installation.md) and the
[recovery-to-candidate example](../mockups/recovery-to-candidate.md).

## Current implementation

The [1.1 changelog](../../CHANGELOG.md) records work since the first agent
investigation. Use the [agent guide](../agent-guide.md) for available tools.
User-session selection and dedicated live-package helpers remain later work. Do not rebuild completed stages or treat old milestone
ordering as a restriction on useful fixes.

Develop substantial connected changes with fast integration tests. Exercise actual
controller/target code and replace expensive or privileged OS operations. Mocks
are test substitutes, never production placeholders. Complete independent software
work when hardware checks need the human; state what remains undemonstrated.

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

Use fresh development state for breaking formats. No migrations or compatibility
machinery; never reset owner state or activate an installation incidentally.

[AGENTS.md](../../AGENTS.md) governs review, push/merge and live-operation approval.
No image builds, flashing, QEMU or kernel compilation for routine edits. Obtain one
independent review after substantial implementation, before merge; documentation
does not need reviewer agents. Do not repeat reviews or full suites per small step.

Designs describe intended behavior. Documentation edits do not establish working
features or authorize live installations and hardware operations.

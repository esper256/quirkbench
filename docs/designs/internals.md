# Conventional internals

Revisable design supporting the [requirements](../requirements/product.md).

Use Python for the controller, CLI, recovery application and shared test tooling.
This is the chosen implementation language. Continue using existing Linux tools
for building, deployment and boot.

Design the application to be integration tested from the beginning. It is fine
to shape production code for this purpose. Keep external commands and other
expensive dependencies easy to replace in tests while exercising real Quirkbench
behavior. Follow the [testing strategy](testing.md); do not leave tests to work
around an already completed implementation.

- Reuse existing controller state, operations, content storage, build/deployment components and bounded workers. Share services between human UI and agent CLI. Delete duplicate orchestration and setup ledgers instead of adding a registry to reconcile them.
- Single-user software needs locks, not accounts/tenants. Ordinary data is not invalid merely because of group-readable permissions or symlinked ancestors. Create secrets privately and authenticate network peers.
- This is a personal experiment tool, not a hardened OS distribution service. Use ordinary tool-supported authentication and package checks. Do not add a release-security system, extra signing approvals or repeated verification rituals for experiments. Keep practical disk protection separate from this choice.
- Derive managed paths from current roots and existing IDs; don't persist redundant absolute or relative paths. Store necessary external locations once in configuration. Paths and permanent inode observations are not durable identity.
- Validate untrusted inputs at entry and recheck where mutation/races matter. Keep actual containment and live ownership checks; avoid repeated full-tree validation. Configuration edits are normal, not integrity violations.
- Stable captures, matching symbols, run attribution, atomic publication and retry handling are useful designs supporting the loop—not additional human requirements or a scientific certification system.
- Cache loss should cause recomputation. Show storage use and offer cleanup/export; preserve active work and unuploaded evidence. Report partial results and full-disk failures honestly.
- Use ordinary Git/files for patch and evidence export where practical. Comprehensive backup machinery is not a prerequisite to a useful loop.
- For 1.0, use Fedora packages, rpm-ostree assembly and OSTree deployment as described in [candidate deployment](candidate-deployment.md). Reuse dracut, GRUB and the existing container tools. These design choices do not limit the product requirements to one platform. Broader support needs working implementation, not universal claims.

Changes should leave fewer concepts and fewer sources of truth. A filename changing should not make every subsystem panic.

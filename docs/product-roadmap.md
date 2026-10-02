# Product roadmap

## Delivery contract

The first usable version supports one attended journey on Fedora/x86-64/UEFI with
direct external USB storage: install and pair, prepare a supported baseline, work
with an external coding agent, explicitly approve an experiment, retrieve evidence,
and export a patch or an honest inconclusive report. Essential pause/resume,
monitoring, shutdown, storage and backup behavior belongs to that journey.

The [first-usable tracker, #29](https://github.com/esper256/quirkbench/issues/29)
owns priority, dependencies and current task status. Its child issues and merged
PRs are the development record. Use [CONTRIBUTING](../CONTRIBUTING.md) to claim
work and [the worker prompt](cloud-worker-prompt.md) for consecutive cloud tasks.
Do not maintain a second task/status list in Markdown.

The [acceptance guide](installation-to-patch.md) defines what must work.
The [README](../README.md) remains the desired product manual, not proof that every
command exists. Executable CLI/services and retained validation establish availability.

## Scope and architecture

Reuse the existing controller database, durable operations, attempt state machine,
content-addressed artifacts, retained OSTree closures and native systemd user-service
ownership of rootless workers. No second scheduler/database, MCP service, web app
or cloud account is required. Cloud development does not imply cloud controller
execution; see [development setup](testing-policy.md#portable-software-development).

Preserve the [C0–C7 contracts](implementation-contracts.md),
[C8 product interface](product-interface.md) and
[storage policy](architecture.md#storage-protection-policy). Their detailed
requirements remain binding; moving tasks to issues does not relax them.

- Fresh controller setup starts with zero enrolled targets. Authentication,
  enrollment, connection, experiment eligibility and unattended eligibility remain
  separate facts.
- Recovery uses locked stock Fedora packages, DNF5 installroot, dracut and the
  existing GRUB/GPT adapter. No required recovery kernel compile or second builder.
  Keep the reviewed appliance/runtime choices in [recovery design](recovery-base.md).
  Refresh the recovery base within Fedora's support lifetime; replacements are
  explicit releases, not automatic updates.
- Fixed recovery confines storage operations to its identified boot device.
  Candidate profiles retain internal-controller exclusions. Unsupported hardware
  is explicit; installed-OS and firmware writes are prohibited.
- Credential-free factory media gains private configuration through authenticated
  attended setup. Moved/cloned media never inherits enrollment or execution grants.
  Existing manual setup remains compatible but does not complete fresh-user acceptance.
- The initial platform's SMBIOS UUID gates remain accidental-mismatch protection,
  not attestation. Missing/default/known-duplicate identity blocks candidates;
  attendance alone does not replace the early gate.
- A proposal, successful build, pairing or upload never authorizes execution.
  Approval binds the exact candidate and attempt. Uncertain execution is reconciled,
  not automatically repeated.
- Immutable source captures preserve the actual Git base and distribution provenance.
  No build reads a changing worktree; interrupted edits remain recoverable.
- Pause stops new scheduling and drains existing bounded work. Restart requires
  explicit resume. Worker stop, recovery arrival, evidence durability and safe
  shutdown are reported separately.
- External-agent use is the default. No AI invocation is needed for monitoring.
  Unsupported hangs remain attended; reset does not prove a crash or a dump.
  Kdump stays blocked by the no-kexec policy pending separate review.

## Delivery order

The tracker sequences candidate-job integration, supported build/composition,
approved baseline round trip, durable proposals/context, the repeated external-agent
loop, comparisons/export and everyday operations. Independent setup, source-facing
and backup tasks can proceed when their issue dependencies allow.

Close software gaps using installed interfaces and actual application services with
injected native boundaries. Then demonstrate the installed journey on the supported
native host and target under an explicit commissioning request. Software completion,
first usable native acceptance and final release qualification are distinct.

For compatibility with existing contracts, M1 means controller setup, M2 connected
target, M3 baseline investigation, M4 external-agent loop and M5 reports/everyday use.
These are component outcomes, not separate competing backlog authorities. M2 alone
does not fulfill the first-usable product outcome above.

## Later work

Final general-release qualification (old M6/P8) is tracked separately in
[#44](https://github.com/esper256/quirkbench/issues/44) and requires an explicit
final major-version release request. Follow [testing policy](testing-policy.md);
ordinary changes never trigger real image, QEMU or endurance campaigns.

Optional managed invocation (old M7/P6c) is
[#45](https://github.com/esper256/quirkbench/issues/45).
Scoped unattended authorization and physical reset/suspend coverage (old M7/P5/P7c)
are [#46](https://github.com/esper256/quirkbench/issues/46).
These do not block attended use and do not authorize one another. Hardware-specific
kernel tailoring is a bounded later proposal, not a setup prerequisite.

Infrastructure issues #23–#26 support development; they are not prerequisites to
every product task. Use GitHub's built-in Actions performance metrics; no separate
performance dashboard is planned.

## Planning history

The old handoff and long implementation checklist are preserved at
[08c03bd](https://github.com/esper256/quirkbench/tree/08c03bd992092aadc4125ba83f5417acda4e0d67/docs).
They contain historical evidence, including machine-local paths, and are not the
current task queue. Completed foundations should be reused, not reimplemented from
old packet descriptions. The tracker records the migration and remaining ownership.

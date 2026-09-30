# Attended-first design revision — 2026-09-29

**2026-09-30 implementation superseding note:** v2 stock contracts, stage APIs,
manual private activation, recovery-only uploads and attended approval now exist.
The original documentation-only record below remains historical; see the
[current software handoff](stock-recovery-attended.md) for implementation evidence
and product-operation limits. Passive kernel disk enumeration and
partition-table reads are permitted; the historical zero-registration language
below was superseded on 2026-09-29.

## Decision and implementation status

The [architecture storage policy](architecture.md#storage-protection-policy) is the
single authority for separating trusted fixed recovery from experimental kernels.
The [recovery decision](recovery-base.md) defaults to stock Fedora kernel/module
packages with DNF5/dracut and the existing GRUB/GPT assembly. The
[roadmap delivery tiers](product-roadmap.md#delivery-contract) make an attended,
owner-controlled external-agent journey with manual authenticated setup first.

This change edits documentation only. Existing recovery recipes, profiles, validators
and synthesis code still implement the previous custom-kernel contract. P3a1 must
introduce versioned successor recipe/release/profile records for stock package
provenance and separate recovery/candidate policies before that path can be built.
Keep legacy readers and identifiers; do not satisfy old fields with invented source
or fragment hashes, weaken validators, or relabel old artifacts.

The custom kernel `7.2.7` completed and remains retained in
`.quirkbench/inputs/fedora44-kernel-build-20260929/kernel-record.json`; its recorded
kernel SHA-256 is `aa65953cc52c2b96a1dfaa038103ba76e4015b1a6fc1af31a5ab9b0b630c254e`.
It is historical build evidence, not a mandatory default recovery input and not
image/physical qualification. No artifact or expensive run was deleted or repeated.

Remaining attended essentials are versioned stock-kernel staging, early-boot and
userspace boot-device enforcement, runtime assembly/manual setup, private activation
and binding, durable coordinator integration, immutable proposal/source handling,
exact-candidate operator approval and the recovery-to-candidate-to-recovery journey.
Existing software primitives/tests do not prove that integration is complete.
Pairing/lifecycle automation, advanced wizards, guided backup completeness, managed
scheduling and unattended grants remain later packets. Existing implemented checks
are preserved. Basic backup reporting must explicitly state contents and omissions.

## Document disposition

Every Markdown design document under `docs/`, plus repository/operator instructions,
was audited. Historical records retain their original measurements and instructions
as evidence of those runs, with dated superseding notes. No blanket historical
terminology or provenance replacement was performed.

| Document | Disposition |
| --- | --- |
| [Architecture](architecture.md) | Revised; authoritative storage policy and attended boundary |
| [Recovery decision](recovery-base.md) | Revised; stock packages, assembly, maintenance and versioned migration |
| [Roadmap](product-roadmap.md) | Revised; initial/later delivery tiers, flow and dependencies |
| [Implementation contracts](implementation-contracts.md) | Revised; distinct policies, manual setup, approval and failure fixtures |
| [Product interface](product-interface.md) | Revised; minimum first-journey freeze and deferred UX |
| [Handoff](implementation-handoff.md) | Revised; P0–P8 retained, attended dependency path and later packets |
| [Milestones](milestones.md) | Revised; attended gates, later enrollment/reset/managed work |
| [Build and boot](build-and-boot.md) | Revised; stock default and clearly labelled current legacy staging |
| [Debug image](debug-image.md) | Revised; manual setup and recovery/candidate policy distinction |
| [Recovery and evidence](recovery-and-evidence.md) | Revised; attended/reset limits and unchanged experimental no-kexec policy |
| [First boot operator](first-boot-operator.md) | Revised; artifact-specific observations and storage-policy limits |
| [Agent guide](agent-guide.md) | Revised; exact operator approval and no agent-expanded authority |
| [Controller installation](controller-installation.md) | Revised; manual first delivery, provisional implementation limits |
| [Monitoring](monitoring.md) | Revised; initial manual readiness, independent protection/approval facts |
| [External hardware](external-hardware.md) | Revised; inherits split protection policies; remains deferred |
| [P0 contract record](product-contract-v1.md) | Revised; frozen fixtures preserved, delivery clarification only |
| [Protocol](protocol.md) | Revised; manual provisioning versus later enrollment; wire API unchanged |
| [Terminology](terminology.md) | Revised; separate recovery/candidate scope; wire names preserved |
| [Watchdog qualification](watchdog-qualification.md) | Revised; later capability, existing activation checks retained |
| [Testing policy](testing-policy.md) | Consistent already; documentation checks and release-only expensive gates |
| [Fedora input candidate](fedora44-input-candidate.md) | Historical with dated superseding note; original evidence retained |
| [Implementation progress](implementation-progress.md) | Historical with current-state note and new revision entry |
| [Stock recovery and attended implementation](stock-recovery-attended.md) | Added after design revision; current software interfaces, retained evidence and qualification limits |
| [This revision audit](design-revision-20260929.md) | New; disposition, remaining implementation and validation record |
| [README](../README.md) | Revised; attended first journey versus later finished-product walkthrough |
| [Agent instructions](../AGENTS.md) | Revised; new authoritative policy and delivery tiers |
| [Environment guidance](../environments/README.md) | Revised; experimental builds versus stock recovery packages |

`docs/__init__.py` is package plumbing, not a design document; unchanged.

## Validation and future acceptance

Documentation validation: local Markdown file/heading links, policy searches,
packet dependency inspection and `git diff --check`. No software tests, kernel/image
builds, QEMU boots or physical campaigns are authorized by this revision. Existing
unrelated working-tree changes are not implementation of this revision.

Higher-reasoning storage-boundary review completed. Corrections applied: prevent
kernel/autoload non-boot disk registration/probing before userspace; assign minimum
crash-safe private-state activation to initial P3c; clarify manual trust provisioning
and the no-attendance-bypass candidate identity rule. The reviewer confirmed the
remaining candidate, approval, compatibility and qualification boundaries. This is
a design review, not evidence that stock recovery enforcement is implemented.
Future owning packets use focused injected fixtures for stock package/module
provenance; kernel/autoload, initramfs and userspace internal-disk non-access; duplicate/unresolved
boot-device identity; candidate exclusions and storage-sensitive changes; manual
trust/credential rejection; missing/mismatched approval; interrupted attempts and
recovery reconciliation. Fixtures cannot qualify real storage preservation.
Explicit device commissioning and final release gates remain separate future
operations; no new boot, preservation, reset or endurance result is claimed.

### Completed documentation checks

- `python /tmp/quirkbench-check-docs.py`: 26 Markdown documents, 151 local file/heading
  links checked; no missing targets or headings. All audited design documents have
  a disposition in the table above.
- `git diff --check`: passed for the working tree. Policy searches and packet
  dependency inspection found no remaining shared recovery/candidate exclusion
  mandate outside explicitly preserved historical/legacy descriptions.
- Higher-reasoning reviewer rechecked the corrected passages and closed review with
  no remaining actionable contradictions. Stock-kernel containment remains pending
  implementation/support evidence, not a new safety or qualification claim.

Superseding implementation policy, 2026-09-29: the accepted passive-metadata
exception permits kernel disk registration and partition-table reads. The prior
review's requirement to prevent those reads is withdrawn. Userspace internal block
opens and filesystem probes remain prohibited from initramfs onward; stock-kernel
recovery and candidate controller exclusions remain separate policies.

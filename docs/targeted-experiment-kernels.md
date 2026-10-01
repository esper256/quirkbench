# Hardware-specific experiment kernels

Status: proposed design, 2026-09-30. This document is the next P1b/P1c/P4
planning packet. It changes no schema, baseline catalog or kernel configuration.
Recovery continues to use pinned stock Fedora packages under the
[recovery decision](recovery-base.md). Experiment execution retains the
[storage policy](architecture.md#storage-protection-policy), exact candidate
operator approval and the existing configuration/module/initramfs checks.

## Goal and inputs

Use the hardware report collected on the first recovery boot to build a smaller
experiment kernel. Avoid spending the first candidate boot discovering basic
hardware already available in that report. A smaller configuration can reduce
compile time and output size; it cannot establish compatibility or kernel safety.

Planning is deterministic from retained inputs:

- The authenticated recovery inventory digest, target identity, report collection
  version, completeness flags and observed boot-media transport.
- Exact candidate baseline, source archive and patches, baseline configuration,
  toolchain/RPM identities and reviewed protection profile.
- Experiment requirements: affected device/driver, debug facilities, bounded recipe,
  networking and evidence requirements.
- A reviewed minimum boot seed, driver-to-configuration mapping, and module alias,
  built-in alias and dependency metadata tied to that exact candidate baseline.

Unknown hardware or missing inventory sections produce specific blockers. An
agent may propose additions for review; it may not fill gaps by silently selecting
all drivers or relaxing internal-storage exclusions.

## Driver selection

Match retained bus modaliases against the candidate's retained alias metadata,
then resolve the candidate module dependency closure. Record ambiguous matches
and optional alternatives explicitly. Require a reviewed mapping from each chosen
module or built-in driver to the candidate's Kconfig symbols and firmware names.
Mappings include their source revision and review identity; a mapping from a
nearby Fedora kernel is not accepted for different sources without review.

Recovery's loaded modules and stock kernel configuration are observations. They
can corroborate hardware matches, but are not a configuration seed or evidence
that candidate dependencies match. A driver built into recovery may never appear
in its loaded-module list. Preserve aliases for built-in drivers as well.

Include everything needed for the attended journey:

- The external device's complete boot path, USB/controller/bridge support,
  partition format, external filesystems, EFI/initrd and module-loading support.
- Local console/display fallback, keyboard/input and local recovery diagnostics.
- The selected network path, required firmware and upload/control dependencies.
- Matching symbols, requested trace/debug facilities, evidence storage and the
  particular experiment's devices and bounded recipe.

Internal disks remain outside investigations. A required internal-storage driver
is a hard blocker under the current policy, including dependencies that select
one indirectly. Do not treat USB attachment alone as storage authorization.
Attendance and a successful recovery boot do not sandbox an arbitrary modified
kernel or authorize experimental access to internal storage.

## Configuration and validation

Use the pinned kernel's own Kconfig tools. Start from a reviewed minimum seed,
apply the selected reviewed symbols through the pinned `scripts/config` tooling,
and resolve with the source's `allnoconfig`/`KCONFIG_ALLCONFIG` and `olddefconfig`
workflow. Pin the exact command order and seed identity in the planning record.
Do not add another Kconfig parser or infer dependencies from text searches.

Check the resolved configuration against every mandatory selection and protection
exclusion. A requested symbol that Kconfig disables, changes or cannot find is a
blocker with its hardware/experiment reason attached. Then compile using the
existing immutable BuildInputs interface, retain the final configuration and
matching symbols, and run existing module-tree and initramfs exclusion checks
before execution. Approval binds the exact resulting deployment and revision;
regeneration, retries and subsequent attempts require fresh approval.

## Proposed planning record

Propose `TargetedKernelPlan` v1 in a future schema packet. It contains the target
and inventory identity; candidate baseline/source/patch identities; mapping,
minimum-seed and alias/dependency metadata identities; experiment requirements;
selected symbols/drivers/firmware and their reasons; unresolved matches and
blockers; Kconfig tool/command identities; generated/final configuration hashes;
and a deterministic plan digest. No secret configuration or raw internal-disk
contents belong in this record.

Initially pass the generated config and its hash through the existing
`kernel_config` and `kernel_config_sha256` build inputs. Retain a planning document
as an ordinary attributed artifact. Future catalog metadata and typed planning
records require explicit versions and compatibility dispatch; do not add fields
to Experiment/Result envelopes or reinterpret old protection policy IDs.

## Implementation sequence and later evidence

1. Add reviewed metadata for one exact supported candidate baseline, with a minimum
   boot seed and explicit internal-controller exclusions.
2. Implement inventory matching and a deterministic proposed plan, with diagnostic
   fixtures for absent mappings, ambiguous aliases and excluded dependencies.
3. Generate and validate configurations using the pinned kernel tools; integrate
   the existing build/composition and operator-approval path.
4. As separately requested product operations, compare broader and tailored
   configurations using the same sources/toolchain and recorded cold/warm cache
   conditions. Record compile duration, resource use, output size and omitted
   drivers. Perform attended boot checks on the actual target.

Hardware coverage, real storage preservation and boot/reset qualification remain
separate recorded release activities. No new compile-time or compatibility claim
follows from this design document.

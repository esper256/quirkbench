# General-purpose boundary audit

Quirkbench connects controller-side coding agents to Linux targets, retains work
across reboots and produces patches backed by attributable experiments. External
agents are the primary interface; managed invocation is optional and explicit.

## Ownership of assumptions

| Finding/evidence | Category and correction | Acceptance |
| --- | --- | --- |
| Task-labelled local controller directories; CLI/service paths differ | Local setup → version/archive-digest installation identity and guarded activation | Clean-home repeat install, mismatch reporting, busy refusal, rollback |
| Installation/main handoff links into audio case history | Historical evidence → separate history from current instructions | Current commands need no case history |
| Build path protection rejects the effective account home when it resolves under /var | Local setup → use canonical account home rather than one presumed home layout | Current account home allowed, other /var/system paths rejected |
| Distrobox in README/contracts/setup requirements | Local setup → optional environment; native services and Podman remain current requirements | Native setup without Distrobox |
| Fedora release/kernel/key and package closure embedded in recovery_inputs | Supported-platform implementation → selected immutable acquisition specification | Exact inputs, trust/repository changes detected, legacy reader |
| x86 build commands and sole adapter directly constructed by planner | Supported-platform implementation → reviewed adapter owns commands and selection | Same x86 behavior, unsupported input blocked |
| Audio bindings duplicated; all recipes advertised in recovery | Optional diagnostic recipe → common reviewed bindings, eligible capabilities | Recovery excludes audio; non-audio collection needs no audio tools |
| Generic collection result names example peripherals | Core invariant → describe actual collection and unknown reproduction | Generic result remains inconclusive |
| Managed invocation described as eventual default | Product preference → external default, explicitly selected managed mode | Contracts and preview agree |
| Device IDs, recipes, observations and patches in a session | Investigation inputs → retain exact identities and explicit selection | No inferred issue/model defaults |
| USB/UEFI/GRUB, Fedora RPM/OSTree assembly and internal-controller exclusions | Supported-platform implementation and core protection → retain current backend, isolate selection | No support or qualification inferred from generic design |
| Recipe CLI default names Fedora 44 independent of its lock | Investigation input → default identity derives the selected verified lock digest | Existing IDs preserved; omitted ID carries no release assumption |
| Stock-recovery guide still sends readers to audio setup history | Historical evidence → current installation/acquisition instructions stand alone | Follow supported workflow without case history |
| Environment guide calls Distrobox the normal controller/build path | Local setup → describe optional shell separately from native worker ownership | Native services/Podman guidance agree across manuals |
| Archive points to an installation manual absent from its wheel | Core delivery invariant → bundle current documentation and put implemented commands first | Clean-home archive contains installation, acquisition and build/boot manuals |

## Boundary inventory

The ownership review covers selections and execution boundaries, not only example
names. Generic controller/target protocol, campaign/operation/worker, CAS/evidence,
observation and approval modules own durable identity and execution invariants;
they do not select an issue, vendor, recipe or coding-agent provider. CLI state,
service trust and publication paths are explicit local setup. Resource budgets,
loopback binding and bounded read/recipe deadlines are core safety/availability
defaults, not details from an investigation.

`platform_adapters` owns reviewed architecture, profile catalog, kernel command,
RPM/SRPM architecture and removable EFI image data. `hardware_plan` matches pinned
profiles; `baseline_catalog` selects retained exact inputs. Frozen v1 profile,
plan, baseline and recipe-manifest schemas retain their current restrictions;
another supported platform requires explicit versioned contracts. Registering a
backend does not silently broaden them. The Fedora build/composition, recovery
stock/rootfs/dracut/vendor-unit, GRUB/GPT/image and QEMU components implement today's
backend. Their package formats, policies and boot-specific checks are deliberate
implementation limits. No scheduling or agent-provider requirement follows from them.

Acquisition specifications, source manifests, problems, target IDs, patches and
recipe parameters are investigation inputs. Reviewed recipe bindings and privilege
checks own optional diagnostics. Audio's stdlib implementation reports unavailable
tools explicitly; generic collection invokes only kernel inventory/log collection.
Recipe eligibility does not imply a peripheral is present or a symptom reproduced.
The development `assemble.ini` and historical command/container layouts are optional
local setup or recorded evidence. Dated investigation guides, fixed historical
candidate snapshots and old acquisition/custom-recovery readers preserve provenance;
they do not select new runtime installations or grant support to new inputs.

## Bounded implementation packets

Apply these sequentially under existing handoff contracts. P2d/C2/C8: immutable
controller installation, additive revision reporting and activation under stopped,
idle ownership; preserve private settings and prior runtimes. P1b/c/C1/C8: reviewed
adapter selection and acquisition specifications, preserving old readers. P7a/C5/C8:
common installed recipe bindings and eligibility reporting. Documentation then
records commands, software evidence and local migration status. Worker ownership
and activation boundaries require higher-reasoning review.

Existing schema meanings and wire names remain frozen. New acquisition metadata
has its own [versioned specification](recovery-acquisition.md); it does not alter recovery locks or experiment/result
records. The current x86-64/UEFI/USB Fedora backend remains the only implemented
platform. Other Linux platforms can acquire reviewed adapters; fixture acceptance
or generic design is not physical support. Internal disks and the installed OS
remain outside experiments. The settled recovery assembly is retained.

## Review and completion

An example can motivate a capability, but cannot become a default or requirement
without an explicit product reason. Every special case needs one owner: core
invariant, supported-platform implementation, optional diagnostic recipe,
investigation input, local setup or historical evidence. Review behavior and
selection boundaries, rather than banning hardware/issue names in text.

Use focused installer/setup, acquisition, profile and recipe/runtime tests; check
local links and whitespace. No kernel/image production, VM campaign or hardware
qualification is authorized by this packet. Historical provenance stays unchanged;
new runtime/image bytes remain unqualified. Completion requires recorded validation
and an honest local activation result, including blockers if active work prevents it.

## Completion record — 2026-09-30

This records the first implementation pass. The follow-up below identified and
closed remaining adapter/default/documentation gaps; the initial completion claim
was too broad.

All audit findings above have an implementation owner and focused verification.
The README presents implemented commands; the future wizard walkthrough is in
[product preview](product-preview.md). Distrobox remains optional. New acquisition
plans retain a versioned specification and exact repository closure, bound to their
retention owner and CAS identity. Legacy command syntax freezes the named historical
candidate before planning; old owners remain readable. Recovery advertises only
eligible recipes. Source-reviewed adapter data owns kernel architecture/default/image
selection; no additional physical platform support is claimed.

Higher-reasoning read-only review approved installer shutdown/ownership/rollback,
recipe eligibility, acquisition identity/closure and the narrow canonical account-home
build-path correction after its identified blockers were fixed. Controller worker
fencing, target/media binding, authorization and storage policies remain unchanged.

Focused software evidence: **286 distinct cases passed** across installer/archive,
setup, recipe/runtime, profile/baseline, acquisition, build planning/cache, product
contract, durable operations, physical handoff/approval, observations and runtime
revision suites, plus selected boot runtime fixtures. The complete relevant run was:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q \
  --basetemp=/home/eric/.cache/quirkbench-boundary-tests \
  tests/test_controller_install.py tests/test_controller_archive.py \
  tests/test_controller_setup.py tests/test_recipe_registry.py tests/test_runtime.py \
  tests/test_hardware_plan.py tests/test_recovery_inputs.py tests/test_recovery_acquisition.py \
  tests/test_build.py tests/test_build_pipeline.py tests/test_baseline_catalog.py \
  tests/test_product_contracts.py tests/test_operations.py tests/test_physical_handoff.py \
  tests/test_operator_approval.py tests/test_observations.py tests/test_recovery_runtime_revision.py
```

That run passed 261 cases and found four obsolete operation-stage fixtures. They
were updated to the existing `recovery_rootfs` allowlist without changing production
policy; `tests/test_operations.py` then passed all 18 cases with basetemp
`/home/eric/.cache/quirkbench-boundary-operation-tests`. `tests/test_boot.py -k runtime`
passed 20 selected cases (40 deselected by scope) with basetemp
`/home/eric/.cache/quirkbench-boundary-boot-tests`. The additional interrupted-activation
busy-rollback regression passed in the final 14-case installer run with basetemp
`/home/eric/.cache/quirkbench-boundary-installer-tests`. Earlier setup/archive/cache
fixtures were corrected to describe already implemented behavior, preserving their
integrity checks. Initial `/tmp` and `/var/tmp` fixture placements were rejected by
existing checkout/system-path guards; account-cache fixtures exercised the actual
canonical-home policy. No failing qualification was skipped or relabeled.

Documentation local links and `git diff --check` passed. This is software evidence;
no kernel/image build, real composition, VM boot or hardware qualification ran.
The system-observation manifest now binds changed source bytes; the old installed
baseline catalog remains historical and requires separately reviewed new identities
before it can describe a candidate built from this runtime.

### Native installation migration

The native controller had no operations, attempts, jobs or claimed workers. Archive
packaging used native Python, then `install()` and guarded `activate()` through the
same implementation exposed by `controller-install`. Installed `setup-check` reported
readiness and no CLI/service mismatch; complete installation-file verification passed.

- Final archive: `/var/home/eric/.local/share/quirkbench/controller/archives/quirkbench-controller-0.1.0-20261001T045808Z.tar.gz`.
- Archive SHA256: `e1157b30bb51d41bad27cfcee4326d8ce0d6e79006e4e6566a9c41ec2cb3abc7`.
- Active runtime: `/var/home/eric/.local/share/quirkbench/controller/0.1.0-e1157b30bb51d41bad27cfcee4326d8ce0d6e79006e4e6566a9c41ec2cb3abc7`.
- State: `/var/home/eric/.local/state/quirkbench`; owner epoch 4.
- Private activation/rollback log: `~/.config/quirkbench/last-activation.json`.
- Selected installation: `~/.config/quirkbench/installation.json`.

These paths are a local completion record, not general installation requirements.
Earlier task-labelled runtimes and the preceding verified runtime remain available;
historical provenance was not rewritten. Archives remain unsigned/unqualified.
No replacement recovery image or baseline/patch execution is implied.

## Follow-up audit and completion — 2026-10-01

The initial pass did not complete the entire plan. The planner still selected a
fixed adapter alias, RPM/EFI command details remained scattered, the recipe default
named Fedora 44 regardless of its lock, two current manuals still treated case
history/Distrobox as the normal path, and the archive referred to an unbundled setup
manual. Those gaps are now corrected. Profile matching resolves reviewed backend
data; frozen v1 membership and schemas remain unchanged. RPM/SRPM/EFI values are
unchanged and centrally owned. Omitted recipe identity uses the verified lock digest.
Current guides explain the native controller journey, and the wheel/archive includes
the documentation rather than requiring access to the original checkout/chat.

**161 distinct focused software cases passed** for this follow-up:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q \
  --basetemp=/home/eric/.cache/quirkbench-boundary-followup-tests \
  tests/test_hardware_plan.py tests/test_baseline_catalog.py \
  tests/test_recovery_inputs.py tests/test_controller_archive.py \
  tests/test_package_resources.py tests/test_compose.py tests/test_image.py \
  tests/test_recovery_source_stage.py tests/test_recovery_rootfs.py
```

That run passed 101 cases and found one typo in a new archive assertion (`README.txt`
instead of the existing `INSTALL.txt`). After correction, the five-case archive
suite passed with basetemp `quirkbench-boundary-followup-archive-tests` under the
account cache. Build/product-contract suites passed 58 cases with basetemp
`quirkbench-boundary-followup-contract-tests`; an actual non-audio dispatch with no
optional audio tools/peripherals passed its additional runtime regression with
basetemp `quirkbench-boundary-followup-recipe-tests`. No production checks were skipped
to obtain these results. Documentation links and whitespace checks passed.

The required higher-reasoning read-only review approved unchanged USB write-bus,
automount/firmware prohibitions, frozen v1 adapter membership and RPM/EFI values;
planning continues to grant no execution/storage authority. No new platform,
kernel/image production, VM run or hardware qualification was performed. Changed
runtime/image inputs remain unqualified.

Guarded native installation/activation passed using the same reviewed helper:

- Archive: `/var/home/eric/.local/share/quirkbench/controller/archives/quirkbench-controller-0.1.0-20261001T051812Z.tar.gz`.
- Archive SHA256: `23f4eccad8f7b340deaa6eceb7d48f5e2c9203eb8708c61699c36692fb9af34e`.
- Active runtime: `/var/home/eric/.local/share/quirkbench/controller/0.1.0-23f4eccad8f7b340deaa6eceb7d48f5e2c9203eb8708c61699c36692fb9af34e`.
- Native `setup-check`: `background_work_ready=true`, revision `mismatch=false`, epoch 5.
- Complete installed-file verification passed; state and older runtime bytes retained.

This local activation is software/service evidence, not image/hardware qualification.
The correction plan is complete at its stated boundary. Full session/setup wizards,
managed scheduling, additional platforms and qualification remain separate roadmap
work. The earlier paths and validation records above are preserved as history.

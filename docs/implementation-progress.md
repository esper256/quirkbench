# Implementation progress after the handoff

This log records bounded software packets against source commit
`dca51b42d14dbf331a75d5212e19839141b2a825` plus the working-tree changes
listed below. It does not advance a physical or release milestone. The image bytes
were not rebuilt, flashed or qualified. Tests use synthetic hardware trees.

## P0 — product contract fixtures

- Contracts: C0, C2, C3, C5 and C8. `product-contracts.v1.schema.json` and the
  four matching examples define strict session intent, agent proposal, observation
  request and response records. `product_contracts.py` validates bounded input,
  duplicate keys, depth, finite numbers and cross-field authority constraints.
- `product_cli.py` freezes planned public argument/help forms, including retained
  `--device` and positional backup syntax. The executable advertises commands
  only as their owning packets implement them; the existing low-level target
  service remains intact.
- `product-contract-v1.md` records source handoff, recipe privilege, readiness and
  service ownership boundaries. No migration, worker or scheduler was added.
- Focused acceptance: `.venv/bin/python -m pytest -q tests/test_product_contracts.py`
  passed (46 tests). Remaining: implement durable admission/ownership and runtime
  services in their owning P2/P6/P7 packets. No image bytes changed.

## P1a — passive hardware inventory

- Contracts: C0/C1. `inventory.py` reads fixed sysfs PCI, USB, network and DMI
  properties plus procfs `MemTotal`; it records recovery versus installed-OS
  provenance, bounded partial results and exit codes 0/2/1. Its importer validates
  before preserving exact input bytes in CAS. The fingerprint excludes timestamp
  and observation order and is explicitly comparison data, not authentication.
- `hardware-inventory.v1.schema.json` and `tests/test_inventory.py` cover limits,
  privacy, safe roots, partial results, malformed imports, exact bytes and no
  privileged probes. The optional standalone installed-OS wrapper remains absent;
  the same Python module can collect in either environment. Recovery setup and
  enrollment do not yet upload this record.
- Focused acceptance: `.venv/bin/python -m pytest -q tests/test_inventory.py`
  passed (16 tests). No image bytes changed; the collector is not yet assembled
  into recovery media.

## P1b — profile and platform planning

- Contracts: C0/C1. `hardware_plan.py`, the packaged candidate
  `generic-x86_64-uefi-usb.v1.json` profile and two schemas select only the explicit
  x86-64/UEFI/USB planning adapter. The profile file SHA256 is
  `f246b080d0e3b1034a612b6e97534e87a131a4d8646f6294a9195c8f7ce20fe4`.
  The planner reports architecture, boot, controller, network, RAM, partial-data
  and driver/protection conflicts. It never relaxes the exclusion policy, queues a
  build or claims that a driver list matches an actual kernel/initramfs.
- `tests/test_hardware_plan.py` covers multiple vendors and chassis forms, missing
  peripherals, unsupported platforms, policy ambiguity and required-driver
  conflicts. `locked_build_inputs` remains null and every plan blocks candidate
  preparation on `baseline_catalog_pending` until P1c binds a real catalog entry.
- The candidate profile still needs storage-policy and driver-list review before
  it can be used for a release. The file encodes planning constraints only; it
  does not change an active kernel configuration or authorize protected writes.
- Focused acceptance: `.venv/bin/python -m pytest -q tests/test_hardware_plan.py`
  passed (14 tests). No image bytes changed or hardware support qualified.

## P1c — baseline catalog selection logic

- Contracts: C0/C1/C8. `baseline_catalog.py`, `baseline-catalog.v1.schema.json`
  and the example record validate versioned Fedora source/configuration, retained
  RPM snapshot and lock, builder/toolchain, build recipe and target recipe digests.
  The planner matches an exact profile/platform/protection identity, verifies
  retained CAS objects and the target RPM lock, and records deterministic
  selection or a specific unsupported, ambiguous or unavailable blocker.
- Replacement kernel and userspace RPMs bind to two fixed relative output slots
  and are verified by digest before they can become `ComposeInputs` paths. Unknown
  fields, moving refs, path escapes and package-lock mismatch fail closed.
- The **installed catalog is empty** (`awaiting-reviewed-closure-v1`). The Fedora
  44 example uses illustrative digests and is not a supported baseline. Existing
  local Fedora 43 trial artifacts use an upstream kernel tarball and do not meet
  the Fedora-configured source/configuration and complete retained closure needed
  for a real entry. Review actual package support lifetime and full closure before
  activating any entry. Catalog selection still leaves
  `build_validation_pending`; it does not claim a built kernel satisfies storage
  protection or that a target can boot.
- Focused acceptance: `.venv/bin/python -m pytest -q tests/test_baseline_catalog.py`
  passed (15 tests). No image bytes changed, built or qualified.

Combined focused regression: `.venv/bin/python -m pytest -q
tests/test_baseline_catalog.py tests/test_hardware_plan.py tests/test_inventory.py
tests/test_product_contracts.py tests/test_contracts.py` passed (139 tests).
`git diff --check` and Python compilation
passed. No release qualification was run.

## P2a — durable operation records

- Contract: C2. `controller.py` adds one SQLite migration for operations,
  operation-scoped references and events; schema upgrades are serialized by an OS
  migration lock and refuse unresolved legacy target attempts. Requests use
  controller-wide IDs and canonical kind, campaign, target, arguments, normalized
  local paths and retained CAS inputs/source refs.
  Exact replay returns the same operation; changed reuse conflicts. A paused
  campaign retains queued work. No campaign is fabricated for pre-boot image work.
- `operations.py` and three versioned schemas define bounded intent, result and
  local JSON response records. `quirkbench operation status ID --json` reads status
  without running startup reconciliation. Terminal public results can name only
  outputs retained by that operation. Private deliverables remain disabled until
  private storage and restore availability checks exist.
- The private publication hook validates worker epoch and claim generation within
  the same transaction that attaches output and result references. It has no
  production caller yet: P2b still needs a single lifecycle owner, atomic claims,
  service-managed workers, process-group fencing and safe restart reconciliation.
  The focused test injects a synthetic owner row to verify complete, partial,
  stale and backup/restore behavior; it is not execution evidence.
- Focused acceptance: `.venv/bin/python -m pytest -q tests/test_operations.py
  tests/test_controller.py tests/test_controller_deployments.py
  tests/test_product_contracts.py` passed (83 tests). `git diff --check` passed.
  No image bytes changed and no manual device or release qualification ran.

## P2b — lifecycle ownership and claim foundation (partial, 2026-09-28)

- A persistent controller epoch and an OS lock now fence a single lifecycle
  owner. The serving controller holds that lock for its lifetime; status queries
  do not advance it. Schema upgrades refuse an active coordinator lock. Legacy
  startup also acquires ownership before reconciliation.
- A private claim primitive atomically records the current epoch, generation,
  unit name, deadline and a unique private staging path for pure
  `image_prepare` operations. Old queued work is not claimed automatically after
  restart, paused campaigns block a new stage, and every unresolved worker unit
  block replacement claims. Output publication verifies the persistent current
  epoch in the same transaction as its references. Owner exit interrupts active
  operations and retains unit identity for stop/reconcile; restoring a backup
  clears copied worker identity from active, interrupted and terminal records.
- The required higher-reasoning review reproduced three boundary failures:
  restored interrupted rows retaining source-controller units, staging directory
  reuse after a rolled-back claim, and legacy startup bypassing the owner lock.
  These were fixed and covered by focused regressions. Tests:
  `.venv/bin/python -m pytest -q tests/test_worker.py tests/test_operations.py
  tests/test_controller.py tests/test_controller_deployments.py
  tests/test_controller_review.py` passed (52 tests). Four HTTPS behavioral and
  four restart handoff cases passed with loopback sockets outside the restricted
  sandbox. `git diff --check` passed.
- A second bounded P2b step added `worker_service.py`: a configured installed
  launcher can be submitted as a uniquely named transient systemd user unit,
  with no shell or terminal pipes. The controller persists its boot ID before
  dispatch. Definite preflight failures reserve no unit; an ambiguous launch
  keeps the unit fenced. Reconciliation calls the user manager outside SQLite,
  then checks stop completion and cgroup-v2 `populated=0` before clearing the
  exact unit/generation/boot identity in a transaction. A changed controller
  boot ID is a separate stop proof. Terminal operations retain unit identity
  until this reconciliation; schema upgrades also refuse unresolved terminal
  units. Backup restore clears copied source-controller unit identities.
- Higher-reasoning service-boundary review found immediate unit collection
  after stop, definite preflight reservation, and already-failed units as
  failure cases. Those paths have injected regressions. Focused software check:
  `.venv/bin/python -m pytest -q tests/test_worker_service.py tests/test_worker.py
  tests/test_operations.py tests/test_controller.py
  tests/test_controller_deployments.py tests/test_controller_review.py` passed
  (67 tests). No user service was started in this check.
- P2b remains open. There is no installed worker executable, verified rootless
  container containment inside the owned cgroup, production dispatch caller,
  explicit interrupted-work resume, source-writer reservation, or demonstrated
  CLI-exit survival. The adapter's manager calls and stop proof have only been
  exercised with injected responses. No image bytes changed or hardware/release
  qualification ran.

## P7a — installed recipe registry foundation

- Contracts: C5/C8. `recipe_registry.py` loads strict versioned manifests from the
  installed package directory. A manifest names one prebound Python callable; it
  cannot import code or supply shell commands. Dispatch verifies the exact
  authorized manifest artifact digest, installed source-file hash, mode,
  architecture, capabilities, reviewed privilege set, typed parameter bounds and
  runtime limit. Fault, suspend and physical-observation recipes remain ineligible
  pending their separate review and observation protocol.
- `system-observation.v1.json` is packaged with the Python runtime and pins the
  existing read-only observation callable. The target service advertises that
  recipe only when the installed registry validates. Registry corruption leaves
  evidence upload running, while recipe dispatch fails closed. The existing
  injected simulation recipe path remains for legacy software fixtures; actual
  recovery/candidate runtime uses the installed registry. `install_runtime` copies
  verified manifest bytes with Python code into both recovery and candidate RPM
  staging; image builder identity includes the manifest bytes.
- `recipe-manifest.v1.schema.json` and `tests/test_recipe_registry.py` cover
  schema, code/manifest mismatch, unknown version, privilege and parameter
  rejection, hardware eligibility, mutated installed metadata and target dispatch.
  Focused acceptance: `.venv/bin/python -m pytest -q tests/test_recipe_registry.py
  tests/test_target.py tests/test_runtime.py tests/test_boot.py tests/test_image.py
  tests/test_compose.py` passed (106 tests). Local physical handoff cases also
  passed (17 cases). The 17 HTTPS handoff cases passed when run outside the
  network-restricted sandbox with a loopback-only ephemeral TLS server:
  `.venv/bin/python -m pytest -q tests/test_physical_handoff.py -k https`.
- P6 proposal validation and full human-request dispatch remain open. No image bytes
  were built or physical/release qualification ran. The staging source has
  changed, so previous image qualification cannot apply to future image bytes.

## P7b — human observation records and CLI foundation (2026-09-28)

- Contract: C8. An additive SQLite migration stores typed request and response
  documents, controller receipt time, late status and replayed command IDs. A
  request binds its session to one existing campaign and any named attempt to
  that campaign. Attempt-bound request deadlines cannot exceed the physical
  attempt deadline; answering never changes that deadline or another request.
- `quirkbench session observations SESSION --json` pages bounded summaries;
  `session observation SESSION --request ID --json` reads the exact document;
  `session respond SESSION --request ID --file FILE --request-id ID` durably
  acknowledges an exact response. Identical retries are idempotent, changed
  replies or reused command IDs conflict, and missing input fails before state
  mutation. Controller receipt time prevents a backdated answer from appearing
  timely. `watch CAMPAIGN --session SESSION` displays outstanding requests from
  the same query API; the monitor does not issue or answer them. Post-test
  interpretation can still attach to an attempt after its physical deadline.
- `tests/test_observations.py` covers pre-test, live and post-test questions,
  replay/conflict, late and missing replies, restart/backup restore, CLI and
  monitor rendering. Focused software check: `.venv/bin/python -m pytest -q
  tests/test_observations.py tests/test_product_contracts.py
  tests/test_monitor.py` passed (73 tests). The related operations, worker and
  controller regressions also passed (116 tests combined). No manual device, image, QEMU or
  release qualification ran, and no image bytes changed.
- This is the durable human-response foundation. P4/P6/P7 recipe flow still
  needs a production question issuer and complete session binding before an
  operator can use these requests during an experiment.

## Remaining path to first flash and kernel issue

### P3a1 locked rootfs staging (partial, 2026-09-28)

- `target-assets/build-rootfs.sh` now requires a catalog, rootfs lock, CAS root
  and new absolute output. The former Fedora-release-only DNF command is gone.
  `recovery_rootfs.py` verifies the selected catalog digest, protection policy,
  all baseline input objects, the recovery fragment, complete RPM snapshot and
  target RPM lock before creating a stage. It copies exact RPM bytes, verifies
  their headers, runs DNF5 against only local files with an empty repository
  directory and no plugins, then compares the installed RPM database with the
  retained target lock before publishing the rootfs directory. A GPG key pseudo
  package is refused until its import can be explicitly pinned. RPM package
  names now accept `+`, needed by real Fedora packages such as `libstdc++`.
- New v1 schemas describe the rootfs lock and RPM snapshot. Focused software
  check: `.venv/bin/python -m pytest -q tests/test_recovery_rootfs.py
  tests/test_baseline_catalog.py tests/test_build_pipeline.py` passed (42 tests);
  `sh -n target-assets/build-rootfs.sh` and `git diff --check` passed. The DNF
  transaction was injected in tests. No real package install or image build ran.
- The rootfs installer is a staging primitive, not a complete synthesis flow or release. The
  installed catalog remains empty. Real Fedora source, configuration, RPM bytes
  and their reviewed closure, protected kernel/modules/firmware, dracut and
  runtime integration, and image publication remain open. New image bytes have
  not been qualified.
- A further P3a1 preflight packet added strict `recovery-recipe.v1` validation.
  The recipe pins the exact baseline/catalog entry, retained rootfs lock, Fedora
  kernel source/configuration, recovery fragment, dracut configuration, OCI
  builder digest, runtime revision, unit allowlist and factory/commissioned
  layout. The validator rejects policy weakening, changed/missing CAS objects,
  incomplete required runtime units and a factory layout that cannot fit its
  commissioned experiment allocation. It reuses the existing rootfs closure
  preflight and performs no DNF, kernel build or image mutation. Focused check:
  `.venv/bin/python -m pytest -q tests/test_recovery_recipe.py
  tests/test_recovery_rootfs.py tests/test_baseline_catalog.py` passed (42 tests).
  This is a locked request contract, not a real supported recipe: the installed
  catalog still has no reviewed entry, and synthesis orchestration remains open.
- The next P3a1 step adds a read-only local RPM closure inspector. It requires a
  canonical directory containing only size-bounded regular RPMs, queries each exact header,
  hashes its bytes, detects files changed during inspection and compares the
  resulting identities with a catalog entry's package list. It returns the
  versioned snapshot and target RPM lock bytes for review; it does not retain
  files in CAS, update the installed catalog, run DNF or establish Fedora support.
  Focused check: `.venv/bin/python -m pytest -q
  tests/test_recovery_rootfs.py tests/test_recovery_recipe.py
  tests/test_baseline_catalog.py` passed (44 tests). The deferred Pi accessory
  design changes no v1 recipe or direct-drive behavior.
- A read-only staged-kernel audit now checks the final recovery config and
  `modules.dep`/`modules.builtin` inventory against the reviewed profile. It
  requires the built-in USB boot path and DMI sysfs, rejects missing network
  drivers, protected internal-controller modules, unindexed loadable modules,
  linked module files and unsafe index paths, and reports a digest of the
  installed module bytes. It preserves Fedora's reviewed
  module-compression choice rather than inheriting the older upstream debug
  build's compression setting. This is a prepublication primitive over synthetic
  trees, not a full Fedora kernel build or initramfs inspection. Focused check:
  `.venv/bin/python -m pytest -q tests/test_recovery_module_audit.py
  tests/test_build_pipeline.py` passed (21 tests); `git diff --check` passed.
- Recovery recipe preflight now parses the retained dracut configuration as a
  small literal allowlist instead of accepting its digest alone. It requires
  generic mode, the reviewed USB boot-driver list and exact protected-driver
  omissions, and rejects shell syntax or duplicate settings. The existing
  dracut command adapter now requires an explicit empty staging `--confdir`,
  preventing ambient `/etc/dracut.conf.d` settings from entering this build
  path. Focused check: `.venv/bin/python -m pytest -q tests/test_build.py
  tests/test_build_pipeline.py tests/test_recovery_recipe.py` passed (40 tests);
  `git diff --check` passed. This validates the recipe and command plan only;
  actual Fedora initramfs contents and boot behavior remain unqualified.
- The P3a1 recovery Kconfig fragment now has an exact, reviewed override set
  shared with the staged final-config audit. Recipe preflight rejects unknown,
  duplicate or weakened fragment symbols and malformed pinned Fedora base
  configuration. A pure merge overlays the protected settings while preserving
  unrelated Fedora module choices, including its compression setting, and
  records the staged config digest. This prepares `olddefconfig`; only the
  post-resolution audit can establish the final kernel policy. Focused check:
  `.venv/bin/python -m pytest -q tests/test_recovery_recipe.py
  tests/test_recovery_module_audit.py` passed (42 tests); `git diff --check`
  passed. No kernel build or image qualification ran.
- The existing `KernelBuild` adapter now stages that merged Fedora recovery
  config into a new object directory, verifies its preflight digest before
  writing, synchronizes the file and directory, and plans only `olddefconfig`.
  It refuses reuse or a changed staged config, so the older
  `x86_64_defconfig` path cannot silently replace this recovery input. Focused
  check: `.venv/bin/python -m pytest -q tests/test_build.py
  tests/test_recovery_recipe.py tests/test_recovery_module_audit.py` passed
  (46 tests); `git diff --check` passed. This adapter step does not run make,
  build modules or inspect an initramfs. Synthesis orchestration remains open.
- A recovery-specific final-config guard now validates the resolved `.config`
  independently of module installation. It rejects missing boot/binding
  settings, enabled protected controllers, duplicate symbols, unsafe values
  and a profile boot/protection conflict. `KernelBuild.recovery_compile_plan`
  applies the guard before returning the existing compile/module-install
  commands; the later module-tree audit uses the same guard. Focused check:
  `.venv/bin/python -m pytest -q tests/test_recovery_module_audit.py
  tests/test_build.py tests/test_recovery_recipe.py` passed (48 tests);
  `git diff --check` passed. The caller still must execute `olddefconfig` and
  use this recovery path before compilation; full synthesis orchestration and
  actual final-kernel evidence remain open.
- The existing bounded command runner now has a staged P3a1 recovery-kernel
  sequence. It stages the preflight config, runs `olddefconfig`, checks the
  resolved config before and after compilation, reads the source tree's exact
  `kernelrelease`, installs modules, and audits the one matching module tree
  before returning kernel/output hashes. It uses the same fixed build
  environment as the existing pipeline and retains phase logs on failure.
  Injected tests reject a changed config, failed configure step, missing
  network module, bad release and a module-release mismatch. Focused check:
  `.venv/bin/python -m pytest -q tests/test_recovery_kernel_stage.py
  tests/test_build_pipeline.py tests/test_build.py
  tests/test_recovery_module_audit.py` passed (34 tests);
  `git diff --check` passed. This lower-level stage still requires a source
  tree extracted from a reviewed Fedora SRPM and a pinned builder/container;
  it does not build an initramfs, publish an image or qualify real hardware.
- A P3a1 source-preparation stage now binds an SRPM to a validated catalog
  entry's exact digest and source NEVRA, copies it into a new private staging
  path, and prepares it with RPM's source install and `rpmbuild -bp` commands.
  The existing bounded runner permits only these exact commands and retains
  phase logs. The stage requires one spec and one x86 kernel tree, rejects
  source symlinks escaping that tree, and records the spec and prepared-tree
  digests. Focused check: `.venv/bin/python -m pytest -q
  tests/test_recovery_source_stage.py tests/test_recovery_kernel_stage.py
  tests/test_build_pipeline.py` passed (28 tests); `git diff --check` passed.
  Tests use an injected runner; no real Fedora SRPM was prepared. The installed
  baseline catalog remains empty, and the prepared tree has not been joined
  to rootfs synthesis, initramfs assembly or image publication.
- A further P3a1 initramfs stage now accepts only the current recovery-kernel
  record: it rechecks the final config, module inventory and kernel output
  hashes, validates the pinned Dracut configuration against the reviewed
  profile, creates an empty private config directory and runs the existing
  bounded Dracut command. It hashes a nonempty output and rejects changes to
  kernel/modules/configuration during generation. Injected success and failure
  checks passed with `.venv/bin/python -m pytest -q
  tests/test_recovery_initramfs_stage.py tests/test_recovery_kernel_stage.py
  tests/test_build_pipeline.py tests/test_build.py` (33 tests); `git diff
  --check` passed. No real Dracut run occurred. The archive contents and
  boot path still require audit before image publication, and the installed
  baseline catalog remains empty.
- A P3a1 archive audit now unpacks only the builder-generated initramfs in a
  new private directory through the bounded runner. It checks that `/init`
  resolves within the archive, Fedora initrd identity and the Dracut module
  list are present, and generic `base`, `rootfs-block` and `systemd` support
  exists. It rejects kernel modules from another release, protected internal
  controller modules, private connection paths and embedded machine-specific
  root settings. The stage checks the archive digest before and after
  inspection and retains an audit record with the initramfs hash. Focused
  check: `.venv/bin/python -m pytest -q
  tests/test_recovery_initramfs_audit.py tests/test_recovery_initramfs_stage.py
  tests/test_recovery_kernel_stage.py tests/test_build_pipeline.py
  tests/test_build.py` passed (38 tests); `git diff --check` passed. All archive
  tests are synthetic; the installed catalog still has no reviewed Fedora
  closure, and no real Dracut output, image assembly or boot was qualified.
- P3a1 now has a private base-stage coordinator that preflights one recovery
  recipe before creating an output directory, installs its locked rootfs,
  prepares its exact Fedora SRPM, and builds/audits the protected kernel from
  the retained base config and fragment. The recipe now requires a
  `source_date_epoch` so replay uses the same build timestamp. The coordinator
  verifies the complete prepared source tree before and after compilation,
  including filenames intentionally excluded from rootfs backup hashes.
  Failure leaves private stage logs but returns no usable base-stage record;
  replay requires a new directory. Focused check: `.venv/bin/python -m pytest
  -q tests/test_recovery_synthesis.py tests/test_recovery_recipe.py
  tests/test_recovery_source_stage.py tests/test_recovery_kernel_stage.py
  tests/test_build_pipeline.py` passed (68 tests); `git diff --check` passed.
  The joined path was exercised with fake DNF/RPM/make adapters only. P3a2
  runtime installation must precede the audited Dracut stage, and neither
  image bytes nor hardware qualification were produced.
- P3a2 now separates generic recovery runtime installation from the
  image-specific `boot.json` write. The generic stage copies the console,
  supervisor, NetworkManager RAM policy and Python runtime into the private
  rootfs without inventing GPT identities. It verifies a retained v1 runtime
  revision manifest for the exact copied Python, recipe metadata and unit
  source bytes, checks the installed copy, and requires its custom unit set
  to match the recipe allowlist. The image adapter checks a pre-staged runtime
  against its current builder bytes before regular-file image writes and now
  includes the console unit in its builder fingerprint. Linked runtime
  destinations fail closed. Focused check: `.venv/bin/python -m pytest -q
  tests/test_recovery_runtime_revision.py tests/test_recovery_synthesis.py
  tests/test_recovery_recipe.py tests/test_boot.py tests/test_console.py
  tests/test_image.py` passed (111 tests); `git diff --check` passed. The
  installed baseline catalog remains empty; runtime and image behavior were
  exercised with synthetic rootfs trees only. The following handoff packet binds
  it to the audited Dracut stage; release publication remains open. No image
  bytes or physical qualification were produced.
- The P3a1/P3a2 handoff now accepts only matching recipe, base and runtime
  records in one private stage before running Dracut. It rechecks the full
  prepared source tree, installed runtime revision, reviewed custom units,
  recovery service links and masks, and absence of image-specific boot identity.
  The existing kernel/module/output and initramfs archive audits run under the
  same recipe and bounded runner. Source and runtime are rechecked after Dracut;
  failures keep private logs and return no completed handoff record. Contracts:
  C1/C4 and `recovery-base.md`; files: `src/quirkbench/recovery_synthesis.py`,
  `src/quirkbench/boot.py`, `tests/test_recovery_synthesis.py`. Focused check:
  `.venv/bin/python -m pytest -q tests/test_recovery_synthesis.py
  tests/test_recovery_initramfs_stage.py tests/test_recovery_runtime_revision.py
  tests/test_boot.py tests/test_image.py` passed (86 tests);
  `git diff --check` passed. The installed baseline catalog remains empty and
  this path uses synthetic builders only. It produced no factory image bytes or
  QEMU/hardware qualification; P3a3 assembly and publication remain open.
- The first P3a3 packet now prepares the existing regular-file image adapter
  from a recipe-bound, audited private stage. It rechecks the prepared source
  tree and kernel/config/initramfs hashes, the reviewed runtime and unit set,
  the exact recovery profile and installed module audit, and rejects saved
  network profiles, credentials,
  machine identity or enrolled state in factory rootfs. It writes a private
  provenance file using the image adapter's existing format and returns its
  `ImageInputs` with recipe layout; it does not call image tools or publish an
  image. The adapter now accepts an explicit reviewed recovery profile audit
  while preserving its legacy config check and legacy input fingerprint when
  the new fields are absent. Contracts: C1/C4 and `recovery-base.md`; files:
  `src/quirkbench/recovery_image_plan.py`, `src/quirkbench/recovery_synthesis.py`,
  `src/quirkbench/image.py`,
  `tests/test_recovery_image_plan.py`. Focused check: `.venv/bin/python -m
  pytest -q tests/test_recovery_image_plan.py tests/test_recovery_synthesis.py
  tests/test_image.py tests/test_boot.py` passed (92 tests);
  `git diff --check` passed. The installed baseline catalog is empty. This
  packet produced no image bytes or QEMU/hardware qualification. Actual image
  assembly, payload capacity measurement, signed release record and publication remain
  open; the image boundary needs the handoff's required higher-reasoning review.
- A further P3a3 packet defines a strict, unsigned recovery release candidate
  v1 record. It reads an existing regular-file image and the image adapter's
  canonical manifest/checksum sidecars, verifies image size and hash against
  the recipe layout, rejects smoke/candidate/enrolled metadata, and recomputes
  current builder/input identities before binding the image to retained RPM,
  toolchain, source, protected kernel, module, runtime and Dracut references.
  The record explicitly says `unqualified` with no qualified capabilities;
  its validator rejects a fabricated qualification claim. Contracts: C1/C4
  and `recovery-base.md`; files: `src/quirkbench/recovery_release.py`,
  `schemas/recovery-release-candidate.v1.schema.json`,
  `tests/test_recovery_release.py`. Focused check: `.venv/bin/python -m pytest
  -q tests/test_recovery_release.py tests/test_recovery_image_plan.py
  tests/test_image.py` passed (43 tests); `git diff --check` passed. Tests use
  synthetic sidecars and an injected image identity, not assembled image bytes.
  This does not sign or publish a release. Exact filesystem capacity validation, signing,
  publication recovery, real Fedora inputs and release qualification remain open.
- P3a3 now scans the staged recovery root without reading file contents and
  records observed bytes, entry count and a conservative partition estimate.
  It rejects clear root or ESP overflow, special files and unbounded entry
  counts before provenance or external image tools are written/run. The image
  adapter repeats the check before assembly, and the unsigned candidate record
  remeasures the payload before reporting observed sizes. Contracts: C1/C4 and
  `recovery-base.md`; files: `src/quirkbench/recovery_capacity.py`,
  `src/quirkbench/recovery_image_plan.py`, `src/quirkbench/image.py`,
  `src/quirkbench/recovery_release.py`,
  `schemas/recovery-release-candidate.v1.schema.json`,
  `tests/test_recovery_capacity.py`, `tests/test_recovery_image_plan.py`,
  `tests/test_recovery_release.py`. Focused check: `.venv/bin/python -m pytest
  -q tests/test_recovery_capacity.py tests/test_recovery_image_plan.py
  tests/test_recovery_release.py tests/test_image.py` passed (48 tests);
  `git diff --check` passed. The test payloads are synthetic; no image was
  built. Passing this estimate does not prove that ext4/FAT packing succeeds.
  Actual assembly, signed publication, real Fedora inputs and release
  qualification remain open.
- A further P3a3 packet prepares a versioned, canonical checksum statement
  binding the unsigned candidate, image bytes, image checksum sidecar and image
  manifest. Its schema and strict loader reject unknown fields, duplicate keys
  and noncanonical bytes. A controller-only GPG adapter signs it with a full
  configured key fingerprint outside the image directory. It checks the detached
  signature and rechecks the source material before returning private staging
  bytes. Failed,
  revoked, mismatched or stale signatures return no result. Contracts: C0/C4 and
  `recovery-base.md`; files: `src/quirkbench/recovery_distribution.py`,
  `schemas/recovery-checksum-statement.v1.schema.json`,
  `tests/test_recovery_distribution.py`. Focused check: `.venv/bin/python -m
  pytest -q tests/test_recovery_distribution.py tests/test_recovery_release.py`
  passed (30 tests); `git diff --check` passed.
  Tests use a synthetic signer and image identity; no key, image or release was
  created. The record remains unqualified and unpublished. Durable atomic release
  publication, independent public-key verification, real Fedora inputs, image
  assembly and release qualification remain open. A higher-reasoning review is
  still needed before the storage publication boundary is enabled.

### P3a2 recovery boot policy and network staging (partial, 2026-09-28)

- Contracts: C4 and `recovery-base.md`. Both fixed recovery GRUB paths now carry
  explicit `selinux=0`; the candidate entry does not inherit it. Recovery boot
  parsing rejects a missing, changed or duplicate SELinux flag before first-boot
  commissioning can write partitions.
- Runtime installation replaces any staged resolver file with a link to
  NetworkManager's RAM resolver, pins `dns=default` and `rc-manager=symlink`, and
  rejects saved network profiles or escaping configuration links in the factory
  tree. The candidate runtime stages the same transient resolver link without
  changing its SELinux boot policy. Recovery and candidate units no longer ask
  for global `systemd-udev-settle`; the boot verifier waits at most 30 seconds
  for the expected USB PARTUUID nodes before strict identity verification.
- Recovery runtime staging replaces `/etc/machine-id` with an empty regular file
  for systemd's transient read-only-root handling and removes any staged
  `/var/lib/dbus/machine-id` that could supply an early-boot factory ID. It
  rejects escaping or invalid identity paths before runtime installation and
  leaves candidate machine identity policy to the candidate build.
- Recovery staging pins `default.target` to `multi-user.target` and rejects
  unreviewed pre-existing `/etc/systemd/system` wants/requires/upholds links,
  including socket activation links. It also rejects symlinked unit destinations
  and dependency/drop-in directories before writing the recovery units. This is
  an explicit `/etc` policy; vendor `/usr/lib/systemd/system` dependencies and
  package presets still need review against the locked Fedora package closure.
- `console.py` and the recovery-only `quirkbench-console.service` add a status
  screen on physical tty1 without a login or network dependency. It can show a
  blocked boot while verification is pending, but invokes the fixed `nmtui`
  command only after the RAM boot record confirms recovery identity and evidence
  mounting and the private NetworkManager profile path is a root-owned tmpfs.
  The recovery image masks tty1's getty; candidate policy is unchanged.
  The screen does not claim connectivity, enrollment or safe shutdown.
- Focused software check: `.venv/bin/python -m pytest -q tests/test_boot.py
  tests/test_console.py tests/test_image.py tests/test_runtime.py` passed (84 tests).
  `systemd-analyze verify target-assets/quirkbench-console.service
  target-assets/quirkbench-recovery.service
  target-assets/quirkbench-candidate.service` passed with socket access permitted
  (exit 0, no diagnostics). No real systemd boot, image build, hardware test or
  release qualification ran. Image inputs changed, so earlier qualified image
  bytes do not apply to a future build.
- P3a2 files changed: `src/quirkbench/boot.py`, `src/quirkbench/image.py`,
  `src/quirkbench/console.py`, the recovery/candidate/console target units,
  `tests/test_boot.py`, `tests/test_image.py`, `tests/test_console.py` and this
  progress record.
- P3a2 remains open: vendor service allowlist audit, pairing and
  selected network profile replay, and cold/restored network operation still
  need implementation and validation. Commissioning still uses one bounded
  `udevadm settle` after creating new partitions.

### P3a5 attended commissioning gate (partial, 2026-09-28)

- Contracts: C4, C8 and the P3a5 handoff. Recovery boot no longer calls the
  partition planner or executor automatically. It verifies the boot-media
  identity, mounts the verified boot-state partition, then requires a completed
  commissioning journal with the same identity and observed geometry before
  preparing data/evidence mounts. Factory media and interrupted commissioning
  stay blocked; the independent offline console still starts.
- The offline tty1 console now offers attended setup before network or
  enrollment. It verifies the USB boot identity and boot-state journal mount,
  displays disk GUID/capacity, target RAM, log budget and proposed partition
  sectors, offers larger experiment/library allocations within a bounded MiB
  range, and requires the operator to type the complete disk GUID after reviewing
  the selected geometry. A wrong answer or changed displayed disk/RAM state runs no partition command. The
  confirmation is synced to the existing boot-state journal before execution;
  the executor refuses an absent or unconfirmed journal and rereads the plan
  under the same commissioning lock. Interrupted runs retain their original
  geometry, selected sizes and format intents. A completed journal is checked
  against the current media and factory sizing bounds before the screen reports
  completion; recovery boot applies the same geometry check. It then reads
  current target RAM without altering the media. If the existing evidence
  allocation is too small, recovery and evidence access continue, while the
  target omits deployment preparation and shows the capacity block on tty1.
- The higher-reasoning storage review reproduced and drove fixes for stale RAM
  during confirmation, a completed-journal shortcut that skipped identity
  comparison, and a filesystem appearing after format intent but before mkfs.
  The executor now refuses that newly observed filesystem rather than formatting
  it. Synthetic regressions cover those cases, wrong GUID, cancellation,
  interrupted writes, selected sizing/retry, existing partitions, low capacity
  and journal locking.
  Focused check: `.venv/bin/python -m pytest -q tests/test_capacity_setup.py
  tests/test_commission.py tests/test_console.py tests/test_boot.py
  tests/test_image.py tests/test_runtime.py
  tests/test_recipe_registry.py` passed (159 tests).
- The attended setup remains software only. Existing QEMU image fixtures expect
  automatic first-boot commissioning and need an attended
  confirmation adapter before future image bytes can be qualified. No image,
  QEMU or physical test ran; changed runtime bytes remain unqualified.

P1c's installed catalog still needs a reviewed supported baseline with real
Fedora source/configuration and retained RPM bytes. P3a1 cannot finish an actual
rootfs without those inputs. P2 still needs persistent controller
services and fenced workers. P3a must synthesize and publish the generic recovery
image with actual locked inputs; P3b–f must complete identity, network, enrollment
and activation. P4 then needs an attended target round trip using the exact
baseline. P7b still needs production request issuance and session integration. A
real target, external drive and operator-run flash/boot are required to evaluate
hardware behavior. Existing
VM results or these software tests cannot qualify new image bytes.

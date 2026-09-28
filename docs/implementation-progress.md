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
- This is a staging primitive, not a finished RecoveryRecipe or release. The
  installed catalog remains empty. Real Fedora source, configuration, RPM bytes
  and their reviewed closure, protected kernel/modules/firmware, dracut and
  runtime integration, and image publication remain open. New image bytes have
  not been qualified.

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

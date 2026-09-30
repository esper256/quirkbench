# Implementation progress after the handoff

> **Local artifact reset, 2026-09-30:** the user authorized permanent deletion of
> the checkout-local `.quirkbench` tree, including delivered images, signing keys,
> retained inputs and local validation/qualification logs. Historical identities
> and results below remain records of those runs; their bytes are no longer
> available. No replacement image or new qualification is supplied by the reset.
> See [current local state](local-state-maintenance.md).


**Current design, revised 2026-09-29:** the historical packet entries below describe
the contracts and bytes used at the time. Stock-kernel recovery with boot-device-only
storage policy supersedes shared recovery/candidate exclusions. V2 contracts and
stock synthesis and fixed image coordination now exist; the software handoff below
records validation and product-operation limits. Attended manual authenticated setup precedes later
pairing, wizards, managed scheduling and unattended grants. Historical passing tests
and build results do not qualify this revised design. See the
[revision audit and remaining work](design-revision-20260929.md).

This log records bounded software packets since source commit
`dca51b42d14dbf331a75d5212e19839141b2a825`. The current checkout starts at
`066f632e3f5303960cf8a01655c24fb891a78490` plus working-tree changes.
It does not advance a physical or release milestone. The image bytes
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
- The installed catalog now contains the exact `fedora44-firstboot-v1` entry.
  Its Fedora 44 source, configured kernel, 248 signed target RPMs, rootfs lock,
  builder archive, toolchain and recipe objects are retained under
  `.quirkbench/inputs/`. Selection still requires the matching CAS objects and
  leaves `build_validation_pending`; it does not claim experiment or hardware
  qualification. The Fedora 44 example remains illustrative.
- A candidate Fedora 44 kernel source input is now retained locally under
  `.quirkbench/inputs/fedora44-kernel-7.2.7/`: DNF5 `updates-source` supplied
  `kernel-7.2.7-200.fc44.src.rpm` (SHA256
  `7dfc6f39d52fbae59e0024fc1bc1d666900b6848d7e1fff53813d8b6b03aa5e8`).
  An isolated RPM key database verified header and payload signatures against
  Fedora 44 fingerprint `36F612DCF27F7D1A48A835E4DBFCF71C6D9F90A6`,
  checked against [Fedora's published key list](https://fedoraproject.org/security/).
  The SRPM yielded `kernel-x86_64-fedora.config` (SHA256
  `cd11b96fabf3cbdaf1063eff6b66fb012edba411762ad994e3eed6236911ce18`)
  and `kernel.spec` (SHA256
  `2a5e8ca46b9ced20510bd068cb16c5a1fc7bfb9067ff403cf6414dd461470885`).
  Acquisition used read-only repository metadata and an isolated extraction
  directory; no host package was installed. The initial sandbox DNS query failed;
  the network-approved retry downloaded the SRPM. Full `%prep` and the exact
  DNF5 rootfs installation have since completed in the restricted rootless
  builder. The protected kernel and first image remain unqualified until their
  own retained records and byte inspections complete. Exact acquisition
  commands and retained identities are in [the acquisition record](fedora44-input-candidate.md).
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
  source-writer reservation, or demonstrated
  CLI-exit survival. The adapter's manager calls and stop proof have only been
  exercised with injected responses. No image bytes changed or hardware/release
  qualification ran.
- A further local-only P2b step adds read-only worker claim verification against
  the live SQLite epoch, exact operation generation/unit/boot/deadline, private
  staging ancestry and the process's service cgroup. It does not grant
  publication authority: the controller still fences reference commits. An
  explicit owner API can requeue an interrupted `image_prepare` after verified
  unit stop, retained CAS input checks and rejection of mutable local/source
  inputs or arbitrary arguments. It preserves partial public outputs and uses a fresh stage and claim
  generation. It is not yet exposed by a running service or wired to a worker
  executable. Focused check: `.venv/bin/python -m pytest -q
  tests/test_worker_claim.py tests/test_worker.py tests/test_worker_service.py
  tests/test_operations.py` passed (50 tests). A review caught a generation race
  across CAS verification; the commit now checks the original generation.
  No downloads or image work ran.

## P2c — bounded operation event query (partial, 2026-09-28)

- A read-only `Controller.operation_events` query and `quirkbench operation
  events ID` command page durable events with integer cursors, at most 100
  entries and a 64 KiB canonical response budget. A large first event fails
  explicitly; later events return a continuation cursor. The query does not
  start lifecycle recovery or infer progress percentages. Contracts: C2/C8 and
  P2c; files: `src/quirkbench/controller.py`, `src/quirkbench/cli.py`,
  `tests/test_operations.py`. Focused check: `.venv/bin/python -m pytest -q
  tests/test_operations.py tests/test_cli.py` passed (19 tests);
  `git diff --check` passed. Event rendering and private deliverable availability
  remain open. No image bytes changed or qualification ran.
- P2c also adds `quirkbench operation output ID DIGEST --offset N --length N
  --json` for at most 16 KiB of base64 public bytes per call. It reads only a
  CAS object attached to that operation with role `output`; input/source refs
  and private deliverables cannot be read through it. Linked objects and a
  changing file fail closed. Contracts: C2/C8 and P2c; files:
  `src/quirkbench/controller.py`, `src/quirkbench/cli.py`,
  `tests/test_operations.py`. Focused check: `.venv/bin/python -m pytest -q
  tests/test_operations.py tests/test_cli.py` passed (20 tests);
  `git diff --check` passed. The API reads existing output bytes and does not
  certify release artifacts or create image bytes.
- Human `operation status` now renders the durable stage, wait event, deadline,
  measured counters without inventing a denominator, public-output count and
  attached failure code/message. The bounded failure read verifies the CAS digest
  and rejects links; a missing record leaves the historical FAILED state visible
  with an explicit unavailable-detail message. Focused check: `.venv/bin/python
  -m pytest -q tests/test_operations.py tests/test_cli.py tests/test_monitor.py`
  passed (40 tests); `git diff --check` passed. Operation progress production still
  depends on the P2b worker path. No image bytes or release evidence changed.
- The human `operation events` view now includes UTC timestamps and fixed
  state/stage/generation/output-count facts for known events, plus the paging
  cursor. It never prints arbitrary event document fields or output digests;
  `--json` retains the bounded structured page. Focused check:
  `.venv/bin/python -m pytest -q tests/test_operations.py tests/test_cli.py
  tests/test_monitor.py` passed (41 tests). Event production still depends on
  the P2b worker path. No image bytes or downloads changed.

## P2d — packaged target-asset foundation (partial, 2026-09-28)

- The wheel now carries the existing `target-assets` tree as
  `quirkbench.assets`. A resource resolver uses those installed bytes and falls
  back to the source tree only during development. Recovery runtime staging,
  synthesis, image input identity and composition no longer require a sibling
  checkout `target-assets` directory after installation. The source tree remains
  the single asset source for wheel construction.
- Contracts: C8 and the P2d handoff; files: `pyproject.toml`,
  `target-assets/__init__.py`, `src/quirkbench/package_resources.py`,
  `src/quirkbench/boot.py`, `src/quirkbench/image.py`,
  `src/quirkbench/recovery_image_plan.py`,
  `src/quirkbench/recovery_synthesis.py`, `src/quirkbench/compose.py`,
  `tests/test_package_resources.py`. A clean copied project built a wheel; an
  isolated Python process loaded its recovery service from the extracted wheel
  without the checkout. Focused check: `.venv/bin/python -m pytest -q
  tests/test_package_resources.py` passed (2 tests), and the affected boot,
  image, synthesis, runtime-revision and compose suites passed (117 tests);
  `git diff --check` passed. No image bytes or hardware/release qualification
  were produced; builder and runtime source identities changed.
- A further P2d resource packet packages the published JSON Schemas, example
  documents and agent guide from their existing source directories. Installed
  callers can resolve those resources without a checkout; development callers
  retain the source-tree fallback. The wheel test compares all packaged schema
  and example bytes plus the guide against the source files in an isolated
  process. Contracts: C8 and the P2d handoff; files: `pyproject.toml`,
  `src/quirkbench/package_resources.py`, package marker files in `schemas/`,
  `examples/` and `docs/`, and `tests/test_package_resources.py`. Focused check:
  `.venv/bin/python -m pytest -q tests/test_package_resources.py` passed
  (2 tests); `git diff --check` passed. The example documents are fixtures,
  not installed supported baselines or authorization to run an experiment.
  Controller installation, setup, service behavior and image qualification
  remain open. No image bytes changed or qualification ran.
- A bounded P2d state-discovery packet reads a versioned controller selection
  from `XDG_CONFIG_HOME/quirkbench/controller.json` (or the user's default config
  home). An explicit `--state` retains priority; absence of a selection retains
  the legacy current-directory `.quirkbench` behavior. A present but invalid,
  linked or unavailable selection fails closed before a controller can create a
  fresh database in the wrong location. This is read-only discovery; it does not
  migrate state or install services. Contracts: C0/C8 and P2d; files:
  `src/quirkbench/state_config.py`, `src/quirkbench/cli.py`, the selection schema
  and example, and `tests/test_installation.py`. Focused check:
  `.venv/bin/python -m pytest -q tests/test_installation.py tests/test_cli.py
  tests/test_operations.py tests/test_package_resources.py` passed (25 tests);
  `git diff --check` passed.
  Setup must still write this selection after creating and validating its state
  root; the archive/launcher, user-service checks and clean-home setup flow
  remain open. No image bytes changed or qualification ran.
- A further P2d packet adds the provisional `quirkbench setup-state` command for
  state selection while the full public `setup` command remains unimplemented. It creates
  a private controller state directory, writes the versioned selection atomically
  under a setup lock, verifies the selected path and returns the same selection
  on repeat calls. An existing current-directory `.quirkbench` requires explicit
  `--state`; a selected root cannot be switched implicitly, and linked,
  nonprivate or unrelated nonempty default directories are refused. Setup does
  not initialize a controller database or claim that user services are ready;
  its response reports `service_management: pending` and
  `background_work_ready: false`. Contracts: C0/C2/C8 and P2d; files:
  `src/quirkbench/state_config.py`, `src/quirkbench/cli.py`,
  `tests/test_installation.py` and `tests/test_package_resources.py`. The
  extracted-wheel test runs setup in an isolated Python process and clean home
  without the checkout. Focused check: `.venv/bin/python -m pytest -q
  tests/test_installation.py tests/test_cli.py tests/test_operations.py
  tests/test_package_resources.py tests/test_product_contracts.py` passed
  (75 tests); `git diff --check`
  passed. The controller archive/launcher and service-manager setup remain
  open. No image bytes changed or qualification ran.
- Another bounded P2d packet adds `quirkbench setup-check` and an injected,
  read-only systemd user-manager probe. It makes bounded `systemctl --user show`
  and `loginctl show-user` queries, reports manager reachability and the observed
  lingering setting, and gives optional logout instructions without enabling
  lingering or starting services. Missing, timed-out or malformed replies remain
  unavailable/unknown; captured stderr is never returned. It explicitly reports
  service installation unverified and background work not ready. Contracts: C2/C8
  and P2d; files: `src/quirkbench/controller_setup.py`,
  `src/quirkbench/cli.py`, `tests/test_controller_setup.py`. Focused check:
  `.venv/bin/python -m pytest -q tests/test_controller_setup.py
  tests/test_installation.py tests/test_cli.py tests/test_package_resources.py`
  passed (25 tests); `git diff --check` passed. The controller coordinator unit,
  installed launcher and service lifecycle still need implementation. No image
  bytes changed or qualification ran.
- The read-only setup report now also states whether `podman` and `distrobox`
  are available and lists missing builder tools without installing host
  packages. In the `dev` Distrobox both are absent from PATH, although the
  Bazzite host has Podman 5.8.4 and Distrobox 1.8.2.5. A host-scope probe
  reached `systemctl --user` (version `259.8-1.fc44`) and found lingering
  disabled. The earlier `loginctl` PID 1 error was from the Distrobox namespace,
  not evidence that the host lacks systemd. Service lifecycle remains unverified.
  Focused check: `.venv/bin/python -m pytest -q tests/test_controller_setup.py
  tests/test_installation.py tests/test_cli.py` passed (23 tests);
  `git diff --check` passed. Background-work readiness remains false until
  the installed service path is implemented and checked.
- The setup probe now labels its process scope and reports absent tools inside
  Distrobox as `not_visible`, not missing from the controller host. It does not
  query `loginctl` from inside Distrobox, where PID 1 can be misleading, and
  directs the operator to run the check from a controller host shell. The
  Bazzite host tools above were independently confirmed through host execution;
  no service was installed or started.
- P3a1 input acquisition confirmed a Fedora 44 rootless base image by immutable
  digest and retained 248 candidate recovery RPMs after a weak-dependency solve
  that includes `NetworkManager-wifi` and `parted`. An isolated Fedora 44 key
  database verified each RPM signature and payload digest. A new read-only
  candidate directory inspector produced exact snapshot and target-lock bytes
  without requiring a prematurely installed baseline catalog. See
  [the candidate record](fedora44-input-candidate.md) for digests and logs.
  This is input review, not a rootfs install or image qualification; catalog
  approval, CAS retention, builder toolchain and runtime audits remain open.
  The Podman pull, DNF download, signature check and candidate inspection all
  completed successfully. Focused software check: `.venv/bin/python -m pytest
  -q tests/test_recovery_rootfs.py tests/test_recovery_recipe.py
  tests/test_baseline_catalog.py` passed (66 tests); `git diff --check` passed.
  No recovery disk image bytes changed and no release qualification ran.

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

- A local-only Fedora 44 kernel-config pass exposed 15 legitimate mixed-case
  Kconfig symbols rejected by the base/final config parsers. Both parsers now
  accept them while the exact protected override set remains unchanged. The
  retained Fedora config and recovery fragment merged deterministically to
  SHA-256 `95d6cc99896aa934dc4582946566b8cca600b5b3e8fe1461f1080f22f3f92029`;
  this is pre-`olddefconfig` and not a resolved kernel. An offline rootless
  Podman `%prep` diagnostic applied Fedora's patch but failed at missing
  `%py3_shebang_fix` before generated configs. The incomplete private stage,
  exact command, log and status are retained under
  `.quirkbench/inputs/fedora44-source-prep/`. The Containerfile now requests
  the missing macro/config-generation packages; source staging checks the
  shebang macro before unpacking. The old local builder image still lacks
  them. Focused software check: `.venv/bin/python -m pytest -q
  tests/test_recovery_source_stage.py tests/test_recovery_synthesis.py
  tests/test_recovery_recipe.py tests/test_recovery_module_audit.py` passed
  (72 tests). A boundary review caught the macro phase missing from the real
  bounded-runner allowlist; it is now routed and covered. No package or image
  download ran; source prep exit was 1.
- A config-only `olddefconfig` on that incomplete patched tree exposed Fedora
  Kconfig selectors that re-enabled `KEXEC_FILE` and `NVME_CORE`. The reviewed
  fragment and final-config audit now also disable handover and four remote/loop
  NVMe selectors. A second offline config pass exited 0 and passed the
  protected final-config audit (resolved SHA-256
  `1dec792a0cfa1312469e4ef52d5103ab2299e2efdf0d1feaac55ca810769d0a2`).
  The exact command, old local builder identity, hashes and log are in
  `.quirkbench/inputs/fedora44-source-prep/config-resolve-v2.status.json`.
  This is provisional: `%prep` stopped before generated configs, so a complete
  source build and module audit remain open. The changed fragment invalidates
  the earlier candidate recipe/lock identity. No download or image build ran.
- The rootfs Podman command planner now rereads the live worker claim before it
  returns argv and checks the caller's service cgroup, claim generation, boot,
  deadline and private stage. Staging accepts catalog and rootfs-lock CAS digests
  instead of arbitrary host paths, reads their exact bytes through bounded
  no-follow file descriptors, and copies only the selected immutable closure.
  The remaining CAS objects are copied with no-follow handles, per-object and
  aggregate byte limits, and a hash of the bytes actually staged.
  This remains a command plan: there is no installed executable, container
  launch, output publication or real rootfs. Focused check:
  `.venv/bin/python -m pytest -q tests/test_recovery_podman.py
  tests/test_worker_claim.py tests/test_recovery_rootfs.py` passed (35 tests).
  No downloads ran.
- The rootfs planner now binds its derived builder config ID to the current
  operation's immutable input record, fetched by the claim's SQLite input
  digest. It checks that the staged catalog and rootfs lock match that record
  and that the referenced builder OCI archive remains in controller CAS with
  exact bytes. A different image with the same Fedora base marker, old unbound
  operation, changed archive and stale claim fail closed. This preserves v1
  recipe/catalog wire fields. A bounded read-only OCI inspector now confirms
  the archive's sole manifest, exact derived config digest, x86-64/Linux config
  and each referenced layer digest before planning execution. It accepted the
  retained older candidate archive, with a record at
  `.quirkbench/inputs/fedora44-builder-candidate/oci-inspection-v1.status.json`.
  This does not launch an image. Explicit resume accepts the same exact, retained
  rootfs input binding after worker stop; arbitrary arguments and mutable
  source paths remain blocked. Focused check: `.venv/bin/python -m pytest -q
  tests/test_recovery_podman.py tests/test_worker_claim.py
  tests/test_worker.py tests/test_operations.py` passed (51 tests).
  The new Python module changes the captured recovery runtime source revision;
  any older candidate record is unqualified for these bytes. No download or
  image build ran.
- A rootless Fedora 44 builder candidate was built from the pinned base image
  with a Containerfile guard that rejects a moving tag and a mismatched Fedora
  release. The build succeeded; its local manifest/config identities, 595-RPM
  installed lock, toolchain lock and retained 402 MiB OCI archive are recorded
  in [the candidate input record](fedora44-input-candidate.md). A read-only
  audit of the 248 candidate recovery RPM payloads found providers for 11
  essential paths. The installed baseline catalog remains empty, and no rootfs,
  kernel or disk image was built or qualified. The named Distrobox and
  controller user-service integration remain open. Focused check:
  `.venv/bin/python -m pytest -q tests/test_build_pipeline.py` passed
  (14 tests); the real rootless builder build succeeded, while separate
  tag-based and wrong-release preflight builds failed before DNF; `git diff
  --check` passed.
- The existing `environments/assemble.ini` Distrobox profile was inspected
  with `distrobox assemble create --dry-run`. It would use `--privileged` and
  bind the host `/dev`, contrary to recovery synthesis's no-block-device rule;
  no new Distrobox was created. A separate rootless Podman probe with no host
  mounts or network observed no block devices and enforced 4 CPU / 4 GiB cgroup
  caps. The operator selected a separate restricted Podman worker for recovery,
  keeping Distrobox for Codex and general development. A focused P3a1 adapter
  now copies only preflighted locked inputs into a private worker stage and
  prepares a fixed local, nonroot Podman command with no network or host device
  mount. It uses private `:Z` relabeled copies, keeps original source/CAS
  labels, and disables Podman cgroup creation so the future systemd user unit
  can own launcher, conmon and payload together. The command requires an
  active fenced claim and does not execute yet. The user-service adapter now
  requests 400% CPU, 4 GiB memory, zero swap and 4096 tasks and rejects a
  launched unit when its cgroup files do not enforce those bounds. Focused
  `tests/test_recovery_podman.py tests/test_worker_service.py
  tests/test_recovery_rootfs.py` passed (43 tests). Rootfs dispatch still
  needs live launcher/conmon/payload membership verification, durable launch
  and logs before a real rootfs stage. The probe does not
  qualify a real rootfs or image build.
- A development-only bounded Podman starter for future ad hoc kernel builds
  now creates the delegated user service and gives the container its own child
  cgroup for `podman stats`, while the
  delegated systemd user service retains the aggregate CPU, memory, swap,
  task and stop boundary. It rejects missing delegation, unbounded services
  and conflicting Podman resource flags. The Fedora 44 controller probe used
  the retained local builder image with no network or host mount: launcher and
  conmon were under the service `runtime` subgroup; payload was under its
  `libpod-*` child; plain `podman stats` showed live CPU/memory with the 1 GiB
  limit. Normal completion removed the probe container. Stopping a long-running
  probe hit the unit stop timeout, killed its processes and left an exited
  Podman record, removed with
  `podman rm`; this needs explicit state cleanup before stage reuse. The
  existing running kernel build and the P3a1 fixed recovery rootfs command
  were not changed. See [the builder environment guide](../environments/README.md).
- The later VMD retry was started from a historical
  `--cgroups=disabled` script with `Delegate=no`, so the helper above was not
  in its execution path and `podman stats` omits that live container. The
  current compile remains in progress in its bounded service. The new
  `start-bounded-podman-build.sh` command now owns both the service properties
  and the fixed helper for future run records; AGENTS.md requires it for new
  ad hoc kernel builds. A short offline probe launched through that exact
  command appeared in plain `podman stats` with a 4 GiB denominator, and
  systemd showed `Delegate=yes`, `DelegateSubgroup=runtime` and the 4 GiB
  service cap. The offline probe used the retained local image
  `sha256:6e51e11c610ebfb6560c231ced072827ade8eaea4a1e82ca0447e691021325db`
  with `sleep 25`, `--network=none`, and no host mount. Its log and final
  status are in
  `.quirkbench/inputs/cgroup-starter-probe-20260929/worker.log` and
  `worker.exit.status` (0); Podman removed the container on exit and the
  completed transient unit was stopped. A second five-second offline probe
  after the bound and status review is recorded in
  `.quirkbench/inputs/cgroup-starter-probe-20260929-v2/worker.log` and
  `worker.exit.status` (0); its container was also removed and its transient
  unit stopped. The recorder now writes `running` before Podman starts, so a
  killed or timed-out build retains an explicit incomplete status. It did not
  compile a kernel or change the active retry.
- A local-only P3a1 retention pass copied the 248 verified Fedora RPMs and ten
  associated candidate inputs into an isolated candidate CAS. It then verified
  all 258 stored objects by digest; the command, status and manifest hashes are
  in [the Fedora 44 candidate record](fedora44-input-candidate.md). This closes
  the missing-byte retention task for the current candidate without adding an
  installed catalog entry. Reviewed repository configuration, build recipe,
  vendor boot policy and protected kernel result remain open. No network,
  rootfs/image build or qualification was involved.

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
- The next P3a3 packet adds independent verification of that private statement
  using a separately supplied public key and an isolated temporary GPG home.
  It rejects changed image/sidecar/candidate bytes, a missing or invalid key,
  wrong fingerprint and invalid signature before returning an unqualified
  statement. Contracts: C0/C4 and `recovery-base.md`; files:
  `src/quirkbench/recovery_distribution.py`,
  `tests/test_recovery_distribution.py`. Focused check: `.venv/bin/python -m
  pytest -q tests/test_recovery_distribution.py tests/test_recovery_release.py`
  passed (39 tests, one opt-in GPG test skipped in the sandbox); `git diff
  --check` passed. The disposable-key GPG test passed separately with socket
  access enabled. No real
  recovery image was built or published; atomic publication and release
  qualification remain open.
- A further P3a3 verification packet closes the candidate wire-byte gap:
  `load_release_candidate` now rejects noncanonical and duplicate-key JSON, and
  independent checksum verification accepts exact candidate bytes rather than a
  parsed object. The signed statement therefore binds the bytes a distributor
  would deliver; changed recipe hashes or qualification claims fail before GPG
  verification. Contracts: C0/C4 and `recovery-base.md`; files:
  `src/quirkbench/recovery_release.py`,
  `src/quirkbench/recovery_distribution.py`,
  `tests/test_recovery_release.py`, `tests/test_recovery_distribution.py`.
  Focused check: `.venv/bin/python -m pytest -q
  tests/test_recovery_distribution.py tests/test_recovery_release.py` passed
  (46 tests, one opt-in GPG test skipped in the sandbox); the opt-in
  disposable-key GPG test passed with socket access; `git diff --check` passed.
  No image bytes or release qualification were produced. Atomic distribution
  publication and its required storage-boundary review remain open.
- Another P3a3 packet adds read-only signed-bundle inspection over exact
  `release-candidate.json`, `checksums.json` and detached-signature sidecars.
  It refuses missing, linked, oversized or changing files and an image adapter
  pending journal. Both signing and inspection now reject a factory manifest
  that carries commissioned/candidate state, extra enrollment fields, invalid
  partition identities or geometry, even if a matching candidate hash is
  supplied. Contracts: C0/C4 and `recovery-base.md`; files:
  `src/quirkbench/recovery_distribution.py`,
  `tests/test_recovery_distribution.py`, `tests/test_recovery_release.py`.
  Focused check: `.venv/bin/python -m pytest -q
  tests/test_recovery_distribution.py tests/test_recovery_release.py` passed
  (59 tests, one opt-in GPG test skipped in the sandbox); the disposable-key
  GPG test passed with socket access; `git diff --check` passed. Tests use
  synthetic image bytes. No image or public release was produced. Atomic
  publication and its required storage-boundary review remain open.
- The P3a1–P3a3 private handoff now has one coordinator that reuses the locked
  base, generic runtime, audited Dracut and image-input stages in order. It
  preflights the recipe and output path before creating the private stage,
  rejects linked output parents, and returns only audited `ImageInputs` without
  running external image tools or publishing an image. Contracts: C1/C4 and
  `recovery-base.md`; files: `src/quirkbench/recovery_synthesis.py` and
  `tests/test_recovery_synthesis.py`. Focused check: `.venv/bin/python -m
  pytest -q tests/test_recovery_synthesis.py tests/test_recovery_image_plan.py`
  passed (38 tests); `git diff --check` passed. The synthetic joined run does
  not establish a real Fedora closure, image assembly or release qualification.
- P3a3 signed sidecars now have a publication transaction separate from image
  assembly. It verifies exact unqualified candidate, statement, signature and
  image bytes before writing; a synced pending marker blocks readers during
  interruption. Identical retries complete only matching sidecars, and a
  different pending or published bundle fails closed. The read-only inspector
  checks both image and signed-publication markers. Focused check:
  `.venv/bin/python -m pytest -q tests/test_recovery_distribution.py
  tests/test_recovery_release.py` passed (62 tests; one opt-in GPG socket test
  skipped); `git diff --check` passed. Independent storage review found that a
  sidecar could change during final GPG verification; final persisted-byte and
  marker rechecks now keep the marker in place on that race. This uses synthetic
  image bytes and does not publish an actual Fedora image or confer flash
  qualification. Noncooperating same-user filesystem mutation after commit is
  outside the cooperating publisher lock and remains a local integrity risk.
- The prepared image handoff now has a final coordinator that calls the existing
  image adapter only when no assembled image exists or its image journal needs
  repair, validates the exact staged result, signs the checksum statement and
  publishes the verified unqualified bundle. An interrupted signing step can
  resume without rebuilding image bytes. Focused check: `.venv/bin/python -m
  pytest -q tests/test_recovery_distribution.py tests/test_recovery_release.py
  tests/test_recovery_synthesis.py` passed (83 tests; one opt-in GPG socket test
  skipped). No real Fedora closure or factory image was used.

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
- A further bounded P3a2 packet inventories `/usr/lib/systemd/system`
  dependency links before staging recovery runtime files and refuses links
  absent from the exact reviewed vendor allowlist. Missing, escaping or linked
  vendor unit destinations also fail closed. The allowlist is empty until the
  real Fedora RPM closure is reviewed; the scanner reports observed links for
  that review. This check covers vendor `.wants`, `.requires` and `.upholds`
  links, not all vendor unit dependencies, presets or generators. Contracts:
  C4 and `recovery-base.md`; files: `src/quirkbench/boot.py`,
  `tests/test_boot.py`. Focused check: `.venv/bin/python -m pytest -q
  tests/test_boot.py tests/test_recovery_image_plan.py tests/test_image.py`
  passed (79 tests); `git diff --check` passed. No real Fedora rootfs, image or
  hardware qualification was produced. Runtime image inputs changed and remain
  unqualified.
- An offline candidate-RPM audit reverified the exact 248 retained RPM bytes
  against the candidate snapshot and target lock, then queried their payload
  headers for systemd units, enablement links, generators and presets. The
  ignored report and digest are recorded in
  [the Fedora 44 candidate record](fedora44-input-candidate.md). It found 87
  vendor enablement links and 17 system generators, including package entries
  for repartition and factory-reset machinery that need explicit recovery
  policy review. The installed rootfs may differ after DNF scriptlets; no
  allowlist, runtime image bytes or qualification changed in this audit.
- A further P3a2 software packet added a fail-closed installed vendor-generator
  audit before recovery runtime files are staged. It hashes bounded, executable
  regular files under `/usr/lib/systemd/system-generators` and requires their
  exact names and bytes in a reviewed allowlist. Symlinks, nonexecutables,
  changed bytes and missing reviewed entries fail. The allowlist remains empty
  pending review of the real installed Fedora closure. Candidate runtime
  policy is unchanged. Files: `src/quirkbench/boot.py`,
  `tests/test_boot.py` and this record. Focused checks:
  `.venv/bin/python -m pytest -q tests/test_boot.py` passed (50 tests);
  `tests/test_recovery_runtime_revision.py tests/test_recovery_synthesis.py`
  passed (22 tests), and `git diff --check` passed. No rootfs/image boot or
  release qualification ran. Recovery runtime source bytes changed and remain
  unqualified.
- An offline P3a2 follow-up now rejects preexisting `/etc` generator overrides
  outside the three recovery masks and checks that all three exact `/dev/null`
  masks survive at the strict image handoff. It also requires every recovery
  unit mask, the multi-user default target and every required enabled service
  link to remain present. This closes paths where package files or a later
  stage could leave an extra generator active or remove required boot policy.
  Vendor generator approval remains empty and separate.
  Files: `src/quirkbench/boot.py`, `tests/test_boot.py`, this record. Focused
  check: `.venv/bin/python -m pytest -q tests/test_boot.py
  tests/test_recovery_image_plan.py tests/test_recovery_synthesis.py` passed
  (93 tests). No network, RPM download, rootfs/image build, QEMU or hardware
  qualification ran. Changed recovery runtime bytes remain unqualified.

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

## Incremental kernel build packet (2026-09-29)

- At the operator's request, the original Fedora 44 first-boot pipeline and
  kernel user services were stopped. Both became inactive; the old rootless
  container exited. Its prepared source, rootfs and approximately 11 GiB
  partial Kbuild tree were retained. `interrupted.json` records that the old
  `pipeline.exit.status` is not a successful kernel result.
- The retained source tree and resolved protected config were verified before
  an explicit same-input retry. The first retry preflight exposed a mistaken
  comparison of the Fedora base marker with the derived builder config ID; the
  two identities are distinct and the check was corrected. A second bounded,
  network-disabled rootless Podman service,
  `quirkbench-fedora44-kernel-resume2-20260929.service`, reached module
  installation. Its audit then rejected Fedora's reviewed `/lib -> usr/lib`
  usrmerge link, which the module audit now handles exactly. That audit also
  exposed compiled VMD support contrary to the protected profile. The reviewed
  kernel fragment and final config now require `CONFIG_VMD=n`; the rejected
  module tree remains in the private run directory as diagnostic evidence.
- `BuildStageCache` now stores private, hash-verified intermediate snapshots,
  takes a per-lineage lock, atomically replaces the prior completed generation,
  keeps a 20 GiB free-space floor and provides `build-cache list` and explicit
  `build-cache prune CACHE_ID`. Cache pruning cannot claim an active lineage.
- Recovery synthesis can opt into separate rootfs, prepared source, kernel,
  runtime and initramfs snapshots. Exact hits are verified; a changed reviewed
  config restores the last audited Kbuild objects into a stable private path,
  runs `olddefconfig` and repeats the protected config, module and output audits.
  Later runtime failure leaves a completed kernel cache usable by a new stage.
- The experiment `BuildPipeline` retains its exact output cache and can use a
  stable private Kbuild workspace across config changes and source snapshots
  carrying the same explicit immutable base archive. A changed toolchain starts
  a fresh lineage. An explicit reconciled retry verifies the retained source,
  exact cache identity and resolved config before using partial objects.
  After verified worker stop, a preconfiguration tree or a tree from changed
  pinned inputs is discarded; a prior completed snapshot can still seed the
  next build. Recovery-only implementation edits do not invalidate experiment
  Kbuild objects.
  Audited kernel objects are snapshotted immediately after module installation,
  so a later initramfs or userspace failure preserves compilation gains.
- Focused check: `.venv/bin/python -m pytest -q
  tests/test_recovery_synthesis.py tests/test_recovery_kernel_stage.py
  tests/test_build_cache.py tests/test_build_pipeline.py tests/test_cli.py`
  passed (59 tests). Python compilation and `git diff --check` passed.
- A third bounded, network-disabled rootless Podman retry,
  `quirkbench-fedora44-kernel-vmd-20260929.service`, is compiling the reviewed
  VMD-disabled config with retained objects. Its result is recorded in
  `.quirkbench/inputs/fedora44-kernel-build-20260929/retry-vmd.exit.status`;
  a complete `kernel-record.json` exists only after all protection, module and
  output audits pass. The service was active at 22:28 UTC with compiler output
  still advancing; no success record existed at that observation.
- The product recovery-image service is not yet wired to this optional cache
  path. Stage cache events are durable in the private stage but are not yet
  mirrored into controller operation events. The current retry is a retained
  development run, not a product image or qualification.
- The expanded focused software checks passed (135 tests across build cache,
  experiment pipeline, recovery synthesis/audits, CLI and rootless worker
  planning); `git diff --check` passed. This does not establish real Kbuild
  reuse or product-image qualification while the protected retry is active.

## Attended development build viewer (2026-09-29)

- Bounded P2c-style presentation work using C2 progress and C8 service ownership:
  `environments/view-build.sh` opens native Konsole through Distrobox's host bridge
  and follows private stage logs, including newly created compiler logs up to four
  directory levels. Closing the viewer stops only its readers. Worker execution,
  authorization and cancellation boundaries are unchanged.
- `start-bounded-podman-build.sh` dispatches the viewer before new attended builds;
  absent desktop or failed viewer dispatch blocks launch. The viewer keeps numeric
  completion status visible with Konsole's hold option; producer buffering and
  successful dispatch without proof of window rendering remain limitations.
  This development helper does not integrate the product operation UI.
- Focused validation: `.venv/bin/python -m pytest -q tests/test_build_viewer.py`
  passed (2 tests: nested logs/failed completion and missing desktop prevents
  worker dispatch). Shell syntax and `git diff --check` passed. No build or
  qualification was started, and no artifact bytes were changed.
- Opened a viewer for the retained VMD-disabled build via
  `bash environments/view-build.sh
  /var/home/eric/dev/quirkbench/.quirkbench/inputs/fedora44-kernel-build-20260929
  /var/home/eric/dev/quirkbench/.quirkbench/inputs/fedora44-kernel-build-20260929/retry-vmd.exit.status`.
  Desktop unit `quirkbench-build-view-1790722932-3208641.service` was active/running;
  its user journal records native Konsole dispatch. The compilation was neither
  restarted nor modified.

## Compiler job budgeting (2026-09-29)

- Focused P3a1/build-adapter tuning within C1/C4; C2 worker ownership and
  enforced CPU/RAM limits are unchanged. `kernel_job_budget` now supplies one
  shared 2-GiB-per-job heuristic to `ResourceLimits.from_cgroup` and
  `KernelBuild` admission. A 4-core/4-GiB worker selects two jobs instead of one;
  CPU quotas, controller reserves and explicit serial requests still apply.
  Cached pages in `memory.current` no longer halve the fixed worker capacity
  again or unnecessarily prevent incremental job admission. Peak memory is
  still bounded by the existing cgroup, not guaranteed by the heuristic.
- Future ad hoc scripts must derive the bounded worker's limits and use
  `jobs=limits.jobs`; historical retained scripts and the active single-job
  retry were not edited or restarted. The environment guide documents this.
- Focused validation: `.venv/bin/python -m pytest -q tests/test_build.py
  tests/test_build_pipeline.py tests/test_recovery_kernel_stage.py
  tests/test_recovery_synthesis.py` passed (67 tests); `git diff --check` passed.
  Regression checks cover compile argv, explicit serial and excessive requests,
  4/8-GiB budgets, CPU caps, cached-page occupancy and invalid CPU periods.
  No kernel/image build or release qualification ran. No existing artifact
  bytes changed; subsequent builds need their own retained input/evidence records.

## Development build memory budget (2026-09-29)

- Operator-authorized development build tuning: the bounded starter and its
  Podman containment validator now permit 8 GiB, capped at half controller total
  RAM. Four cores, zero swap, task limits and worker ownership remain unchanged.
  With sufficient available memory, the shared 2-GiB-per-job policy selects four
  compiler jobs. `KernelBuild` admission retains 2 GiB of available desktop
  headroom rather than halving a reviewed worker capacity again.
- Scope: development/kernel launcher and build admission only; the fixed
  recovery-rootfs worker retains its separate existing 4-GiB service contract.
  Historical launch scripts, active services and artifact bytes were not changed.
- Focused validation: `.venv/bin/python -m pytest -q tests/test_build.py
  tests/test_build_pipeline.py tests/test_recovery_kernel_stage.py
  tests/test_recovery_synthesis.py tests/test_build_viewer.py` passed (73 tests).
  Fake launch checks cover the 8-GiB maximum and smaller-controller half-memory
  bound while retaining CPU/swap/stop properties. Admission tests cover four jobs
  at 12 GiB available and reduction under low headroom. Shell syntax and
  `git diff --check` passed. No real build or qualification was launched; speed
  and peak memory at four jobs remain unmeasured.

## P2d/P2b/P2c bounded software work during the protected kernel run (2026-09-29)

- P2d archive/launcher subpacket (C8): `controller_archive.py` packages a wheel's
  runtime code and installed assets into a relocatable controller archive, with
  runnable `bin/quirkbench` and `bin/quirkbench-worker`. The development build
  script builds its wheel in a temporary copied checkout. Packaging excludes
  workspace/state bytes, refuses unsafe wheel members and existing outputs,
  records wheel/runtime file hashes and explicitly labels the archive unsigned
  and unqualified. Clean-home tests exercise setup/state selection, native
  prerequisite reporting, worker help and relocation without a checkout or venv.
  `make controller-archive OUTPUT=...` builds only software packaging.
- P2b fixed rootfs worker subpacket (C2/C4): the installed worker verifies its
  exact read-only live claim, loads immutable operation arguments from retained
  CAS, stages private reviewed inputs and invokes only the existing restricted
  Podman rootfs command. It drains merged stdout/stderr into an 8-MiB private log,
  bounds execution and records private stage results with claim/input identity.
  Numeric process success alone cannot complete the stage: the installed lock
  and a final exact live claim must agree. It never publishes CAS references,
  writes the controller database or completes the image operation. Direct-child
  cleanup is not whole-unit reconciliation; uncertain cleanup stays interrupted.
  Startup/staging failures rely on unit stderr, and ownership checks bracket
  synchronous staging. The owned service deadline still bounds preparation.
- Required higher-reasoning worker boundary review found and drove fixes for an
  unbounded installed-lock read race, canonical lock serialization and final
  cleanup timeout handling. Focused regressions cover those findings. Final
  review found no remaining actionable code issues; no service or real Podman
  worker was launched for review. Existing operation/stop/publication authority
  remains with the controller. The planner permits only one additional private
  diagnostics child, not arbitrary stage paths.
- P2c viewer subpacket (C2/C8): `operation watch ID [--once] [--json]
  [--interval SECONDS]` consumes existing operation facts with no lifecycle
  ownership, scheduler or agent. It stops on success/failure/interruption and
  preserves active claims/epochs. Missing state is rejected without creating a
  database. Absent measured progress stays unavailable. Worker private results
  and logs are not yet consumed as durable operation progress.
- Validation: `.venv/bin/python -m pytest -q tests/test_recovery_worker.py
  tests/test_recovery_podman.py tests/test_worker_claim.py
  tests/test_worker_service.py tests/test_worker.py tests/test_operation_watch.py
  tests/test_operations.py tests/test_cli.py tests/test_monitor.py
  tests/test_controller_archive.py tests/test_installation.py
  tests/test_package_resources.py` passed (131 tests, 8.92 seconds).
  `git diff --check` passed. The result/command is retained in
  `.quirkbench/controller-archives/p2d-20260929/software-check.txt`.
- Concrete development artifact produced by `.venv/bin/python
  environments/build-controller-archive.py --output
  .quirkbench/controller-archives/p2d-20260929/controller.tar.gz`, exit 0.
  SHA-256: `65167457ce7e2f9d575e70082d29e00028f976384b5efb62667641da60f56512`.
  `.quirkbench/controller-archives/p2d-20260929/build-record.json` records its
  originating wheel and exact included file identities. This is not a signed
  release archive or a claim of controller/image compatibility qualification.
- Remaining P2 dependencies: persistent coordinator/service installation,
  production dispatch/result consumption and source-writer reservation. P2d
  setup-check still truthfully reports background work not ready. P3b–f binding,
  network/enrollment and P6 source/session integration remain separate packets.
  See [development controller installation](controller-installation.md).
  No active kernel stage, historical run script, user configuration, image,
  release gate or physical target was modified/launched by these packets.
  New runtime/source bytes need new input records before subsequent image stages;
  historical qualification never transfers to them automatically.

## Protected Fedora kernel retry completed (2026-09-29)

- The retained `quirkbench-fedora44-kernel-vmd-20260929.service` retry completed
  with exit status 0 at approximately 16:33 PDT, after starting at 14:56 PDT
  (about 1 hour 37 minutes). Its recorded command used one compiler job and
  the original 4-GiB/four-core service budget. It was not restarted with the
  newer development defaults.
- `.quirkbench/inputs/fedora44-kernel-build-20260929/retry-vmd.exit.status`
  contains `0`; `kernel-record.json` records release `7.2.7`, final configuration,
  module audit and kernel/vmlinux/Module.symvers/System.map output hashes.
  The successful worker wrote this record only after module installation and
  protection/output audits completed. The module audit reports 350 builtin
  entries and 4,847 loadable modules, with the reviewed network drivers present.
- The final configuration SHA-256 was independently checked against the record:
  `415b50a935969fae740560ba8539ac0764a1d97295e604464519d85d22527085`.
  VMD, NVMe and ATA are disabled and USB storage is built in. The kernel image
  exists in `kernel-obj/arch/x86/boot/bzImage`; no image/initramfs was produced
  by this kernel-only retry. Service observation found inactive/dead, success,
  `ExecMainStatus=0`. Logs remain in `kernel-logs-vmd/` and the worker log.
- This removes the protected-kernel stage blocker. Runtime/recipe identities
  must be refreshed for subsequent staged source changes, followed by audited
  initramfs generation and recovery-image assembly. Completion is not boot,
  physical hardware or release qualification, and no such gate was invoked.

## Attended-first design revision (2026-09-29)

- Documentation only: distinct recovery boot-device policy and candidate exclusions;
  stock Fedora recovery packages with existing assembly; manual authenticated
  attended journey before automation. P0–P8 identifiers and wire records retained.
- [Revision audit](design-revision-20260929.md) records every document disposition,
  remaining versioned schema/implementation work and validation. Historical custom
  kernel/software records above remain unchanged; no build or qualification rerun.
- Documentation acceptance: 26 documents / 151 local file-and-heading links checked
  without errors; `git diff --check` passed. Higher-reasoning storage review closed
  after early-probing, initial activation ownership and trust/identity clarifications.
  No code/schema changes, software tests, builds or physical qualification in this
  documentation packet.


## Stock recovery and attended loop software (2026-09-30)

- Implemented distinct recovery policy, recipe/rootfs-lock/release v2 dispatch and
  default v2 input generation, while preserving v1 readers, candidate policies,
  public wire envelopes and the completed custom-kernel artifact.
- Exact recorded binary acquisition plan, CAS RPM/key retention, signature/header/
  installed-closure verification, stock kernel/modules/firmware staging, independent
  generic dracut and storage audit, existing image/provenance/checksum integration,
  and separate stock cache identities. No compiler in the stock branch.
- Early boot-device guard, reviewed udev/dracut hooks and masks, bounded one-USB
  initial support, boot-only userspace destination validation, read-only recovery,
  RAM failure diagnostics and evidence independent of optional volumes.
- Manual validated private generations with atomic activation, runtime/config locks,
  no unresolved claim rotation, TLS/signing trust checks and nested-device refusal.
- Recovery-only uploads/reconciliation and local exact-attempt approve/reject,
  negotiated capability/authenticated status route, durable approval context and
  pre-BOOT_PENDING fencing. HTTPS fake-adapter journey exercises candidate evidence
  and recovery return without duplicate execution.
- V2 fixed rootfs worker staging, transitive CAS reference retention across backup,
  stopped-rootfs validation and current-owner fenced audit adoption. Rootfs adoption
  remains incomplete image preparation; private rootfs backup/resume is not claimed.
- Required higher-reasoning storage/private-state/approval and worker reviews found
  concrete failure cases; focused regressions and fixes address their findings.
  See [software handoff and product-operation limits](stock-recovery-attended.md).
- No packages downloaded, kernel/image built, VM booted, external media modified,
  hardware campaign or release qualification run by this implementation work.

- Full-image integration completed in the same software packet: immutable recipe
  admission, fixed stock image workload inside the existing pinned Podman worker,
  optional recovery executor on `serve`'s existing lifecycle owner, responsive HTTPS,
  independent current-owner validation/signing, signature hash binding and fenced
  final CAS publication. Existing rootfs-only intents remain accepted and untouched.
- Stop evidence is journaled in existing operation events, tied to exact unit/boot/
  generation/input/stage identity, so collected transient units are not stopped twice.
  Success atomically clears verified stopped ownership. Exact live-owner failure may
  close an expired operation, including deadlines crossed during validation/signing.
  Stale ownership cannot publish. Completed public image bundles are retained; private
  build directories remain outside backup completeness claims.

- Final focused software validation: recovery/staging/ownership/compatibility suites
  **391 passed, 1 skipped** in 18.57 s; authenticated attended/runtime suites
  **103 passed** in 19.06 s. The skipped opt-in real-GPG integration was not run;
  signing/publication fixtures use injected GPG adapters. Temporary localhost HTTPS
  and Unix notification sockets exercised the actual software transport. No real
  kernel/image/VM/device/release operation was performed.
- Durable command, source identity, result and log record:
  `.quirkbench/validation/stock-attended-20260930/result.json`, `recovery-tests.log`
  and `socket-tests.log`. Source hashes describe the modified checkout, not a new
  committed or qualified release. Required storage/private-state/approval and full
  worker/publication reviews closed with no remaining concrete blockers.
- Documentation validation: 27 Markdown documents, 160 local file/heading links,
  no errors; `git diff --check` passed. The software handoff distinguishes admitted
  rootfs-only stages, complete signed image operations and still-unperformed product
  commissioning/physical/release qualification. Historical records remain intact.

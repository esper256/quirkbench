# Changelog

## 1.3 — 2026-10-24

Source tag `v1.3`; Python and RPM package version `1.3.0`.
See [release notes](docs/release-notes/1.3.md).

- Replace generic command sequences with explicit, optional prepare/deploy/boot
  follow-ups. Show each phase; stop on failure without replaying uncertain work.
- Correct login-session discovery and report inspection failures clearly.
- Distinguish invalid command observations from failed experiments. Add optional
  bounded waiting across reconnection.
- Capture named incidents with investigation-owned collectors, initial snapshots,
  hashes and separate collection/receipt status. Human CLI triggers use the same
  service; partial original files survive for recovery upload.
- Deliver human requests to a selected existing desktop session, with explicit
  redelivery and CLI/menu fallback.
- Offer bounded keep-awake commands with visible ownership, expiry and release.
- Render OSTree cursor-save/restore progress without accumulating escape-code text.
- Update agent instructions and examples; retain generic investigation behavior.

## 1.2 — 2026-10-23

Source release `v1.2`; Python and RPM package version `1.2.0`.
Changes since `v1.1`. See [release notes](docs/release-notes/1.2.md) for upgrade
information and verification limits.

### Faster agent investigations

- Reuse candidate container layers across preparation runs through a persistent OCI
  cache; avoid retaining RPM installers in candidate layers. Record reuse and timings.
- Summarize run evidence by default. Page files, commands, transfers and collection
  reports; read bounded log tails without downloading whole responses.
- Inspect login sessions and sleep inhibitors. Run commands as an explicitly selected
  active session's user, with normal deadlines, output and evidence handling.
- Upload up to 32 files in one SFTP call, reporting each outcome and stopping on failure.
- Coordinate human actions with explicit readiness, acknowledgment and cancellation.
  Late replies cannot hide a newer request; investigation exports include responses.
- Support interactive command terminals and graceful process-group cleanup.
- Improve agent examples for sequences, session access, preparation and evidence.
  Sequences remain optional; evaluate their usefulness again before 1.3.

### Human-facing tools

- Add an interactive controller dashboard with readable command intent and work
  summaries, while retaining plain-log operation.
- Simplify installation and development commands. Add native controller RPM packaging
  support and recovery release-download preparation without requiring host RPM layers
  for development.
- Use `--data-dir`, remove `doctor`, and check required tools at the relevant operation.
- Add readable recovery listings, human flash defaults and `flash --built`.
- Group storage by cleanup action. Preview cleanup, export and delete investigations
  while preserving software used elsewhere.
- Add controller status, stop and update checks. Explain CLI/controller mismatches
  instead of failing on incompatible replies; replace SSH logspam with connection health.

### Recovery and correctness

- Relabel the temporary initramfs build-script mount for SELinux container access,
  matching the RPM input mounts (#234).

- Pair the recovery drive separately from the computer. Moving it preserves saved
  networking while keeping computers and investigations distinct.
- Improve early USB4 discovery in recovery and candidate startup. Keep internal-disk
  protection based on identity rather than excluding whole driver families.
- Run candidate startup fallback independently of Python preparation. Support explicit
  candidate package removals, replacements and SELinux choices.
- Keep evidence uploads working across slow links and lost replies; delete target
  evidence only after confirmed receipt.
- Expose the recovery TUI's last rendered screen for remote inspection; normalize
  progress display while retaining original logs.
- Accept a recorded baseline's digest and original reference as the same selection.
  Distinguish inventory warnings from exit failures and label diagnostic boot origins.
- Preserve failed command outcomes when first read after return to recovery. Session
  discovery obeys command stop and deadline controls.
- Use asynchronous SQLite access and bounded/cached reads. Limit unchanged-response
  metrics to read/wait calls.
- Replace the load tool's white-noise-like audio with a more recognizable sample.
- Reduce avoidable waits in integration tests, including controller terminal Escape
  handling; retain real connections, restarts and process-stop checks.

## 1.1 — 2026-10-21

Changes since the first real-world agent investigation ([#191](https://github.com/esper256/quirkbench/issues/191)).
The Git tag is `v1.1`; Python and RPM package versions are `1.1.0`. Updating the
checkout does not update a running controller, flashed recovery or prepared candidate.

### Faster investigations

- Track several named problems in one investigation, with shared runs and evidence.
- Pause, resume or force-stop through the CLI or MCP. Resume permits new work;
  it does not replay commands or boot a candidate automatically.
- Optionally chain preparation, deployment, boot, transfers and commands in an
  ordered sequence. Failures stop progression; continuation after restart is explicit.
- Include a debugging toolkit in candidates, select additional repository packages,
  and inspect available tools and build information. The toolkit can be omitted.
- Provide an optional bounded CPU/headless Vulkan load utility, with optional
  authorized audio playback and timestamped fault observations. It does not start
  automatically or establish that a driver is correct.
- Remove the fixed two-inspection limit in recovery.

### Clearer agent tools

- Short SSH commands can return completed output on the first call. Accept either
  exact arguments or an explicit shell script, with independent execution and wait deadlines.
- Publish input choices and bounds in MCP schemas; report completion, outcome,
  attention and output pagination consistently for command/work results.
- Use clear upload/download path names and directly readable saved-output references.
  Return structured results and readable JSON together.
- Keep context focused on current work; page through run history separately.
  Distinguish retained deployment files from each new boot's run ID.
- Record individual MCP calls, including reads, waits and rejected requests:
  counts, time, response size and unchanged results. Do not copy commands, secrets
  or output into this activity history. Controller work timing remains separate.

### Bugs fixed

- Generated quiet-wait commands work when Quirkbench is absent from shell `PATH`.
- Preparation checks tools visible to the running controller, rather than assuming
  the agent's shell has the same environment.
- Candidate summaries include supplied packages, replacements and startup probes.
- Recovery collects the correct candidate journal and reports collection failures;
  receiving some files no longer implies complete diagnostics.
- Successful discard clears stale retained-run messages. Repeated controls preserve
  completed outcomes; pause does not weaken a force-stop already in progress.
- Inspections label failed services accurately and explain internal-disk exclusion.
- Nonzero command exits report failure and attention; disconnected sequence waits
  do not busy-poll. Live and saved output expose the same byte cursors.

### Recovery reliability

- Run recovery from a compressed OS copied into RAM using dracut. Programs remain
  available after storage loss; shared data and candidate access still need the drive.
- Keep human-confirmed force reboot and power-off. RAM residency cannot rescue
  a stuck kernel or guarantee that unsaved evidence survives.
- The RAM boot method works with the existing data-preserving update: no new
  partition layout is required when the image fits.

### Verification and use

Fast integration tests cover application behavior, local connections, restart and
failure handling. RAM recovery also has a separate [QEMU check record](docs/observations/2026-10-21-ram-recovery.md).
These checks do not establish every new image or hardware workload works.

Start with the [agent guide](docs/agent-guide.md) or [installation instructions](docs/controller-installation.md).
Examples are optional routes, not a prescribed investigation procedure. Interactive
terminal input, user-session selection and dedicated live-package helpers remain
future work. Existing candidate commands can perform suitable live changes.

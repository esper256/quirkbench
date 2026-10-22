# Changelog

## Unreleased

- Detect CLI/controller software mismatches before requests and explain how to restart, instead of failing on an incompatible response.

- Group storage by cleanup action and support deleting paused investigations while preserving shared software.

- Replace `--state` with `--data-dir`, remove `doctor`, and check build/flash tools where needed.
- Keep status output factual and replace routine SSH messages with target connection health reports.

- Native controller RPM packaging and release recovery download preparation.
- Human recovery defaults, readable image listings and `flash --built` for local development.
- Host development launcher with private Python dependencies and no host RPM installation.
- Clearer controller status, setup and host-tool reporting; explicit noninteractive choices.

- Graceful command cleanup, interactive terminals and optional human notifications.
- Whole-file evidence uploads survive slow links; confirmed receipts still control deletion.
- Candidate startup fallback runs independently of target preparation and Python startup.
- Controller status, stop and update checks; bounded operator logs and clearer config errors.
- Explicit connection refresh on existing media, keeping pairing and network settings.
- Asynchronous SQLite access, filtered status reads and completed command output caching.
- Storage cleanup previews, investigation export and selectable candidate SELinux policy.
- Normalize terminal progress in recovery Activity; keep original logs unchanged.

New target action records use individual files. No migration or owner-state reset is
included. Updated boot behavior and SELinux modes still need separate image/hardware checks.

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

# Run Quirkbench locally

Recovery and candidate boot, controller connection, MCP commands and return with
uploaded diagnostics have been observed in QEMU and on a physical target. That
does not establish that every newer image works. See [earlier boot checks](observations/2026-10-20-boot-checks.md),
[physical run findings](observations/2026-10-20-nvme-first-candidate.md) and the
[separate boot test](../boot_tests/README.md).

## Release installation

A release RPM installs `quirkbench` and its dependencies for all users. Each user
owns `~/.quirkbench`; the controller runs as that user in a foreground terminal.
See [release packaging](releases.md) for package creation and distribution status.
No published recovery is selected in the source checkout yet.

```sh
quirkbench setup
quirkbench controller run
# In another terminal, with a published release installed:
quirkbench recovery flash
```

Flashing downloads the recovery chosen for that installed release, or reuses its
cached download. `recovery download` downloads it ahead of time for offline use.
Use a target-reachable HTTPS address in setup. Configure the agent with the MCP
URL and Bearer token file shown under **C — Connections** in the controller
dashboard. Keep the controller terminal open. Use `controller run --no-tui` for
plain logs; redirected output also uses plain logs. **Q** asks before stopping;
Ctrl+C and `controller stop` still work. There is no controller service manager.

## Development without host RPM installation

```sh
./dev quirkbench setup
./dev quirkbench controller run
# In another terminal:
./dev quirkbench recovery build
./dev quirkbench recovery flash --built
```

`./dev` forwards from Distrobox to the host, then manages private Python
dependencies under the checkout's ignored `.dev-runtime/` directory. Initial
setup needs network access; unchanged dependencies are reused. Host Python must
be 3.12 or newer. No RPMs are installed by this launcher. It uses the host's
existing Podman and disk tools. Build and flash check their required tools before work starts.
The checkout must be accessible at the same path from the host.

Both entry points use `~/.quirkbench` by default, never the current working
directory. An older controller using a different directory still needs its
explicit `--data-dir PATH`; the launcher does not move or reset that data. Stop and
restart a development controller explicitly to load edited code. Do not run the
installed and development controllers against the same directory simultaneously.
A stopped controller is reported by `status`; `controller status` also compares
installed and running software. CLI requests refuse a different running software
revision with a restart message, before sending work or interpreting its response.
`controller check` and `controller stop` remain available. No restart is automatic.
Never delete a lock file to bypass a running process.

## Recovery media

`recovery flash --built` selects the last successful local build, including an
explicit build that reused cached results. Failed builds keep the previous
selection. `recovery list` shows dates, sizes and release/local defaults; `--image ID`
remains available for exact scripts. Unknown IDs fail without prompting. JSON
flashing requires an explicit device and confirmation, plus a target name for
new media. Human flashing lists drives by model, size and serial, then asks for
one final write confirmation.

Builds require rootless Podman. The build container supplies Fedora tools; no
Distrobox is required. Flashing additionally
needs sudo, parted, util-linux, e2fsprogs, dosfstools and GRUB tools. It refuses
mounted disks and disks not reported as USB or hot-pluggable, requires confirmation,
and elevates only its writer. USB4/Thunderbolt NVMe drives are supported by the
selection checks. Confirm the model and serial, not just the current device name.
On existing compatible Quirkbench media, `recovery flash` updates recovery and
boot files while preserving saved Wi-Fi, pairing and shared data. It refuses an
incompatible layout or settings format; `--erase` explicitly requests a fresh
installation and deletes those files. Disconnect the drive from the target and
unmount it on the controller before either operation.
The first image is x86-64 UEFI and unsigned; it requires Secure Boot to be disabled.
Quirkbench does not change firmware settings or enroll keys. The first physical transfer wrote 10.8 GB of candidate content. Peak space for
ongoing investigation remains unmeasured; this is not a minimum-drive-size claim.

Recovery builds retain their package list and container identity. Completed images
use content IDs. Failed builds do not replace completed images. `--force` creates a
new completed image if its bytes change. Candidate preparation selects Bazzite
stable once per investigation and records its digest. A new investigation can select
an updated baseline. Interrupted preparation retains logs/cache; use a new request ID
to retry. A lost deployment reply waits for target status instead of booting again. Rejected
requests finish with an error; use a new request after fixing the cause. A failed
boot selection is cleared before another run is allowed. Diagnostics collection
failure does not prevent an attempted return to recovery.

The controller's `logs/`, `reports/` and `results/` contain ordinary files. `status`
and MCP work results expose progress and log locations. Recovery's local menu offers
network setup, logs, report export/upload and a terminal. Reports debug Quirkbench;
run evidence is stored separately.

See the [milestone mockup](mockups/recovery-to-candidate.md) for the agent interaction.
See the [agent guide](agent-guide.md) for current command, transfer and investigation
controls, and the [1.1 changelog](../CHANGELOG.md) for recent additions.

## Deploy, boot, inspect and discard

Quirkbench separates `recovery_deploy_ostree` from `recovery_boot_ostree` and
retains returned OS files until `recovery_discard_ostree`. Each repeat boot gets a
new run ID. Temporary `/usr` edits from `ostree admin unlock` may disappear;
persistent configuration and application files remain. Recovery images without
these capabilities must be updated before using the new deployment flow. Updating
this checkout does not update an already-running controller or target.

Saved results are available through `run_results` and `saved_output_read`.
`ssh_recovery_inspect` offers an `emergency` view which launches no external
programs or waits for shared storage. Recovery now copies its compressed OS into
RAM at boot, keeping programs available after drive loss. Saved data still needs
the drive; kernel or memory failures can still prevent recovery. Force reboot
and power off remain available from the local menu.

Fresh flashes reserve the recovery image size plus 1 GiB. Existing-media updates
only require the image to fit; they do not enforce unused headroom or repartition.
An erase is needed to enlarge the old layout, not merely because the software changed.

Build and physical check records belong in [dated observations](observations/2026-10-21-recovery-build-notes.md),
not setup prerequisites. Inspect live context for current target state and supported
operations. A newer unflashed image does not prevent supported operations on the
connected recovery. For possible investigation routes, see the
[agent guide](agent-guide.md#choosing-a-starting-point).

## Controller and storage maintenance

Use the same `--data-dir` and host/container environment as the running controller.
`controller status` identifies both running and installed software. Before an
update restart, run `controller check`, then `controller stop`, then `controller run`.
A locked directory with an unreachable port may mean the command is in a different
container network; it does not prove the controller stopped. Operator failures are
in `logs/controller.log` (rotated and size-limited).

After changing the address in setup, `recovery flash --refresh-connection` explicitly
updates that controller's address and certificate on compatible media. Pairing and
Wi-Fi remain. A different controller identity requires new pairing, not this option.

The controller terminal reports target connections and disconnections. If a status
reply takes more than 10 seconds, it warns once and reports when replies resume.
A status request still times out after 30 seconds. Normal SSH session traffic is
quiet; requested reboots are identified separately from unexpected disconnects.

## Freeing space

Use `quirkbench storage` to see groups and investigation names.
`storage clean --cache`, `--recovery-images`, `--unassigned-diagnostics` or `--logs`
previews eligible files and asks before removal. Add `--yes` for scripts; JSON
mode previews without prompting unless `--yes` is supplied. Selected recovery
images, exact recovery rebuild inputs and the current controller log are kept.

To remove an investigation, return/discard its retained runs, pause it, then run
`quirkbench investigation delete NAME`. Export anything you want to keep first with `investigation export NAME DIRECTORY`.
Shared software stays until no remaining investigation needs it. If deletion
fails, correct the reported problem and retry the same command. These commands
do not clear Podman storage or operate on target drives.

# Run Quirkbench locally

Recovery and candidate boot, controller connection, MCP commands and return with
uploaded diagnostics have been observed in QEMU and on a physical target. That
does not establish that every newer image works. See [earlier boot checks](observations/2026-10-20-boot-checks.md),
[physical run findings](observations/2026-10-20-nvme-first-candidate.md) and the
[separate boot test](../boot_tests/README.md).

Use Python 3.12 or newer:

```sh
uv sync --locked
uv run quirkbench setup
uv run quirkbench controller run
# In another terminal:
uv run quirkbench recovery build
uv run quirkbench recovery flash
```

For an ordinary user installation, `uv tool install .` installs the CLI. Setup
saves editable `~/.quirkbench/config.toml`; the controller stays in the foreground.
Use a target-reachable HTTPS address. The same process serves local MCP and target
HTTPS/reverse SSH. Configure the agent with its printed MCP URL and the Bearer token
from the printed token file. There is no controller service manager.

From Distrobox, use `distrobox-host-exec` to run commands on the host when its
Podman and block devices are not available inside the container. The selected
Python installation and controller directory must be accessible there.

If `controller run` reports that the directory is already in use, check
`quirkbench status` from the same host environment. A responding controller can
be reused; do not delete its lock file or start another copy.

Builds require rootless Podman. The build container supplies Fedora tools; no
Distrobox is required. `quirkbench doctor` lists host tools. Flashing additionally
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

Use the same `--state` and host/container environment as the running controller.
`controller status` identifies both running and installed software. Before an
update restart, run `controller check`, then `controller stop`, then `controller run`.
A locked directory with an unreachable port may mean the command is in a different
container network; it does not prove the controller stopped. Operator failures are
in `logs/controller.log` (rotated and size-limited).

After changing the address in setup, `recovery flash --refresh-connection` explicitly
updates that controller's address and certificate on compatible media. Pairing and
Wi-Fi remain. A different controller identity requires new pairing, not this option.

`storage` shows controller disk use. `storage clean --images --cache` previews
removal and asks before deleting; choose `--candidates` or `--reports` separately.
Results and retained candidates remain. `investigation export NAME DIRECTORY`
creates ordinary files containing problems, run/candidate metadata and received
results. It does not include credentials or claim missing evidence has arrived.

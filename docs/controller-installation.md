# Run the rewrite locally

Milestone 1 has an implementation and fast integration coverage. **The real image
build, USB flash and Bazzite boot/return are not yet demonstrated.** Do not treat
passing software tests as proof that the USB boots.

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

Builds require rootless Podman. The build container supplies Fedora tools; no
Distrobox is required. `quirkbench doctor` lists host tools. Flashing additionally
needs sudo, parted, util-linux, e2fsprogs, dosfstools and GRUB tools. It refuses
mounted disks and non-USB disks, requires confirmation, and elevates only its writer.
The first image is x86-64 UEFI and unsigned; it requires Secure Boot to be disabled.
Quirkbench does not change firmware settings or enroll keys. USB capacity for a real
Bazzite run has not yet been measured.

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
General candidate shell access and live software changes are later milestones.

## First real check

With explicit authorization, first build the recovery image, then select the USB
for erasure. Observe recovery boot and MCP inspection, candidate preparation,
one-time candidate boot, return, uploaded diagnostics and retained network settings.
Check read-only recovery mounts, fresh `/etc` and `/var`, GRUB clearing `next_entry`,
and the initial candidate connection timeout. Record image ID, candidate digest,
logs, observed USB usage and failures. A physical boot failure must be investigated,
not replaced with a passing fixture result.

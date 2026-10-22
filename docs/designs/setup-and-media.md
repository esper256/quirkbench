# Setup, build, flash

Revisable [design](../README.md); commands describe the destination, not current availability.

**Distribution:** ship a native RPM with ordinary dependencies and one command
available to all users. Each user owns `~/.quirkbench` and runs a foreground
controller. Default recovery comes from a release download; keep local builds
explicit. The package helper embeds an exact recovery download selection. Publishing is an
explicit release task; the source checkout does not invent a published selection.

**Development:** `./dev quirkbench ...` runs checkout code on the host with private
Python dependencies. Distrobox forwarding belongs only in this launcher, never
the application. Install no host RPMs as part of development setup. Use existing
host tools and report missing ones; do not silently install or layer packages.
Use normal user data by default, with `--data-dir` for explicit alternatives. Test
release RPM installation in a disposable Fedora environment at packaging checkpoints.

```sh
quirkbench setup
quirkbench controller run
# In another terminal:
quirkbench recovery flash
```

**Setup:** ask short terminal questions with useful defaults; Enter accepts each default. Ask for the target-reachable controller URL, generate missing connection credentials and save `~/.quirkbench/config.toml`. Setup saves configuration; it does not start a background service. Print `quirkbench controller run` as the next step. The controller runs in the foreground in a separate terminal. Re-running edits choices. Direct edits apply at restart; unrelated edits preserve credentials. No controller systemd integration, automatic backgrounding or service manager.

**Download:** default flashing downloads the recovery selected by the installed
release, or reuses its completed cache entry. `recovery download` acquires it
without flashing or controller setup. Verify the archive checksum and expected
files before publishing the cache entry. Release selection is independent of
connected targets and their supported operations.

**Build:** automatically acquire the selected pinned inputs and build the generic recovery image. Cache by relevant code, included files, build instructions, packages and build tools—not paths or unrelated settings. A hit says the image is available; `--force` rebuilds without deleting the last good image first. Show phases/logs; incomplete output is not selectable.

Recovery installs its selected target files directly; it does not build or install
Quirkbench's candidate RPM. One explicit target file list and staging function
serve both image and RPM builders. Hash the selected files and build inputs, not
the entire controller package. Controller-only edits leave recovery reusable.
Shared target code changes still affect both environments.

The first build resolves a complete Fedora RPM set against an empty system and
retains the files, package versions and digests under `recovery-bases/`. Later
builds, including `--force`, install that selection without repository access.
`recovery build --update-base` explicitly selects current packages and keeps the
previous base. Missing or changed retained RPMs stop the build; restore the exact
files or explicitly select a new base. They never silently select newer packages.
The base ID and target runtime identity are embedded separately in recovery.
This retains package inputs, not a promise of byte-identical rebuilt images.

This command builds the recovery image. Experiment candidates use the separate
[candidate deployment path](candidate-deployment.md); ordinary experiments do not
reflash the USB. Include tools for the selected deployment and outbound target connection.

**Flash:** refuse missing setup before writes. Default to the release selection;
`--built` selects the last successful local build or cache reuse. Neither choice
requires a hash. `--image ID` selects an exact image and fails if unknown.
List dates, sizes and default labels in `recovery list`. Offer USB and hot-pluggable external drives, including USB4 NVMe. Show one clear confirmation in the writer after resolving the device path: model, serial, size and current partitions with their mount locations. Refuse mounted drives, including mounted partitions and active swap; recheck after confirmation. Elevate only the disk writer, retain system-disk exclusion and recheck the selected device before writing. Verify/flush completion before declaring success.

Existing Quirkbench media defaults to updating recovery and boot files, keeping
shared data, Wi-Fi settings and pairing. Check the existing layout, recovery
partition capacity and saved filesystem identities first. Image metadata declares
the settings format; format 1 is the original settings directory with saved
NetworkManager profiles. Change it when the location or format becomes incompatible.
Do not silently erase an incompatible USB: explain the issue and require `--erase`
plus the normal erase confirmation. Updates keep filesystem UUIDs and clear any
pending candidate boot so the next boot enters recovery. Shared data is only
mounted read-only during the update. An interrupted update can leave recovery
unbootable; it must not erase saved settings or diagnostics.

Fresh flashes size the recovery partition to the image plus 1 GiB, rounded up
to a whole MiB for alignment. This is unused partition capacity, not padding
added to the image or download. Existing partitions are not moved or resized;
updates still preserve data whenever the image fits. Enlarging an old layout
requires an explicit erase after saving any needed data.

Prepare the final [USB layout](recovery.md) during flashing. The recovery image
is generic: put local controller settings and pairing credentials in the shared
data partition, separately from recovery. Saved network settings go there too.
Candidates receive read-only access to the connection settings they need and use
private run copies for network changes; those changes do not alter recovery.
The pairing invitation is single-use and non-expiring; the resulting target
credential remains stable across reboots until explicitly revoked. No target
resizing or RAM admission limit. After the fixed boot and recovery space, use the remaining
space for candidate software, settings, evidence and temporary files together.
Use the shared space as needed; report shortages when they occur.

Setup also creates one private local MCP access token. Show its file location
with the running controller's MCP URL for one-time agent configuration; keep the
secret out of routine output. Do not require a specific agent product. The same
controller process serves MCP, HTTPS and incoming AsyncSSH connections. Setup records the target-reachable
connection addresses and ports; flashing supplies them to the target. No separate
MCP command or tunnel setup.

Scripting supplies explicit choices without prompts. Use the same configuration,
image cache and disk writer as interactive setup.

## Storage cleanup

Group controller storage by how the user reduces it: build cache, investigations,
recovery images, unassigned diagnostics, and controller/build logs. Podman storage
is separate. Recovery images include their retained base packages.

`storage clean` previews the selected group and asks before removal; `--yes`
confirms explicitly. Keep selected recovery images, exact recovery rebuild inputs
and the current rotating controller log. Clearing caches only removes rebuildable
files. No automatic deletion of collected diagnostics.

`investigation delete NAME` requires a paused investigation with no unfinished
work or retained target deployment. Delete its diagnostics and records; reclaim
candidate files and supplied packages only when no other investigation references
them. OSTree removes unreferenced OS files using its own pruning command. Shared
files are counted once, not charged in full to every investigation. If removal
fails, keep the investigation unavailable and let the same command retry.

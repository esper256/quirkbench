# Recovery and practical protection

Revisable design supporting the [requirements](../requirements/product.md).
Keep a dedicated recovery system separate from the writable candidate. Both
connect outward to Quirkbench. The candidate provides live debugging; recovery
provides inspection, evidence upload and deployment of the next candidate.
The [first milestone](development.md#milestone-1-bootable-recovery-and-agent-driven-deployment)
delivers this recovery-to-candidate boot journey before interactive candidate commands.

## USB and saved files

The controller creates the final layout when flashing. No target resizing or
RAM-based partition sizing. Use these partition roles:

| Partition | Purpose |
| --- | --- |
| Firmware boot | Start Quirkbench from USB. |
| Recovery | Hold the generic recovery system, separate from experimental software. |
| Boot selection | Store a one-time candidate boot request using the chosen bootloader's supported mechanism. |
| Shared data | Hold recovery settings, candidate software, evidence and temporary working directories. |

Size the fixed parts for their actual contents with modest headroom; shared data
gets the remaining space. No separate evidence or unused library partition.
Use the [GRUB arrangement](candidate-deployment.md#recovery-owns-boot-selection)
with the OSTree layout. The first implementation uses a separate ext4 boot
partition for GRUB and OSTree boot files. Recovery mounts it at the candidate
storage root’s `/boot`; candidate package tools do not own its GRUB configuration.

Recovery settings include saved network connections, the controller address,
public key and connection credentials. Flashing writes local settings separately
from the generic recovery image. Recovery saves later changes there. Use ordinary
file permissions and expose only needed directories to candidate programs.

Build recovery as a compressed SquashFS image inside the recovery partition.
Dracut's live-image support copies it into RAM before starting recovery
(`rd.live.ram=1`). Mount the OS read-only; use RAM for temporary runtime files.
Keep saved settings, downloads and diagnostics in shared data. Insufficient RAM
is a boot failure, not a silent fallback to a disk-backed OS. Measure memory use
and boot copying time with the built image.
Prepare authentication and fixed system defaults during image creation. Network
services write runtime configuration under `/run`, not read-only `/etc`. Generic
media uses worldwide Wi-Fi limits rather than guessing a country from a timezone.
Remote inspection cannot install software or edit recovery files. Managed
deployment writes candidate data and the one-time boot request; it does not rewrite
the recovery OS. The human local root terminal remains an explicit exception.

The candidate needs read access to saved network profiles, including credentials
needed to connect, and the controller address and trust information. Supply a
read-only view of the needed connection settings, not the entire recovery storage.
Its network service may import them into private run files; subsequent candidate
network edits never write back to recovery. Keep secrets out of ordinary tool
output and reports. Missing settings produce a connection error, not an automatic
overwrite. Read-only access reduces accidents; it cannot contain arbitrary root code.

Give every run its own evidence and temporary working directories. The evidence
path, saved command output and retry records must be backed by shared USB storage
even if exposed under `/run`; temporary memory alone is insufficient. Supply their
locations through environment variables and the agent instructions. Tell the
agent to leave recovery settings, boot selection and Quirkbench-managed files
alone. Live software edits belong to this run; retaining them requires explicit
candidate preparation. These protections reduce accidents; they cannot contain
a modified kernel.

## Boot, run, return

1. Boot recovery by default and connect using saved settings.
2. Pull the controller's completed OSTree candidate and create fresh run files,
   including `/etc`, `/var` and homes, from its defaults. Supply saved connection
   settings read-only. Recovery deploys software; it does not compile agent code.
3. Wait for an explicit controller boot request after preparation succeeds. The bootloader
   clears the request before attempting the candidate, so the next boot returns
   to recovery even if the candidate never starts successfully. Candidate package
   tools do not change this selection or recovery's boot files.
4. Run prepared tests or interactive commands with independent command timeouts.
   Accept new probes, file changes, package installations and inspections without
   another boot. Stream logs and save evidence during execution. Candidate writes
   must not alter recovery or reusable baseline files.
5. Reboot into recovery on completion or when failure handling permits. Collect
   surviving diagnostics and report interrupted or unknown outcomes honestly.
6. Upload evidence. Delete the USB copy only after the controller confirms it has
   stored it durably. Retry interrupted uploads; a lost reply must not duplicate
   results or cause evidence loss. The controller keeps investigation results.
7. Retain mutable OS files after return, including interruption. The controller
   explicitly requests another boot or discard after diagnostics receipt. Uploaded
   evidence is still deleted from the target. Temporary `ostree admin unlock` edits
   do not survive reboot; persistent configuration and application files do.

A newly booted candidate has a target-side startup connection deadline, supplied
when requesting the boot. Its startup code starts this timer and saves the reason
before returning to recovery if no authenticated controller connection succeeds.
Cancel the timer after the first successful connection; later network loss does
not reactivate it. Recovery does not retry the failed candidate automatically.
This cannot rescue a kernel that hangs before startup code runs.

Use panic reboot and supported hardware watchdogs for best-effort restart, not an
overall run timer. The target enforces individual command timeouts while disconnected.
A healthy idle system stays available. Use the target OS's systemd support to own
the hardware watchdog. Report when automatic restart is unavailable. Suspend tests may need different
watchdog settings. A hard hang can still require manual reset. Network loss alone
is not a reason to reboot or repeat a run. Full-memory crash dumps are optional
investigation work, not a prerequisite for ordinary experiments.

Use the [selected deployment tool's cleanup](candidate-deployment.md) for unused candidates and disposable
files before refusing new work for lack of space. Never discard unuploaded
evidence to make room. Shared storage can fill or become damaged; keeping recovery
separate lets it boot and explain the failure, but cannot guarantee evidence survives.

## Evidence uploads

Follow the [controller API](controller-api.md) for pairing and evidence receipt.
Recovery initiates HTTPS uploads using its paired connection.
The controller does not need to connect back to the target. Use established HTTP
libraries to stream files into ordinary controller directories. Start with whole
file retries; do not add rsync, a second transfer service or custom chunk handling.
Keep incomplete uploads separate from received files and acknowledge stored files
before recovery deletes them. A repeated upload must not duplicate results.

This is a small file-transfer feature. Its implementation should be short and
simple. If it grows into a complicated subsystem, reconsider the approach rather
than treating that complexity as inevitable.

## Screen and troubleshooting

Use the [recovery console mockups](../mockups/recovery-console.md) for appearance
and navigation. A quiet dark background, warm light text, muted green headings
and a solid selection bar take inspiration from [Omarchy's consistent themes](https://omarchy.org/manual/themes/).
Use one 16-color console palette, readable without special fonts; no desktop or
theme manager. Color supports words and selection markers, never replaces them.

### A dedicated screen

Assign boot console output to `tty1` (`console=tty1`) and explicitly redirect
kernel messages there with Linux `TIOCL_SETKMSGREDIRECT`. The kernel otherwise
writes to the active terminal even with `console=tty1`. The recovery TUI owns
`tty2`; no login prompt shares it. Send service logs to the journal. Switch to
tty2 once the TUI can draw, without waiting for network or controller success.
Show setup failures there too. Do not repeatedly switch while the human is using
another terminal. Merely calling `chvt 2` or hiding boot messages is insufficient.
Keep logs available through the menu. See Linux [kernel-message redirection](https://man7.org/linux/man-pages/man2/TIOCLINUX.2const.html)
and systemd [terminal settings](https://www.freedesktop.org/software/systemd/man/256/systemd.exec.html).

### Dashboard and actions

Use two upper panels: the action menu on the left and current status on the right.
Align status labels and values in two columns; give distinct facts their own rows.
Use “Target IP” and “Controller URL” to identify which computer each address belongs to.
A full-width activity log below shows recovery events and incoming MCP actions,
with timestamps and explicit RECOVERY/MCP labels. It follows new events without
moving menu selection; Activity opens scrollable history and action details.
Keep target label and a short stable target ID in current status; show the model
and full ID in Target details. Use the flashed label before pairing, and say when no label
is saved. Never use a new boot ID as the target's identity.
Show network name/interface and address separately from authenticated controller
connection, address and last contact. Internet access is not a prerequisite.
Show actual recovery mount and shared-storage status, including unknown values.
Keep the action menu usable while disconnected or busy.
A persistent Current work row shows idle, active work or what it is waiting for.
Label available shared storage “Data free”, regardless of the device type.
Below Activity, reserve one row and a divider for progress. Show the current step
and elapsed time during transfers, deployment, cleanup, diagnostics and report
uploads. Use an animated ASCII bar when no reliable percentage is available;
do not invent a percentage or add machinery just to estimate one. Report collection
and upload must leave the menu responsive. Child tools such as `nmtui` own their
screen until they return.
Update it from the same work records as Activity. Connection and storage problems
stay visible in their status fields; do not add pinned warnings to the activity log.

The menu offers Network, Controller, Activity, Logs & reports, Target details,
Open terminal and Power. Arrow keys and Enter work; visible letter shortcuts
avoid dependence on function keys. Esc returns. Preserve selection on refresh;
returning from `nmtui`, logs or the shell redraws the whole screen. Fit 80×24;
on smaller screens stack panels and scroll instead of clipping controls.

The activity log shows actions received by this target: readable name, running or
finished state, elapsed time, last update and measured progress when available.
Details show the MCP tool and request/run reference when supplied, plus bounded
output. Distinguish target work from preparation still running on the controller;
do not invent incoming commands or percentages. Hide connection heartbeats and
credentials. Keep recent activity bounded, using existing work records and a small
status file from the target process; no new daemon or event database. Reconnection
must not label interrupted work successful. Viewing activity never approves work.
Keep this disposable status file in RAM under `/run`, outside the shared-data
mount. Refreshing the screen must not flush USB writes or delay SSH replies during
a transfer. Evidence and saved work records still belong on shared storage.
Power offers Reboot and Shut down in recovery; the candidate calls its action
Reboot into recovery. Idle recovery acts directly; confirm when work or uploads
would be interrupted. Show stopping work and the system power request on screen
without freezing the TUI. Show command failures and retain terminal access.
Tell the user to wait for power-off before disconnecting the drive. Do not imply
that restarting resumes a candidate.

Provide a separate, human-confirmed emergency reboot or power-off when normal
shutdown fails. Keep the small emergency control and its dependencies in RAM;
launching another tool from the lost recovery drive is not a fallback. Explain
that unsaved data may be lost. Do not force power-off automatically after a timeout.
This is best effort: losing the system drive can also stop the console or hang
the kernel. Test the emergency control without access to the recovery filesystem,
replacing the final power action in fast tests.

Initialize temporary networking without evidence-storage or pairing prerequisites; use
`nmtui`. Save settings automatically when storage works. If it does not, explain
that the connection is temporary and offer the terminal; do not block networking.

Menu/T opens a real local root shell; exit returns. Function-key switching is
optional. The human accepts unrestricted shell risk. The agent gets recovery
inspection through `ssh_recovery_inspect`, plus managed deployment and boot controls,
not its unrestricted terminal. Full remote command access,
including root within investigation scope, belongs in the candidate.
Give interactive child programs terminal input, output and errors; the console's
own errors go to the journal. Keep a child launch or exit failure visible until
Enter, so returning to the menu does not erase the explanation.

Provide an explicit Quirkbench internal debugging report through
`ssh_recovery_inspect` with `view: report` and the menu's Logs & reports actions.
Include boot logs, failed services, process states and waits, mounts, networking,
current work and the latest deployment log. Reports are not experiment evidence.
Menu uploads use HTTPS independently of SSH and require confirmation of storage
before claiming success. Keep credentials out of reports. No automatic report
capture or upload is required. Show connection failure reasons when known.

Keep blocking recovery file operations off the network event loop using Python's
bounded thread pool. Await durable writes before acknowledging work; do not cancel
a write and release its ownership while its thread is still running. Limit work
in flight rather than creating an unbounded queue. Status reads use memory.
Disk stalls may delay the affected operation, but must not prevent unrelated
network requests. This cannot guarantee responsiveness when the kernel or system
drive fails. The human terminal keeps a visible prompt and readable errors.

Disable internal-disk automount/discovery, swap/resume, repair and firmware-update
helpers. Exclude internal-storage drivers where practical. NVMe stays available
for external USB4/Thunderbolt drives. Before connecting, mark other disks and their
partitions read-only with `blockdev`; this is an accidental-write precaution, not
containment of privileged code. Internal storage and
persistent firmware changes remain outside investigation scope. Power handling
preserves evidence where possible and explains uncertainty. Failure leaves useful
diagnostics and a terminal, not only blocked actions.

## Focused implementation checks

Follow the [testing strategy](testing.md): real recovery code, files and uploads;
replace mount and reboot operations in fast tests. Image and hardware checks
separately establish boot, mount, watchdog and crash-log behavior.
Exercise connected, offline, busy and failed screens through the real TUI with
small recorded status changes. Check navigation, resize and returning from child
programs; avoid exact screen snapshots. These are fast integration tests, without
QEMU. After implementing boot/display changes, manually confirm once in QEMU that
late kernel/service messages stay off tty2 while remaining readable in logs.
Inspect the real console at 80×24; do not add a slow TUI regression suite.

Emergency inspection reads `/proc` and the kernel message buffer without launching
disk-backed programs. It remains useful when shared storage fails. It cannot guarantee survival
through kernel or memory failures.
Saved reports have distinct names and bounded reads. Missing report sections are
reported individually. Complete candidate journals are saved as files; MCP does
not paste them into every response.

## When the recovery drive fails

Keep the menu, connection status and essential controls independent of disk work.
Draw from `/run`; read network addresses from the kernel. Mark stalled storage
checks unavailable instead of keeping an old value. Save network changes in the
background and say whether they were saved for the next boot.

Run the complete recovery OS from the RAM copy; do not lock individual processes
in memory or maintain lists of libraries to preload.
Cache the connection details needed for report upload in private `/run` files.
Evidence collection must not delay SSH reconnection. Emergency reports read bounded
kernel information without launching programs or waiting for full reports. They
are saved in RAM and uploaded over HTTPS; they disappear at power off unless uploaded.

Normal power actions still finish work and clear boot selection where needed.
Offer confirmed force reboot and force power off, including while normal shutdown
is stuck. These ask the kernel directly, without syncing, programs or disk writes.
Warn about lost unsaved data and unchanged boot selection. Never escalate silently.
This improves the chance of recovery; it cannot fix a stuck kernel or guarantee
that the kernel, network hardware or firmware keeps responding. Programs can
still block on failed shared storage. Reboot needs working boot media again.

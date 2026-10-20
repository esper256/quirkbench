# Recovery and practical protection

Revisable design supporting the [requirements](../requirements/product.md).
Keep a dedicated recovery system separate from the writable candidate. Both
connect outward to Quirkbench. The candidate provides live debugging; recovery
provides inspection, evidence upload and deployment of the next candidate.
The [first milestone](development.md#milestone-1-bootable-recovery-and-agent-driven-deployment)
delivers this recovery-to-candidate boot journey before interactive candidate commands.

## USB and saved files

The controller creates the final layout when flashing. No target resizing or
RAM-based capacity checks. Use these partition roles:

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

Mount recovery OS files read-only. Keep saved settings, downloads and diagnostics
in shared data, and temporary runtime files in writable runtime directories.
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
3. Record a one-time boot request only when preparation succeeds. The bootloader
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
7. Discard finished runs' mutable OS and temporary files after collecting available
   diagnostics, including after interruption. Files the agent needs returned
   belong in evidence, not temporary storage. Do not remove active-run files.

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

Show a clean dashboard automatically, including on failure: one next action plus
Network, Connection, Troubleshooting, Open terminal and Power. Prefer VT2 for UI,
VT1 for boot logs; switch to the UI without requiring a keypress. Initialize
temporary networking without evidence-storage or pairing prerequisites; use
`nmtui`. Save settings automatically when storage works. If it does not, explain
that the connection is temporary and offer the terminal; do not block networking.

Menu/T opens a real local root shell; exit returns. Function-key switching is
optional. The human accepts unrestricted shell risk. The agent gets recovery
inspection through `ssh_recovery_inspect`, plus managed deployment and boot controls,
not its unrestricted terminal. Full remote command access,
including root within investigation scope, belongs in the candidate.

Provide Quirkbench internal debugging reports for repairing recovery itself:
boot logs, service errors and system information. They are not experiment
evidence. Collection works without an investigation or successful setup. Allow
preview, local export or upload to the paired controller. Omit credentials and do
not post publicly. Pairing is required for upload, not local collection.

Disable internal-disk automount/discovery, swap/resume, repair and firmware-update
helpers. Exclude internal-storage drivers where practical. Internal storage and
persistent firmware changes remain outside investigation scope. Power handling
preserves evidence where possible and explains uncertainty. Failure leaves useful
diagnostics and a terminal, not only blocked actions.

## Focused implementation checks

Follow the [testing strategy](testing.md): real recovery code, files and uploads;
replace mount and reboot operations in fast tests. Image and hardware checks
separately establish boot, mount, watchdog and crash-log behavior.

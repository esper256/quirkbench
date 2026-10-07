# Recovery and practical protection

Revisable design supporting the [requirements](../requirements/product.md).

## USB and saved files

The controller creates the final layout when flashing. No target resizing or
RAM-based capacity checks. Use these partition roles:

| Partition | Purpose |
| --- | --- |
| Firmware boot | Start Quirkbench from USB. |
| Recovery | Hold the generic recovery system, separate from experimental software. |
| Boot selection | Hold the existing small record requesting one experiment boot. |
| Shared data | Hold recovery settings, OSTree candidates, evidence and temporary working directories. |

Size the fixed parts for their actual contents with modest headroom; shared data
gets the remaining space. No separate evidence or unused library partition. Keep
the existing boot-selection mechanism unless a simpler supported layout replaces
it; its separate partition is an implementation choice, not a requirement.

Recovery settings include saved network connections, the controller address,
public key and connection credentials. Flashing writes local settings separately
from the generic recovery image. Recovery saves later changes there. Use ordinary
file permissions and expose only needed directories to candidate programs.

Give every run its own evidence and temporary working directories. Supply their
locations through environment variables and the agent instructions. Tell the
agent to leave recovery settings, boot selection and Quirkbench-managed files
alone; change installed software by submitting another candidate. Cooperation and
ordinary permissions reduce mistakes; they cannot contain a modified kernel.

## Boot, run, return

1. Boot recovery by default and connect using saved settings.
2. Fetch missing OSTree content from the controller and prepare a complete
   candidate on shared data. Recovery does not compile or resolve RPM packages.
3. Record a one-time boot request only when preparation succeeds. The bootloader
   clears the request before attempting the candidate, so the next boot returns
   to recovery even if the candidate never starts successfully.
4. Run the experiment with the agent's time limit. Stream logs when connected and
   save evidence locally during execution; do not wait until shutdown to save it.
5. Reboot into recovery on completion or when failure handling permits. Collect
   surviving diagnostics and report interrupted or unknown outcomes honestly.
6. Upload evidence. Delete the USB copy only after the controller confirms it has
   stored it durably. Retry interrupted uploads; a lost reply must not duplicate
   results or cause evidence loss. The controller keeps investigation results.
7. Clear finished runs' temporary directories after collecting available
   diagnostics, including after interruption. Files the agent needs returned
   belong in evidence, not temporary storage. Do not remove active-run files.

Use the agent's run deadline, panic reboot and supported hardware watchdogs for
best-effort restart. Let systemd own the hardware watchdog rather than adding a
second controller for it. Report when automatic restart is unavailable; do not
require a new per-kernel approval process. Suspend tests may need different
watchdog settings. A hard hang can still require manual reset. Network loss alone
is not a reason to reboot or repeat a run. Full-memory crash dumps are optional
investigation work, not a prerequisite for ordinary experiments.

Use [OSTree cleanup](candidate-deployment.md) for unused candidates and disposable
files before refusing new work for lack of space. Never discard unuploaded
evidence to make room. Shared storage can fill or become damaged; keeping recovery
separate lets it boot and explain the failure, but cannot guarantee evidence survives.

## Screen and troubleshooting

Show a clean dashboard automatically, including on failure: one next action plus
Network, Connection, Troubleshooting, Open terminal and Power. Prefer VT2 for UI,
VT1 for boot logs; switch to the UI without requiring a keypress. Initialize
temporary networking without evidence-storage or pairing prerequisites; use
`nmtui`. Save settings automatically when storage works. If it does not, explain
that the connection is temporary and offer the terminal; do not block networking.

Menu/T opens a real local root shell; exit returns. Function-key switching is
optional. The human accepts unrestricted shell risk; do not expose this emergency
tool as remote agent access.

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

Use existing software tests for interrupted candidate preparation, consumption of
the one-time boot request, lost upload replies, and cleanup that preserves active
files, settings and unuploaded evidence. Check the screen with missing storage
and no network. Reuse unrelated run names and paths. Hardware checks separately
establish actual boot, watchdog and crash-log behavior; software tests cannot
promise those outcomes. Do not add image builds or QEMU to routine checks.

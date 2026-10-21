# First physical NVMe candidate: debugging friction

Observed October 20, 2026 (Pacific time). These are findings and suggested improvements,
not new product requirements. Keep Bazzite-specific fixes in its preparation code.

## What happened

Recovery received the candidate, booted Bazzite, connected to the controller and ran
`uname -a` successfully (kernel `7.2.9-ogc3.1.fc44.x86_64`). Bazzite's
`bazzite-hardware-setup.service` then changed kernel arguments, created another
OSTree deployment and logged “Rebooting to apply karg changes.” Recovery booted,
uploaded diagnostics and command output, and completed cleanup. This was an
OS-requested reboot, not an observed kernel crash or Quirkbench connection timeout.

The pull fetched 19,808 metadata and 194,177 content objects: 4,785,679 KiB transferred
in 684 seconds, with 10.8 GB of content written. Recovery remained responsive to MCP.

## Friction and useful next changes

| Observed friction | Improvement |
| --- | --- |
| Transfer and boot are one action; no chance to inspect deployment before reboot. | Separate deployment from explicit one-time boot; retain a convenient combined action if useful. |
| Download showed a spinner and empty log for eleven minutes. We inferred progress from free disk space. | Expose OSTree's object counts, transferred bytes, rate and stage. Mark changing totals as estimates. |
| Repeated 45-second MCP waits consumed agent interactions. | Keep the intended quiet wait/notification design; do not require conversational polling. |
| The running controller lacked the newly implemented cancel tool. We discovered this by listing tools and restarted it. | Show running software identity and capabilities clearly, including differences from installed code. |
| We reused a prepared candidate with an older target package than recovery. | Show the selected candidate's base and target-support version before deployment. |
| GRUB displayed an undefined `load_video` warning before the candidate booted. | Supply the helper expected by Fedora's boot-entry reader; check the actual generated menu. |
| Bazzite first-boot setup changed the deployment and rebooted without agent intent. | Disable that automatic deployment/reboot service in the Bazzite preparation; record the deviation. Preserve useful hardware setup deliberately where needed. |
| The candidate connected, then disappeared. The agent initially could not distinguish reboot, connection loss or crash. | Show last contact, known boot transition and evidence-upload progress; leave the cause unknown until supported. |
| `uname` finished successfully, but the agent's next read saw disconnected with empty output. | Make pending evidence distinct from absent output; point to a wait/read action that later returns uploaded command results. |
| We read controller SQLite records to find run/evidence state and local paths. | Expose the run summary, command results and uploaded files through MCP; no database inspection in the ordinary journey. |
| Controller command work still said accepted after the uploaded record said finished. | Reconcile final command status from received evidence. |
| Recovery's generic outcome was interrupted, although the journal showed an orderly OS-requested reboot. | Keep observed outcome separate from inferred cause and link relevant logs. Do not automatically call every return a crash. |
| Reports returned large inline text as well as a saved path. The temporary MCP helper needed filtering to avoid flooding agent context. | Return a compact summary and bounded/paginated reads of saved reports. |
| Candidate journal was capped at 64 KiB and began mid-traceback. The reboot cause survived only because it was near the end. | Retain complete logs as files where practical; bound individual responses and clearly disclose truncation. |
| Combined reports include boot-log content again inside recent activity details. Repeated reads overwrite the same report path. | Avoid duplicate payloads; make report time/boot identity clear and allow deliberate retention of a useful earlier snapshot. |
| Recovery cleanup removed the old run, so “boot it again” requires a fresh deployment. | Make this explicit in the run result and offer a clear repeat action using cached candidate content. |
| The UEFI boot-menu failure was invisible to Linux logs; the human had to launch the EFI file manually. | Keep firmware-stage failures distinct from candidate failures. Preserve the human observation without claiming Linux logs explain it. |

## Evidence

Local run: `run-58684e7c8fe6`; deployment: `ab62f0de7882c1d3f70aabb4`.
Candidate: `3b6c5849cd0321cb38503546`.
Files are under the controller's `results/run-58684e7c8fe6/`, especially
`candidate-journal.log`, `outcome.json` and `commands/`.
Reports are under `reports/target-e438d64a7291/`, keyed by boot ID.
These identifiers are historical evidence, not defaults or test requirements.

## Later recovery I/O failure

The same recovery boot remained connected, but remote `boot-log` and `processes`
inspection both failed to start their programs: `[Errno 5] Input/output error`
for `journalctl` and `ps`. The last saved report predates this failure. This
establishes that existing networking can survive while new programs cannot be
read; it does not identify the enclosure, cable, port, drive or filesystem as
the cause. An emergency report must use kernel memory interfaces directly,
without needing to execute a program from the failing drive.

## Follow-up implementation

Deployment, boot and discard are separate tools. Returned files remain until
explicit discard; a repeat boot has new run and command identities. Plain
`ostree admin unlock` changes may be lost on reboot, as agreed. Added native
OSTree terminal progress, the GRUB video helper, Bazzite automatic-reboot/update
masks, bounded saved-output reads, command-result reconciliation, retained full
candidate journals and emergency memory-only inspection. A quiet CLI wait avoids
repeated short MCP calls when the agent can await a local process. No universal
background MCP notification is promised for clients without task support.

Recovery has since been rebuilt; see [current build status](../controller-installation.md).
The latest changes still need a real boot/return check with matching candidate tools.
They do not explain the firmware boot-menu failure or the later I/O errors.

Independent review also caught restart after action admission, interruption after
boot selection, and reusing evidence identity after failed discard. These now
have focused integration checks. Transfer cancellation waits for process-group
termination and output writes even when cancellation is requested repeatedly.

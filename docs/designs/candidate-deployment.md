# Prepare candidates and keep runs disposable

The first target baseline is **latest stable Bazzite**, using rpm-ostree/OSTree.
Default to the standard desktop image `ghcr.io/ublue-os/bazzite:stable` for x86-64.
Resolve `stable` when preparing the investigation baseline and record the exact
image digest and version. Reuse that selection for retries and subsequent runs;
updating the baseline is explicit. Disable unattended OS updates in candidates.

Bazzite publishes a bootable container image, not a bare OSTree repository URL.
Use supported image tooling to derive it with the target RPM and debugging tools.
Choose compatible build tooling and preserve the user/session setup needed for
reproduction. Report Quirkbench's changes and differences from the installed OS.
See [Bazzite's image references](https://docs.bazzite.gg/Installing_and_Managing_Software/Updates_Rollbacks_and_Rebasing/rebase_guide/).

## Common code, OS-specific preparation

Keep MCP, reverse SSH, command recording, evidence and recovery common.
`bazzite.py` prepares the first OS; `candidates.py` handles OSTree deployment and
cleanup without selecting a distribution. Other deployment mechanisms can be added
alongside it. Start with small modules, not a plugin framework.
The target RPM packages the staged connection and execution files also used by
recovery; recovery copies those files directly. Neither contains controller/MCP
implementation modules. Keep the explicit file list shared with build hashing.
Include package instructions and the actual builder image identity in its cache
key, and check the recorded RPM digest before reuse. Candidate preparation also
includes OS-specific preparation code/assets, the base and captured inputs.
Source identities describe installed code; unequal controller and target hashes
are expected, not compatibility failures. New actions needing newer target code
must explain that an update is required. Bazzite preparation
adds its service overrides and diagnostic health-check runner before publishing the
candidate; MCP does not repair OS policy after deployment.
[Greenboot](https://github.com/fedora-iot/greenboot-rs) combines checks with automatic
reboot/rollback. The Bazzite adaptation runs its installed required/wanted scripts
as diagnostics, honoring disabled checks, without running green/red actions. Disable competing update,
rollback and automount services, while preserving desktop/audio services. Report
these differences with the candidate. Boot setup supplies target network settings
and connection details, not distribution policy.

A future Debian implementation can reuse the same target program and provide a
`.deb`; the first packages that program as an RPM. MCP stays on the controller.

The controller prepares the selected base plus the Quirkbench target tools RPM, debugging
tools and requested changes. It serves completed OSTree content over HTTPS.
Recovery pulls missing content and deploys it. A separate controller request selects its one-time boot. Neither
routine changes nor fresh runs reflash the USB. Reuse packages, builds and content.
Import Bazzite inside the tools container as container root, using a bare OSTree
cache so layered files retain their ownership and executable permissions. Publish
the result through the controller's archive repository.

## Where changes live

These are storage areas, not additional partitions:

| Area | Lifetime |
| --- | --- |
| Recovery settings: network profiles, controller address and credentials | Until explicitly changed in setup or recovery |
| Prepared candidate: OS, packages, configuration and target program | Unchanged after preparation; reusable |
| Run files: writable OS configuration, application data, homes, installed-file changes and scratch files | Until the controller explicitly discards the deployment |
| Evidence files | On USB until confirmed uploaded; retained on controller |
| Command records | Kept on target through run completion for retry/reconnect; results retained on controller |
| Package, build and transfer caches | Disposable; not the only copy of results or their source/build information |

Each new deployment starts with the selected candidate's defaults and fresh mutable files,
including `/etc`, `/var` and home directories. Ordinary services use ordinary Linux
paths. Do not inherit a previous run's configuration merges, application state or
writable overlays. OSTree's usual shared `/var` and merged `/etc` are not a clean
reset. Keep run files separate from reusable repository content; never modify
shared hard-linked files in place. Keep returned run files until the controller explicitly discards them. Upload evidence immediately and delete only its confirmed copy. A repeat boot reuses persistent edits but receives a new run ID; commands are never replayed. A clean repeat requires discard followed by a new deployment. Preserve unuploaded evidence.

First arrangement to verify: recovery creates a separate OSTree storage root for
each run, with a fresh deployment, `--no-merge` configuration defaults and its own
`/var` and home directories. Mount those
directories before dependent services start. Reuse unchanged OS content; do not
undo the previous run's edits. Check service initialization and OSTree's mount
ordering before treating this arrangement as working.
Write a normal `/boot` fstab entry into the fresh candidate configuration, so
systemd mounts the boot filesystem before ordinary services. Reject unexplained
inherited filesystem mounts rather than mounting another machine's disks.
Recovery continues to own boot selection.
On explicit discard, let OSTree undeploy and clean up before removing the run's remaining
writable directories. Keep references to reusable candidates in its repository.

## Live changes and explicit persistence

| Request | Behavior |
| --- | --- |
| Probe or script | Transfer through SSH and execute in the run's working directory. No RPM required. |
| Live package change | Try supported RPM installation in the temporary writable layer; report unsupported transactions as requiring a prepared candidate and new boot. |
| Direct installed-file edit | Use a temporary writable layer for this run; leave prepared content unchanged. |
| Retain changes or replace a kernel | Prepare a named candidate from an explicit base, package additions/replacements and selected files or instructions. Boot through recovery. |

Use `ostree admin unlock` for disposable `/usr` edits first. Those edits are lost on reboot, including a repeat boot of a retained deployment. Persistent `/etc`, `/var` and home files remain until discard. No custom overlay is needed. Do not build custom
overlay management unless the actual supported image exposes a concrete need. The proposed `rpm_overlay_install` helper must
establish package dependencies, scripts and affected files before promising live
installation. An overlay alone is not a package manager. Verify diagnostic-package
addition and PipeWire replacement early; do not implement every transaction as
bare `rpm -Uvh` or silently reduce all changes to reboot-only operation.

Do not wrap `rpm-ostree --apply-live` and assume it is run-local: it also prepares a
pending deployment. Use rpm-ostree for controller-side candidate preparation;
retain its live path only if explicitly shown to preserve recovery boot ownership.

Retaining build output does not retain a live installation. Do not automatically
save a running machine or turn shell history into a reproducible candidate.
Context reports supported live modes and what lasts until run completion. Do not
promise arbitrary mixing or reversal of package changes and direct file overlays;
explain when a fresh run is needed. An installed kernel is not the running kernel.

## Recovery owns boot selection

Prepare into unfinished locations; only completed output is selectable. Recovery
installs the candidate and requests one boot, keeping itself the default. Candidate
package operations must not replace recovery or independently select the next boot.
Use supported rpm-ostree/OSTree operations; verify their boot integration early,
rather than maintaining a second boot manager or silently fighting package scripts.
Use GRUB one-time `next_entry` as the first boot arrangement to verify, with a
fixed recovery default. Confirm GRUB can clear that entry on the selected storage,
and that candidate package operations cannot replace recovery's selection or boot
files. Check preparation, live changes, fresh files and fallback together before
extending deployment. Naming GRUB does not establish that this integration works.
If this needs upstream patches or competing boot managers, follow the
[stop-and-ask guidance](development.md#agent-decisions-and-when-to-ask).

After a crash, report the changed boot and partial results. Never send a candidate
command to recovery automatically. Reuse tool-provided cleanup; preserve active
runs, settings and unuploaded evidence. Report space shortages plainly.

Measure peak USB space for the first Bazzite journey, including preparation,
mutable files, cached content and evidence, before recommending a drive size.
Share unchanged content and clean unused files. Do not add static-delta generation
or an arbitrary candidate-count limit without a measured need.

References: [OSTree deployments](https://ostreedev.github.io/ostree/deployment/),
[development unlock](https://ostreedev.github.io/ostree/man/ostree-admin-unlock.html),
[rpm-ostree live changes](https://coreos.github.io/rpm-ostree/administrator-handbook/),
[GRUB one-time boot](https://www.gnu.org/software/grub/manual/grub/html_node/next_005fentry.html).

## Cancel a deployment

`recovery_cancel_ostree(work_id)` returns promptly. The controller saves the request
and retries after reconnection. `controller_work_wait` reports `cancelled` only
after recovery stops the deployment process, clears boot selection and removes the
unfinished run. Downloaded content remains reusable. Repeated cancellation is safe;
a late deployment request cannot revive cancelled work.

A stalled disk can delay cancellation; networking remains available, but a replacement
deployment waits for cleanup. Cleanup failure remains visible and blocks target reuse.
Once recovery commits to reboot, use `recovery_return` after the candidate connects.
Cancellation does not stop controller-side candidate preparation.

## Separate transfer, boot and discard

`recovery_deploy_ostree` creates fresh files without selecting a boot. The agent can
inspect progress and recovery before `recovery_boot_ostree(run_id)` boots them.
After return and confirmed diagnostics receipt, boot again to keep persistent files,
or use `recovery_discard_ostree` before deploying a fresh candidate. Both normal and
unexpected returns retain files. A repeat boot gets new evidence and command IDs.
Recovery enforces these requests; it never interprets upload receipt as permission
to remove OS files. Old recovery images without these actions must be updated.

OSTree's terminal output supplies transfer progress, with changing object totals
treated as estimates. Other stages show activity without invented percentages.
Bazzite preparation masks its automatic hardware-setup reboot and update services;
these changes are reported with the base and target-tools source identity.

## Agent inputs and reuse

`rpm_ostree_prepare_candidate` accepts controller-local RPM `packages`, RPM
`replacements`, and an optional executable `startup_probe`. The agent builds or
acquires these; Quirkbench copies their bytes before starting asynchronous work.
A changed input with the same request ID conflicts. Keep originals in the
investigation workspace, not Quirkbench source.

The startup probe runs after shared storage is available and before networking,
with `startup_probe_timeout_seconds` (default 60 seconds) and output in the journal. It is for early
userspace diagnosis, not instrumentation before the kernel or initramfs starts.
Kernel/initramfs instrumentation belongs in the supplied packages.

Reuse completed preparation for the same selected base, target-tools source and
captured inputs. Export and import are separate visible stages. This cache avoids
repeating work; it does not replace retained investigation results.

`recovery_deploy_ostree` accepts optional `kernel_args` for debugging. They are
recorded with the deployment and survive repeat boots. Quirkbench retains control
of root/deployment selection, internal-disk protections, recovery behavior and
hibernation-resume settings. Startup probes receive their evidence and scratch
directories before networking, so early failures can leave retrievable files.

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

Keep MCP, reverse SSH, command recording, evidence and recovery common. Small
OS-specific Python modules prepare candidates, deploy them and describe boot files
and supported live changes. Start with one implementation, not a plugin framework.
A future Debian implementation can reuse the same target program and provide a
`.deb`; the first packages that program as an RPM. MCP stays on the controller.

The controller prepares the selected base plus the Quirkbench target RPM, debugging
tools and requested changes. It serves completed OSTree content over HTTPS.
Recovery pulls missing content, deploys it and selects its one-time boot. Neither
routine changes nor fresh runs reflash the USB. Reuse packages, builds and content.

## Where changes live

These are storage areas, not additional partitions:

| Area | Lifetime |
| --- | --- |
| Recovery settings: network profiles, controller address and credentials | Until explicitly changed in setup or recovery |
| Prepared candidate: OS, packages, configuration and target program | Unchanged after preparation; reusable |
| Run files: writable OS configuration, application data, homes, installed-file changes and scratch files | One run |
| Evidence files | On USB until confirmed uploaded; retained on controller |
| Command records | Kept on target through run completion for retry/reconnect; results retained on controller |
| Package, build and transfer caches | Disposable; not the only copy of results or their source/build information |

Each run starts with the selected candidate's defaults and fresh mutable files,
including `/etc`, `/var` and home directories. Ordinary services use ordinary Linux
paths. Do not inherit a previous run's configuration merges, application state or
writable overlays. OSTree's usual shared `/var` and merged `/etc` are not a clean
reset. Keep run files separate from reusable repository content; never modify
shared hard-linked files in place. Discard the run after collecting its diagnostics
rather than attempting to undo individual edits. Preserve unuploaded evidence.

First arrangement to verify: recovery creates a fresh deployment with candidate
configuration defaults and fresh per-run `/var` and home directories. Mount those
directories before dependent services start. Reuse unchanged OS content; do not
undo the previous run's edits. Check service initialization and OSTree's mount
ordering before treating this arrangement as working.

## Live changes and explicit persistence

| Request | Behavior |
| --- | --- |
| Probe or script | Transfer through SSH and execute in the run's working directory. No RPM required. |
| Live package change | Try supported RPM installation in the temporary writable layer; report unsupported transactions as requiring a prepared candidate and new boot. |
| Direct installed-file edit | Use a temporary writable layer for this run; leave prepared content unchanged. |
| Retain changes or replace a kernel | Prepare a named candidate from an explicit base, package additions/replacements and selected files or instructions. Boot through recovery. |

Prefer a temporary writable `/usr` layer for quick changes, using OSTree's
supported development unlock. The proposed `rpm_overlay_install` helper must
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

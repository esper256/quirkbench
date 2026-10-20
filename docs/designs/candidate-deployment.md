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

## Live changes and explicit persistence

| Request | Behavior |
| --- | --- |
| Probe or script | Transfer through SSH and execute in the run's working directory. No RPM required. |
| Live package change | Use supported rpm-ostree live operations scoped to this run. Report service restarts or a required fresh boot. |
| Direct installed-file edit | Use a temporary writable layer for this run; leave prepared content unchanged. |
| Retain changes or replace a kernel | Prepare a named candidate from an explicit base, package additions/replacements and selected files or instructions. Boot through recovery. |

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
Select the concrete directory/mount layout and bootloader integration during the
first implementation stage. Check preparation, live changes, reset and fallback
together. If this needs upstream patches or competing boot managers, follow the
[stop-and-ask guidance](development.md#agent-decisions-and-when-to-ask).

After a crash, report the changed boot and partial results. Never send a candidate
command to recovery automatically. Reuse tool-provided cleanup; preserve active
runs, settings and unuploaded evidence. Report space shortages plainly.

References: [OSTree deployments](https://ostreedev.github.io/ostree/deployment/),
[rpm-ostree live changes](https://coreos.github.io/rpm-ostree/administrator-handbook/).

# Documentation

Read the guide for the task at hand; the repository does not require a full-document
read-through. Superseded plans, run logs and investigation histories live in Git history.

| Need | Start here |
| --- | --- |
| Desired finished product | [Product manual](../README.md); its warning distinguishes proposed behavior |
| Next development task | [First-usable tracker #29](https://github.com/esper256/quirkbench/issues/29), [contributing](../CONTRIBUTING.md), [cloud worker prompt](cloud-worker-prompt.md) |
| Focused CI and retained failure evidence | [Subsystem suites and diagnostics](ci-evidence.md) |
| Local or cloud software development | [Development setup and tests](testing-policy.md#portable-software-development) |
| Product scope and acceptance | [Roadmap](product-roadmap.md), [acceptance guide](installation-to-patch.md); task status lives in GitHub |
| Approved CLI redesign and implementation guidance | [CLI redesign plan](cli-redesign-plan.md); approved design; current commands are described in the agent guide and installed help |
| Approved Unix configuration and path design | [Configuration and path design plan](path-portability-plan.md); implemented in PR #144; current operation is in the controller installation guide |
| Implementation rules | Relevant [C0–C7 contract](implementation-contracts.md), [C8 interface](product-interface.md) |
| Current installation and commands | [Controller installation](controller-installation.md), [agent guide](agent-guide.md) |
| Clone and run, optional manual command symlink | [Source checkout workflow](controller-installation.md#run-from-a-source-checkout) |
| Signed release preparation and operator gates | [Publication preflight and runbook](release-publication.md) |
| Controller deployment from a cloud container or Distrobox | [Choosing the controller host](controller-installation.md#choosing-the-controller-host) |
| Recovery inputs, image production and manual target setup | [Acquisition](recovery-acquisition.md), [retained RPM replay](recovery-rpm-replay.md), [recovery operations](recovery-operations.md) |
| Kernel builds and deployments | [Build and boot](build-and-boot.md), [builder environment](../environments/README.md) |
| State, cleanup and visibility | [Retention](local-state-maintenance.md), [monitoring](monitoring.md) |
| Backup coverage and paused restore | [Backup and restore](backup-and-restore.md) |
| Architecture and target interfaces | [Architecture/storage policy](architecture.md), [protocol](protocol.md), [image layout](debug-image.md) |
| Recovery design and limitations | [Recovery synthesis](recovery-base.md), [evidence coverage](recovery-and-evidence.md), [watchdog qualification](watchdog-qualification.md) |
| Planned recovery dashboard and local troubleshooting | [Recovery console UX plan](recovery-console-ux-plan.md); intended behavior, not current capabilities |
| Proposed experimental kernel tailoring | [Kernel design](targeted-experiment-kernels.md), an M3 input |
| Vocabulary and validation | [Terminology](terminology.md), [testing policy](testing-policy.md), [release gates](../acceptance/README.md) |

The executable CLI and installed schemas describe available interfaces. Historical
record readers remain supported where the code requires them; removing old prose
does not authorize dropping compatibility or reinterpreting stored evidence.

- [Investigation results and retention](investigation-reports.md)
- [Public patches and investigation results export](investigation-export.md)

- [Portable recovery input bundles](recovery-input-bundles.md): prepare, verify, transfer and build without a controller.

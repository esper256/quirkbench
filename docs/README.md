# Documentation

Read the guide for the task at hand; the repository does not require a full-document
read-through. Superseded plans, run logs and investigation histories live in Git history.

| Need | Start here |
| --- | --- |
| Desired finished product | [Product manual](../README.md); its warning distinguishes proposed behavior |
| Next development task | [Implementation handoff](implementation-handoff.md) |
| Delivery order and feature gaps | [Roadmap](product-roadmap.md), [command checklist](installation-to-patch.md) |
| Implementation rules | Relevant [C0–C7 contract](implementation-contracts.md), [C8 interface](product-interface.md) |
| Current installation and commands | [Controller installation](controller-installation.md), [agent guide](agent-guide.md) |
| Recovery inputs, image production and manual target setup | [Acquisition](recovery-acquisition.md), [recovery operations](recovery-operations.md) |
| Kernel builds and deployments | [Build and boot](build-and-boot.md), [builder environment](../environments/README.md) |
| State, cleanup and visibility | [Retention](local-state-maintenance.md), [monitoring](monitoring.md) |
| Architecture and target interfaces | [Architecture/storage policy](architecture.md), [protocol](protocol.md), [image layout](debug-image.md) |
| Recovery design and limitations | [Recovery synthesis](recovery-base.md), [evidence coverage](recovery-and-evidence.md), [watchdog qualification](watchdog-qualification.md) |
| Proposed experimental kernel tailoring | [Kernel design](targeted-experiment-kernels.md), an M3 input |
| Vocabulary and validation | [Terminology](terminology.md), [testing policy](testing-policy.md), [release gates](../acceptance/README.md) |

The executable CLI and installed schemas describe available interfaces. Historical
record readers remain supported where the code requires them; removing old prose
does not authorize dropping compatibility or reinterpreting stored evidence.

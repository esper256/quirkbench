> Current image contract: [six-partition v1 layout, live evidence and qualified reset](docs/debug-image.md). [Fresh layout-2 qualification](docs/v1-qualification.md) passed 405 software tests and ten real UEFI boot trials; physical Acer qualification remains outstanding.

# Quirkbench

The [forward product plan](docs/product-roadmap.md) defines the remaining work:
read-only target inventory → tailored provisioned image → attended Acer cycle →
CLI-driven problem-solving sessions → qualified unattended operation. Its new
commands are planned interfaces, not features already available. MCP is deferred;
the local CLI and durable operations will be the agent API.

Quirkbench implements a durable controller/target lab with a human monitor, authenticated HTTPS, strict contracts and fault tests. The current six-partition image includes a physical handoff supervisor, live evidence and watchdog integration, and has passed fresh VM boot qualification. **It is not yet commissioned for unattended Acer operation.** Its hardware, reset and extended-campaign acceptance gates remain separate from software and VM results.

## Run the software acceptance gate

Routine development uses focused tests; expensive infrastructure qualification
are final major-version release gates with explicit opt-in. See the
[testing and agent quota policy](docs/testing-policy.md) and [agent instructions](AGENTS.md).

Python 3.11+ is required. The core controller and simulated loop use the standard library. OSTree composition, deployment and backup also require system OSTree tools; strict signature verification requires Python GI (`python3-gobject-base`). Install test/build dependencies into a project environment, not system Python:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
make acceptance-m1
```

The current workspace already has a prepared `.venv`. `make` exports the local `src` directory, so the tests and demo also work without an editable installation. CI tests Python 3.11 and 3.13; local verification used Python 3.14. TLS fixtures are generated in temporary directories with test-only cryptography dependencies, without optional skipped transport tests.

## Try the durable loop and monitor

```sh
make demo
make monitor
```

The demo uses a fake builder, scripted agent and simulated target. It submits two repetitions, records evidence, pauses after one, reconstructs the controller, explicitly resumes, completes the second, and pauses again. All experimental outcomes are explicitly inconclusive because no kernel behavior was tested. State persists under `.quirkbench/demo/`.

Equivalent commands after installation:

```sh
quirkbench --state .quirkbench/demo demo
quirkbench --state .quirkbench/demo/controller watch demo
quirkbench --state .quirkbench/demo/controller watch demo --once --json
```

The monitor shows real repetition and byte-count bars, phase, elapsed time, last contact/report/advancement and deadlines. Unknown percentages stay unknown. `WAITING`, `REPORTING_LATE`, `SUSPECTED_STALL`, `OVERDUE` and `UNCERTAIN` communicate different conditions. Monitoring uses no agent tokens. See [monitoring semantics](docs/monitoring.md).

## Components

- **Controller:** SQLite migrations/state machine, immutable specifications and results, resumable SHA-256 artifact uploads, durable evidence acknowledgements, generation/lease fencing, pause/resume, source checkpoints, usage ledger and consistent backup/restore.
- **Target:** durable claim request and execution journal, retained evidence outbox, validated acknowledgements, one local supervisor at a time, allowlisted bounded recipe processes and heartbeat supervision. The physical runtime includes a streaming `system-observation` recipe; simulation has its own smoke recipe. No shell supplied by the controller executes on the target, and observations alone do not establish an issue fix.
- **Adapters:** local and authenticated HTTPS transports, provider-neutral JSON coding-agent command adapter, fake builder, OSTree composition/deployment and repository-retention adapters, and image/QEMU qualification tools. Target tokens are separate from AI credentials. AI credentials stay on the controller and are never configured into target images.
- **Visibility:** persistent progress reports and event cursor, independent heartbeat and advancement ages, terminal/JSON watcher, measured upload bytes and compact result context.

Use `quirkbench --help` for commands. Administration stays local: register a capability report, create/submit a campaign, resume/pause/status, put artifacts, snapshot explicitly selected source files, invoke an agent decision, resolve uncertainty with a reason, and back up/restore. `serve` requires TLS certificate/key and a protected JSON device-token file; LAN binding requires `--allow-lan`. `target` requires a pinned CA, token file and capability report. A restarted server always pauses scheduling. `serve-repository` separately requires a server certificate/key, client CA and configured repository aliases; it publishes only read-only OSTree content using mutual TLS. Supply aliases through `--repositories` or the state directory’s `repositories.json`. Read [the protocol](docs/protocol.md) before running a LAN service.

A controller command defaults to a 20 GiB free-space reserve. The simulation opts out only for its small temporary artifacts. Retained source files must be explicitly selected; credential-like paths and symlinks are rejected. Do not embed credentials in source files, notes or recipe output. Generic content redaction cannot reliably remove secrets from arbitrary debugging data.

## Architecture and remaining work

[Architecture](docs/architecture.md), [milestone briefs](docs/milestones.md), [M1 review](docs/m1-review.md), and [OSTree build and boot workflow](docs/build-and-boot.md) define ownership, public interfaces, acceptance commands and limitations. The existing v1 experiment envelope is preserved; its `deployment` artifact role references a versioned deployment manifest. Schemas live in `schemas/` with examples in `examples/`.

The approved OS deployment backend is **OSTree/rpm-ostree**, using a traditional signed OSTree repository published by the controller over authenticated HTTPS. Each experiment authorizes an exact commit containing matching kernel, modules, initramfs and userspace. Quirkbench owns experiment authorization, one-shot boot control and evidence; OSTree owns candidate filesystem deployment. Recovery is independent and never updated by an experiment. See [deployment architecture](docs/architecture.md) and [build and boot](docs/build-and-boot.md).

The external image uses six partitions: fixed EFI/recovery, narrowly writable GRUB state, experiments, a read-only library and independent evidence storage. A compact factory image commissions the remaining capacity on first boot, preserving interrupted work. Use a normal writer such as Etcher; Quirkbench does not provide a USB writer. Existing prototype images need rebuilding, not in-place conversion. Existing campaign records, source checkpoints and evidence remain readable.

M1 is achieved. **Historical M2 layout revision 1** passed 286 software tests and ten real UEFI VM boot trials, plus composition, incremental deployment, container recreation and complete backup/restore checks. [Qualification observations](docs/ostree-review.md) link the evidence. Passing unit tests alone does not qualify an image. QEMU tests boot infrastructure and disk/firmware sentinels, not Acer behavior. Hardware recovery, watchdogs, netconsole, kdump, Acer diagnostics, the campaign exceeding 30 hours and evidence-backed fixes have separate acceptance gates. See [qualification fixtures](acceptance/README.md).

The [OSTree architecture revision review](docs/m2-ostree-review.md) records implemented boundaries, observed qualification and remaining integration gates. New deployment submissions retain an explicit build-evidence closure containing matching symbols, configuration, modules and source archives; an OS revision alone is not a complete debugging checkpoint.

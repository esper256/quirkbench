# Quirkbench

Milestone 1 implements the durable controller/target foundation and an executable simulated lab. It includes a human monitor, real authenticated HTTPS adapter, strict contracts, fault tests and later-milestone acceptance fixtures. **It is not yet a commissioned bootable USB debugging system.** Candidate-kernel execution fails closed until the reboot/recovery adapter is implemented and qualified.

## Run the software acceptance gate

Python 3.11+ is required. Runtime uses the standard library. Install test/build dependencies into a project environment, not system Python:

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
- **Target:** durable claim request and execution journal, retained evidence outbox, validated acknowledgements, one local supervisor at a time, allowlisted bounded recipe processes and heartbeat supervision. Simulation smoke is the only bundled runnable recipe; no shell supplied by the controller executes on the target.
- **Adapters:** local and authenticated HTTPS transports, provider-neutral JSON coding-agent command adapter, fake builder, and explicit build/image/QEMU scaffolding. Target tokens are separate from AI credentials. AI credentials stay on the controller and are never configured into target images.
- **Visibility:** persistent progress reports and event cursor, independent heartbeat and advancement ages, terminal/JSON watcher, measured upload bytes and compact result context.

Use `quirkbench --help` for commands. Administration stays local: register a capability report, create/submit a campaign, resume/pause/status, put artifacts, snapshot explicitly selected source files, invoke an agent decision, resolve uncertainty with a reason, and back up/restore. `serve` requires TLS certificate/key and a protected JSON device-token file; LAN binding requires `--allow-lan`. `target` requires a pinned CA, token file and capability report. A restarted server always pauses scheduling. Read [the protocol](docs/protocol.md) before running a LAN service.

A controller command defaults to a 20 GiB free-space reserve. The simulation opts out only for its small temporary artifacts. Retained source files must be explicitly selected; credential-like paths and symlinks are rejected. Do not embed credentials in source files, notes or recipe output. Generic content redaction cannot reliably remove secrets from arbitrary debugging data.

## Architecture and remaining work

[Architecture](docs/architecture.md), [milestone briefs](docs/milestones.md), [M1 review](docs/m1-review.md), and [build/boot prototype notes](docs/build-and-boot.md) define ownership, public interfaces, acceptance commands and limitations. Six v1 schemas live in `schemas/` with examples in `examples/`.

The current image helper is a two-partition prototype for virtual fixture development. M2 must implement the agreed immutable recovery / separate GRUB state / journaled data layout, controlled expansion, actual Fedora image build and reboot fallback. No USB writer is implemented: the final image is intended for a normal writer such as Etcher. No physical USB device, installed OS, host packages, or firmware was modified during M1 implementation.

The QEMU fixture needs a built image, QEMU and OVMF inputs. `make acceptance-qemu` fails explicitly if they are missing. It records internal sentinel and firmware-variable snapshots; no VM boot has been performed here. Hardware endurance and patch fixtures are explicitly unqualified and cannot pass until populated with real evidence. Watchdogs, netconsole, kdump, Acer diagnostics, a campaign beyond 30 hours, and evidence-backed issue fixes remain future gates. See [qualification fixtures](acceptance/README.md).

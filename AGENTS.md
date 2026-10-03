# Quirkbench agent instructions

Quirkbench connects coding agents to Linux targets for reproducible experiments and
evidence-backed patches. **Controller** builds and owns investigations; **target**
boots experiments and produces evidence. Keep vendor, architecture and boot quirks
in reviewed profiles/adapters. Generic design does not imply universal tested support.
An example cannot become a default or requirement without a product reason. Classify
special cases as core invariant, supported-platform implementation, optional recipe,
investigation input, local setup or historical evidence.

## Start here

Use the [documentation index](docs/README.md) for operating guides. The root
[README](README.md) is the aspirational product manual, not available-command evidence.
The [first-usable tracker #29](https://github.com/esper256/quirkbench/issues/29)
is the task queue: select one ready, unclaimed issue, check dependencies and open PRs,
and claim a topic branch before editing. Follow [CONTRIBUTING](CONTRIBUTING.md);
read only the issue's relevant [C0–C7 contracts](docs/implementation-contracts.md)
and [C8 product interface](docs/product-interface.md). The
[roadmap](docs/product-roadmap.md) and [acceptance guide](docs/installation-to-patch.md)
define scope, not a second status ledger.

Prioritize the attended installation-to-patch journey. Reuse implemented foundations;
do not rebuild them from historical packet descriptions. Managed invocation and
unattended grants remain separate optional follow-ons. No MCP service in v1.

When authorized for consecutive work, finish a bounded PR, record focused evidence
and required review, merge only with session authorization, update its issue/tracker,
then select the next ready task from current main. Never close an unmerged software
issue or infer native acceptance from fixtures. Coordinate claims/shared files;
do not take over another active worker's task. Supporting infrastructure is not
a prerequisite unless it actually blocks the selected product work.

Pause for conflicting contracts, a new scheduler/database/service, incompatible
wire/storage semantics, weakened ownership/trust/storage/approval rules or a material
scope change. Record evidence, options and a recommended decision in the issue.
Continue independent ready work if safe; otherwise ask the owner. Missing required
review, production credentials/publication and physical execution are explicit gates.
See the [cloud worker prompt](docs/cloud-worker-prompt.md) for a reusable work loop.

## Single-user scope

One user installs a per-user instance and uses it exclusively. Multi-user/shared
installations, accounts, roles, tenants and collaboration features are out of scope.
Do not anticipate them with extra architecture or permission enforcement. Components
and concurrent workers act for the same user; process coordination and authenticated
target communication remain necessary. See the [single-user contract](docs/implementation-contracts.md#single-user-installation).

## File permissions

Apply the owner-approved [file-access policy](docs/implementation-contracts.md#file-access-and-permission-policy)
tracked in [#66](https://github.com/esper256/quirkbench/issues/66). Ordinary user data
must not be rejected merely for group/other bits or non-0600/0700 modes. Keep private
creation defaults for secrets and preserve real storage, integrity and worker
coordination requirements. Existing checks/tests are not their own justification.
The earlier blanket privacy rule is superseded; implementation review should use
this revised contract rather than ask the owner to approve the same decision again.

## Preserve the boundaries

- Keep existing wire names (`device_id`), CLI options (`--device`, network `--host`)
  and schema meanings. Incompatible changes require versioned readers/migrations.
- Reuse the controller database, attempt state machine and native systemd user-service
  ownership of rootless workers. Preserve state and unrelated uncommitted work.
- Follow the [storage policy](docs/architecture.md#storage-protection-policy).
  Recovery uses stock Fedora packages, DNF5 installroot, dracut and the existing
  GRUB/GPT assembler; its storage operations are boot-device-only. Candidate kernels
  retain internal-controller exclusions. No required custom recovery compile or
  incidental second image builder. See [recovery design](docs/recovery-base.md).
- Require authenticated setup and exact-candidate/attempt operator approval. Agent
  proposals, successful builds, pairing and watchdog availability do not grant it.
- Obtain higher-reasoning review for changes to storage, trust/binding, source/worker
  ownership, durable execution, shutdown or watchdog authorization boundaries.
- Keep readiness, source capture, worker draining, recovery arrival, evidence durability,
  safe shutdown and unattended eligibility separate. Do not infer them from one status.
- New state and build staging belong outside Git checkouts. Use configured home state
  and a manually opened `quirkbench monitor`; never launch popup viewers.
- Ad hoc kernel builds in rootless Podman use the [bounded starter](environments/README.md#observable-bounded-kernel-builds).
  Verify plain `podman stats` visibility and launcher/conmon/payload containment under
  the delegated service before a long compile. Recovery workers have their own contract.

## Validate the change

Follow the [testing policy](docs/testing-policy.md). Use `make smoke` as needed
during development and focused software regressions for changed code. `make test`
defaults to smoke; `make test TESTS=...` selects focused cases. Run `make test-full`
or dispatch the full CI matrix only at larger software integration milestones,
not after every small bugfix or change. Documentation changes need link/consistency
checks and `git diff --check`. Image,
QEMU, composition/transfer/backup qualification and endurance gates require an
explicit final major-version release request; never bypass `RELEASE_QUALIFICATION=1`.
Requested image production, scientific experiments and attended commissioning are
product operations, not authorization for the release suite.

Do not repeatedly poll, sleep through long jobs or delegate agents to watch tests.
Record durable commands, identities, logs and results; continue independent work or
return with an honest pending status. Reuse matching evidence, rerun only checks
invalidated by changes, and keep changed artifacts unqualified until checked.

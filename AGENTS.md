# Quirkbench agent instructions

Quirkbench connects coding agents to Linux targets for reproducible experiments and
evidence-backed patches. **Controller** builds and owns investigations; **target**
boots experiments and produces evidence. Keep vendor, architecture and boot quirks
in reviewed profiles/adapters. Generic design does not imply universal tested support.
An example cannot become a default or requirement without a product reason. Classify
special cases as core invariant, supported-platform implementation, optional recipe,
investigation input, local setup or historical evidence.

## Start here

Use the [documentation index](docs/README.md) to find the relevant guide. The root
[README](README.md) is the aspirational product manual, not available-command evidence.
Follow the [roadmap](docs/product-roadmap.md) and [implementation checklist](docs/installation-to-patch.md).
Select one bounded task from the [handoff](docs/implementation-handoff.md) and read
only its relevant [C0–C7 contracts](docs/implementation-contracts.md) and
[C8 product interface](docs/product-interface.md). Fresh setup and authenticated
pairing lead to attended external-agent use; managed invocation and unattended
grants are separate optional capabilities. Do not treat proposed commands or tests
as implemented behavior. No MCP service in v1.

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

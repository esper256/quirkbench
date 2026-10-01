# Upload retention and background jobs handoff — 2026-09-30

Implementation packets: upload retention, background kernel builds, then
composition with controller-owned signing/publication. Hardware-specific kernels
have a [separate proposed design](targeted-experiment-kernels.md).
No target wire names or upload declarations changed. Migrations 14–15 add upload
ownership/housekeeping and job capture/output/service records to the existing DB.

## Upload retention

Upload ownership is committed before partial files. Pending/resumable uploads and
completed uploads awaiting acknowledgement retain their bytes and owning attempt;
acknowledged uploads can expire with that attempt. Failed/explicitly abandoned
uploads use `failed_staging_days`. Shared evidence remains reachable through its
existing references. Retirement commits before unlink, so interrupted deletion
repeats safely. Historical attempt and acknowledged evidence records remain.

Existing activity records recover legacy ownership and exact acknowledgements.
Valid unidentified declarations protect their expected CAS digest. Missing/corrupt
metadata defers CAS deletion conservatively. Maintenance lists unidentified IDs;
`maintenance abandon-upload ID` refuses an active/unresolved owner, and refuses
unidentified abandonment while any physical attempt is unresolved.

Terminal uploads, attempt completion and recovery return increment a durable
housekeeping request. The current service owner handles it after target replies,
while no physical attempt or heavy worker is active. This works without a recovery
image coordinator. There is no timer, scheduled task or new service.

## Background kernel-build handoff

`build MANIFEST` defaults to immediate accepted operation JSON. `--request-id`
provides exact replay; changed inputs conflict. `--wait` reads status and prints
the original output mapping on success. Interrupting a waiter leaves execution
owned by the service. `operation resume ID --request-id REQUEST` records an
explicit durable request; the owner reconciles the old whole service and claims
a new generation. Queued or running work interrupted by owner restart needs an
explicit resume. Campaign pause prevents the next stage.

The input worker copies declared source/lock/config files and a credential-filtered
target sysroot, verifies existing hashes, and stages a retained input manifest.
The controller adopts those inputs before dispatching compilation. Build handlers
are fixed; operation arguments cannot choose worker commands. The single heavy
worker uses the bounded delegated Podman adapter with four CPU/8 GiB ceilings,
reduced for controller capacity. Recovery retains its separate four-GiB contract.
Pinned images must already be locally installed (`--pull=never`).

Workers have captured inputs/private outputs and read-only verified cache hints.
Writable Kbuild work and cache proposals stay private. The owner approves reusable
entries after shutdown; unavailable cache space skips optional publication.
Existing count/grace settings retain required artifacts and diagnostics. Failed
stages and obsolete resumed generations receive diagnostic grace; interrupted work
remains protected until reconciled/resumed/abandoned. Proven stopped input stages
can be cleaned again from the existing journal after interruption.

## Composition handoff

Composition captures input artifacts/evidence/replacements and calls the existing
FedoraComposer in unsigned private staging mode. Signing secrets and shared
repositories are absent from the worker mounts. The controller stops the entire
unit, checks complete input/runtime identities, rejects repository symlinks and
special nodes, runs OSTree closure validation and checks composed kernel,
configuration, initramfs, modules and candidate runtime against retained inputs.

Signing and shared-repository publication happen only in that owner. The signed
revision is pinned outside the short SQLite writer transaction; successful result,
evidence/deployment references and retention ownership commit with the exact
current epoch/generation/stop proof. Failed signing/publication cannot report
success. A lost DB commit can leave an extra conservative OSTree pin for later
housekeeping. Post-commit cleanup failure records deferred cleanup and preserves
the committed outcome.

## Validation and reviews

Commands used: changed-file Python `ast.parse` and import/parser checks;
`git diff --check`; `bash -n` on the added bounded helper; local Markdown link
checks; and `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python
/tmp/quirkbench-packet-fixture.py` (ephemeral fixture, no repository test additions).
The fixture completed in 0.067 seconds (final rerun 0.075 seconds). It used temporary SQLite/CAS and fake
workers, with the checkout guard overridden only for temporary fixtures because
this sandbox exposes `/tmp/.git`; no product state was created there.

Fixtures covered resumable uploads, expired acknowledged metadata, shared CAS
bytes, unidentified/corrupt legacy declarations, exact lost-ack recovery,
repeatable cleanup, request replay/conflict, captured-input mutation, stale
publication, explicit resume/fresh generation, campaign pause, composition failure
before publication, deadline expiry during adoption, owner loss, interrupted waiters and post-commit cleanup errors.
A separate in-memory guard fixture rejected a custom signing-home name and
sysroot overlap in both directions without copying files.
The earlier upload-only fixture took 0.043 seconds. The migration/parser fixture
also completed essentially immediately. No test infrastructure was added.

Required higher-reasoning read-only reviews addressed upload deletion/legacy
attribution, owner/deadline fencing, cache mount isolation, full immutable input
binding, repository containment, controller signing and short publication
transactions. Both final review dispositions report no remaining concrete blockers. Follow-up
review also confirmed restart handling and journal-driven stage retirement.
The final privacy review required deriving publication identity and private-input
exclusions from one validated configuration snapshot. Admission now uses that
snapshot, and input capture rejects configured signing/control paths before copying.

Unchecked: live controller-unit installation, real Podman descendant containment,
actual kernel compilation, dependency downloads, OSTree composition/signing,
image production, target boot, physical storage preservation, reset coverage and
release qualification. Fake services/static inspection establish software
boundaries, not those product/release results. Service/trust installation and a
locally pinned builder image are explicit prerequisites before real jobs can run.

Read-only `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python -m quirkbench setup-check`
reported background readiness false in this sandbox: its service manager and
builder tools are not visible and its selected DB is unavailable. No service or
trust installation was attempted. Native host readiness must be checked after
manual provisioning; this implementation does not claim jobs were run here.

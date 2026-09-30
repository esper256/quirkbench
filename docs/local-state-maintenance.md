# Home state and intentional reset — 2026-09-30

The user selected a complete wipe, superseding the salvage/relocation proposal below.
No local image, key, database, pinned input, kernel output or qualification evidence
will be copied into the new state. Historical documents retain their original
results and identities, but their local artifacts are unavailable after the reset.
Replacement input acquisition, image production, trust provisioning and real boot
checks are separately requested product operations. This work claims no qualification.

Persistent state uses `$XDG_STATE_HOME/quirkbench`, defaulting to
`~/.local/state/quirkbench`; user configuration retains its canonical selection.
No command defaults to checkout-local `.quirkbench`. New state/build staging in a
Git checkout is rejected. The ignore rule remains only a historical safeguard.
Run `quirkbench setup-state`, then manually open `quirkbench monitor` in an existing
terminal. No automatic Konsole windows or watching agents remain. See
[monitoring](monitoring.md) and [development builds](../environments/README.md#observable-bounded-kernel-builds).

Retention uses configurable counts, without an overall disk quota. Defaults keep the
last **five completed physical attempts globally**, two recovery releases, five
completed outputs per build/composition/development category, two input generations
and two qualification runs. Active attempts and unfinished jobs retain their inputs;
uncertain/interrupted/resumable work and explicit pins remain protected. Completed
physical attempts are eligible only after recorded recovery return. Simulated work
does not displace the retained physical history. Library maintenance keeps the latest
completed library per target plus the configured input history; ongoing maintenance
stays protected.

Old payload references are retired durably before deletion. Shared CAS objects and
OSTree revisions survive while any retained investigation, release, input or pin
needs them. Historical database rows remain, but expired payloads are unavailable.
Successful disposable staging is removed after verified publication and shutdown
proof; failed disposable stages remain seven days. Resumable work needs reconciliation
or explicit abandonment. Unreferenced CAS orphans receive a seven-day grace period.
Optional reusable caches alone have a 50 GiB logical-byte limit, including pending
entries; locked/resumable work is protected and can temporarily exceed it. Cache
publication is skipped when safe eviction cannot make room. No total-size guarantee
is made: counts, pins and protected live work determine required storage.

All values are in the selected state's private `settings.json` and can be changed
through `settings set`. Housekeeping runs with mutating commands and the existing
owner's startup/completion. Read-only commands and the monitor never clean up.
There is no cron job, timer or separate housekeeping service. Idle operators can
inspect a dry run and explicitly prune:

```sh
./environments/quirkbench settings show
./environments/quirkbench settings set completed_attempts 5
./environments/quirkbench maintenance status
./environments/quirkbench maintenance pin OWNER --note 'Keep this investigation'
./environments/quirkbench maintenance unpin OWNER
./environments/quirkbench maintenance abandon OWNER
./environments/quirkbench maintenance prune --dry-run
./environments/quirkbench maintenance prune
```

`maintenance status` reports owner identities, pins, settings and recent retirements.
Pin before retirement; pinning cannot restore deleted bytes. Abandonment excludes
publishers, requires recorded shutdown proof and begins the failed-stage grace.
Ad hoc systemd development runs instead use the existing `retain-run --abandon`
interface. Neither command authorizes a physical attempt.

| Setting | Default | Purpose |
| --- | ---: | --- |
| `completed_attempts` | 5 | Completed physical attempts, globally |
| `recovery_releases` | 2 | Verified recovery publications and managed exports |
| `completed_builds` | 5 | Completed build, composition and development outputs; checkpoints and nonphysical attempts |
| `input_generations` | 2 | Retained raw-input and recipe history separately; completed libraries per target |
| `qualification_runs` | 2 | Explicit qualification output workspaces |
| `failed_staging_days` | 7 | Stopped, failed disposable staging and diagnostics |
| `orphan_days` | 7 | Unreferenced CAS grace; retired payloads are reclaimed immediately |
| `cache_gib` | 50 | Optional reusable cache logical-byte limit |

Recovery releases and orphan grace require at least one; other values permit zero.
Pins and live dependencies override the counts. Inputs shared by several generations
are stored once. Successful signature/download staging transfers its references to
the durable lock instead of consuming extra history slots. Pin reusable builder
archives or other inputs before preparing more generations than the configured
raw-input history; an admitted investigation/image retains its complete closure.
Native OSTree reference deletion and `ostree prune --refs-only`
reclaim unreachable objects under the existing repository locks; unrelated refs
are preserved. Repositories must live under selected-state `repositories/`; missing
tooling or unresolved work defers native pruning visibly. No global Podman pruning
or pruning of other applications' storage occurs.

The former multigigabyte directories are covered by their producer rather than by
blind deletion based on a directory name:

| Former area | New lifecycle |
| --- | --- |
| `inputs` | Recorded acquisition generations; verified RPMs/locks in CAS, duplicate downloads removed after import, completed generations counted |
| `images` / `deliveries` | Fenced image worker stages removed after publication/stop proof; two retained releases and managed exports |
| `ostree` | Managed repositories; retained deployment/attempt/checkpoint roots, native unreachable-object pruning |
| `m2` / `v1-layout` / `validation` | No automatic historical fixture trees; new explicit qualifications use counted managed workspaces |
| Development kernel work | Fresh recorded workspaces, verified outputs in CAS, disposable successful work removed; bounded optional incremental cache |

Reset completed on 2026-09-30. The exact canonical repository `.quirkbench` tree
was permanently removed, with no archive or trash copy. The final inspection of
12 legacy controller databases found no active/uncertain attempts or live owned
operations. All 18 remaining legacy Quirkbench user units had no live payload;
they were stopped and their failed status cleared. No Quirkbench units remained.
The deletion guard rejected symlink roots, nested mounts and open legacy paths.
Container-owned files were removed through native rootless Podman's user namespace:
`distrobox-host-exec podman unshare /usr/bin/python3 environments/discard-legacy-state.py --discard`.
It exited successfully. No global Podman pruning was performed.

`./environments/quirkbench setup-state` initialized fresh private mode-0700 state at
`/home/eric/.local/state/quirkbench` and selected it in
`/home/eric/.config/quirkbench/controller.json`. No old inputs, images, credentials
or execution records were imported. Service management remains pending; this reset
does not establish background-service readiness. A manual `monitor --once` snapshot
reported no recorded work. The filesystem reported about 603 GiB available after
deletion; allocated directory totals are not a physical-space savings measurement.

Higher-reasoning reviews covered state, cleanup and worker-reporting boundaries;
their identified issues were corrected before deletion. Validation was limited to
changed-file syntax, local document links, `git diff --check`, tiny read-only snapshots
and direct code review. The initial home snapshot was sandbox-blocked by SQLite
sidecar access; it succeeded with normal home access. Interactive curses behavior,
new worker runs and interruption/retention behavior have not been exercised.
No pytest, builds, flashing, QEMU/hardware campaigns or release qualification ran.

## Retention implementation handoff — 2026-09-30

Count-based retirement, pins, native OSTree collection and managed staging extend
the initial reset policy above. They reuse the controller database/CAS and existing
execution owner. Shared command publication locks permit concurrent uploads and
heartbeats; exclusive cleanup/abandonment prevents claim-to-launch races. Retirement
is committed before filesystem deletion and retries retain current live references.
Higher-reasoning boundary review covered these controls.

Validation remains deliberately small: changed-source syntax/import/parser inspection,
local document links, `git diff --check`, and isolated metadata/CAS checks that
each completed in under 0.1 seconds. They checked dry-run behavior, expired payload reclamation,
shared-object survival, missing-stop protection and pins. No tests were added. Real
worker interruption, native OSTree reclamation, curses interaction, package acquisition
and image generation remain unexercised here. No new qualification is claimed.

Fresh-state setup completed with controller migration 13 and private settings at
`/home/eric/.local/state/quirkbench`. `settings show` reported the defaults above;
an idle `maintenance prune --dry-run` found no removals, retirements or blockers.
`monitor --once` reported no work and approximately 603 GiB free. The repository
`.quirkbench` remains absent. User-service setup still reports pending, and deleted
signing/trust material must be explicitly reprovisioned for replacement delivery.
These observations establish local setup, not a produced or qualified image.

## Superseded read-only audit

Everything below records the earlier inventory and proposed salvage policy. It is
historical context, not current cleanup instructions.

### Earlier local-state cleanup and relocation

Read-only inventory dated 2026-09-30. No files were deleted or relocated.

## Location

Use the existing home-state convention: `$XDG_STATE_HOME/quirkbench`, or
`~/.local/state/quirkbench` when unset. `quirkbench setup-state` already supports
this location and writes the selection under `~/.config/quirkbench/controller.json`.
However, unconfigured `discover_state_root()` currently falls back to the working
directory's `.quirkbench`. Setup refuses to choose implicitly when legacy local
state exists. Existing selections cannot be silently switched by repeating setup.

The current repository `.quirkbench` is a mixed development workspace containing
multiple controller databases, fixtures, historical product runs, inputs, signing
keys and deliveries. It is not a single active controller database. Future product
commands should use configured home state; new ad hoc product runs must also select
an explicit location outside the repository. Reproducible intermediate work should
have a documented pruning policy and remain distinguishable from retained inputs.

Do not simply move the workspace yet. Historical launchers, handoffs and database
worker records contain absolute repository paths. Preserve immutable records with
their original paths and record a relocation map; create fresh launchers for future
runs. Do not mass-rewrite signed/CAS records or treat a symlink as a complete migration:
worker and state validators deliberately require canonical directories.

## Measured workspace

GNU `du -h` measured allocated file blocks:

| Area | Allocated blocks |
| --- | ---: |
| Entire `.quirkbench` | about 292 GiB |
| `inputs` | about 56 GiB |
| Stock image controller, including worker stages | about 56 GiB |
| Historical OSTree area | about 114 GiB |
| Historical v1-layout area | about 46 GiB |
| Deliveries | about 13 GiB |

The filesystem is Btrfs. Reflink/shared extents and compression mean these figures
are not promised disk-space savings. Directory totals also account for hardlinks
according to the traversal, so individually measured totals are not all additive.

## Retain

- Final signed `deliveries/stock-recovery-first-investigation-20260930` bundle,
  including checksum statement/signature, manifest, release candidate, public key
  and handoff. Its byte identity must remain unchanged.
- Private signing keys and explicitly provisioned trust/credentials.
- Exact retained Fedora RPMs, public package-signing keys, SRPM/source inputs,
  pinned OCI archives, locks, recipes and runtime manifests. Deduplicate only after
  proving hashes and retaining every referenced object; directory age is insufficient.
- Controller databases, their referenced CAS objects, attributable evidence,
  run commands, identities, status records, logs and historical qualification results.
- Completed custom recovery kernel as a historical artifact. It is no longer a
  recovery-image prerequisite, but its outputs must survive cleanup.

The custom run `inputs/fedora44-kernel-build-20260929/artifacts` is **empty**.
These four files in `kernel-obj` match `kernel-record.json` and must be retained
before pruning that tree:

| Recorded output | Actual location | Approximate size |
| --- | --- | ---: |
| kernel | `kernel-obj/arch/x86/boot/bzImage` | 18 MiB |
| vmlinux | `kernel-obj/vmlinux` | 491 MiB |
| system_map | `kernel-obj/System.map` | 12 MiB |
| module_symvers | `kernel-obj/Module.symvers` | 2 MiB |

Also retain the final configuration, installed matching module tree, source/input
provenance and build/audit logs. Preserve or archive a matching prepared source tree
before removing redundant unpacked copies. No new qualification is implied.

## Cleanup candidates and prerequisites

| Candidate | Measured size | Required preservation/check |
| --- | ---: | --- |
| Custom kernel `kernel-obj` intermediates | 28 GiB | First retain the verified outputs/configuration above; deliberately relinquish incremental build reuse. |
| Custom run `rejected-modules-vmd` | 7.6 GiB | Keep rejection diagnostics, configuration identities and final accepted module tree. |
| The two source-preparation `rpm-topdir/BUILD` trees | about 1.8 GiB each | Keep exact SRPMs/patches/specs, source identities and one matching prepared source/archive. |
| Old rootfs/runtime diagnostic sysroots | about 629 MiB each | Preserve logs, lock/runtime manifests and failure/completion records outside the disposable trees. |
| Stock image controller `workers/*` bulky outputs | about 43 GiB total | Preserve each stage's diagnostics and nested tool logs; confirm public outputs are retained in CAS/deliveries. Do not edit claims or delete CAS by filename. |
| `deliveries/.stock-recovery-inventory-20260930.pending` | about 4.1 GiB | Failed export; published verified bundles and CAS copies already exist. Confirm no process uses it. |

Do not delete entire `inputs` or historical qualification directories. Their names
combine disposable material and irreplaceable records. Broader m2/OSTree/v1-layout
fixture cleanup needs a separate disposition preserving the existing release evidence.

## Observed execution state and maintenance order

The stock image database has six failed operations, three successful operations
and one old queued operation. It has no running operation or retained worker-unit
claim. Two Konsole viewer services remain running; they are readers, not builds.
Close/stop those viewers before relocating their log paths. Recheck execution state
immediately before cleanup; this inventory is not permanent authorization evidence.

1. Retain and hash-verify the custom kernel artifact set and matching modules/source.
2. Record a deletion manifest of exact disposable subtrees and retained diagnostics.
3. Prune only those subtrees; leave databases, CAS references, keys and signed bundles intact.
4. Reconcile the stale queued operation explicitly if that controller will be reused.
5. Move retained state with services stopped, preserving permissions and identities;
   record old/new roots and verify delivered checksums after the move.
6. Configure the intended active controller root explicitly. Preserve old run records
   as historical, and use new canonical home paths in future launchers.
7. Address the unconfigured working-directory fallback and development run-location
   guidance separately, with focused compatibility tests and the required durable
   boundary review. No build, QEMU or release campaign is needed for maintenance.

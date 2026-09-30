# Local state cleanup and relocation

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

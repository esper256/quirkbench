# Controller backup, paused restore and coverage

Back up into a new directory outside Git checkouts. Existing positional commands
remain supported. The output/input aliases add guidance to the existing synchronous
backup and restore commands; they do not start another service or background job.

```sh
quirkbench admin backup --output /backup/quirkbench-cut
quirkbench admin restore --input /backup/quirkbench-cut --output /backup/restored-controller
```

`backup DESTINATION` and `restore BACKUP` preserve their answer formats. New CLI
backups using `--output` also include `coverage.v1.json`; the Python `Controller.backup(destination)`
API keeps its legacy format unless `coverage=True` is requested. A backup's success
marker is `manifest.json`, published only after SQLite, referenced CAS, native
OSTree export and requested coverage checks succeed. An interrupted `.pending-*`
directory without that marker is incomplete and cannot be restored.

## Capture editable sources first

Stop every writer before explicitly handing off a registered source workspace:

```sh
quirkbench investigation source show NAME
quirkbench investigation source capture NAME --workspace WORKSPACE --quiesced --request-id CAPTURE_ID
quirkbench admin operation show OPERATION_ID
quirkbench admin backup --output /backup/quirkbench-new-cut
```

Use the returned operation ID. Capture runs through the existing configured
controller service; a paused investigation requires explicit resume before its next
stage. Acceptance is not completion. Wait for `SUCCEEDED` and stopped worker
ownership before expecting current-source coverage. No command here grants attempt
approval or launches an experiment. Backup itself neither takes over a writer nor
snapshots a changing worktree. It reports editing, missing, preparing, failed or
interrupted source coverage as incomplete. Keep source writers quiesced throughout
the backup if you need the captured tree to describe current edits.

The SQLite copy identifies the cut. Its exact database and manifest hashes bind the
coverage report; facts come from that copy and copied immutable artifacts, not later
live queries. The shared publication barrier excludes retention while allowing
target heartbeats and evidence publication. Later publications belong to a later
cut, so the report cannot promise it contains every concurrent producer's output.

The report lists source scopes, actual Git base OIDs, capture identities, checkpoints,
active operations, retained public artifact/deployment counts and pending upload
metadata. Source archives preserve approved unfinished bytes and deletions. A prior
stopped capture remains historical evidence after a writer is released, but does
not cover current edits. `captured_dirty_state=modified` proves changes since a
retained preparation; otherwise dirty state is `unknown`. Equality with the
preparation does not prove a clean Git base. Current dirty state is unknown whenever
current-source coverage is incomplete. The report does not inspect a live tree.

Private identity files, operator settings/repository locations, editable workspaces
and Git metadata, private worker staging and partial upload bytes are omitted.
Back these up separately through an operator-controlled protected process. Known
controller pending returns/uploads are counts at the cut; target-only evidence
backlog remains **unknown**, even when the last report says recovery or simulation.
Controller completeness is not whole-session completeness or a fully resumable
session promise. Public patch/report exports are not backups.

## Restore and reconcile explicitly

Restore verifies the existing database/CAS/OSTree closure and, when present, the
coverage companion against its cut before native restoration. Legacy backups without
a companion retain existing validation and report coverage as unknown. All supported
backup versions reject unmanifested SQLite journals before opening the stopped cut;
unchecked WAL bytes cannot enter restored state. A present
invalid companion is an error, not a reason to fall back to legacy unknown coverage.

The output is an offline restored artifact; it does not change the one selected
controller, start another owner or initialize the current controller. Keep the
current owner stopped before explicitly selecting restored state in the ordinary
local configuration. Preserve the original state.

The restored controller stays paused; running operations become interrupted and
unresolved attempts retain uncertainty. Restore private identity/configuration
separately, reconcile outstanding execution, recovery returns and unacknowledged
evidence, and restore editable Git separately before explicitly resuming. Missing
private deliverables remain unavailable; restore does not silently regenerate them.
Immutable source evidence remains available, but old inode-scoped workspace rows
do not grant ownership of a newly restored directory. There is no automatic writer
rebinding in this command. The restored companion describes the **historical backup
cut**, not the modified paused database or current restored readiness.

## Storage and cleanup guidance

```sh
quirkbench admin storage show --json --limit 20
quirkbench admin storage show --json --after OWNER --limit 20
quirkbench admin storage show
quirkbench admin storage pin OWNER --note 'Keep required evidence'
quirkbench admin storage prune --dry-run
```

`storage` is a bounded read-only query: it lists retained owners, pin presence,
retention settings and counts of unresolved attempts/owned workers. It does no
initialization, migrations, startup recovery, locking or cleanup. It does not read
pin notes. Eligibility stays unchecked/null until the existing maintenance service
evaluates it; the dry run may defer while work is unresolved. Counts are not a total
disk quota. [Existing retention](local-state-maintenance.md) protects active,
required, unresolved and pinned objects. Pin before retirement; a pin cannot
recover already deleted bytes. Only an explicit maintenance action deletes content.

Software fixtures exercise these rules with tiny injected repository adapters.
They do not establish native composition/backup qualification or hardware readiness.

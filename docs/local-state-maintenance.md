# Controller state and retention

Persistent state uses `$XDG_STATE_HOME/quirkbench`, defaulting to
`~/.local/state/quirkbench`; user configuration retains its canonical selection.
No command defaults to checkout-local `.quirkbench`. New state/build staging in a
Git checkout is rejected. Run `quirkbench setup`, then manually open `quirkbench monitor` in an existing
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
quirkbench admin settings show
quirkbench admin settings set completed_attempts 5
quirkbench admin storage show
quirkbench admin storage pin OWNER --note 'Keep this investigation'
quirkbench admin storage unpin OWNER
quirkbench admin storage abandon OWNER
quirkbench admin storage prune --dry-run
quirkbench admin storage prune
```

`maintenance status` reports owner identities, pins, settings and recent retirements.
`storage --json --after OWNER --limit 20` gives a bounded read-only summary without
pin notes or cleanup. Eligibility remains unknown until an explicit maintenance
dry run evaluates it. See [backup coverage and paused restore](backup-and-restore.md).
Pin before retirement; pinning cannot restore deleted bytes. Abandonment excludes
publishers, requires recorded shutdown proof and begins the failed-stage grace.
Ad hoc foreground development runs instead use the existing `retain-run --abandon`
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

## Upload retention

Upload ownership is committed before partial files. Pending/resumable uploads and
completed uploads awaiting acknowledgment protect their bytes and owning attempt.
Acknowledged uploads can expire with the attempt; shared evidence remains reachable
through its other references. Missing or corrupt ownership metadata defers deletion.
`maintenance abandon-upload ID` refuses active or unresolved owners; unidentified
uploads cannot be abandoned while any physical attempt is unresolved.

Terminal uploads, attempt completion and confirmed recovery return request idle-owner
housekeeping. The current service handles it after target replies, while no physical
attempt or heavy worker is active. Retirement commits before unlink so interrupted
cleanup can retry. There is no additional timer, service or monitor-owned cleanup.

An unsuccessful **unused** setup can be restarted with the guarded
[`admin controller reset`](controller-installation.md#start-over-after-unsuccessful-setup)
command. It archives only known database/configuration/setup files and preserves
images, packages, keys and logs. Reset archives protect their inventoried CAS objects
from pruning; they have no automatic expiry. Reset is not an alternative to backup
or target/worker reconciliation for a controller that has already been used.

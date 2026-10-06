# Restore conventional Unix configuration and filesystem behavior

Status: implemented in [PR #144](https://github.com/esper256/quirkbench/pull/144), merged at `febba250`. This records the owner-approved correction to configuration, identity and ownership. Affected development formats were replaced directly; existing files are preserved and incompatible state requires fresh initialization. See the [controller installation guide](controller-installation.md) for current operation. The filename is retained for easy reference.

## 1. Direction: remove unnecessary machinery

Quirkbench should act like a single-user Unix application: configuration contains the user's choices; the process discovers its installation and data roots when it starts; files within those roots follow the application's layout; ordinary pathname resolution accepts symlinks; and the operating system provides permissions and process locks. The database records domain objects and work, not a parallel description of where every file lives.

A renamed home, `/home` versus `/var/home`, a Distrobox mount namespace, or moving a stopped data directory are examples that expose the design flaw. They illustrate the design requirement, not a mandate for a new relocation test suite. Do not solve this with a pathname translation framework, resource registry, relocation daemon, adoption wizard or exceptions for specific directory prefixes.

Owner-approved principles:

- **Derive a location whenever it can be derived.** Do not store a redundant relative path instead of a redundant absolute path. A workspace ID already determines its managed directory; a digest already locates an artifact.
- **Store genuinely necessary user-selected locations once**, in ordinary local configuration at the owning subsystem. Configuration is allowed to change. Do not copy it into every setup journal, database row and capability fingerprint.
- **Separate content and authority from location.** Content digests, keys, experiment IDs and operation generations are meaningful identities. Parent directories, usernames and historical inode values are not portable identities.
- **Validate for a concrete operation and threat.** Ordinary reading does not need the same safeguards as recursive deletion, source capture or USB writing. Do not make arbitrary canonical-spelling requirements a universal prerequisite.
- **Preserve the necessary invariants, not every historical implementation.** Exact experiment inputs, authenticated target communication, stopped-worker reconciliation and scoped destructive actions stay. Duplicated path records and redundant fences need an actual justification to stay.

There are no production users to migrate. Replace affected formats directly: no schema migrations, backwards-compatibility readers, conversion tools or legacy-path translation. No new scheduler, database, service, virtual filesystem or all-purpose reference type. Prefer deleting persisted fields and checks over building infrastructure to reconcile them. No live installation changes or application implementation are authorized by writing this plan.

## 2. Grounding and design audit

This plan incorporates the related analysis in the **Implementation** chat and [issue #143](https://github.com/esper256/quirkbench/issues/143). That thread observed a real startup failure: setup inside Distrobox recorded `/home/...`, while the host resolved the same installed files through `/var/home/...`. The first attempted narrow alias fix was set aside in favor of a coordinated refactor. Do not ship or overwrite another worker's partial patch without reconciling its status.

Pre-refactor findings, recorded before PR #144:

| Area | Existing coupling | Correction |
| --- | --- | --- |
| State selection | `state_config.py` writes an expanded absolute root and rejects configured symlinks. | Default discovery at startup; one optional explicit selection. |
| Setup | `controller_setup.py` and setup contracts hash absolute state/runtime/config/bin paths into retained requests. | Idempotent setup of current configuration, with historical request attribution separate from current lookup. |
| Runtime installation | `controller_install.py` treats the receipt's recorded runtime root as installation identity. | Verify installed bytes against archive/manifest identities at the installation currently selected. |
| Runtime publication | `job_operations.py` stores a runtime pathname in the live-owner table; readiness compares configuration paths. | Logical software identity plus transient process ownership observations. |
| Enrollment | `enrollment_runtime.py` hashes the whole service configuration, including local paths, into its capability identity. | Semantic capability/trust identity distinct from local placement and running configuration generation. |
| Workspace | `source_workspace.py` already derives paths from workspace IDs, but permanently checks CAS-recorded device/inode values. | Keep derived lookup; use filesystem observations to fence a current writer/capture session. |
| Filesystem access | `_managed_path` and numerous consumers reject linked ancestors or changed canonical spelling. | Accept ordinary selected path aliases; protect operations against actual escape, substitution and unauthorized deletion. |
| Worker and storage outputs | Worker records retain absolute output/log paths; cleanup/reset/rollback compare saved roots. | Derive managed locations from their owning operation/layout; retain genuine process and deletion-scope checks. |

These are concrete findings, not a claim that every subsystem has already been audited. During implementation, identify affected producers and consumers together and record only findings needed to explain the changes. Do not build an exhaustive inventory ledger or require a new test for each field. Read joined code paths rather than merely searching for `resolve()`.

Cover config and setup, SQLite columns, JSON/CAS records, hashing, runtime receipts, signing/publication, generated launchers, source/worktree handling, worker/container mounts, staging, caches, logs, backup/restore and cleanup. Also review setup prerequisites and maintenance flows that exist chiefly to keep redundant records synchronized. The audit must ask what can be removed, not only what path representation can replace it.

Classify each retained field as one of:

- A domain/content/trust identity.
- A necessary external user choice.
- A live execution or ownership observation.
- Historical evidence/provenance.
- A semantically meaningful path, such as a source-tree filename or container/target destination.

“Existing tests require it” is not a justification. Source filenames, target boot paths and addresses are not all interchangeable with managed host paths; do not blindly strip them from hashes.

## 3. Desired application structure

### Discovery and local configuration

Keep existing XDG defaults: `$XDG_STATE_HOME/quirkbench` or `~/.local/state/quirkbench`, and configuration under `$XDG_CONFIG_HOME/quirkbench` or `~/.config/quirkbench`. The user's `.quirkbench/` example is not a request to relocate the product. Do not require a disk-layout overhaul.

Do not persist expanded defaults. Derive them from the current environment. For necessary custom locations, keep one setting; accept documented home-relative values and absolute paths. Home-relative settings follow the current home. An explicitly supplied absolute external location may need editing when that resource moves; the application cannot guess where arbitrary user files went.

Explicit `--state` must select existing state before consulting a stale configured selection. Missing configured state gives an actionable error; never silently initialize a replacement. Help/status must not mutate or repair state. A settings edit and restart is sufficient for ordinary configuration changes; initialize fresh development state when the revised formats require it; do not implement upgrades from the superseded formats.

Resolve a user-selected alias at entry and open the real directory. Internal helpers may operate on that resolved root. Do not persist its canonical spelling as an identity or require the old alias to remain present. Two aliases to one directory must use the same filesystem locks. There is no special `/home` or Distrobox compatibility mode.

### Derived application paths

Use small layout functions over the current root and existing IDs: artifact digest → CAS object, workspace ID → workspace directory, operation/generation → owned stage, fixed executable role → selected installation executable. Do not add a registry row or serialized path for these.

Runtime installation identity comes from software bytes and the verified manifest, not a stored installation directory. Discover the running/selected installation and derive its packaged resources. Preserve explicit version selection, archive verification and rollback; do not silently select another installed version because its path exists.

Generated commands use current roots. Host mount sources are resolved at launch, while meaningful container destinations stay stable. Prefer relative managed symlinks and relocatable launchers where possible. An external OS launcher with an absolute target may require regeneration through existing setup/install tooling; that is one local integration concern, not a reason to bake its path into the database and evidence.

A mounted resource must be accessible in the execution environment. This refactor does not make arbitrary host paths visible in containers. Report missing mounts/tools honestly and materialize mount sources in the namespace where the relevant engine executes. Do not infer equivalence across namespaces from matching strings.

For external signing keys, repositories and workspaces, retain only the necessary owning configuration selection. Derive descendants; verify expected key/content/source identities when used. A missing signing home blocks signing, not unrelated inspection. Do not introduce a generic external-resource binding service.

### Mutable configuration versus durable truth

Separate three things currently conflated:

1. User preferences and local locations, read from current configuration.
2. Semantic inputs and authorizations that must remain exact for a particular operation.
3. Observations of the currently running controller and workers.

A moved file with identical verified bytes does not change software or trust identity. A different certificate, repository endpoint, recipe or source content may change semantics and still needs the existing validation/approval. Hash the explicit semantic fields for that purpose, not every configuration field by default.

Configuration generation can fence an active process against changes while it runs. Restarting after a location change publishes fresh runtime observations; it must not mint new content identities or invalidate completed results. Distinguish configuration-generation checks from the durable trust capability used by enrollment. The first implementation slice replaces the affected definitions, producers and consumers together, using fresh-state initialization rather than schema migrations.

Request idempotency must survive equivalent path spellings and ordinary managed-root movement. A different external source or export destination must not become an identical request merely because path fields were deleted. Define semantic inputs for each affected operation. Keep exact-device/filesystem confirmation for destructive actions; those plans must be re-created after their physical target changes.

### Filesystem safety without permanent location identity

Use existing OS locks, process start identity, boot IDs, epochs and worker generations. Retain file descriptors and device/inode checks for in-progress traversal/capture and substitution detection. Do not treat an inode captured years ago as the permanent identity of a workspace.

Reject traversal, untrusted links inside managed staging, archive escapes, unexpected device nodes and unsafe recursive cleanup. Accept user-selected symlink roots without disabling those protections. Review `_managed_path` callers by purpose instead of globally deleting `O_NOFOLLOW` or blanket-changing every validator.

A stopped rename works normally. A stopped copy/restore may change inode/device values; fresh session checks and the existing writer-handoff/reconciliation path establish current ownership before mutation. Do not invent a mandatory relocation workflow for every copy. Unknown or still-running prior workers must be reconciled, not presumed gone because their paths vanished. Never auto-resume paused work or approve a target run on startup.

A copied controller is a replacement, not an independently runnable clone with duplicated target credentials. Concurrent copies and live cross-host migration remain unsupported; do not add a distributed coordination system. Different Unix UIDs or denied filesystem access remain ordinary permission issues, not conditions for automatic `chown` or blanket privacy enforcement.

## 4. Clean replacement, not compatibility work

The owner explicitly requires **no schema migrations and no backwards-compatibility complexity** for this refactor. Change affected current schema definitions, configuration formats, receipts and contracts directly; update their writers, readers, examples and existing fixtures together. Do not append upgrade steps, preserve dual-format readers, translate old path records, build import/adoption tools or test old-version compatibility for these changes. Remove superseded compatibility code in the affected paths where it exists only to support the replaced design; do not expand into an unrelated repository-wide rewrite.

Use new disposable state for development and validation. Existing development state may be incompatible; report that it requires fresh initialization. Do not silently reinterpret it or reset/delete the owner's current files. Preserving files on disk is not a requirement to make the new implementation consume old records. No live data reset, credential regeneration, activation or target operation is authorized by this document.

Update backup/restore to the current format only. Do not implement restoration of superseded records. Content integrity, authorization and ownership remain necessary within the new implementation; there is no promise of preserving operation IDs or retry continuity across this one-time breaking format change.

Installation verification checks the selected current installation's bytes; it must not require an obsolete recorded pathname to exist. Audit managed Git metadata, absolute alternates, shebangs and caches only where they perpetuate the same design flaw. Prefer supported tool behavior and disposable-cache invalidation over a new repair framework. Do not rewrite arbitrary user repositories.

## 5. Delivery: coherent slices that leave less code to maintain

1. **Design and contracts.** Identify redundant persisted fields and their producers/consumers, then update the relevant contracts to this owner-approved clean replacement. Obtain required independent review for storage, ownership and capability identity changes. Coordinate issue #143 and current branches before editing code; no new status ledger or audit framework.
2. **One joined setup-to-running slice.** Update installation verification, root discovery, setup/configuration, controller launch, live-owner publication, readiness and enrollment capability together. Replace affected schemas and fixtures directly. Do not ship half the callers on each model or introduce schema upgrades.
3. **Workspace and operation slice.** Derive managed source/stage/log/output locations; separate physical observations from durable content/request identities. Keep exact captures, current stopped-worker rules and duplicate-request behavior. Update related container/worker producers and consumers together.
4. **Storage and integration cleanup.** Complete affected cache, retention, reset, rollback, current-format backup/restore, external selections and generated commands. Remove obsolete fields, checks and affected compatibility branches. Update current documentation and the recovery-console implementation guidance.
5. **Prevention through clear instructions.** Put the short rule below in `AGENTS.md` and respect it in future changes. No path lint framework, relocation matrix, new general-purpose harness or ongoing migration infrastructure.

A routine settings edit, alternate path spelling or fresh version installation must not require a multi-step identity ceremony. If the implementation starts demanding a new registry, durable owner or compatibility engine, stop and simplify it.

### Standing agent guidance

> Derive managed paths from the current root and existing IDs; do not persist redundant absolute or relative paths. Store necessary external locations once in local configuration. Paths are locations, not durable identities. Accept ordinary Unix aliases; retain operation-specific containment and live ownership checks.

### Proportionate validation

- Update existing focused tests and fixtures for the affected current interfaces and fresh schema. Remove assertions whose only purpose was enforcing the rejected pathname-identity design or compatibility with replaced formats.
- Run relevant existing fast tests and ordinary setup/startup smoke checks against disposable fresh state. Keep existing checks for real content integrity, authorization, active-worker ownership and scoped deletion.
- Do not add elaborate home-rename, copy/adoption, namespace, alias-permutation, relocation or migration tests. No new test suite or infrastructure is required for this refactor. Prevention is the simple design rule above and normal code review.
- Check documentation/links and `git diff --check`. No image builds, flashing, QEMU, kernel compilation or release qualification. Do not repeatedly run full suites.

Completion is a simpler application model: current configuration chooses external resources, layout functions find managed files, content and domain IDs identify durable work, and runtime checks guard active operations. Home renames are one consequence of getting that model right.

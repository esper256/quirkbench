> **Legacy reference — not current requirements or agent instructions.**
> This document was archived on 2026-10-07. Consult [current documentation](../README.md).
> Commands and implementation claims below may be obsolete.

# Simplify Quirkbench into a human-operated lab with an agent-driven experiment loop

Status: owner-requested design and implementation plan. This intentionally supersedes older no-prompts/no-wizards requirements for human setup/build/flash workflows. It is a subtraction project, not a cosmetic CLI wrapper.

## 1. Product boundary and cause of the problem

Humans install, configure, build recovery media and select the physical USB to erase. Coding agents investigate a problem, collect target data, edit source, submit experiments and inspect results. Both use the same application services, but they do not need the same interaction style.

The existing design extended experiment-grade durable identities and transactional machinery into routine local configuration. The old CLI plan explicitly prohibited interactive wizards. That combination exposed internal request IDs, content hashes, signed-publication inputs and journal reconciliation as user tasks. Treating ordinary local configuration edits as integrity violations compounds the problem. The result is not an unavoidable cost of kernel debugging.

Current examples verified in the repository:

- `cli_setup_commands.py` exposes setup retry/runtime/builder identities and configuration flags; `controller_setup.py`, `setup_service.py` and their contracts maintain overlapping setup progress and exact-input relationships.
- `setup_service._same_file` can reject a changed local configuration and direct the user to maintenance rather than accepting a normal configuration edit.
- `recovery prepare` exposes plan files, exact confirmation digests, output directories and publisher keys/fingerprints. `preparation.py` materializes and rechecks planning metadata before a second public apply call.
- `cli_dev_commands.py` splits recovery acquisition, locking, recipe creation, bundles and build execution into a long public chain. `recovery_bundle.py` instructs the operator to run download argv and return with another command.

The modules and tests around these paths contain thousands of lines, but not all are redundant. The goal is a substantially smaller implementation, including many deleted tests. A renamed facade over all existing journals and validators does not meet acceptance. Do not delete necessary image-building or device-safety code to meet a line-count quota.

### Settled rules

- Human commands are interactive on a terminal, human-readable by default, with ordinary defaults and actionable errors.
- JSON is explicit, never the normal progress or error presentation. Noninteractive use must not unexpectedly prompt.
- No user-supplied request IDs, hashes, UUIDs, internal manifests or output directories in the normal setup/build/flash journey.
- No migrations, compatibility aliases, dual-format readers or support for superseded development state. Update affected formats and fixtures directly. Do not reset/delete the owner's current installation as part of implementation.
- Paths remain locations, not identities. Follow the completed path refactor; do not reintroduce path-bound hashes through this work.
- Preserve authenticated target communication, exact experiment/run approval, bounded workers and automated internal-disk protection. These belong to actual execution, not every settings read.
- One independent **gpt-6-astra, medium** review at the end of the implementation campaign before merge, per current AGENTS.md. No per-step reviewer subagents. Concrete unresolved architectural or data-loss concerns still require the owner's decision.

## 2. The normal experience

```sh
quirkbench recovery build
quirkbench setup
quirkbench recovery flash
```

Build may run before setup. If the user runs flash before setup, fail before opening the disk for writes: “Set up this controller first: quirkbench setup”. No manual publication/signing ceremony is a prerequisite for one's own locally built recovery image.

### `quirkbench setup`

A small keyboard-driven wizard asks for the address targets should use to reach this controller. Suggest a plausible LAN address, allow a hostname/URL, and explain that localhost cannot serve a different computer. Show interface choices when discovery is ambiguous. Do not ask for a separate bind-address choice unless an advanced configuration requires it; derive a suitable listener from the selected address. A URL is a choice, not proof the target can reach it. Unsupported remote/proxy configurations get a clear error, not a new networking framework.

Show a short summary, save on confirmation, create missing keys/certificates and initialize fresh controller state as needed. Cancelling leaves the previous configuration usable. Re-running setup edits current choices; it does not conflict with a previous request or require reset. Do not ask about caches, log budgets, private directory modes, runtime paths, signing identities or experiment internals in this wizard.

Use one normal editable configuration file, `~/.quirkbench/config.toml`. Group application-owned private material, state, cache and logs beneath that root with a simple fixed layout; explicit root selection remains available. This is the new installation layout, not a migration of the owner's current state. Do not maintain a second configuration source in XDG paths or SQLite. Paths inside the application are derived, not repeated in this file. Necessary external paths appear once. Print the config path at completion.

The configuration contains human choices: advertised controller URL and any explicitly overridden operating preferences. Secrets belong in private files, never inline in displayed config. Generate local trust automatically; preserve it on repeated setup and unrelated edits. If a hostname change requires a new server certificate, issue it under the retained local trust where compatible and explain any target update needed. No global configuration fingerprint makes an innocent preference edit invalidate enrollment or all application commands.

Direct edits take effect on the next start or explicit restart. Parse/type errors identify the file, field and correction. Do not overwrite manual edits from a retained setup journal. Do not reload a changed configuration into an active experiment midway through execution.

### Controller process: no hidden prerequisite

Setup offers **Start controller now**, selected by default. Reuse the existing controller executable, locks, worker lifecycle and readiness checks. A small launcher may start that same process in the background with logs under the application root; it is not a second supervisor, watchdog, daemon framework or host-systemd dependency. Add simple `start`, `stop` and `status` actions over that lifecycle. Stop drains/stops workers using existing rules, not a raw PID kill. Retain a foreground form for debugging; do not keep duplicate admin/public facades.

Flash starts the configured controller if stopped and waits for a bounded readiness result before issuing pairing credentials or touching the USB. If startup fails, show the actual reason and log path; do not emit unrelated housekeeping failures. Do not automatically modify firewalls, install host packages or create login/autostart integration. A reboot may require `quirkbench start`; the tools should say so rather than imply the controller stays running forever.

### `quirkbench recovery build [--force]`

Use the installed/checkout Quirkbench recovery definition and today's supported platform by default. The definition supplies exact packages, repository/trust inputs, target payload, builder image and layout policy. Users do not assemble or transcribe these inputs. A source checkout includes its current relevant edits, not just its Git commit ID.

Compute one build-input fingerprint from the effective recipe, pinned package identities, builder identity and actual generator/target-payload bytes that affect the image. Reuse existing input capture/hashing machinery; exclude absolute paths, timestamps of invocation, controller credentials and unrelated application settings. Do not use the entire repository hash or invalidate recovery because a controller-only preference changed.

If a complete matching image is cached, say “Recovery is already available” with a short human label and location. `--force` rebuilds the same selected inputs; it does not download silently newer packages, bypass integrity checks or delete the existing good image first.

Otherwise acquire required pinned inputs using the existing acquisition implementation, then invoke the existing bounded recovery builder/assembler. Show phases and useful progress. Missing host prerequisites give a short installation hint; do not automatically install host packages. Downloaded inputs keep authenticity/checksum checks at acquisition. Missing pinned inputs give an explicit error, not automatic substitution.

Build in a temporary owned directory, publish the image and its completion manifest atomically when successful, and keep logs for a failed attempt. An interrupted build is not selectable as a completed recovery. Ctrl-C stops the owned build/container and leaves any previous completed image intact. Lock by effective build key so concurrent calls do not launch duplicate builds; do not add another operation scheduler or resumable setup ledger.

The image cache uses its existing manifest/content identity and directory layout as the source of truth, not a new database index plus duplicated catalog. Hash/check the selected completed image before destructive flashing; normal list/status commands need not read every cached image byte. Local user modifications are not an adversarial trust domain: changed input creates a new build, changed config is configuration, and a damaged cached image is a specific rebuild error.

### `quirkbench recovery flash`

1. Check configuration and controller readiness. No disk mutation if either is missing.
2. Use the sole completed compatible recovery if only one exists. If several exist, show a small selector with build date, Quirkbench revision/version, platform and size; select the current-input match by default but allow an explicit older compatible selection. Show compatibility failures clearly. If none exist, say to run `quirkbench recovery build`.
3. List eligible whole USB disks by model, capacity, device name and existing filesystem labels. Never silently choose an erase target, even if only one is available. Reject the controller's system disk and ambiguous devices using the existing device-safety implementation.
4. Show one plain-language destructive confirmation: which device will be erased, which image will be written, and which controller it will connect to. Default is Cancel. No typed SHA-256, GUID or saved plan filename.
5. Obtain ordinary privilege elevation for the narrow disk writer. A sudo password prompt is normal in an attended workflow; do not require an unexplained prior sudo command. Cancellation or failure occurs before writes. Keep the controller process unprivileged.
6. Recheck the selected device after selection/elevation and immediately before destructive work, write the existing image and final layout, and provision controller trust plus a non-expiring single-use enrollment credential. Reuse the existing prepared-media format and writer where suitable. Present phases without JSON or repeated confirmations.
7. Flush and verify the written image/required metadata through the existing writer's checks. Only then say it is ready to unplug and boot. Failure says the USB may be incomplete and gives the retained log location. No false success after a lost helper response.

Device/layout confirmation belongs to the current flash invocation. Keep any necessary helper handoff private and ephemeral; it is not a saved public plan/apply protocol or a second database transaction. Generate target display names automatically and allow renaming later; no UUID entry is required. Cancelled/failed flashing revokes an unused newly issued credential; a known interrupted issuance is reconciled through the existing enrollment machinery, not another flash transaction engine.

The USB has its final layout before boot. Library space follows actual shipped contents, currently zero payload plus necessary filesystem overhead; split remaining capacity per the existing agreed policy. No target-side repartitioning or RAM admission limit. Space shortages affect the actual operation and preserve existing evidence.

On the target: boot to the dashboard, configure Wi-Fi if necessary, connect to the prepared controller, and return to the investigation workflow. Normal pairing needs no copied fingerprint. Paired recovery-debug upload and the menu-accessible root terminal remain available as designed.

### Optional scripting

Keep one explicit noninteractive route through the same services: `--non-interactive`, named input flags, and `--json` when requested. JSON implies no prompts. Example scripting inputs are a controller URL for setup and an image selector, whole-device selection and `--erase` for flash. Missing choices fail immediately. Do not require caller-generated request IDs or expose private helper references. Authorization and actual disk checks are the same as the interactive path; `--erase` is not a universal bypass.

The agent experiment interface keeps durable submission IDs, exact source capture, exact-run approval and restart-safe evidence attribution. Those features serve the debugging loop. Do not replace them with guesses or silently broaden execution approval while simplifying installation.

## 3. Delete complexity rather than conceal it

| Area | Keep | Remove or collapse |
| --- | --- | --- |
| Setup | Current config, generated trust, database initialization, ordinary atomic writes and one process lock. | Initial/service setup journals and request identities, exact-config replay contracts, predecessor/successor config hash chains and setup-specific reset machinery made unnecessary by fresh initialization. |
| Recovery build | One pinned definition, acquisition cache, bounded build runner and existing image assembler. | User-operated acquire/lock/recipe/bundle chain; overlapping foreground/managed orchestration for the ordinary build. Select the existing foreground pipeline as the single normal recovery build execution path. |
| Flash | Explicit device selection, one confirmation, narrow privilege helper, actual device/content checks, completion verification. | Public prepare/plan/apply records, confirmation digests, required output directories and repeated whole-input validation across frontend layers. |
| Local configuration | Editable config with validation when read/applied; real key/content identity. | Whole-config cryptographic identity, rejecting files just because bytes changed since setup, generic stopped-maintenance ceremonies for ordinary preferences. |
| Release engineering | Package authenticity and optional publisher-release verification at import/download boundaries. | Publisher keys/fingerprints and publication setup as prerequisites for flashing one's own build. Keep release tools only where an actual release workflow still calls them. |
| Tests | Existing meaningful tests of behavior and consequential safety. | Tests exclusively asserting removed commands, obsolete schemas, immutable setup preferences, legacy readers, journal permutations or arbitrary path/mode restrictions. |

Determine callers before removing a module: retain domain behavior used by experiment execution, but remove obsolete entry points and unreachable branches. Portable recovery bundles can remain a specialist transport format only if still used; they must not remain a competing normal build journey. Remove superseded public commands without aliases or deprecation layers. Keep root help concise; show build/flash/list under recovery and ordinary controller lifecycle actions where discoverable.

One source of truth for local choices, one normal build route, one flash operation. Do not wrap the current setup/service setup/publication/prepare transaction sequence in a wizard and declare success.

## 4. Validation appropriate to a normal Linux application

Validate untrusted data at actual entry points and native-operation boundaries. Pass already parsed/validated values through internal code without repeatedly serializing, rehashing and revalidating entire trees. Recheck immediately before mutation when races actually matter. Preserve immutable source evidence by digest; do not treat the user's editable configuration as immutable evidence.

Keep:

- Disk selection/system-disk exclusion and revalidation before erasure.
- Correct image input identity, completed-output publication and write verification.
- Network authentication, safe secret handling and exact target execution approval.
- Bounded worker shutdown, locks and evidence durability under failures.
- Archive traversal/input bounds at import/network boundaries and scoped deletion.

Remove checks justified only by distrust of the same user's ability to edit their own configuration or choose a symlink. No permission changes to unrelated user files. An invalid cache blocks that build/flash; it does not make every command panic. Human errors report the failed action, cause and next step, with detailed traceback in logs rather than repeated generic “conflict” messages.

Changing a meaningful certificate, runtime or active worker input can require restart/reconciliation. State that exact reason. Do not remove real race or ownership checks just because they look repetitive; retain one check at the owner of the guarantee and eliminate redundant callers around it.

## 5. Implementation sequence and deletion evidence

1. **Set the contracts.** Update AGENTS.md and affected CLI/product/recovery guides to supersede no-wizard, immutable setup and public plan/apply requirements for these human workflows. Preserve the agent-loop rules. Reconcile current active issues/branches and the existing path/recovery work; use current implementation rather than reconstructing old plans.
2. **Replace setup and config.** Implement the editable config and small wizard, fresh-state initialization and practical controller start/stop/status. Delete superseded setup progress/schema/request machinery and its dedicated tests in this slice. Do not add migrations or a compatibility bridge.
3. **Consolidate recovery build.** Make the existing foreground pipeline the normal cached build service, automatically resolve/acquire inputs and remove the manual CLI chain. Retain specialist low-level routines only where a real caller needs them. Add cache-hit/force behavior by adapting existing tests with a fake small builder.
4. **Replace flash interaction.** Drive selection, confirmation and progress through the existing disk writer, remove public plan/apply plumbing, and integrate prepared enrollment. Delete obsolete confirmation-digest/plan-file tests; keep actual wrong-device and interrupted-write checks.
5. **Remove leftovers and document the journey.** Rewrite the main walkthrough to the three commands. Delete stale examples, schemas, unused modules, compatibility aliases and tests for removed behaviors. Update root/family help and installed agent instructions. The UX plan and path plan remain historical design references only where consistent; current operating docs must not offer two competing journeys.
6. **Campaign review.** Run the relevant existing focused suites and smoke checks, then obtain the single required gpt-6-astra/medium independent review of accumulated changes. Resolve findings within that cycle; request another only if the design/guarantees materially change. Do not review every small change separately.

Measure baseline and final production/test lines and deleted modules/commands; summarize the removed responsibilities in the PR. Target substantial net deletion, including thousands of lines where the obsolete orchestration warrants it. Do not pad deletions, remove necessary coverage or minify code to claim a number. If the work mostly adds another facade and leaves the previous workflow intact, it has failed this plan and should be simplified before continuing.

No migrations or backward compatibility: fresh development configuration/state is acceptable. Do not delete live owner files or activate an installation without authorization. Unaffected records and files need no speculative conversion. Do not add a second scheduler, daemon manager, configuration identity service, generic TUI framework or cache catalog to complete this plan.

## 6. Focused acceptance, not another infrastructure project

Adapt existing tests; delete obsolete ones. Add only small cases needed for newly introduced behavior. No rename matrices, new broad integration harness, migration suites or adversarial permutations of every config field.

- Setup wizard accepts a controller URL, generates missing trust, saves editable config and can be cancelled/re-run. A direct valid edit takes effect after restart without reset or hash-conflict ceremony. Ordinary edits preserve pairing trust.
- Non-TTY/JSON invocation never blocks for input. Human output is readable without knowing any internal ID. Test the same services through the two presentation modes.
- Build cache hit performs no build; relevant changed inputs cause a miss; force rebuild preserves the prior good result until completion; interrupted output never appears in the selector. Use the existing small/fake build fixture, not a real recovery build.
- Flash refuses missing setup before writes, selects an image simply, always requires explicit disk selection and erase consent, rejects a substituted/system disk, and reports failed privilege elevation clearly. Existing writer tests cover critical actual-write semantics without new physical execution.
- A stubbed joined human journey exercises setup/build/cache hit/flash selection and returns clear target boot instructions without manually supplied hashes, UUIDs, JSON or hidden prerequisite commands.
- Run focused changed-code tests, help/smoke and `git diff --check`. No image builds, flashing, QEMU, kernel compilation or release qualification during implementation. Actual commissioning remains a later explicitly requested product operation, not proof supplied by software fixtures.

Completion means the normal user journey is small and the implementation supporting it is materially smaller. The complicated part of Quirkbench should be running reproducible kernel experiments across reboots—not asking a person to configure their own laptop application.

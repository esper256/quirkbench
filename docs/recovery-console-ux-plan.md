# Make recovery feel like a finished Quirkbench product

Status: software implementation is complete in [PR #147](https://github.com/esper256/quirkbench/pull/147), including reconciliation with the path-portable controller. Independent boundary review and local focused acceptance passed. Actual USB preparation/boot and production release acceptance remain separate gates. Existing images do not acquire these changes automatically.

Owner-approved direction: prepare the final USB layout and controller connection on the controller; never repartition on the target. Reserve library space only for actual shipped contents (currently zero), split remaining capacity between experiments and evidence, and handle space shortages at the affected operation rather than imposing a RAM-based admission limit. Debug uploads require normal pairing; collection and local export do not. A selectable terminal action must work without function keys.

Product acceptance: an operator prepares one USB on the controller, boots a target, configures networking if necessary and reaches the existing investigation workflow without partition management or copied fingerprints. A failure always leaves an understandable explanation and accessible diagnostics/terminal. This plan includes controller preparation because it removes target-side work; it is not permission for a general installer, UI framework or pairing redesign.

Implementation guidance: follow the [current location and identity contract](implementation-contracts.md#local-locations-and-durable-identity). Prepared-media identity comes from the selected device and immutable content, not a saved controller root. Derive managed trust/enrollment and staging files from existing IDs and layout. Preparation uses current explicit image/device selections, compares content/trust and physical attachment identities, and never persists their paths in plans or handoffs. Necessary external configuration remains once in ordinary local configuration. Use fresh current-format state for software checks. This changes no boot-device protection, pairing or exact-run authorization guarantee.

## 1. Experience: a guided dashboard

Replace the numbered menu with a full-screen, keyboard-operated dashboard. It should answer three questions immediately:

- Is this computer ready?
- What should I do next?
- Where can I go if something is wrong?

Borrow Omarchy’s discoverable keyboard navigation, direct access to important tools and consistent presentation. Keep the implementation suitable for a recovery environment, without a desktop compositor. [Omarchy navigation](https://omarchy.org/manual/navigation/)

**First boot:**

```text
 QUIRKBENCH                                      Recovery

 Let's get this computer ready.

 USB             Prepared
 Network         Not connected
 Controller      Configured · waiting for network

 Connect this computer to your network.
 Quirkbench will contact the controller prepared on this USB.

 > Wi-Fi & Ethernet
   Connect to controller
   Troubleshooting
   Open terminal
   Power

 ↑↓ Move   Enter Open   ? Help
 L Logs    T Terminal   Exit the terminal to return
```

**After setup and connection:**

```text
 QUIRKBENCH                                      Recovery

 Connected — ready for your next step

 Computer        target-01
 Network         Connected · Workshop
 Controller      Connected · workstation
 USB             Ready

 Continue your investigation on the controller.
 Each target run requires its own approval.

 > Connection details
   Wi-Fi & Ethernet
   Troubleshooting
   Open terminal
   Power

 ↑↓ Move   Enter Open   ? Help
 L Logs    T Terminal   Exit the terminal to return
```

These are illustrative values, never defaults. “Connected” requires a live authenticated connection; “USB prepared” requires the validated media record. Neither promises that every experiment or dump fits. Show operation-specific capacity problems when they arise. Update the primary action with state: configure network, connect/retry, or view connection details; do not show a redundant connect action after connection succeeds.

**When something fails**, replace the welcome text with a specific explanation:

```text
 USB storage needs attention

 Quirkbench could not open its evidence partition.
 Experiments cannot start until this is fixed.

 > View error and recovery steps
   Retry recovery checks
```

Network, logs, terminal and power remain accessible. Never reduce missing, running and failed checks to “pending or blocked.”

Use restrained color, generous spacing and readable labels. Color supplements words. Support 80×24 terminals, larger screens and monochrome output. Keep selection stable during automatic refresh; never move an action under the user’s cursor.

## 2. User journeys and action placement

### Prepare the USB on the controller

Provide a task-oriented `recovery prepare` action in the existing controller CLI. It selects a verified recovery artifact, identifies the explicitly selected USB and displays the proposed final layout before writing. Preserve the noninteractive controller CLI: a read-only planning invocation returns a device-bound plan/confirmation reference; an explicit apply invocation supplies that reference and the required erase acknowledgement. Missing inputs return actionable errors, never prompts. Bind the plan to artifact identity, selected device identity and observed size/layout; revalidate before every destructive phase, rather than trusting a reusable `/dev/sdX` name. Reuse existing image/layout, identity and write primitives; do not build a second image builder. Keep potentially destructive device access in a bounded privileged operation rather than running the entire controller as root.

The controller writes the final partitions/filesystems and stages its reachable address, public trust fingerprint and a USB-specific enrollment credential. Never copy controller private keys. The fingerprint authenticates the controller; the enrollment credential authorizes initial pairing, not experiments. Reuse existing enrollment machinery and retain exact-run approvals. The target supplies its own hardware identity on first connection; do not bind the USB to the controller's hardware. Interrupted preparation must be visibly incomplete and safely retryable; only verified completion reports the USB ready to boot.

Pairing has no elapsed-time deadline. Prepared-USB enrollment credentials and ordinary initial-pairing invitations remain valid until successfully redeemed or explicitly revoked/cancelled; do not add a TTL, expiry countdown or periodic renewal requirement. Issue prepared credentials near successful preparation completion and retain single-use redemption through the existing controller. Reuse the existing challenge/redemption and retained-request machinery; a lost acknowledgement retries the same redemption, and a duplicate USB credential must not enroll another target. Erase the staged bootstrap secret after durable activation, retaining only the normal paired credentials and necessary retry records. Individual network requests still have bounded timeouts so the UI remains responsive; a timeout or interruption must not expire the invitation or require starting pairing over. Refresh transient handshake state automatically as needed without imposing a user-facing pairing deadline. Test successful pairing after an arbitrarily long simulated delay, explicit revocation/cancellation, duplicate redemption and interrupted activation. Represent non-expiring invitations explicitly in versioned records/readers rather than as a huge fake expiry timestamp; preserve the meaning of historical records. This owner-approved lifetime policy replaces the current five-to-fifteen-minute invitation limit. Independent trust review checks implementation correctness, not whether to reinstate an owner-rejected expiry.

This is local Linux USB preparation, not writing a remotely attached disk from the cloud. The selected controller endpoint must be reachable from the target LAN; reject loopback-only configuration and show the selected address, but do not claim reachability before the target connects. Reuse current endpoint repair for address changes. Keep signed input verification and fixed recovery bytes intact: write installation-specific layout and enrollment metadata to the intended mutable partitions, not into the signed recovery root. If existing factory geometry cannot support the new layout, introduce a versioned factory format through the existing assembler; never silently shrink a populated filesystem or reinterpret an old manifest.

Preparation is a destructive fresh-media operation, not an upgrade or resize command. Identify already provisioned media and explain that re-preparation erases its credentials and evidence; require the same explicit device-bound acknowledgement. Interrupted preparation may be resumed only where existing write records prove it safe, otherwise require a fresh explicit preparation. Unsupported old media gets re-preparation guidance, not an in-place migration implementation.

Preparation reports usable experiment and evidence capacities, not a maximum supported RAM size. No target-RAM input is required. The target validates the prepared layout but never creates, grows, shrinks or repairs partitions automatically. Incomplete/invalid media gets an actionable message to reprepare it on the controller, while local logs, temporary networking and the terminal remain accessible. Do not retain first-boot partitioning as a parallel normal setup path.

### First boot and connection

1. **Automatic local initialization:** check prepared media and create private temporary network storage without asking the user to approve routine service startup. Show concrete failures, not “verifying recovery.”
2. **Wi-Fi & Ethernet:** open `nmtui`, then return to a refreshed dashboard. Ethernet may already be connected. Temporary networking does not depend on writable evidence storage.
3. **Connect to controller:** use the prepared endpoint and trust to complete normal pairing through existing authenticated enrollment. Controller preparation is the explicit authorization to use that staged endpoint/trust for initial enrollment; connect automatically when its prerequisites become available. No manual fingerprint transcription in the normal prepared-USB journey. Permit only one enrollment attempt at a time and use bounded retry backoff; authentication/trust failures require repair rather than endless retries. Show connecting, paired and currently connected as distinct states. Expired historical credentials or changed controller trust require explicit repair, never a verification bypass.
4. **Remember this connection:** offer explicit saving of selected connections when verified writable control storage and binding are available. Otherwise say “Connected for this session. USB storage must be available to remember this connection.”
5. **Ready:** direct the user back to their controller and coding agent.

The dashboard recommends the next useful action without hiding independent troubleshooting tools. Returning boots recover connection state from existing records; no second setup database. Pairing can still fail when credential storage or binding is unavailable. Simplifying that dependency is a separate future design, not temporary pairing added to this work.

### Everyday operation

Show the current activity using existing authoritative state: waiting for approval, preparing to leave recovery, receiving evidence, or needing attention. A connection, successful boot or completed run must not imply experiment approval or a successful fix.

### Troubleshooting

Group exceptional actions behind human descriptions:

- View recovery checks and logs.
- Collect and send a recovery debug report.
- Repair the controller connection.
- Apply controller setup from a file.
- Use this USB with another computer.
- Review evidence still on the USB.
- Open a root terminal.

Keep existing binding, evidence-transfer and retarget confirmations within their respective actions. Raw UUIDs, record names and diagnostic details belong here, rather than in the welcome screen.

### Debug the recovery image itself

Provide **Troubleshooting → Send recovery report**, separate from experiment evidence. Upload requires a normally paired target with usable authenticated controller access. Collection, preview and local export work offline and without successful pairing, hardware identity, a boot marker or writable evidence storage. If those failures prevent normal authentication, explain that sending is unavailable and offer local export or the terminal. Accept this coverage limit; do not add a second diagnostic pairing/invitation system.

1. **Collect:** capture a bounded snapshot of this recovery boot into private RAM. Show collection stages and which sources were unavailable. Missing boot-verification records are useful diagnostic facts, not reasons to refuse collection.
2. **Review:** show image/build identity, boot ID, collection time, included files, size, omitted/truncated sources and a readable preview. Explain that logs can contain computer names, network addresses and other identifying details. Allow cancellation and local export before any upload.
3. **Check connection:** use the normally paired controller and its existing authentication. If unavailable, retain the report in RAM and offer connection repair or local export. Do not treat merely knowing a controller address or fingerprint as upload authorization.
4. **Send:** explicitly confirm sending the frozen report. Show upload progress and useful connection/authentication errors. Repeating a send after a lost reply must return the same receipt rather than create another report.
5. **Received:** show a report ID and the exact controller command to inspect/export it. Only say “received” after durable controller acknowledgement. Export supports attaching the report to a bug report; do not automatically post to GitHub or another public service.

Use a small controller command family, `admin diagnostics list/show/export/delete`. Existing target authentication authorizes this distinct bounded upload; no experiment or attempt is required. A diagnostic upload grants no enrollment, evidence-drain or execution rights. The controller labels all received contents as reported diagnostic data, not proof of verified recovery or hardware identity.

The default report includes current-boot kernel logs, a bounded current-boot journal excerpt, Quirkbench service status and failure output, sanitized recovery configuration and kernel arguments, relevant mount/service facts, network link/address/route/DNS summaries without connection secrets, and recovery package/build identities when available. Include early-boot failure records already retained by the boot machinery; explicitly say when early logs were not retained. Never invent an identity from a missing marker.

Collect only reviewed sources. Do not recursively archive `/etc`, control state, evidence directories, home directories or shell history; do not scan or mount internal disks. Exclude network keyfiles, passwords, private keys, invitation tokens, authentication headers and private controller credentials. Apply source-specific sanitization before preview, hashing or transmission; unfamiliar structured configuration fields are excluded by default. Free-form logs cannot be promised anonymous, so preserve the explicit review/consent step.

Initial bounds: 10 seconds total collection time, 16 MiB total collected payload and 1 MiB maximum manifest; retain recent log tails and record truncation. Check advertised and actual sizes on the controller. Store attachments as opaque files rather than extracting an uploaded archive. Escape terminal control characters in previews and controller displays. Reports have a versioned manifest, per-file digests, a frozen report identity and a transfer request ID; changed content creates a new report.

The collector and sender must also have a packaged local terminal entry point, documented in the root-terminal banner, so a broken dashboard does not remove the reporting path. Reuse the same implementation as the UI. RAM reports survive UI restarts but not reboot; display this limitation. Explicit export may use already verified boot-media storage or a user-selected destination through the manual terminal. Do not add automatic disk discovery or persistent spooling as a prerequisite. Controller reports use existing managed artifact storage and explicit retention/deletion, with bounded incomplete uploads and per-report size limits. Give reports a separate identity in the existing managed storage, keep their retention independent of experiment completion, and ensure explicit deletion removes only the selected report and unreferenced objects. A full controller rejects the upload without a receipt; the target retains the report for retry/export.

### Power

Provide **Shut down** and **Restart**, with any outstanding work explained before confirmation.

Use existing coordinated shutdown where available. Restart must first complete the same applicable work/evidence preparation, then request an ordinary OS reboot; it must not arm a candidate or manufacture a recovery-arrival acknowledgement. When broken recovery prevents that workflow, offer an explicitly labeled local OS shutdown/restart and explain that evidence preservation could not be confirmed. Do not report a successful coordinated shutdown or clear durable records without verification.

## 3. Implementation and boundary corrections

### Capacity policy: use the USB, do not predict target RAM

- Allocate boot/recovery space from the actual selected artifact and required filesystem/metadata overhead.
- The optional library is a Quirkbench release input, not a user sizing choice. Its payload budget is currently zero. Do not reserve tens of GiB for hypothetical models. If the current layout needs a library filesystem even when empty, account for only its minimum technical overhead and report that separately; changing the partition count requires a versioned layout reader.
- Default implementation policy: split the remaining usable capacity equally between experiments and evidence, rounded to filesystem/alignment requirements. This is a transparent starting policy, not a measured optimum; changing the ratio later is a policy adjustment, not a new storage architecture. Do not introduce a sizing wizard or multiple profiles. Display the resulting capacities before confirmation. Future tuning can change this documented default based on measured use.
- Reject media only when the actual recovery artifact and minimum usable filesystems cannot fit. Remove the blanket twice-RAM-plus-log-budget/headroom admission rule and RAM-dependent commissioning journal requirement from the new layout. Do not advertise a maximum RAM capacity or promise full-memory crash capture.
- An 8 GiB laptop with a nominal 32 GB USB is an explicit software sizing/journey case. It may attempt work that fits; it is not a guarantee that every candidate, log stream or memory dump fits. Use actual device bytes, not the marketing label, when calculating capacity.
- Check available bytes/inodes before operations when requirements are known. Report the specific shortage and remedy at that operation. Free-space checks are advisory; handle ENOSPC, quota errors and short writes during actual execution too.
- On exhaustion, stop the affected write safely, preserve existing unuploaded evidence and any valid partial capture, and report incomplete results. Never acknowledge undurable data or publish partial candidates/reports as complete. Recovery, diagnostics and the terminal remain usable through bounded RAM state; report metadata failures honestly rather than promising durable error recording on a full filesystem.
- Offer upload/export, explicit eligible cleanup or preparation of a larger USB. No silent eviction of unuploaded evidence and no target-side repartitioning. Do not invent automatic streaming-only capture, compression guarantees, cross-partition borrowing or a new cache/retention manager to compensate for small media. Space cleared through existing cleanup may allow an explicit retry; retries must retain existing ownership and identity rules.

Controller preparation needs a versioned media record that describes final geometry, completion and capacity without pretending it observed target RAM. Preserve fixed recovery artifact integrity and old record interpretation. Reuse common planning/write logic but separate controller-side device validation from checks that assume the selected USB is the currently booted disk. Update the storage/commissioning contracts to this owner-approved policy before implementation; retain independent storage/trust review rather than asking the owner to reapprove the same product choice.

### Give the UI its own console

- Run Quirkbench on **VT2**. Switch there once its first screen is rendered, including a useful startup or failure screen; do not wait for successful storage checks, networking or evidence verification.
- Keep boot output on **VT1** and retain serial diagnostics. Replace recovery’s active-VT console routing so subsequent kernel output does not follow the user onto VT2. Audit systemd and journal console forwarding as well.
- Switch automatically only on initial presentation, not repeatedly after the user opens logs or a terminal.
- Restart a failed UI without restarting target work. Provide a plain-text fallback for unsupported terminals and retain independent terminal access if the renderer fails.

### Make networking independently usable

The current restriction exists twice: in the menu and in service dependencies. Fix both.

- Separate creation of private RAM network storage from verified recovery and saved-profile restoration.
- Start local NetworkManager functionality once its actual RAM-storage and service prerequisites are satisfied.
- Allow manually entered connections without evidence verification or pairing.
- Restore saved credentials only after existing media and target-binding checks.
- Preserve fail-closed behavior if secret-profile cleanup is incomplete. Explain that specific problem instead of blaming “evidence.”
- Keep candidate networking semantics unchanged.

Revise the contract’s blanket restriction on “recovery setup” to distinguish **temporary local connectivity** from operations requiring verified persistent storage or identity.

### Provide a real terminal

- Provide an independent, local-only root terminal on **VT3**, available through the main menu’s **Open terminal** action and `T`, without pairing, a boot marker or an installed-OS password. Selecting it switches to the real shell; `exit` returns to the dashboard. `Ctrl+Alt+F3` and `Ctrl+Alt+F2` are additional escape routes, never required for the normal journey, including keyboards with unusual function-key mappings.
- Show a brief banner: “Root terminal: commands are unrestricted and can modify internal disks.” Include the return shortcut and useful log commands.
- Honor the user’s explicit acceptance of manual disk risk. Do not substitute a restricted command runner.
- Do not add remote root login or expose this shell to agent proposals.
- Opening or exiting the terminal does not grant approval, mark recovery healthy or bypass existing runtime checks.

The existing recovery design already permits a privileged local maintenance shell. Clarify that this human-operated escape hatch is distinct from Quirkbench’s automated storage protections.

### Separate presentation from behavior

Use a lightweight Python `curses` frontend over a bounded, read-only recovery status model and existing action services.

The status model reports distinct facts about boot checks, prepared USB validity, evidence storage, network, pairing and controller connectivity. Each unavailable action has a concrete reason and remedy. Rendering must not mount storage, enroll targets or repair state.

Adapt existing attended routines into consistent screens; keep their validated operations and confirmations. Use one small frontend over existing services, with bounded reads and serialized state-changing actions. Do not add a general background-job framework, plugin system or second durable operation ledger. Existing records determine recovery after a UI crash.

During Quirkbench-owned slow operations, keep logs/terminal accessible and show measured progress when available, otherwise the current stage and elapsed time. Leaving a progress screen does not cancel or duplicate the operation; conflicting actions stay unavailable with a reason. Existing full-screen tools such as `nmtui` temporarily own the terminal and their own keys; restore/redraw the dashboard on return instead of trying to embed them or service every dashboard shortcut while they run. The emergency terminal remains independent of the dashboard. If the dashboard has failed, exiting the terminal must leave a usable shell/fallback message rather than a blank console.

Plain-text fallback is a minimal status/error display with access to diagnostics and the shell, not a second complete interactive application. No custom Wi-Fi editor, dashboard theme system, mouse support or universal serial-console UX is required for this delivery.

No new scheduler, database or controller service is required. Recovery diagnostics does require a narrowly scoped, versioned extension to the existing controller HTTPS API and the small administrative command family above. Reuse authenticated transport, bounded upload and artifact-storage primitives where their authorization semantics fit; do not fabricate experiment/attempt records or relax existing evidence endpoints. Review the new diagnostic authorization and retention records in the existing controller database. Verify and explicitly package terminal, curses and VT-switching dependencies.

## 4. Delivery and fast acceptance checks

**First implementation step:** define and independently review the controller-prepared media record, device-write validation, capacity policy and staged enrollment handoff. Update the relevant storage/trust contracts and define the recovery status/action table for that journey. This replaces first-boot partitioning rather than polishing it. Reuse existing services and preserve historical record meanings; no new scheduler or database.

Deliver in the following bounded steps, with each step’s focused tests and documentation included before proceeding:

1. Controller preparation, final-layout validation and prepared enrollment inputs, with capacity and interruption regressions.
2. Independent networking startup, VT ownership and menu-accessible terminal; remove automatic target partitioning.
3. Dashboard, consistent action screens, diagnostics and power flow.
4. Recovery-report collection, paired upload and controller inspection/export.
5. Join the already-tested pieces into the packaged preparation-to-report journey and remove superseded first-boot setup instructions/entry points. Do not postpone integration tests or packaging checks until this final step.

Obtain the required independent review for changes affecting credential restoration, storage, target ownership, diagnostic upload authorization and shutdown. Preserve existing wire formats and durable evidence; version the new diagnostic interface explicitly.

Keep new tests fast:

- Render first-boot, connected, disconnected, moved-media and failed-storage states with unrelated hardware names.
- Exercise keyboard navigation, resizing, automatic updates and returning from `nmtui` through pseudo-terminal tests.
- Verify an initial screen appears without Enter and remains responsive during action progress.
- Check actual staged units, dependencies, console arguments and packaged entry points.
- Prove temporary networking works without a recovery marker, while saved credentials remain unavailable without valid binding.
- Verify missing Wi-Fi hardware, radio blocking and NetworkManager failure produce different actionable messages.
- Verify terminal access remains available when boot checks or the dashboard fail.
- Verify explicit controller preparation authorizes its exact USB writes and staged initial enrollment; unrelated enrollment/retarget, evidence removal and power actions retain their own required confirmations.
- Verify setup and network actions never authorize a target run.

### Integration gates before producing the next image

The tests must exercise the implementation that will be packaged, including failed startup. Tests which only assert menu strings or stub out the relevant collector, launcher, uploader or action dispatch are insufficient.

| Layer | Required checks |
| --- | --- |
| State and action model | Table-driven states for prepared USB, incomplete controller preparation, exhausted experiment/evidence storage, missing/malformed boot records, missing hardware identity, unavailable evidence storage, invalid saved binding, network loss, paired-but-disconnected controller and healthy recovery. Assert both the recommended action and the complete action eligibility set. Rendering is read-only. |
| Real terminal process | Launch the actual packaged console entry point on a pseudo-terminal, using an existing lightweight terminal-screen emulator test dependency to inspect the visible screen. Exercise initial paint without input, arrow/Enter/help, 80×24 and resize, stable focus during refresh, subprogram return, EOF/signals, UI restart, slow actions and failure messages. Assert visible semantics and navigation rather than exact ANSI escape sequences or spacing snapshots. Bound waits by observable events, not arbitrary sleeps. |
| Action/service integration | Drive real adapters against disposable state, fake only host-facing operations such as mounts, VT ioctls and poweroff. Inject failures before/after each consequential operation and assert that no unconfirmed write, automatic replay of untrusted credentials or duplicate action occurs. Test late completion after leaving a progress screen. |
| Controller preparation and space exhaustion | Exercise the real layout planner against actual-byte capacities including nominal 32 GB, zero library payload and varied recovery/library sizes; verify alignment, no overlap, deterministic allocation and no target-RAM gate. Test device replacement between plan/apply, selected-device validation, signed artifact preservation, non-expiring prepared and ordinary pairing credentials, explicit revocation and interrupted preparation with disposable fixtures. Inject ENOSPC, inode exhaustion and short writes through actual storage adapters for candidate staging, evidence/control writes and report receipt; ensure no false completion, lost existing evidence or implicit resize. Assert target startup never dispatches partition/format operations. Use tiny files and bounded subprocesses, not full recovery-image builds or physical writes. Exercise the actual filesystem/GPT tool adapters against small disposable regular-file fixtures where supported; inject syscall failures at storage boundaries. Do not require privileged loop devices in portable CI or claim a mocked device write proves physical writing works. |
| Packaged recovery dependencies | Stage with the production installer and inspect actual units, masks, drop-ins, launchers and payload imports. Use cached pinned native tools to verify units/generator output and prerequisites. Check VT ownership, fixed boot-log routing, no network/evidence dependency for console or emergency terminal, independent RAM-network startup and no accidental candidate behavior changes. Verify required executables and Python extension dependencies against the package manifest. |
| Recovery-report round trip | Run the real collector against disposable log/record fixtures and actual subprocesses where relevant, then send through a real loopback HTTPS controller with temporary trust credentials. Exercise report collection without boot marker/evidence mount/target binding, paired authenticated upload, unpaired upload refusal with local export available, controller list/show/export and content/digest agreement. Do not mock away HTTP routing, authorization, storage or the durable receipt. |
| Faults and confidentiality | Test timeouts, missing journal tools, collection errors, truncation, control characters, known secret canaries in each source, malformed manifests, oversized bodies, changed digests, expired/revoked target credentials, wrong controller certificate, disconnects, lost replies and controller restart after receipt. Retry identical reports without duplication; changed bytes conflict. Assert diagnostics never create an experiment, authorize execution or mark recovery ready. |

Maintain one short representative journey that joins the real status reader, action dispatch, staged entry points and report transfer: controller preparation fixtures → recovery → temporary network → normal pairing → collect/review → authenticated diagnostic upload → controller export. Add an unpaired failure case that reaches collection/local export without transmitting anything. Follow with recovery-check success to prove the UI updates without losing focus or requiring a restart. Keep boundary-specific faults in focused tests rather than multiplying every possible state combination.

Demonstrate test sensitivity with temporary fixture mutations: omit a required executable/module, mask a prerequisite, restore the erroneous evidence dependency for temporary networking, route logs to the active VT, withhold the first UI render, and lose an upload acknowledgement. Each must cause a targeted assertion to fail. Do not add a repository-wide mutation-testing project.

Use existing focused suites and cached integration infrastructure. Aim for 30 seconds for the new focused console/report gate with dependencies present, with a 60-second hard timeout per portable/native gate. Keep acquisition separate from execution; required CI fails clearly when dependencies are missing rather than silently skipping. If a check cannot fit, identify the gap and obtain a specific decision instead of adding an expensive default check or weakening the assertion. Capture bounded terminal transcripts, rendered screen text, staged unit manifests and HTTP/collector diagnostics on failure, with secrets excluded.

Select these gates when console code, controller preparation/layout policy, action adapters, network/boot services, diagnostic transport, payload packaging, pinned dependency identities or console boot arguments change. Run the matching gate once per relevant revision; reuse matching evidence. No image builds, flashing, QEMU or kernel compilation for these checks. A report records the source revision and dependency identities tested, so passing checkout tests are not confused with checks of different image contents.

At the next separately requested physical boot, use a short acceptance checklist: the dashboard appears without Enter; late boot logs stay off VT2; Wi-Fi works even when persistent USB storage is unavailable; the menu opens the real terminal and `exit` returns without function keys; a recovery report reaches the controller and can be exported; prepared media connects normally and reaches truthful readiness without partition changes. Pseudo-terminals and staged-unit checks cannot prove real VT switching, graphics-driver behavior, Wi-Fi drivers or actual PID 1 startup ordering. Software tests alone must not be presented as physical-console qualification. Feed failures back into the narrowest fast regression that can faithfully detect them.


Completed software acceptance:

| Acceptance group | Implemented and checked |
| --- | --- |
| Preparation/layout | Exact device-bound plan/erase/apply; v3 final geometry; zero library payload plus minimum filesystem overhead; equal remainder split; nominal 32 GB actual-byte case; no RAM gate or target startup partitioning; signed fixed-byte preservation, replacement/interruption/mutation rejection and completion-last publication. Native GPT/filesystem adapters use disposable regular files. |
| Enrollment/trust | Controller-staged public trust and USB-specific invitation; normal non-expiring single-use initial pairing with revocation, long-delay, duplicate/lost-reply and interrupted activation coverage; bootstrap secret erased after durable activation; historical readers preserved; no new execution authority. |
| Networking/terminal | Private RAM networking independent of persistent storage; saved-profile restoration requires current verified binding; uncertain cleanup fails closed; candidate semantics unchanged. Actual packaged root shell exits to UI/fallback; VT2 UI, VT1/serial logs and independent VT3 terminal. |
| Dashboard/actions | Independent complete state/action table including storage, moved media, missing identity, radio/hardware/service failures and disconnected pairing. Real PTY tests cover first paint without Enter, keyboard/help, resize, refresh/focus, slow/late completion, tool return, fallback/monochrome, EOF/signals and restart. Setup-file activation repeats storage/mount/binding/maintenance checks around existing supervisor ownership; power actions retain existing confirmations and evidence fences. Journal activity is explicitly recorded/pending, never inferred active execution. |
| Reports | Offline bounded collection/preview/export and packaged terminal entry point; reviewed sanitized sources, secret canaries, truncation/control characters, deadline and descendant cleanup. Real paired HTTPS round trip, exact consent, durable receipt and replay across actual server restart; malformed/oversized/changed input, static/expired/revoked authorization, wrong certificate, disconnect/lost reply, collection and upload deadlines, ENOSPC and quota failures. Existing DB/CAS retention, selected deletion and completed-report backup/restore; no fabricated experiment/attempt or readiness. |
| Joined/packaged journey | Production staged enrollment handoff → real status/action dispatch → temporary networking → normal pairing → collect/review → authenticated TLS upload → controller export, plus unpaired offline refusal/export. Fresh stock v3 recipe/foreground/durable worker and candidate joins; historical v2 readers retained; superseded target partition entry point omitted. Actual pinned Fedora units, generator consumers, extensions and payload entry points checked. |
| Sensitivity | Targeted fixture mutations detect missing executables/extensions, masked native prerequisite, erroneous evidence-network dependency, active-VT log routing, withheld first paint and lost upload acknowledgement. No global mutation framework. |

Final evidence (dependencies acquired separately; no image/physical operations):

- Portable selected gate: `python -m ci.run run --suite recovery-console-reports
  --output /tmp/quirkbench-final-console-report-gate --timeout 60`, **88 passed in
  41.88s** (42.24s including evidence capture). This is above the 30s target and
  within the unchanged 60s hard limit. The later setup eligibility correction
  passed all 30 affected portable state tests; its unchanged TLS check passed in
  the selected gate. Evidence: `/tmp/quirkbench-final-setup-eligibility-portable.log`.
- Cached native gate: `QB_NATIVE_RECOVERY_CACHE=/tmp/quirkbench-console-native-cache-v6
  python -m ci.run run --suite recovery-native --output /tmp/quirkbench-final-native-gate
  --timeout 60`, **10 passed in 15.37s** (15.82s including evidence capture).
  The preceding native integration evidence remains in
  `/tmp/quirkbench-final-console-preparation-native.log`.
- Additional dependency/routing/terminal sensitivity: 13 passed in 0.81s
  (`/tmp/quirkbench-final-packaging-mutations.log`); CI selection and storage checks:
  39 passed in 0.47s (`/tmp/quirkbench-final-selection-storage.log`).
- Prior matching bounded-slice evidence remains at the paths recorded in the
  preceding commits: preparation, invitation and native prefix checks; stock-v3
  compatibility and durable-worker checks; report retention/backup and shutdown.
  First-failure diagnostics are retained; they were not overwritten.
- Source/dependency hashes: `/tmp/quirkbench-console-final-source-identities.json`.
  CI evidence records base commit, dirty checkout, runtime/dependency versions,
  exact selection, JUnit and bounded diagnostics. These source checks must not be
  confused with a built image's identities.
- Independent review: `prepared_media_design_review`, **gpt-6-astra/high**,
  all bounded slices including final integration over
  `7eb173e2520c218a7b6702818e365d90e9aecc8b`. Storage mount fences, recorded activity
  labels and upload recovery-mode admission findings were corrected and reviewed;
  final setup eligibility correction approved. No remaining source blockers.

Physical acceptance remains the separately requested checklist above: actual USB
preparation/interruption, boot readiness without Enter or partition changes, late
logs staying off VT2, Wi-Fi with unavailable persistent storage, real VT3 shell/exit,
normal prepared pairing and controller receipt/export. No software result proves
physical VT switching, graphics/Wi-Fi drivers or PID 1 ordering. Production publisher
provisioning/publication remains an independent release gate. RAM reports survive
UI restart but disappear on reboot; full filesystems may prevent durable failure
metadata. No automatic eviction, repartitioning or diagnostic authentication bypass.


Path-portable integration evidence (PR #147): source `dba707b487f295a614747c8d5ed1cf38469a2eaf`. Preparation plans contain content/trust and physical attachment identities, without image/device/sysfs paths. Apply accepts current `--image` and `--device`, verifies them against the confirmed plan, and derives managed components from current staging. External source/device/certificate locations are transient helper arguments; certificate and device fences remain. Report export accepts Unix aliases. Diagnostic/reset-retained artifacts remain opaque during GC; selected report deletion preserves reset archival roots.

- `make smoke`: 90 passed. Focused prepared-media/enrollment/copy/completion/stock-worker/runtime selection: 813 passed in 29.02s (`/tmp/quirkbench-reconcile-final-foundations.log`).
- `python -m ci.run run --suite recovery-console-reports --output /tmp/quirkbench-reconcile-final-console --timeout 60`: 91 passed, 31.42s including evidence capture.
- `QB_NATIVE_RECOVERY_CACHE=/tmp/quirkbench-console-integration-native python -m ci.run run --suite recovery-native --output /tmp/quirkbench-reconcile-final-native --timeout 60`: 10 passed, 14.59s including evidence capture. Cache prepared separately from already retained hash-pinned RPMs; no download during tests.
- Diagnostic opacity/reset retention/device observation: 20 passed in 11.79s. Joined prepared enrollment, controller reset and shutdown: 139 passed; corrected fixture-only device failures are covered by the final foundation selection. First failures remain in `/tmp/quirkbench-reconcile-first.log`, `/tmp/quirkbench-reconcile-second.log` and the initial gate directories.
- 110 local documentation links, changed JSON schemas and `git diff --check` passed. Required independent `console_path_review` (GPT-6-astra/high) approved exact source commit for paths, trust/device fencing, GC opacity/retention and shutdown. No installed state or physical target was changed.

Follow-up integration checks: joined setup/startup/reset/shutdown selection passed 100 tests in 27.83s. Cold native CI exposed #148 (binary/source RPM version mismatch); corrected source NVR is proven by the retained RPM's SOURCERPM and an actual signed download matching the unchanged package hash. Manual console fixtures use current materialized TLS locations without writing them into configuration; historical expiry coverage explicitly injects v1 invitations. No product trust or package pin was changed.

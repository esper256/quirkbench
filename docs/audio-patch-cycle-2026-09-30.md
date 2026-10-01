# First audio-patch cycle — integration handoff

**Listing follow-up, 2026-10-01:** `quirkbench recovery-images` is now available
in the native home launcher, with `--json` and bounded pagination. It queries
published image operations read-only and shows exact retained image/checksum paths;
it neither initializes state nor scans/hashes large payloads. A temporary sparse
8-GiB fixture covered pagination, missing/linked images and missing setup in
0.057 seconds; syntax, local links and `git diff --check` passed. No tests were
added. The running controller installation was preserved; only the home CLI launcher
now points to a fresh verified `20261001-recovery-listing` runtime (archive SHA256
`d9fc8a5b1c7823647ef4da1b6d19186bb2e619d712daf549e63a9e3f703478c9`).

The native listing confirmed **no published recovery image and no image operation**.
The preceding archive preparation stopped during RPM verification because RPM
could not create `diagnostics/signature-rpmdb/.rpm.lock` (permission denied).
Its diagnostic is retained under
`STATE/inputs/archive-signature-verification-20261001/package-verification.log`.
This supersedes the running-input snapshot below. Image production and flashing
remain pending; a commit title or downloaded inputs do not establish image readiness.

This packet repairs the existing attended path. It introduces no alternative
controller, image builder, scheduler or hardware-to-configuration generator.
The stock recovery image and candidate exclusions still follow the
[storage policy](architecture.md#storage-protection-policy). This is a software
and local setup record, not physical boot or storage-preservation qualification.
Native product-operation timestamps extend into 2026-10-01 UTC.

Current disposition: software corrections and native local controller setup are
complete. Signed archive reacquisition is running; the last handoff snapshot had
128 of 252 downloads verified against their recorded identities, no image operation
yet admitted, and native background readiness true at epoch 2. Image completion,
flashing, a real recovery report and baseline/patch execution remain pending.

## Implemented corrections

Both packaged and development bounded Podman helpers inspect containment options
before the immutable image argument. The fixed container command can use Python's
`-m`; it cannot override service/container resource containment. The actual worker
command, resource planning and systemd ownership remain fixed.

Build/compose argument version 2 binds the retained OCI archive, actual builder
config ID and Fedora base marker separately. Workers reuse recovery's hash/layer
verification and execute the actual builder. Original build provenance retains
its base-image meaning. Old records remain readable; ambiguous legacy jobs are
interrupted with a resubmission diagnostic instead of silently receiving new inputs.

One advisory heartbeat thread refreshes only the current owner's service presence
under the existing epoch checks. Long signing, validation or cleanup does not
deliberately block that heartbeat. Monitor activity remains distinct from owner
presence and authoritative publication. Input restoration now retains realistic
sysroot permissions and absolute OS links; extraction rejects links overlapping
archive members and never writes through them.

The candidate-only `audio-observation` recipe accepts exactly `card` (0–31),
`pcm_device` (0–31) and boolean `playback`. It collects bounded ALSA proc metadata,
kernel logs, device listings and read-only mixer contents. Optional playback is a
fixed five-second, stereo 440 Hz tone at 5% digital amplitude with fades; it does
not change mixer settings. Actual loudness depends on existing hardware settings.
Its tools inherit the recipe's process group so the outer deadline stops them.
Successful tool playback produces `NEEDS_HUMAN`, not an audio correctness claim.
Diagnostics-only operation and failed playback produce `INCONCLUSIVE`.

Recipe manifest schema v2 explicitly adds the audio privilege. V1 keeps its original
meaning. Recovery never grants playback. There are no Experiment/Result envelope
changes. `session request` exposes the existing typed human-question API; responses
use existing durable observation commands. Neither questions nor answers authorize
boot or extend an attempt's physical deadline.

The installed historical candidate catalog still names its historical observation
manifest. Updating runtime code changes its manifest identity. A reviewed candidate
baseline must bind the new recipe/runtime and retained audio utilities explicitly;
the old catalog is not evidence that this new candidate is buildable. No fabricated
package/source hashes or automatic catalog reinterpretation were introduced.

## Local operational setup

Persistent state is `/var/home/eric/.local/state/quirkbench`; the development
`/home/eric` alias resolves to the same physical home. Native Bazzite supplies Python,
Podman, systemd, DNF5, RPM, OSTree, GPG and OpenSSL. Build packages stay in the mutable
builder. Native setuptools packaged the canonical runtime because the development
Python lacks setuptools. Archive manifests and all installed file hashes were
verified. No controller packages, global trust, power policy or lingering settings
were changed.

The native launcher is `/var/home/eric/.local/bin/quirkbench`. Run execution/setup
commands in a native terminal. From Distrobox, prefix them with
`distrobox-host-exec /var/home/eric/.local/bin/quirkbench`; a development-container
service-manager check does not establish native readiness. The commands below
assume the native launcher is on `PATH`.

The fixed controller user service is manually configured with fresh private lab
trust, device credentials and signing material. It listens on loopback only during
factory bring-up. A reachable target endpoint and matching server certificate are
still required before recovery can report from another computer. If this laptop is
the target, a separate controller must remain running while it reboots.

Installed runtime:
`/var/home/eric/.local/share/quirkbench/controller/20261001-audio-integration/quirkbench-controller-0.1.0`.
Retained controller archive SHA256:
`d75bddef1f8bdbc579f9f76109fd250e75fd3072884566c71c5c4c781a9d54b1`.
Native `setup-check` confirmed the live owner/user service and background readiness.
The local service was restarted with no claimed worker, using a new installation;
no active executable tree was replaced.

Public signing fingerprint: `A4DFA24683000EC202B687B694AAF581313CE6E8`.
The retained builder identities are:

| Meaning | Identity |
| --- | --- |
| Fedora base marker | `sha256:fb31d002de20bfa7742b8c9b0d0ff723bb9fa2534fd43ecac0101a35f703fef0` |
| Finished builder config | `sha256:6e51e11c610ebfb6560c231ced072827ade8eaea4a1e82ca0447e691021325db` |
| Verified OCI archive | `2c79ca63793a410b1e4dc4302fc05edf5c214c211808e4a2841091a4a399f3fc` |

Builder export was retained in CAS, then its proven stopped disposable stage was
removed through existing retention mechanisms. Exact recovery acquisition requests
the recorded Fedora 44 package closure and stock `7.2.7-200.fc44.x86_64` packages.
The first attempt failed because native DNF excluded the stock kernel. Native DNF's
own log identified that cause. A second attempt proved ordinary exclusion overrides
do not bypass Bazzite's versionlocks during dependency resolution. Acquisition now
uses a private sibling `dnf-root`, explicit Fedora repository paths and
`use_host_config=False`, keeping the host RPM database/versionlocks outside the
solver. This follows [DNF5's installroot-based versionlock selection](https://github.com/rpm-software-management/dnf5/blob/main/libdnf5/rpm/package_sack.cpp).
It retains bounded failure stderr and does not alter host filters or locks.
The isolated third attempt then found the exact recorded
`glibc-0:2.43-8.fc44.x86_64` unavailable in the current repositories and stopped
before image admission. The official signed Fedora Koji archive returned that
exact RPM, with usable exact build metadata. No replacement package revision was
selected. A separately recorded archive operation checks metadata identities,
original userspace hashes, the same Fedora signatures and exact stock-kernel
identities before using the existing lock/recipe/image interfaces. It is an explicit
product reacquisition, not another image builder or a new default download backend.
Unavailable or changed archive bytes still block preparation. If that occurs,
propose a separately named/refreshed Fedora 44 binary candidate and reviewed lock,
preserving the original record; never substitute new packages into the old lock.

The latest input preparation is a resource-bounded native user service:
`quirkbench-build-recovery-archive-20261001.service`. Its immutable recorded command,
log and exit status are beneath
`STATE/development-runs/quirkbench-build-recovery-archive-20261001`.
Recorded script SHA256:
`d39d156257c56835b0d83a93c7c810d0faca4852bdf798ca6400eb4b3d91e77b`.
It conditionally verifies and locks downloaded packages, captures the v2 recipe
and admits `stock-recovery-audio-20261001` through the existing image coordinator.
Any prerequisite failure stops it before admission. Image execution/signing belongs
to the controller; this script does not build an image or flash media itself.
Pending status is not a completed image. The failed preceding run was stopped and
explicitly abandoned through the existing development-run retention command;
failed package stages retain diagnostic grace.

```sh
quirkbench monitor --run quirkbench-build-recovery-archive-20261001
quirkbench monitor --run quirkbench-build-recovery-archive-20261001 --once
```

These records are diagnostics; storage ownership and resumability remain in the
existing retention journal. Failed acquisition stages get the configured diagnostic
grace. Completed downloads stay protected until a signature-verified lock takes
ownership of their retained closure. No agent waits on unchanged logs.

## Remaining product path

1. Verify the exact downloaded RPM closure and Fedora public key; retain the signed
   stock lock, then generate a v2 recipe and submit `recovery-image`. Keep any exact
   dependency failure visible. An explicit replacement candidate revision is required
   if the recorded dependency cannot be reacquired.
2. Verify published image hashes/signature. Confirm the particular external drive
   before writing it; use the [commissioning guide](first-boot-operator.md). Obtain
   the target/media binding, provision its authenticated endpoint, and keep recovery
   in recovery-only mode until real inventory has arrived.
3. Read `quirkbench target-inventory TARGET_ID --json`. Retain completeness/blockers,
   PCI/USB identities, driver observations and firmware information. Record the
   specific audio symptom. Add only missing passive observations warranted by that
   actual hardware. Do not fabricate inventory or treat loaded recovery modules as
   the candidate config.
4. Retain pinned source, the separately reviewed minimum/target configuration and
   patch. Pin candidate ALSA utilities/firmware and the new recipe/runtime identities.
   Keep internal-storage exclusions and config/module/initramfs audits mandatory.
   Build an unpatched baseline before changing the driver.
5. Run that baseline and one focused diagnostic/corrective patch with the same
   configuration and recipe. Both need fresh exact operator approval. Compare
   attributable evidence and listening observations, and confirm return to recovery.

Existing command sequence after preparing the real manifests and binding:

```sh
quirkbench campaign create audio-first --device TARGET_ID
quirkbench snapshot audio-first --source /absolute/source PATCH_FILE CONFIG_FILE
quirkbench build /absolute/inputs/build.json --campaign audio-first --request-id audio-baseline-build
quirkbench operation status BUILD_JOB_ID --json
quirkbench compose /absolute/inputs/compose.json --campaign audio-first \
  --request-id audio-baseline-compose --publish-repo /SELECTED_STATE/repositories/lab
quirkbench operation status COMPOSE_JOB_ID --json
quirkbench campaign submit audio-first /absolute/inputs/baseline-experiment.json
quirkbench attempt status ATTEMPT_ID
quirkbench attempt approve ATTEMPT_ID --request-id audio-baseline-approval
quirkbench watch audio-first --once --json
```

The snapshot records declared source files; the build's source archives/config
and all manifest hashes still need verification/capture. Submission cannot approve
execution. The experiment's existing `deployment` artifact must be the published
exact composition. Set recipe `audio-observation`, required capability
`recipe.audio-observation`, parameters such as
`{"card":0,"pcm_device":0,"playback":false}`, and the actual reviewed baseline ID.
Enable attended candidate preparation only through the existing explicit target
service override after recovery-only inventory succeeds. Inspect each attempt's
target/media/deployment binding before approval.

For listening, issue an existing `post_test_interpretation` document naming the
session, attempt, recipe step, concrete prompt and bounded answer deadline:

```sh
quirkbench session request audio-first --campaign audio-first --file /absolute/question.json
quirkbench session observations audio-first --json
quirkbench session respond audio-first --request QUESTION_ID \
  --file /absolute/answer.json --request-id ANSWER_COMMAND_ID
quirkbench session observation audio-first --request QUESTION_ID --json
```

Questions/responses follow the existing [product contract](product-interface.md).
Distinguish missing tools/firmware, mixer/routing configuration and driver behavior.
Raw ALSA playback alone does not reproduce every desktop audio symptom.

## Validation and review

Temporary fixtures (no repository tests or infrastructure added) exercised both
exact launcher filters, permission/link capture round trips and malicious archive
overlap rejection, typed audio parameters/statuses, immediate independent heartbeat
pulses, upload/job request replay, stale publication, owner loss, interruption,
campaign pause and publication failure. Commands were
`PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python /tmp/quirkbench-integration-fixture.py`
(0.047 seconds) and the analogous `/tmp/quirkbench-packet-fixture.py` (0.075 seconds).
Checks do not establish actual compiler, image, hardware or acoustic behavior.

Required higher-reasoning static review approved owner/signing/input boundaries,
versioned recipe privileges, inherited recipe deadlines and isolated download-only
package resolution and the recorded exact-archive reacquisition journal/stop-proof
and admission sequence. V1 recipe schema remains unchanged. Source syntax, shell syntax, links
and whitespace checks passed: changed Python syntax in 23 files, changed shell
syntax, `git diff --check`, and 203 local links across 32 documents. No broad suite
or release campaign ran.

Actual Podman worker containment must still be inspected once during the first
real build. Real recovery boot, target report, baseline/patch evidence and recovery
return remain separate attended product evidence. No new universal support,
storage-preservation, reset or release qualification is claimed.

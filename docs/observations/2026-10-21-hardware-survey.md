# Bazzite hardware survey through Quirkbench

The owner authorized unattended, bounded experiments on the external boot device.
Internal disks and firmware changes are excluded. No microphone/camera recording
or audible playback is needed for this survey. Findings are observations, not a
claim of hardware health or proof that a kernel patch is needed.

## Recovery

Recovery source `3ff91dbda25b2e98fdae8228cd52456eac6de452e46febac9fda995461cfaa54`,
boot `64ccf5a6-68c2-41d3-8fdb-fb304a3c7a5f`, connected with no failed services.
Root is read-only. The emergency report read kernel messages without subprocesses.
The AMD SOF driver reported missing `amd/sof/community/sof-rmb.ri` or
`amd/sof-tplg/sof-rmb-rt5682s-rt1019.tplg` and failed with -2.
This is not yet a Bazzite finding: recovery has a different package set.
No USB I/O failure or kernel crash was found in this recovery boot's saved log.

## Quirkbench observations

- The combined recovery report includes only the end of the boot log. It omitted
  the earlier audio probe failure; the emergency report exposed it. A successful
  report must not be interpreted as an error-free boot. A complete saved journal
  with a bounded summary would improve the normal inspection path.
- Investigation context includes an old `boot-requested` target work entry even
  though the associated run is finished. Current run state must take precedence;
  stale work makes the displayed state harder to trust.
- Built-in hardware inspection reports CPU details but lacks PCI/USB driver and
  audio inventory. Candidate shell inspection can fill that gap.

- Candidate preparation exports a full OCI archive after adding the small target
  RPM, then imports it into OSTree. This repeats substantial local I/O even with
  a cached base. The displayed stage remains “Adding target connection support”
  during export, so it does not identify the actual long-running operation.
  Reuse of an unchanged prepared result and clearer export progress merit review.

## Saved data

Controller-local `survey/` contains MCP responses and diagnostic scripts.
Recovery reports are under `reports/target-e438d64a7291/64ccf5a6-68c2-41d3-8fdb-fb304a3c7a5f/`.
Candidate results and conclusions will be recorded below as experiments finish.

## Candidate results

Selected Bazzite stable digest
`5d49529cf809ff5b9e1ba52fd4833ec4099005673af4587b15d55b69210264e3`,
image label `44.20261012.1`; `/etc/os-release` reports `44.20261012.0`.
Kernel `7.2.9-ogc3.1.fc44.x86_64`; Google/ASUS Frostflow rev3,
Ryzen 3 7320C, Radeon 610M, MediaTek MT7921 Wi-Fi, about 8 GiB RAM.
Candidate `3444720c3fcb6de79526758d`, commit
`07afd22afee31d051accdbd1432411598b5d8622dfaf4852eff847fbbb5c242f`.
Preparation took about 299 seconds; deployment about 65 seconds using cached content.

### First boot failed; retained repeat connected

Run `run-e31d5d4a9c2d` reached Bazzite userspace but never connected. Its journal
ends shortly after mounting the boot filesystem during Quirkbench preparation.
Recovery returned unaided and uploaded a 183,271-byte candidate journal.
Its next kernel log explicitly reports `hardware watchdog timer expired`
(reset reason `0x02000800`). This establishes the reset mechanism, not what
stalled. No kernel panic or USB I/O error was found in the saved candidate log.

Repeat run `run-5de9deb5a0e1` reused the same deployment and connected in about
62 seconds. This does not prove the first failure is fixed. First-boot work,
storage latency and Quirkbench's early boot setup remain possible causes.

### Missing onboard audio: a reversible fix demonstrated

Initially only HDMI audio existed; onboard playback/capture were absent.
The kernel requested two missing AMD files despite installed
`alsa-sof-firmware-2025.12.2-1.fc44.noarch`.
The exact candidate image has an empty `/usr/lib/firmware/amd` directory.

Using MCP, an agent-written Python command downloaded the files below into
`/run/quirkbench-audio-survey`, verified hashes, set the temporary firmware search
path and re-bound only PCI audio device `0000:05:00.5`. The driver then loaded
firmware and topology and exposed `sof-rt5682s-hs-rt1019`, with playback and capture
PCMs. No OS rebuild, persistent firmware update, playback or recording occurred.
This demonstrates repair of device enumeration, not working acoustic output,
microphone quality or desktop routing. It is a missing-file problem, not yet a
reason to write a kernel patch.

Files came from the [Chromebook audio support project](https://github.com/WeirdTreeThing/chromebook-linux-audio/tree/84b700e271145df82ae71d0497af11705a2bb166/blobs/mdn),
whose Mendocino setup installs these paths. Its general installer was not run.

| File | SHA-256 |
| --- | --- |
| `amd/sof/community/sof-rmb.ri` | `7ee9fe398b1d2e8b5a4d98e73cc3c85510244a79e216fe68319eeb0cbb4ad4b6` |
| `amd/sof-tplg/sof-rmb-rt5682s-rt1019.tplg` | `0d152eb4833aea4b653407b263997204f81a58f177af5c05f4ef052d79a278b4` |

### Other checks

- CPU/memory: 60 seconds, two CPU workers at 50% load and one 128 MiB verified
  memory worker passed. This short test does not establish hardware health.
- Network: 60 controller pings, no loss, 29.4 ms average, 103 ms maximum.
  These ran before the successful load test; no simultaneous-load claim.
- Graphics: Vulkan enumerated the Radeon with RADV/Mesa 26.2.4. No rendering
  stress or visual inspection was performed.
- Internal NVMe and its partitions were read-only and unmounted.
- Firmware clocksource warnings occurred (`Watchdog remote CPU ... read timed
  out`), including in recovery. These are distinct from the hardware watchdog
  reset and remain unexplained.
- Bazzite's `terra-wireplumber` is installed; a query for the literal package
  `wireplumber` alone misleadingly reported it missing.

### Further Quirkbench findings

- `bootloader-update`, `greenboot-healthcheck`, `rpm-ostree-fix-shadow-mode` and
  `ublue-os-media-automount` failed. The first three expect normal `/boot` mount
  and GRUB arrangements; automount failed while reading the empty fstab. They
  are candidate-integration failures, not evidence of bad hardware. Candidate
  preparation must keep automatic boot changes/automount out of the experiment
  while retaining normal OSTree compatibility.
- Commands default to `/`, which is read-only here. The first stress command
  failed before testing because it wanted writable temporary space. Supplying
  `/tmp` fixed it. Context should name a writable work directory explicitly.
- Separate deploy/boot, repeat boot with a new run ID, full saved journals,
  emergency inspection and paginated MCP output all worked in this survey.

The audio experiment used only temporary RAM files and a runtime kernel parameter.
Its disappearance on reboot is intentional. `alsaucm` was unavailable, so UCM
routing was not validated. Package-name checks must account for replacement
packages such as `terra-wireplumber`.

Kernel taint was from unsigned external modules shipped by this Bazzite image
(`ryzen_smu`, `kvmfr` and others), not a recorded kernel warning/oops taint. An
upstream bug report may need reproduction with an unmodified upstream kernel.
Post-test thermal-zone readings were approximately 32–38°C; these are snapshots,
not measurements of peak core temperature during load.

## Repeat experiment

Run `run-25b92056437b` connected in about 56 seconds. The RAM directory and firmware
search path had reset, and only HDMI audio was present. Repeating the same two-file
probe restored onboard playback/capture enumeration again. This gives a repeated
before/after result without changing the base image or Quirkbench.

A second 60-second CPU/memory verification passed while 90 controller pings saw
zero loss (23.2 ms average, 113.9 ms maximum). One clocksource remote-CPU timeout
warning occurred during that interval; no panic, USB error or memory verification
failure was recorded. The hottest thermal-zone snapshot immediately afterward was
67.8°C. This remains a short survey, not exhaustive stress or hardware validation.

The first successful run's return took about 36 seconds and confirmed all command
outputs received. Uploaded results include command arguments, so the precise
agent-written diagnostic and audio scripts are retained with their results.

Other interface friction: successful deployment/boot/return responses still name
`controller_work_wait` as the next tool, even when already complete. The stage
text is useful, but the next-tool guidance should reflect the completed action.
The current MCP has no file-upload helper; this probe downloaded small pinned
files directly on the candidate. That worked, but does not demonstrate the later
agent-built-binary transfer journey.

For debugging failures before the candidate connects, the current prepare MCP
accepts only base/name/request ID. It cannot yet accept an agent-authored startup
probe or changed package. That limits how far an external agent can investigate
such a failure without developing Quirkbench itself. This is a missing planned
capability, not a reason to bypass the supported interface or edit controller
records during an investigation.

## Fresh-deployment comparison and conclusion

After confirmed receipt, explicit discard removed the original deployment in
about 34 seconds while keeping cached content and controller results. Deploying
the same candidate afresh took about 48 seconds. Run `run-533e15f17f7c` then
connected in about 124 seconds: the initial watchdog reset did **not** reproduce.

Its service timings explain part of the cold-start cost: `ldconfig.service` took
41 seconds, manual-page cache update 21 seconds, and Quirkbench preparation
18.5 seconds. NetworkManager waited behind the system initialization chain.
These timings do not prove why the earlier watchdog expired. Keep the first
failure's evidence and avoid declaring startup reliable from later successes.

Across four candidate boots: one watchdog return before connection, two successful
retained boots, and one successful fresh boot. No human intervention was needed.
The onboard-audio enumeration problem and its RAM-only fix reproduced across two
boots. Two short CPU/memory checks passed, and 150 total pings had no loss,
including 90 under load. No hardware defect was established.

Quirkbench now supports a useful live investigation and a concrete reversible
repair. It is not yet demonstrated ready for an arbitrary person's kernel bug:
startup reliability, candidate integration, custom-package/early-probe preparation
and agent file transfer need work. The audio finding calls for packaging and
configuration investigation before any kernel patch. Acoustic tests, suspend,
extended stress and internal installed-system behavior remain untested.

The final return completed in about 35 seconds, with recovery boot
`b407d167-6da6-4b86-be64-006829b1613e` and confirmed evidence receipt.
Its deployment and all controller evidence remain for follow-up. The audio
workaround is gone with RAM; the person's internal system was not changed.

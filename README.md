> **Product preview:** Quirkbench is still under development. This page describes
> the intended finished product. The release downloads, setup wizard and commands
> below are not all available yet; these instructions do not currently work end to
> end. See the [implementation roadmap](docs/product-roadmap.md) for current scope.

# Quirkbench

Quirkbench helps you investigate Linux hardware problems and develop fixes backed
by evidence. A coding agent proposes changes, Quirkbench builds them, and a second
computer boots the experiments directly on its hardware. Logs and results return
to the agent for the next decision.

The **controller** is the Linux computer running Quirkbench and your coding agent.
The **target** is the computer with the issue. The target runs from an external USB
drive; its installed OS and internal disks stay outside the investigation.

You describe the problem, help with physical observations when needed, and decide
when to pause or share the results. Quirkbench keeps the source changes, experiment
history and debugging evidence together. An investigation can produce a patch or
an actionable bug report; it cannot promise to reproduce or fix every issue.

## 1. Get ready

You need:

| Item | What to prepare |
| --- | --- |
| Controller | An x86-64 Linux computer with internet access, Python 3.11+, rootless Podman and Distrobox. Setup checks these and gives installation instructions for anything missing. |
| Build storage | Start with about 200 GiB available for source, builds and evidence, with additional room for retained results and backups. Setup checks capacity and keeps a free-space reserve. |
| Target | An x86-64 UEFI computer that can boot USB storage, with Secure Boot disabled. Peripheral support is checked after boot. |
| External drive | A USB SSD is preferable to a small thumb drive. 256 GB is a useful starting size; targets with substantial RAM or large logs may need more. **Flashing erases the selected external drive.** |
| Network | Both computers on the same trusted local network. Ethernet is simplest; Wi-Fi setup is available. Guest networks that isolate devices will not work. |
| Coding agent | An account or credentials for a supported coding-agent command. Setup lists supported adapters and walks you through authentication on the controller. Agent usage may incur charges. |

Keep both computers connected to power. Keep the controller awake during an
investigation; putting it to sleep interrupts builds and communication. Quirkbench
does not change your power or firmware settings automatically.

You do not need to install Quirkbench on the target's existing OS, collect a hardware
report in advance, or know how to build a kernel. Targets without a working automatic
reset mechanism can still be investigated while someone is available to reset them.

## 2. Install and set up the controller

On the controller, open [Quirkbench Releases](https://github.com/esper256/quirkbench/releases)
and download `quirkbench-controller-linux-x86_64.tar.gz` and `SHA256SUMS` from the same
release. In the download directory:

```sh
sha256sum --check --ignore-missing SHA256SUMS
tar -xzf quirkbench-controller-linux-x86_64.tar.gz
./quirkbench/install.sh
```

Continue only if the archive's checksum is reported as `OK`. The installer puts the
launcher in your user account and explains any PATH adjustment needed. It does not
replace system packages or require a system-wide Python installation of Quirkbench.

Start setup:

```sh
quirkbench setup
```

The wizard guides you through:

1. **Storage:** choose where builds and investigation data live.
2. **Build environment:** create the isolated Fedora containers and check CPU,
   memory and disk limits. Experimental kernels and packages stay inside build
   directories; they are never installed into the controller's OS.
3. **Coding agent:** select an adapter and authenticate in the controller environment
   that will run it. Set usage limits before the first investigation. Credentials
   stay on the controller and are not copied into target images or evidence bundles.
4. **Connection:** choose the controller's LAN address. Quirkbench creates its TLS
   identity and checks the device and repository endpoints. If your firewall needs
   a change, setup shows the specific local-network rules for you to apply.
5. **Recovery media:** download and verify the matching recovery image. Setup displays
   its saved path, for example `~/Downloads/quirkbench-recovery-x86_64.img.xz`.

Setup can be rerun after an interruption. It reuses completed work and existing
credentials instead of creating another controller identity. Container downloads
and initial setup have their own progress display; no coding agent needs to watch them.

The controller service runs independently of the terminal. Setup reports whether
it can continue after you log out; keep your login session open unless you enable
that option. Rebooting the controller preserves progress and pauses investigations.

## 3. Flash the drive and boot the target

Use a normal image writer such as balenaEtcher:

1. Select the recovery `.img.xz` downloaded by setup.
2. Select the external USB drive. Check its identity and capacity carefully.
3. Flash it and let the writer finish verification.
4. Connect it to the target and select it in the computer's boot menu.

Use the owner's normal firmware controls to disable Secure Boot and allow USB boot
if necessary. Quirkbench never changes those settings. For repeated experiments,
the USB drive must remain the selected boot device across restarts; a one-time boot
menu choice is not sufficient on every computer.

Recovery opens a local setup screen. First boot expands the external drive's data
partitions and shows progress. Later boots reuse that layout. No desktop installation
or internal-disk selection is part of this process.

If the image cannot support the target's boot or storage-protection requirements,
it stops with an explanation. Do not install it onto the internal disk as a workaround.

## 4. Connect and pair the target

On the target, choose **Network setup**. Use the connection screen to enable Ethernet
or join Wi-Fi, then return to Quirkbench. The target only needs access to the controller;
it does not need its own internet connection.

On the controller:

```sh
quirkbench pair
```

This displays the controller address, its certificate fingerprint and a short-lived
pairing code. Enter the address on the target. Compare the fingerprint shown on the
target with the controller's display, then enter the pairing code. Do not accept a
fingerprint that differs.

Choose a target name, such as `target-01`. Recovery saves its network configuration
and device credentials privately on the external drive, then sends a hardware
inventory to the controller. It does not start an experiment merely because pairing
succeeded. Pairing-code expiry is harmless: run `quirkbench pair` again.

Check the connection:

```sh
quirkbench targets
```

The target should appear as **Recovery ready**. On subsequent boots it reconnects
using the saved configuration. A changed controller address can be entered through
the recovery setup screen without reflashing.

Moving the drive to another computer requires explicit setup for that target.
Quirkbench will not resume the previous computer's experiment. Previous evidence
keeps its original attribution; retargeting does not erase it.

## 5. Start an investigation

On the controller:

```sh
quirkbench session start --device target-01
```

The wizard asks what is wrong, how you trigger it, what you expect instead, and
whether someone can observe or reset the target. A useful description is concrete:

> After waking from suspend, the built-in trackpad sometimes stops responding.
> A USB mouse still works. I can reproduce it by closing and reopening the lid,
> but not on every attempt. I can help check whether the pointer moves.

You can also provide a prepared description:

```sh
quirkbench session start --device target-01 --problem ./problem.md
```

Review the proposed scope and usage budget. Quirkbench selects a compatible baseline,
shows the kernel/source versions and any differences from your installed environment,
and downloads the required sources. Advanced setup lets you select a local source
tree or a specific supported version. Missing driver/profile support is reported as
a blocker rather than silently guessed.

**The first session includes an attended baseline check.** Quirkbench builds the
baseline OS, boots it on the target, collects a small observation, uploads the result
and confirms the return to recovery. Stay nearby for this first cycle. If it fails,
the session preserves the evidence and asks for the necessary recovery action.

Once the baseline check passes, the agent establishes a reproduction before trying
fixes. Each physical attempt follows the same cycle:

```text
Recovery → prepare experiment → reboot → run and upload → reboot → recovery
```

You flash the drive once. Subsequent experiments transfer changed OSTree objects;
they do not rewrite the recovery image. Builds may change the kernel, drivers or
userspace components relevant to the issue.

Some observations need you: confirming that sound actually played, moving a physical
pointer, or supplying a microphone stimulus. The monitor gives a specific instruction
and records your response with the attempt. Quirkbench does not treat software
loopback or simulated input as proof of physical behavior.

If the problem does not reproduce in the debugging environment, the session records
that limitation and investigates relevant differences. It does not call the issue
fixed or modify the installed OS to force a reproduction.

## 6. See what is happening

Starting a session prints its ID and opens the monitor. You can close the monitor
without stopping the work, then reconnect from another terminal:

```sh
quirkbench session watch SESSION_ID
```

Replace `SESSION_ID` with the ID printed when you started the investigation.

The monitor shows the current hypothesis and phase, build or transfer progress,
target boot state, last contact, last measurable progress, pending evidence and usage.
Percentages appear only when there is a known total. A quiet compiler is not reported
as finished, and a heartbeat is not counted as scientific progress.

| Status | What it means |
| --- | --- |
| Working | A build, transfer or experiment is in progress. Inspect the last-progress time and counters. |
| Waiting | Quirkbench is waiting for something named on screen, such as the controller connection or a physical observation. |
| Needs your input | Read the requested check and enter your observation in the monitor. |
| Possible stall / deadline exceeded | The operation is not advancing as expected. The monitor shows its deadline and recovery action. |
| Needs recovery | Execution is uncertain or automatic reset is unavailable. Follow the displayed target-reset instructions; the attempt will not silently repeat. |
| Paused | Progress is saved and no new experiment will start. |

Monitoring does not invoke the coding agent. The agent runs at decision points;
it does not spend quota watching builds or waiting for reboots. Budget exhaustion,
expired authentication and storage pressure pause the session with an actionable reason.

For a quick summary instead of a live display:

```sh
quirkbench session status SESSION_ID
```

## 7. Pause, resume or finish for the day

```sh
quirkbench session pause SESSION_ID
```

Pause stops new scheduling immediately. The active bounded build or agent decision
finishes and saves its output; an active physical attempt preserves its results and
returns to recovery. The monitor distinguishes **Pausing** from **Paused**. It pauses
between individual repetitions, not after the entire batch.

Resume when you are ready:

```sh
quirkbench session resume SESSION_ID
```

Source edits, hypotheses, rejected approaches, experiment results and usage survive
container recreation and controller restart. After a restart, Quirkbench reconciles
outstanding work and requires this explicit resume. An uncertain attempt needs
review before another is authorized.

To disconnect the external drive, pause first and wait for the target to show
**Recovery ready**, then choose **Shut down** on the target. If the controller is
unavailable, recovery retains unacknowledged evidence on the drive for the next
connection. Keep that drive intact; do not reflash it to fix a connection problem.

### Leaving an investigation unattended

Start with attended operation. To assess automatic recovery on a target:

```sh
quirkbench target qualify target-01
```

This is a separate, guided hardware check that may deliberately cause hangs or
resets. It explains each trial and requires someone able to recover the machine.
The resulting report states which failures can be reset and what evidence survives.
Some early hangs may still require a power button; a watchdog does not guarantee a
crash dump.

An eligible session's settings can then enable unattended operation within the
qualified limits and an explicit experimental-kernel risk scope. Changing hardware,
firmware or recovery-sensitive code can require renewed qualification. A completed
baseline boot alone does not enable unattended operation.

## 8. Take the results with you

Export an investigation at any point:

```sh
quirkbench session export SESSION_ID --output ./investigation
```

The export includes a readable report, the experiment history and selected evidence.
When a fix is supported by the observations, it also includes:

- Patches grouped by the affected source component.
- Exact source and build identities, with matching debug symbols.
- Baseline, patched and reverted comparisons, plus regression checks.
- Reproduction instructions, exposure counts and remaining uncertainty.

Incomplete or inconclusive investigations are labeled accordingly. Exports exclude
agent credentials, pairing secrets and saved Wi-Fi profiles, but raw debug logs can
still contain identifying information. Review the contents before sharing them.
Quirkbench does not publish patches or install them into your normal OS automatically.

An export is for sharing; it is not a complete resumable backup. Use:

```sh
quirkbench backup --output /path/to/backup-directory
```

Backup includes the controller database, retained source/evidence and referenced
OSTree content. The command explains how to back up private controller credentials
separately. Restore into a new state directory with `quirkbench restore`; restored
sessions stay paused until credentials and target state are reconciled.

## If something gets in the way

| Problem | What to do |
| --- | --- |
| The controller cannot prepare its environment | Rerun `quirkbench setup`. It reports missing dependencies, rootless-container permissions, service support or storage instead of requiring you to diagnose a failed kernel build. |
| The target cannot reach the controller | Check the address shown by `quirkbench pair`, local firewall rules and Wi-Fi client isolation. Use recovery's network screen to correct the connection; never disable TLS checking. |
| The target has no usable Wi-Fi | Try Ethernet or a USB Ethernet adapter supported by the recovery release. Check the release's hardware limitations; unsupported hardware may need a newer recovery image. |
| A candidate hangs or fails to boot | Allow a qualified reset to run, or follow the monitor's manual-reset instructions. Boot the USB into recovery so it can upload surviving evidence. Missing logs remain an explicit limitation. |
| The installed OS boots after an experiment | Restore the USB boot preference using the computer's normal controls, then boot recovery. Do not start a second session to replace the interrupted one. |
| The agent needs you to authenticate again | Use the agent settings in `quirkbench setup`, then resume the existing session. Credentials are not entered on the target. |
| Space is running low | Pause and inspect storage usage. Remove disposable build caches through Quirkbench's maintenance screen or add controller storage. Never manually delete target evidence or retained deployments. |

Recovery updates are occasional explicit releases, independent of experimental OS
updates. Before replacing or reflashing media, upload or back up pending evidence.
Keep a working recovery drive until the replacement has passed its first boot check.

## Contributing

Start with the [architecture](docs/architecture.md), [roadmap](docs/product-roadmap.md)
and [implementation briefs](docs/implementation-handoff.md). The
[recovery image decision](docs/recovery-base.md) describes how release media is built.
Follow the [testing policy](docs/testing-policy.md): focused software tests during
development, with expensive image and hardware qualification reserved for explicit
release work. Product use does not require running the project's release test suite.

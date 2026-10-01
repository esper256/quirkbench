# Quirkbench

**Turn a reproducible Linux problem into a patch, with an experiment history you can inspect.**

> [!WARNING]
> **This is the manual for the intended finished product. Quirkbench does not
> currently work this way end to end.** Commands, screens and release packaging
> below describe the experience we want to build, including proposed command
> names. They are not a claim of available features or tested hardware support.
> For the software you can run today, see [controller installation](docs/controller-installation.md)
> and the [current agent guide](docs/agent-guide.md#current-implemented-commands).
> This manual is the destination; the [implementation roadmap](docs/product-roadmap.md)
> and [command checklist](docs/installation-to-patch.md) track the work to reach it.

Quirkbench gives a coding agent a persistent lab for investigating a Linux computer.
The agent reads source, forms hypotheses and writes changes. Quirkbench builds those
changes, runs approved experiments on the computer, and brings the results back.
You supply the problem, approve physical experiments and contribute observations
that software cannot make.

An investigation can span many builds, reboots and agent conversations. Its source
changes, hypotheses and evidence remain available when you return. A successful
investigation ends with a reviewable Linux kernel patch or patch series, reproduction
instructions and the evidence supporting the fix. An inconclusive investigation
ends with a useful record of what was tried and what is still unknown.

**In this manual:** [Equipment](#1-prepare-your-equipment) ·
[Installation](#2-install-quirkbench-on-the-controller) ·
[Recovery drive](#3-prepare-the-external-drive) ·
[Connect the target](#4-connect-and-name-the-target) ·
[Start an investigation](#5-open-an-investigation) ·
[Work with an agent](#6-hand-the-investigation-to-your-agent) ·
[Run experiments](#7-review-and-run-experiments) ·
[Monitor and resume](#8-monitor-recover-and-resume) ·
[Export a patch](#9-produce-the-kernel-patch-and-report) ·
[Troubleshooting](#troubleshooting)

## How the lab works

You use two computers:

- The **controller** runs Quirkbench and your coding agent. It keeps source
  workspaces, builds, results and the investigation history.
- The **target** is the computer with the problem. It boots Quirkbench from an
  external drive and runs experiments on its real hardware.

The external drive contains a fixed **recovery environment** and space for
experimental systems and evidence. Recovery connects to the controller, prepares
the next approved experiment and receives the target again afterward:

```text
Describe the problem → establish a baseline → propose a change
                              ↑                     ↓
                         read evidence ← build and test

Each target run: recovery → experimental system → recovery
```

The target's installed Linux system is left in place. Quirkbench does not mount its
internal filesystems or install experimental kernels into it. Experiment kernels
use a reviewed protection profile that excludes internal storage controllers.
A problem that requires access to those controllers or filesystems is outside this
workflow. These controls reduce accidental damage; an experimental kernel is
privileged code, so source review and physical experiment approval still matter.

Quirkbench is independent of the coding agent you choose. Codex or another agent
needs shell access to the controller and access to the investigation's source
workspace. A chat application with no access to those files and commands cannot
operate the lab by itself.

## 1. Prepare your equipment

| You need | What to check |
| --- | --- |
| A Linux controller | Use a controller platform supported by the selected release. Setup checks its service manager, rootless container support and available resources. |
| Build storage | Allow roughly 200 GiB to start, plus room for retained builds and backups. Setup estimates the space needed for your selected sources and keeps a free-space reserve. |
| The target computer | It must support the release's external boot method. Check the release's architecture, firmware and peripheral limitations before preparing media. |
| An external drive | A USB SSD is a practical choice for the USB boot workflow. Start with about 256 GB; large-memory targets or long recordings can need more. The drive will be erased. |
| A network connection | The target must reach the controller. Wired networking is usually easiest. Both can use a suitable local Wi-Fi network; client isolation must not prevent communication. |
| A coding agent | Use your existing agent on the controller. Its account, authentication and usage charges are managed by that application. |

Keep both computers powered and the controller awake while work is running. The
target will be unavailable for ordinary use during experiments. Plan to stay nearby
for the initial setup and baseline runs.

The target's installed distribution does not have to match the debugging environment.
That difference can affect reproduction, however. Quirkbench records the environment
used for every result; booting successfully is not evidence that the original problem
has been reproduced.

Support is specific to a release and an experiment. A computer may boot recovery
successfully while a particular device, sleep mode or reset mechanism remains
unsupported. Quirkbench reports those limits before starting the affected work.

## 2. Install Quirkbench on the controller

Download the controller archive for your platform from
[Quirkbench Releases](https://github.com/esper256/quirkbench/releases). Follow that
release's signature-verification instructions before running the installer. A
checksum verifies the downloaded bytes; release signature verification establishes
who published them.

Extract the archive and run the included installer. In this example, replace
`ARCH` with the architecture named in your download:

```sh
tar -xf quirkbench-controller-linux-ARCH.tar.gz
./quirkbench/install
quirkbench setup
```

The installer adds Quirkbench to your user account and explains any required PATH
change. Setup then walks through four decisions:

1. **Where to keep the lab.** Choose persistent storage for source workspaces,
   builds and evidence. This location is independent of any agent chat or checkout.
2. **How much resource to use.** Set CPU, memory and storage limits. Setup prepares
   the isolated build environment and lists any controller packages you must install.
3. **How the service should run.** Start the controller service and choose whether
   it may continue after logout. Closing a terminal does not stop submitted work.
4. **How the target connects.** Select a reachable controller address. Setup creates
   its connection identity, checks the endpoints and explains any necessary firewall
   changes for you to apply.

You can rerun setup after an interruption. It resumes completed preparation and
preserves the controller's identity. Your agent credentials remain in your normal
agent environment; they are not copied to the target.

Check the result:

```sh
quirkbench status
```

Proceed when controller setup is ready. Build tools run in isolated containers;
you do not need to create a development container or learn kernel packaging to start
a supported investigation.

## 3. Prepare the external drive

Download a verified recovery image:

```sh
quirkbench recovery download
```

Choose the **target's** platform when prompted. Quirkbench verifies the release
signature and image checksum, checks controller compatibility and prints the
image's saved location.

Use a standard disk-image writer to write that image to the external drive. Check
the selected drive's model and capacity, then let the writer complete verification.
**Writing the image erases the selected drive.**

Connect the drive to the target and select it in the target's boot menu. Follow the
recovery release's firmware and Secure Boot requirements using the computer's normal
firmware controls. Quirkbench does not change those settings for you.

On its first boot, recovery asks you to confirm the external drive and the space
allocated to experiments and evidence. It accounts for the target's memory and
the selected log budget. If the drive is too small or its identity is ambiguous,
setup explains what must change before it can proceed.

You normally write this image once. Later experiments transfer new experimental
systems to the drive while keeping recovery intact.

## 4. Connect and name the target

On the target, open **Network** and connect to the controller's network.

On the controller, start pairing:

```sh
quirkbench target add target-01
```

`target-01` is a name you choose. The command displays the controller address,
its identity fingerprint and an expiring pairing code. Enter these through the
target's **Connect to controller** screen. Compare the fingerprint displayed on
both computers before confirming.

Recovery saves its private connection settings on the external drive and reports
the target's hardware. Check the result on the controller:

```sh
quirkbench target show target-01
```

The report distinguishes:

- **Connected in recovery:** the controller can communicate with this recovery boot.
- **Experiments available:** a supported baseline and protection profile can be
  prepared for the target.
- **Attended or unattended:** which recovery/reset behavior has actually been checked.

Pairing alone starts no experiment. If support is missing, the report identifies the
blocker and saves the hardware information needed to investigate it. An agent cannot
make the target eligible by ignoring that blocker.

Leave the drive with this target during the investigation. Moving it to another
computer requires explicit reassignment; old results and permissions do not transfer
to the new machine.

## 5. Open an investigation

An investigation holds one problem, its source workspace and its accumulated
reasoning and results. Create one:

```sh
quirkbench investigation start first-fix --target target-01
```

Use any name in place of `first-fix`. The wizard asks you to describe:

- What happens, and what you expected instead.
- The steps and circumstances that trigger the problem.
- How often it occurs, including known successful cases.
- The installed distribution and kernel version, if known.
- Earlier working versions, relevant logs and workarounds you have already tried.
- What you can observe or do physically during a test.

You can write this description beforehand and pass `--problem ./problem.md`.
Distinguish observations from suspected causes: “the link disappears after this
sequence” is more useful than asserting that a particular driver must be broken.

### Select a baseline and sources

Quirkbench proposes a supported baseline: exact kernel sources, configuration,
userspace and diagnostic tools that can run on the target. Review how it differs
from the system where the problem occurs. Supplying a kernel version helps select
inputs; it does not let Quirkbench reconstruct your installed system automatically.

Accept a suitable baseline, or select another supported version. If you already
maintain a kernel checkout, choose **Use existing source** and provide its path and
base revision. Quirkbench checks that a supported build recipe can handle it and
creates a separate investigation workspace. Your original checkout is preserved.

The investigation records the chosen base revision and prepares an editable source
workspace for the agent. Downloads and builds show progress without needing an
agent to watch them. If a pinned input is unavailable, preparation stops with that
missing input identified rather than substituting a newer version.

The wizard also asks for practical limits: maximum test duration, repetitions,
resource use and whether you can attend the target. **Attended operation is the
starting choice.** Creating an investigation does not grant permission to boot
new experimental code.

### Check the lab before debugging

Quirkbench prepares a first baseline attempt. Review and approve it using the
process in step 7. Stay with the target for this round trip:

1. Boot the baseline.
2. Check that the required devices and diagnostic tools are available.
3. Collect a small result and upload it.
4. Return to recovery.

This establishes that the lab can run an experiment and preserve its evidence.
The next task is to reproduce your reported problem under recorded conditions.
Keep those two questions separate.

## 6. Hand the investigation to your agent

Print the investigation's agent handoff:

```sh
quirkbench investigation brief first-fix
```

The output gives you the source workspace, a handoff file and a ready-to-copy prompt.
Open that workspace in Codex or your preferred coding agent. Give it the prompt,
which asks it to do the following:

> Work on Quirkbench investigation first-fix. Read the handoff file at the path
> supplied by Quirkbench and follow its agent guide. Read the existing evidence
> before starting more work. First establish a reproducible baseline. Then propose
> a test that distinguishes likely causes, make source changes when justified,
> and submit experiments through Quirkbench. Record hypotheses, rejected approaches
> and conclusions in the investigation. When submitted work is still running,
> return its operation ID and yield. Ask me for physical observations and approvals
> when needed.

Quirkbench's installed agent guide supplies the actual commands and proposal
formats. You do not need to teach the agent a kernel build command, relay target
logs into the chat or maintain an experiment spreadsheet.

The agent can read context, inspect evidence, choose an eligible diagnostic recipe,
edit source and submit a proposed experiment. A recipe describes a bounded test:
its inputs, permitted actions, timeout, collected measurements and any observations
you must supply. Missing diagnostics may require a new reviewed recipe before the
investigation can continue.

Before building, Quirkbench captures an immutable source revision. For uncommitted
edits, the agent stops writing while the capture completes. The resulting experiment
keeps those exact bytes even if the workspace changes later.

Quirkbench runs submitted builds independently of the agent conversation. When
results are ready, continue your agent with a prompt such as:

> Continue first-fix. Read the new Quirkbench results and decide what they imply
> before proposing the next experiment.

Closing the chat does not cancel submitted lab work. In this external-agent
workflow, the next reasoning step waits for you to continue an agent, unless your
agent application has an explicitly configured completion-event integration.

A fresh agent can use the same handoff later. It reads the saved history rather
than depending on another conversation's memory. Use one agent or editor at a time
for the investigation workspace.

## 7. Review and run experiments

An experiment connects a question to a source revision, a diagnostic procedure and
an expected observation. One experiment may need several physical **attempts**.

When a build is ready, Quirkbench asks you to review it:

```sh
quirkbench experiment review EXPERIMENT_ID
```

Use the ID shown in the notification or monitor. The review presents the hypothesis,
source changes, exact built candidate, target, procedure, duration, observations
requested from you and recovery limitations. Changes affecting storage protection,
boot or privileges require independent review before they become eligible.

Approve the specific prepared attempt when you are ready:

```sh
quirkbench attempt approve ATTEMPT_ID
```

Approval applies to that exact candidate and attempt. A changed patch or a new
attempt needs its own authorization. If you cannot attend or the proposed test is
unclear, leave it waiting; a completed build does not start the target by itself.

Quirkbench transfers the candidate, boots it once, runs the selected procedure,
retains its results and returns to recovery. It saves the source and build identities,
logs, measurements and outcome together. Matching debug symbols remain available
for investigating failures. Recovery is maintained separately from candidate changes.

Some evidence requires a person: operating a physical control, connecting a
peripheral, or observing whether the reported behavior occurred. Read and answer
the pending request:

```sh
quirkbench investigation respond first-fix
```

The command shows the relevant attempt and asks for the specific observation.
Report what you saw, including uncertainty. Missed observations remain missing;
an answer to an earlier attempt is not treated as evidence for a later one.

### Build evidence for a fix

Ask the agent to work toward a comparison you can explain:

1. **Baseline:** reproduce the problem with a recorded procedure.
2. **Diagnostic experiment:** collect evidence that narrows the likely cause.
3. **Patched candidate:** apply a focused change and repeat the same procedure.
4. **Regression checks:** check related behavior that the change might affect.
5. **Revert comparison, where practical:** remove the fix and test whether the
   original behavior returns.

For intermittent problems, agree on repetitions and exposure before interpreting
the results. Quirkbench records counts, conditions and failed observations; it
does not turn one successful run into proof.

If the problem does not reproduce, compare the debugging environment with the
reported system and record the differences. The outcome may be a better reproducer,
a diagnosis outside the kernel, or an unresolved limitation. Patch generation is
useful only when the proposed change addresses evidence you actually have.

## 8. Monitor, recover and resume

Open the monitor whenever you want:

```sh
quirkbench monitor first-fix
```

It shows the current question, running operation, build or transfer progress,
target state, latest evidence and anything waiting for you. Monitoring does not
invoke an AI. Closing the monitor leaves work running.

For a snapshot:

```sh
quirkbench investigation status first-fix
```

| What you see | What to do |
| --- | --- |
| Building or transferring | Let the bounded operation run. Progress shows measurements and the last activity time. |
| Awaiting approval | Review the prepared experiment and approve its attempt when ready. |
| Awaiting observation | Answer the named request, or record that you could not make the observation. |
| Results ready | Continue your agent and ask it to interpret the new evidence. |
| Recovery needed | Follow the displayed reset instructions and boot the external drive back into recovery. |
| Paused or blocked | Read the reason, resolve it, then resume the existing investigation. |

A failed boot or hard hang may require you to reset the target. A validated watchdog
can cover some failures, but cannot guarantee recovery from every failure or capture
a crash dump. Quirkbench reports which logs survived and which parts of the run are
unknown. It reconciles the interrupted attempt before permitting another one;
loss of contact never causes a blind repeat.

If the network disappears, the target keeps produced evidence on the external
drive and uploads it when the controller becomes reachable. Do not reflash the
drive to repair a connection problem.

### Stop for the day

```sh
quirkbench investigation pause first-fix
```

Pause prevents new work from starting. Active bounded work finishes or reaches its
declared stopping condition; a running target attempt returns through recovery.
The status distinguishes stopping new work, draining workers, target recovery and
pending uploads. Also stop your external agent from editing the workspace.

To turn off the target:

```sh
quirkbench target poweroff target-01
```

Wait for confirmed shutdown before disconnecting the external drive. If the
controller is unavailable, use recovery's local shutdown screen. Evidence may be
saved safely on the drive while its upload is still pending; keep the drive intact.

Return later with:

```sh
quirkbench investigation resume first-fix
```

After a controller restart, investigations remain paused until outstanding work
has been reconciled and you resume them. Then continue your agent using the same
handoff. Sleep and power loss preserve recorded progress but cannot keep a build
executing while the controller is unavailable.

## 9. Produce the kernel patch and report

When the evidence supports a fix, ask your agent to prepare it for review:

> Prepare the final kernel patch series for first-fix against its recorded base
> revision. Separate temporary diagnostics from the fix, explain the cause and why
> the change addresses it, and cite the relevant experiments. Identify regression
> coverage and unresolved limitations. If cleanup changes the tested source,
> submit a final validation experiment before declaring the series ready.

The agent writes the patch and commit messages. Quirkbench connects that work to
the tested sources and evidence. A cleanly building patch is not automatically a
tested fix.

Inspect the investigation summary and export it:

```sh
quirkbench investigation report first-fix
quirkbench investigation export first-fix --output ./first-fix-results
```

The export is a review package:

```text
first-fix-results/
  README.md          How to read and reproduce the investigation
  report.md          Diagnosis, comparisons, results and limitations
  patches/           Kernel patches in git format-patch format
  reproduce/         Exact base revision and recorded test instructions
  experiments/       Experiment and attempt records
  evidence/          Selected logs and measurements
```

The report identifies the source base, final tested revision, kernel configuration,
relevant userspace and hardware, repetitions and any missing observations. It
distinguishes an evidence-supported fix from an unvalidated patch or an inconclusive
investigation. You can export an unfinished investigation too.

Check the patch against a separate clean kernel checkout at the recorded base:

```sh
git switch --detach BASE_COMMIT
git switch -c review-quirkbench-fix
git am /absolute/path/to/first-fix-results/patches/*.patch
```

Replace `BASE_COMMIT` and the path with the values in the export. Applying the
patch confirms that it applies to those sources; it does not retest the hardware.
A distribution-specific fix may still need adaptation and new testing against the
upstream tree before submission.

Review the patch, reproduction instructions and report yourself. Exports exclude
controller credentials and saved network secrets, but diagnostic logs may contain
identifying information. Inspect what you intend to share.

You can now send the patch and supporting report to a maintainer, continue testing
on another target, or retain the package for your own work. Quirkbench does not
publish patches or install them into your normal operating system automatically.

## Keep the lab reusable

### Back up before moving or upgrading it

An export is for review and sharing. A backup preserves the state needed to continue
work, including retained source workspaces and artifacts:

```sh
quirkbench backup --output /path/to/backup-directory
```

Stop external editors when asked. The backup reports which workspaces were captured,
whether a target still holds evidence that has not uploaded, and how to preserve
private controller credentials separately. It cannot include evidence that exists
only on an offline drive.

Restore into a new state location with `quirkbench restore`. The restore wizard
checks private configuration and reconnects targets before allowing paused
investigations to resume.

Recovery updates are explicit maintenance, separate from experimental kernel
updates. Upload or back up pending evidence before replacing recovery media.

### Optional: let Quirkbench invoke the agent

You can keep using an interactive coding agent for the entire investigation.
For a supported agent with a noninteractive command interface, Quirkbench can
instead invoke it when a decision is needed:

```sh
quirkbench agent configure
quirkbench investigation pause first-fix
quirkbench investigation driver first-fix --managed
```

Configure authentication, a usage budget and a spending-limit policy. Complete the
handoff from the external editor before resuming. Quirkbench calls the managed
agent for new results, actionable failures or human responses; the agent exits
while builds and experiments run. Authentication or usage-limit failures pause
the investigation.

Managed invocation uses the same source workspace, experiment records and approval
rules. It does not make the target eligible to run unattended. Quirkbench cannot
meter or control calls you make independently through another agent application.

### Optional: authorize unattended experiments

Assess the target's reset behavior through a separate attended qualification:

```sh
quirkbench target qualify target-01
```

This guided process explains and runs deliberate failure/reset trials. The report
states what it demonstrated and what still needs a person. Where the target and
test are eligible, you can explicitly authorize a bounded unattended plan naming
the approved candidates, attempts, limits and stop conditions. Permission does not
extend to future patches the agent has not written yet.

If qualification cannot cover the required failure modes, keep the investigation
attended. Managed AI, successful pairing and a working baseline do not replace
hardware recovery checks.

## Troubleshooting

| Problem | Next step |
| --- | --- |
| Setup cannot prepare the controller | Rerun `quirkbench setup` and follow the named dependency, service or capacity correction. Completed setup steps are retained. |
| Recovery will not boot | Check image verification, target-platform support and firmware boot settings. Save any visible boot error; an agent cannot diagnose an unreachable target without observations. |
| The target cannot connect | Check the controller address, firewall and network isolation. Use recovery's network screen. Never bypass an identity mismatch to make pairing succeed. |
| Recovery has no usable network device | Use a device supported by that recovery release, or obtain a release with the required support. A new kernel experiment cannot run before the lab has a working connection. |
| The problem vanishes in the baseline | Record an inconclusive reproduction result and compare kernel, userspace, firmware and test conditions with the reported system. |
| A build fails | Continue the agent with the build result. The failed build has logs and source identity; it has not become a target experiment. |
| The target boots its installed OS after a reset | Select the external drive through the normal boot menu and let recovery reconcile the attempt. |
| Storage is nearly full | Use `quirkbench storage` to review usage, retention and disposable caches. Add storage or remove eligible caches; do not manually delete evidence or active builds. |
| You want to change agents | Pause, finish the workspace handoff, then give the new agent the existing investigation brief. |
| No supported fix was found | Export the investigation. A reproducible failure, narrowed cause and clear account of failed approaches are useful results. |

## Developing Quirkbench

This README defines the desired user experience. Use the
[documentation index](docs/README.md) to find current operating guides. The
[roadmap](docs/product-roadmap.md), [implementation handoff](docs/implementation-handoff.md)
and [contracts](docs/implementation-contracts.md) describe implementation work and
its current limits. Changes to the future CLI shown here do not silently rename
existing commands or stored protocol fields.

Use the [testing policy](docs/testing-policy.md) for development validation.

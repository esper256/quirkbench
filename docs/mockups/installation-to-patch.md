# Installation to investigation patch

> **EXAMPLE ONLY — intended design, not current functionality.** Commands, screens
> and results are illustrative. Codex, Wi-Fi and this kernel problem are examples,
> not requirements or limits on supported agents, connections or investigations.

## Controller computer · Install

The user downloads a Quirkbench release archive for their Linux computer.

```console
$ tar -xf quirkbench-linux-x86_64.tar.gz
$ cd quirkbench-linux-x86_64
$ ./install
Installed: ~/.local/bin/quirkbench

Next: quirkbench setup

$ quirkbench setup
Press Enter to accept the value in brackets.

Address your target computers will connect to [https://192.168.1.20:7443]:

Controller config saved: ~/.quirkbench/config.toml

Next: quirkbench controller run

$ quirkbench controller run
Quirkbench controller listening at https://192.168.1.20:7443
Keep this terminal open. Ctrl+C stops the controller.

In another terminal: quirkbench recovery build
```

## Controller computer · Prepare USB

```console
$ quirkbench recovery build
Quirkbench recovery image

✓ Download packages
  Build recovery system          00:38 elapsed
  Latest activity: preparing boot files, 2 seconds ago

Log: ~/.quirkbench/logs/recovery-build.log
Ctrl+C Cancel build
```

```console
✓ Recovery image ready: recovery-1

Next: quirkbench recovery flash

$ quirkbench recovery build
Recovery image recovery-1 already matches these build settings.
No build needed. Use --force to rebuild.

$ quirkbench recovery flash
┌─ Prepare a Quirkbench USB ─────────────────────────────────┐
│ Recovery image: recovery-1                                │
│ Controller: https://192.168.1.20:7443                      │
│                                                          │
│ Select the USB drive to erase                             │
│ > USB Flash Drive     32 GB     /dev/sdb                   │
│                                                          │
│ Internal system disk excluded from selection.             │
│                                                          │
│ ↑↓ Select     Enter Continue     Esc Cancel               │
└──────────────────────────────────────────────────────────┘

Erase USB Flash Drive, 32 GB, /dev/sdb? [y/N] y
[sudo] password for sam:

✓ Write recovery system
✓ Prepare shared space for experiments, evidence and settings
✓ Save controller address and pairing credentials on USB
✓ Check written data and finish disk writes

USB ready. Boot your target computer from this drive.
```

## Target · First boot

The user inserts the USB drive into the target computer and boots from it.

```text
┌─ Quirkbench ─────────────────────────── Recovery system ──┐
│                                                          │
│ Welcome. Connect to your network to get started.          │
│                                                          │
│ Network       Not connected                              │
│ Controller    https://192.168.1.20:7443                    │
│ Connection    Waiting for network                        │
│                                                          │
│ > Connect to a network                                   │
│   Controller connection                                  │
│   Troubleshooting                                        │
│   Open terminal                                          │
│   Power                                                  │
│                                                          │
│ ↑↓ Select     Enter Open     T Terminal                   │
└──────────────────────────────────────────────────────────┘
```

```text
┌─ NetworkManager · Activate a connection ──────────────────┐
│                                                          │
│ Wi-Fi                                                    │
│ > Workshop Wi-Fi                       ▂▄▆█             │
│   Guest                                ▂▄▆_             │
│                                                          │
│ [Activate]                                      [Back]  │
└──────────────────────────────────────────────────────────┘

Password for Workshop Wi-Fi: ************

[Connect]  [Cancel]

Connected to Workshop Wi-Fi. Saved for the next boot.
```

```text
┌─ Quirkbench ─────────────────────────── Recovery system ──┐
│                                                          │
│ Ready. Start an investigation on your controller computer.│
│                                                          │
│ Target        lab-laptop                                 │
│ Network       Workshop Wi-Fi                             │
│ Controller    Connected · https://192.168.1.20:7443        │
│ Pairing       Complete                                   │
│                                                          │
│ > Network                                                │
│   Controller connection                                  │
│   Troubleshooting                                        │
│   Open terminal                                          │
│   Power                                                  │
│                                                          │
│ ↑↓ Select     Enter Open     T Terminal                   │
└──────────────────────────────────────────────────────────┘
```

## Controller computer · Start an investigation

```console
$ quirkbench target list
TARGET        CONNECTION    SYSTEM
lab-laptop    Connected     Recovery system

$ quirkbench investigation start wake-wifi --target lab-laptop
What problem do you want to investigate?
> Wi-Fi sometimes fails after waking from sleep.

┌─ Start investigation ─────────────────────────────────────┐
│ Problem                                                  │
│ Wi-Fi sometimes fails after waking from sleep.            │
│                                                          │
│ Target                       lab-laptop                  │
│ Candidate source             Supported Fedora kernel    │
│                              [Choose version]            │
│ Agent may change             Kernel and Wi-Fi test code  │
│                                                          │
│ The agent can build, reboot and run tests within this     │
│ scope until you pause the investigation.                  │
│                                                          │
│ Internal disks and persistent firmware changes excluded. │
│ Experimental code can crash; protections are best effort. │
│                                                          │
│ [Authorize and start]  [Cancel]                           │
└──────────────────────────────────────────────────────────┘

Investigation wake-wifi started.
Preparing candidate source… 84 MiB downloaded
✓ Candidate source ready; unchanged baseline saved.
Candidate source: ~/quirkbench-work/wake-wifi/linux

Give your agent this guide: ~/quirkbench-work/wake-wifi/INVESTIGATION.md
Watch: quirkbench monitor wake-wifi
```

The user runs Codex and gives the agent this prompt:

```text
Investigate wake-wifi using Quirkbench. Reproduce the Wi-Fi failure after
sleep, find its cause, and test a fix. First read
~/quirkbench-work/wake-wifi/INVESTIGATION.md.
Leave me an investigation patch and a summary of the evidence.
```

## Agent · Reproduce the problem

```console
$ quirkbench investigation show wake-wifi
wake-wifi · Authorized

Target: lab-laptop
Problem: Wi-Fi sometimes fails after waking from sleep.
Changes allowed: Kernel and Wi-Fi test code
Experiment time limits: Supplied by the agent for each run

Candidate source: ~/quirkbench-work/wake-wifi/linux
Baseline: Saved unchanged code and build settings
Agent guide: ~/quirkbench-work/wake-wifi/INVESTIGATION.md
Build instructions: ~/quirkbench-work/wake-wifi/BUILD.md
Deployment requirements: ~/quirkbench-work/wake-wifi/DEPLOY.md
Test programs: ~/quirkbench-work/wake-wifi/tests/

Submit: quirkbench experiment submit wake-wifi --help
Results: quirkbench investigation results wake-wifi

$ cat ~/quirkbench-work/wake-wifi/INVESTIGATION.md
# Investigation wake-wifi

Problem: Wi-Fi sometimes fails after waking from sleep.
Target: lab-laptop
Authorized changes: Kernel and Wi-Fi test code
Do not modify Quirkbench, internal disks or persistent firmware.

Work in this directory. Candidate source is in ./linux/.
Read BUILD.md for build commands and DEPLOY.md for required output files.
Use ./tests/README.md for candidate hooks and example test programs.

On the target, candidate hooks receive:
  QUIRKBENCH_EVIDENCE_DIR   Save logs and results to return to the controller.
  QUIRKBENCH_WORK_DIR       Use for temporary files; cleared after the run.

Use these supplied paths, not guessed USB locations. Leave recovery settings,
boot-selection files and Quirkbench-managed files alone. Submit a new candidate
to change installed software. Recovery uploads evidence and deletes the USB copy
only after the controller confirms it is stored. Temporary files are not results.

1. Read target information with quirkbench target show lab-laptop.
   Collect diagnostics and try to reproduce the problem before fixing it.
   Write a test and choose a time limit long enough for it to finish.
2. Test unchanged software with --source baseline. Quirkbench builds it
   or reuses a matching build. Compile any new test programs yourself.
3. For changed software, edit ./linux/ and run ./build-candidate.sh.
   The script builds replacement RPMs for Quirkbench to deploy.
   Read ./build.log and fix build errors yourself. Reuse the build
   directory so normal build tools can rebuild only affected files.
4. Submit changed software with --files ./output --source ./linux.
   Submit the completed output published by the build script. Quirkbench
   copies it with its recorded code and settings; later builds stay separate.
5. Read results and evidence, then decide the next experiment.

Example baseline submission:
  quirkbench experiment submit wake-wifi --source baseline \
    --test ./check-wifi-after-sleep.sh --timeout 15m --request-id reproduce-1

Choose a new request ID for each new experiment. Retrying the identical
submission with the same ID returns the existing experiment.
Quirkbench assembles and deploys supplied files; it does not fix or
rebuild your changed code. A completed run is not proof of a fix.

Use quirkbench experiment submit --help for all submission options.
```

The agent reads target system information and diagnostics, inspects the candidate source, and writes a suspend-and-Wi-Fi test to reproduce the problem.

```console
$ cd ~/quirkbench-work/wake-wifi
$ quirkbench experiment submit wake-wifi --source baseline \
    --test ./check-wifi-after-sleep.sh --timeout 15m --request-id reproduce-1
Accepted: reproduce-1
Capturing test program. Preparing the baseline build.

Wait: quirkbench experiment wait wake-wifi --request-id reproduce-1
Status: quirkbench experiment status wake-wifi --request-id reproduce-1
Watch: quirkbench monitor wake-wifi
Read next: ~/quirkbench-work/wake-wifi/instructions/wait.md

$ quirkbench experiment status wake-wifi --request-id reproduce-1
reproduce-1 · Preparing baseline
Elapsed: 03:42
Latest activity: preparing NetworkManager package, 1 second ago
Stock packages: Downloaded; using cached copies

Run time limit: 15 minutes, selected by the agent.
The run will start automatically within the authorized scope.
Read next: ~/quirkbench-work/wake-wifi/instructions/wait.md
```

## Controller computer · Monitor

```console
$ quirkbench monitor wake-wifi
┌─ Quirkbench · wake-wifi ───────────────────────────────────┐
│ Running baseline test                        Run 1       │
│                                                          │
│ ✓ Capture   ✓ Build   ✓ Send   ● Run   ○ Recover ○ Upload │
│                                                          │
│ Target            lab-laptop                             │
│ Test              Wi-Fi after sleep                      │
│ Progress          Cycle 6 of 20                           │
│ Run elapsed       02:10 / 15:00 limit                     │
│ Latest activity   Wi-Fi reconnected, 2 seconds ago        │
│ Evidence received 1.8 MiB                                │
│                                                          │
│ 14:32:08  Cycle 6: waking                                 │
│ 14:32:11  Cycle 6: Wi-Fi connected                         │
│ 14:32:12  Cycle 6: connection check passed                 │
│                                                          │
│ P Investigation pause    F Force stop    L Logs           │
│ Q Close monitor (investigation continues)                │
└──────────────────────────────────────────────────────────┘
```

## Target · Experiment running

```text
┌─ Quirkbench ───────────────────────────────── Experiment ─┐
│                                                          │
│ Testing Wi-Fi after sleep                                │
│ Investigation     wake-wifi                              │
│ Run               1 · Baseline                           │
│ Progress          Cycle 6 of 20                           │
│ Elapsed           02:10 / 15:00 limit                     │
│                                                          │
│ Next: sleep, wake, then check Wi-Fi.                      │
│ The screen may turn off during this test.                │
│                                                          │
│ Controller        Connected                              │
│ Latest result     Cycle 6 passed                         │
│                                                          │
│ P Investigation pause    F Force stop    L Logs           │
└──────────────────────────────────────────────────────────┘
```

## Target · Return to recovery

```text
┌─ Quirkbench ─────────────────────────── Recovery system ──┐
│ Collecting results · Run 1                               │
│                                                         │
│ Test          Wi-Fi failed on cycle 9                    │
│ Evidence      Uploading 12 of 18 MiB                     │
│ Controller    Connected                                 │
│                                                         │
│ Keep this computer on until the upload finishes.         │
│                                                         │
│ L Logs    T Terminal                                    │
└─────────────────────────────────────────────────────────┘
```

```text
┌─ Quirkbench ─────────────────────────── Recovery system ──┐
│ Ready for the next experiment                            │
│                                                         │
│ Run 1 results saved on controller.                       │
│ Uploaded USB copy and temporary working files removed.  │
│                                                         │
│ Network       Workshop Wi-Fi                            │
│ Controller    Connected                                 │
│                                                         │
│ > Network                                               │
│   Controller connection                                 │
│   Troubleshooting                                       │
│   Open terminal                                         │
│   Power                                                 │
│                                                         │
│ ↑↓ Select     Enter Open     T Terminal                  │
└─────────────────────────────────────────────────────────┘
```

## Agent · Inspect evidence and test a change

```console
$ cat ~/quirkbench-work/wake-wifi/instructions/wait.md
# Wait for your experiment

Use quirkbench experiment wait with the request ID returned by submission.
It stays quiet until results are available, your attention is needed, or
its wait time ends. Do not repeatedly request status or stream logs to wait.

If it returns pending, wait again with the same request ID. Ending the
wait does not stop the experiment. Use status or logs to investigate a problem.
Follow the next instruction path returned by the command.

$ quirkbench experiment wait wake-wifi --request-id reproduce-1
Run 1 finished. Evidence received.
Results: quirkbench experiment results wake-wifi --request-id reproduce-1
Read next: ~/quirkbench-work/wake-wifi/instructions/results.md

$ cat ~/quirkbench-work/wake-wifi/instructions/results.md
# Use the results

Read the result summary and files returned by experiment results.
Check for missing evidence and uncertain outcomes before drawing conclusions.
Decide whether to change candidate source or design another test.

For changes, follow BUILD.md and DEPLOY.md in the investigation workspace.
Submit a new experiment with a new request ID, then follow its instructions.
When finished, create the investigation patch with Git and pause the investigation.

$ quirkbench experiment results wake-wifi --request-id reproduce-1
Run 1 · Returned to recovery system
Test result: Wi-Fi connection failed on cycle 9 of 20
Evidence: Received; kernel log, test output and wireless trace

Files: ~/quirkbench-work/wake-wifi/results/run-1/
Read next: ~/quirkbench-work/wake-wifi/instructions/results.md
```

The Quirkbench user's agent reads the evidence and edits the candidate source.

```console
$ cd ~/quirkbench-work/wake-wifi
$ cat BUILD.md
Build changed kernel software:
  ./build-candidate.sh

This script uses the supplied build tools and settings.
Build output: ./output/ (replacement RPMs and debugging files)
Deployment requirements: ./DEPLOY.md

$ cat DEPLOY.md
For a changed kernel, supply:
  output/rpms/                    Kernel and matching module RPMs
  output/debug/vmlinux            Kernel with debugging symbols
  output/build.json               Package replacements, code and build settings

The supplied build script publishes this layout only after the build completes.
It records the code and settings used and keeps later builds separate.
Quirkbench combines these RPMs with the selected stock packages.
Debugging symbols stay on the controller computer.
Test programs may use the startup and before-recovery hooks in ./tests/.

$ ./build-candidate.sh
Building changed kernel code · 8 build tasks at a time
Build log: ./build.log
✓ Incremental build complete
✓ Replacement RPMs ready in ./output/rpms/

$ quirkbench experiment submit wake-wifi --files ./output \
    --source ./linux --test ./check-wifi-after-sleep.sh \
    --timeout 15m --request-id fix-1
Accepted: fix-1
Checking and capturing replacement RPMs, build records and test program.

Wait: quirkbench experiment wait wake-wifi --request-id fix-1
Read next: ~/quirkbench-work/wake-wifi/instructions/wait.md

$ quirkbench experiment status wake-wifi --request-id fix-1
fix-1 · Preparing candidate from stock and replacement packages
Capture complete. You may edit and build again.
Later changes will not affect this experiment.
Unchanged baseline files: Reusing files already on target
Changed files: 84 MiB to send
Read next: ~/quirkbench-work/wake-wifi/instructions/wait.md

$ quirkbench experiment wait wake-wifi --request-id fix-1
Run 2 finished. Evidence received.
Results: quirkbench experiment results wake-wifi --request-id fix-1
Read next: ~/quirkbench-work/wake-wifi/instructions/results.md

$ quirkbench experiment results wake-wifi --request-id fix-1
Run 2 · Returned to recovery system
Test result: 20 of 20 cycles passed
Evidence: Received; kernel log, test output and wireless trace

Files: ~/quirkbench-work/wake-wifi/results/run-2/
Read next: ~/quirkbench-work/wake-wifi/instructions/results.md
```

## Controller computer · Results

```text
┌─ Quirkbench · wake-wifi ───────────────────────────────────┐
│ Latest run complete                                      │
│                                                          │
│ RUN   SOFTWARE    TEST RESULT             EVIDENCE        │
│ 1     Baseline    Failed on cycle 9       Received        │
│ 2     Candidate   20 of 20 cycles passed  Received        │
│                                                          │
│ Target            lab-laptop · Recovery system           │
│ Work              Waiting for the agent's next test      │
│                                                          │
│ Enter Results    P Investigation pause    Q Close         │
└──────────────────────────────────────────────────────────┘
```

The agent reviews the results and creates an investigation patch with Git.

```console
$ quirkbench investigation pause wake-wifi
Investigation paused. No run is active.
Target lab-laptop is in the recovery system. Evidence received.

Resume: quirkbench investigation resume wake-wifi

$ cd ~/quirkbench-work/wake-wifi/linux
$ git add drivers/net/wireless/
$ git commit -m "wifi: restore device state after resume"
[fix-wake-wifi 83d1b92] wifi: restore device state after resume
 1 file changed, 8 insertions(+), 3 deletions(-)

$ git format-patch -1 --output-directory ../patches
../patches/0001-wifi-restore-device-state-after-resume.patch
```

```text
Agent:
The baseline lost Wi-Fi on cycle 9. With the change, all 20 cycles passed.
This supports the fix on this laptop; it does not establish other hardware support.

Investigation patch:
~/quirkbench-work/wake-wifi/patches/0001-wifi-restore-device-state-after-resume.patch

Evidence:
~/quirkbench-work/wake-wifi/results/run-1/
~/quirkbench-work/wake-wifi/results/run-2/

The investigation is paused. The laptop is in the recovery system.
```

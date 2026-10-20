# Installation to investigation patch

> **EXAMPLE ONLY — intended design, not current functionality.** Commands, screens
> and results are illustrative. The agent, Wi-Fi and this audio problem are examples,
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
Target SSH port [7444]:

Controller config saved: ~/.quirkbench/config.toml

Next: quirkbench controller run

$ quirkbench controller run
Quirkbench controller listening at https://192.168.1.20:7443
Target SSH: 192.168.1.20:7444
Agent MCP: http://127.0.0.1:7442/mcp
MCP token file: ~/.quirkbench/mcp-token
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
│ Select the external drive to erase                       │
│ > USB Flash Drive     32 GB     /dev/sdb                   │
│                                                          │
│ Internal system disk excluded from selection.             │
│                                                          │
│ ↑↓ Select     Enter Continue     Esc Cancel               │
└──────────────────────────────────────────────────────────┘

[sudo] password for sam:
WARNING: This will erase the entire drive and all its partitions.
Requested path: /dev/sdb
Resolved device: /dev/sdb
Model: USB Flash Drive
Serial: example-drive-01
Size: 32.0 GB
Existing layout:
  /dev/sdb   32.0 GB  not mounted
    /dev/sdb1  32.0 GB  exfat  not mounted
Type ERASE /dev/sdb to continue, or press Enter to cancel: ERASE /dev/sdb

✓ Write recovery system
✓ Prepare shared space for experiments, evidence and settings
✓ Save controller address and pairing credentials on USB
✓ Check written data and finish disk writes

USB ready. Boot your target computer from this drive.
```

## Target · First boot

The user inserts the USB drive into the target computer and boots from it.

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ QUIRKBENCH / RECOVERY                                                        │
├────────────────────────┬─────────────────────────────────────────────────────┤
│ MENU                   │ CURRENT STATUS                                      │
│ > N  Network           │ Target          lab-target (7c2a91)                 │
│   C  Controller        │ Network         Not connected                       │
│   A  Activity          │ Target IP       Not assigned                        │
│   L  Logs & reports    │ Controller      Waiting for network                 │
│   I  Target details    │ Last contact    Never                               │
│   T  Open terminal     │ Controller URL  https://192.168.1.20:7443           │
│   P  Power             │ Recovery OS     Read-only                           │
│                        │ USB free        21 GiB                              │
│                        │ Current work    Idle                                │
├────────────────────────┴─────────────────────────────────────────────────────┤
│ RECOVERY & MCP ACTIVITY                                           Live log   │
│ 14:31:50  RECOVERY  Recovery started                                         │
│ 14:31:52  RECOVERY  Shared USB storage ready                                 │
│ 14:31:54  RECOVERY  No saved network is available                            │
│ 14:31:54  RECOVERY  Waiting for a network connection                         │
│ Next: choose Network to connect this target.                                 │
│                                                                              │
├──────────────────────────────────────────────────────────────────────────────┤
│ ↑↓ Select   Enter Open   A Activity history   T Terminal   Esc Back          │
└──────────────────────────────────────────────────────────────────────────────┘
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
┌──────────────────────────────────────────────────────────────────────────────┐
│ QUIRKBENCH / RECOVERY                                                        │
├────────────────────────┬─────────────────────────────────────────────────────┤
│ MENU                   │ CURRENT STATUS                                      │
│   N  Network           │ Target          lab-target (7c2a91)                 │
│   C  Controller        │ Network         Workshop Wi-Fi                      │
│ > A  Activity          │ Target IP       192.168.1.81                        │
│   L  Logs & reports    │ Controller      Connected                           │
│   I  Target details    │ Last contact    1s ago                              │
│   T  Open terminal     │ Controller URL  https://192.168.1.20:7443           │
│   P  Power             │ Recovery OS     Read-only                           │
│                        │ USB free        21 GiB                              │
│                        │ Current work    Idle                                │
├────────────────────────┴─────────────────────────────────────────────────────┤
│ RECOVERY & MCP ACTIVITY                                           Live log   │
│ 14:31:52  RECOVERY  Shared USB storage ready                                 │
│ 14:31:54  RECOVERY  Connected to Workshop Wi-Fi                              │
│ 14:31:55  RECOVERY  Controller connected                                     │
│ 14:32:01  MCP       Read hardware / received                                 │
│ 14:32:02  MCP       Read hardware / finished                                 │
│ 14:32:04  MCP       Read boot log / finished                                 │
├──────────────────────────────────────────────────────────────────────────────┤
│ ↑↓ Select   Enter Open   A Activity history   T Terminal   Esc Back          │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Controller computer · Start an investigation

```console
$ quirkbench investigation start microphone-dropout --target lab-target
What problem do you want to investigate?
> Microphone capture stops after several minutes.

Allow the agent to change kernel, audio services and diagnostic programs? [Y/n]
Investigation authorized. Internal disks and persistent firmware are excluded.

Add this MCP server in your coding agent's settings:
  URL: http://127.0.0.1:7442/mcp
  Authorization header: Bearer <contents of ~/.quirkbench/mcp-token>
  Save these connection settings once. Keep the token out of agent prompts.

Give your agent this request:
  Use Quirkbench to investigate microphone-dropout. Reproduce the failure,
  inspect it interactively, test changes and leave an investigation patch.

Watch: quirkbench monitor microphone-dropout
```

The user copies the token into their client's MCP authentication settings, adds
the server, and gives their coding agent the displayed request.

```text
Agent · Quirkbench tools
✓ investigation_context       Read the problem and available tools
↻ rpm_ostree_prepare_candidate Prepare latest stable Bazzite baseline
✓ controller_work_wait        Baseline ready · Exact Bazzite version saved
✓ recovery_boot_ostree        Boot the baseline with fresh run files
↻ ssh_command_start           Record microphone · Running
✓ ssh_command_start           Inspect service logs while recording continues
```

## Controller computer · Monitor

```text
┌─ Quirkbench · microphone-dropout ────────────────────────┐
│ Investigating · Run 1                                   │
│                                                        │
│ Target       lab-target · Connected                     │
│ Software     Baseline with live changes                 │
│ Persistence  Live changes apply to this run only        │
│ Active       Recording microphone · 04:12 elapsed       │
│ Last action  Read service logs · 2 seconds ago           │
│ Evidence     Logs arriving · Recording still open       │
│ Timeout      Recording command · 6:48 remaining         │
│                                                        │
│ The agent can inspect and change this running system.   │
│                                                        │
│ P Pause    F Force stop    L Logs    Q Close monitor     │
└────────────────────────────────────────────────────────┘
```

## Target · Live investigation

```text
┌─ Quirkbench ────────────────────────────────────────────┐
│ Investigation active · microphone-dropout               │
│                                                        │
│ Controller    Connected                                │
│ Current work  Recording microphone                      │
│ Recent work   Inspecting audio service                  │
│ Software      Baseline with live changes                │
│ Network       Saved connection · Temporary changes      │
│ Recovery      Clean fallback ready                     │
│                                                        │
│ P Pause    F Force stop    L Logs                       │
└────────────────────────────────────────────────────────┘
```

```text
Agent · Quirkbench tools
✓ rpm_overlay_install      Add a diagnostic tool to this run
✓ controller_build_script_start  Build changed PipeWire RPMs
✓ rpm_overlay_install      Install those RPMs · This run only
✓ ssh_command_start           Restart the affected service
↻ ssh_command_start           Start another recording
↻ ssh_command_wait            Wait quietly for the new recording to finish

✓ rpm_ostree_prepare_candidate  Save baseline + selected PipeWire RPMs
✓ recovery_return             Upload evidence and finish this run
✓ recovery_boot_ostree        Boot the saved candidate for a clean repeat
```

## Controller computer · Results

```text
Agent:
The original recording stopped after 347 seconds. After the change, the
10-minute recording completed. I also repeated the test from a clean candidate.
These results support the fix on this laptop, not a guarantee for other hardware.

Investigation patch: ~/quirkbench-work/microphone-dropout/patches/0001-fix.patch
Evidence and command history: ~/quirkbench-work/microphone-dropout/results/
The target has returned to the recovery system. The investigation is paused.
```

```console
$ quirkbench investigation show microphone-dropout
Paused · Target connected · Recovery system
Evidence received. Uploaded USB copies removed.

Results: ~/quirkbench-work/microphone-dropout/results/
Resume: quirkbench investigation resume microphone-dropout
```

## Controller computer · Update recovery later

```console
$ quirkbench recovery flash --device /dev/sdb --image recovery-2
Update recovery and boot files only. Wi-Fi, pairing, candidates and diagnostics will be kept.
Requested path: /dev/sdb
Resolved device: /dev/sdb
Model: USB Flash Drive
Serial: example-drive-01
Size: 32.0 GB
Existing layout:
  /dev/sdb   32.0 GB  not mounted
    /dev/sdb1   0.3 GB  vfat  not mounted
    /dev/sdb2   4.0 GB  ext4  not mounted
    /dev/sdb3   1.1 GB  ext4 QB-BOOT  not mounted
    /dev/sdb4  26.6 GB  ext4 QB-DATA  not mounted
Type UPDATE /dev/sdb to continue, or press Enter to cancel: UPDATE /dev/sdb
Writing and checking recovery filesystem; keeping shared data
Recovery updated. Saved Wi-Fi and controller pairing retained. Next boot: recovery.
```

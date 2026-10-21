# Interruptions and troubleshooting

> **EXAMPLES ONLY — intended design, not current functionality.** These screens
> show possible situations, not mandatory steps in every investigation.

## Controller computer · Investigation pause

```console
$ quirkbench investigation pause wake-wifi
Investigation pause requested.
Run 2's active commands will finish, then the target will return to recovery
and upload evidence.
No new work will start.

Watch: quirkbench monitor wake-wifi
```

```text
┌─ Quirkbench · wake-wifi ───────────────────────────────────┐
│ Investigation pause requested                            │
│                                                          │
│ Current work       Run 2 · Cycle 12 of 20                 │
│ Next               Return to recovery system and upload  │
│ New work           Stopped                               │
│                                                          │
│ F Force stop    L Logs    Q Close monitor                 │
└──────────────────────────────────────────────────────────┘
```

```console
$ quirkbench investigation resume wake-wifi
Investigation resumed with the same limits.
Waiting for the agent's next action.
```

## Controller computer · Force stop

```console
$ quirkbench investigation stop wake-wifi --force
Force stop requested. New work stopped.
Waiting for target lab-target to respond.
```

```text
┌─ Quirkbench · wake-wifi ───────────────────────────────────┐
│ Target has not responded                                 │
│                                                          │
│ Force stop sent       45 seconds ago                     │
│ Last contact         1 minute ago                        │
│ Run result           Unknown                             │
│ Evidence             Partial logs received              │
│                                                          │
│ Check the target. If frozen, restart it from the USB.     │
│ Quirkbench will collect surviving evidence on reconnect. │
│ The interrupted test will not restart automatically.     │
│                                                          │
│ L Received logs    Q Close monitor                       │
└──────────────────────────────────────────────────────────┘
```

## Target · New candidate could not connect

```text
Quirkbench · Recovery system
Run 3 returned to recovery.

The candidate could not connect to the controller within 5 minutes of startup.
Recovery is connected. Saved candidate logs are uploading.
The failed candidate will not restart automatically.

L Logs    N Network    T Terminal
```

## Target · Controller connection lost during a run

```text
Quirkbench · Run 2
Controller connection lost. Reconnecting automatically.

Recording continues · Timeout in 6:48
Output is being saved on USB.
When current commands finish, this system will wait for reconnection.

P Pause and return to recovery    F Force stop    L Logs
```

## Target · Controller unavailable after returning to recovery

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ QUIRKBENCH / RECOVERY                                                        │
├────────────────────────┬─────────────────────────────────────────────────────┤
│ MENU                   │ CURRENT STATUS                                      │
│   N  Network           │ Target          lab-target (7c2a91)                 │
│ > C  Controller        │ Network         Workshop Wi-Fi                      │
│   A  Activity          │ Target IP       192.168.1.81                        │
│   L  Logs & reports    │ Controller      Disconnected                        │
│   I  Target details    │ Last contact    42s ago                             │
│   T  Open terminal     │ Controller URL  https://192.168.1.20:7443           │
│   P  Power             │ Recovery OS     Read-only                           │
│                        │ Data free       21 GiB                              │
│                        │ Current work    Reconnecting; upload waiting        │
├────────────────────────┴─────────────────────────────────────────────────────┤
│ RECOVERY & MCP ACTIVITY                                           Live log   │
│ 14:31:02  RECOVERY  Returned from candidate run 3                            │
│ 14:31:02  RECOVERY  Candidate connection timed out                           │
│ 14:31:05  RECOVERY  Uploading saved run logs                                 │
│ 14:32:01  RECOVERY  Controller connection lost                               │
│ 14:32:01  RECOVERY  Unconfirmed evidence kept on USB                         │
│ 14:32:43  RECOVERY  Reconnecting / no commands running                       │
├──────────────────────────────────────────────────────────────────────────────┤
│ [------------] No operation running                                          │
├──────────────────────────────────────────────────────────────────────────────┤
│ ↑↓ Select   Enter Open   A Activity history   T Terminal   Esc Back          │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Target · Upload interrupted

```text
Run 1 · Waiting for controller confirmation
18 MiB sent. USB evidence kept until receipt is confirmed.
Reconnecting automatically. Do not repeat the experiment.

R Retry now    N Network    T Terminal
```

## Target · Saved settings unavailable

The [recovery dashboard](recovery-console.md) keeps its menu and shows:

```text
Cannot read shared USB storage.
Saved network and controller settings are unavailable.
Connect temporarily using Network, or open Terminal.
Candidate deployment is unavailable until storage works.
```

## Target · Recovery system problem

The [recovery dashboard](recovery-console.md) keeps its menu and shows:

```text
Cannot save experiment evidence: shared USB storage is full.
Candidate deployment is unavailable.
Network connected · Controller connected
Open Logs & reports to inspect this problem.
```

```text
┌─ Logs & reports  ─────────────────────────────────────────┐
│                                                          │
│ Quirkbench internal debugging report                     │
│                                                          │
│ Diagnose this recovery system, not an experiment.        │
│                                                          │
│ Included      Boot log, service logs, disk space         │
│ Removed       Saved passwords and pairing credentials   │
│ Size          420 KiB                                   │
│                                                          │
│ > Preview                                                │
│   Send to paired controller                              │
│   Save to another USB drive                              │
│   Open terminal                                          │
│                                                          │
│ Enter Select    Esc Back                                 │
└──────────────────────────────────────────────────────────┘
```

```text
Report sent to the Quirkbench controller.
No experiment evidence was changed. Nothing was posted publicly.

On the controller computer:
quirkbench target report show lab-target --latest

Enter Back
```

## Target · Local terminal

The user selects **Open terminal** or presses **T**.

```console
Quirkbench recovery system · Local root terminal
You can change any connected disk here. Type exit to return to the menu.

root@lab-target:~# journalctl -b -p err --no-pager
Oct 07 14:41:03 lab-target quirkbench: Cannot save evidence: No space left on device
root@lab-target:~# exit
```

The recovery dashboard returns with **Open terminal** still selected and the
storage warning still visible.

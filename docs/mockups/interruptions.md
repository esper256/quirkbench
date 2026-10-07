# Interruptions and troubleshooting

> **EXAMPLES ONLY — intended design, not current functionality.** These screens
> show possible situations, not mandatory steps in every investigation.

## Controller computer · Investigation pause

```console
$ quirkbench investigation pause wake-wifi
Investigation pause requested.
Run 2 will finish, return to the recovery system and upload evidence.
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
Waiting for the agent's next experiment.
```

## Controller computer · Force stop

```console
$ quirkbench investigation stop wake-wifi --force
Force stop requested. New work stopped.
Waiting for target lab-laptop to respond.
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
│ Check the laptop. If frozen, restart it from the USB.     │
│ Quirkbench will collect surviving evidence on reconnect. │
│ The interrupted test will not restart automatically.     │
│                                                          │
│ L Received logs    Q Close monitor                       │
└──────────────────────────────────────────────────────────┘
```

## Target · Controller connection lost

```text
┌─ Quirkbench ─────────────────────────── Recovery system ──┐
│                                                          │
│ Run ended. Evidence saved on USB; upload waiting.         │
│                                                          │
│ Network       Workshop Wi-Fi · Connected                 │
│ Controller    Cannot reach https://192.168.1.20:7443       │
│ Evidence      18 MiB waiting to upload                   │
│ Last attempt  12 seconds ago                             │
│                                                          │
│ Check that the Quirkbench controller is running.         │
│                                                          │
│ > Retry connection                                       │
│   Network                                                │
│   Controller connection                                  │
│   Troubleshooting                                        │
│   Open terminal                                          │
│   Power                                                  │
│                                                          │
│ ↑↓ Select     Enter Open     T Terminal                   │
└──────────────────────────────────────────────────────────┘
```

## Target · Upload interrupted

```text
Run 1 · Waiting for controller confirmation
18 MiB sent. USB evidence kept until receipt is confirmed.
Reconnecting automatically. Do not repeat the experiment.

R Retry now    N Network    T Terminal
```

## Target · Saved settings unavailable

```text
┌─ Quirkbench ─────────────────────────── Recovery system ──┐
│ Cannot read shared USB storage                           │
│                                                         │
│ Saved network and controller settings are unavailable.  │
│ Experiments cannot start until storage works again.     │
│ You can connect temporarily to help diagnose this.      │
│                                                         │
│ > Connect to a network                                  │
│   Controller connection                                 │
│   Troubleshooting                                       │
│   Open terminal                                         │
│   Power                                                 │
│                                                         │
│ ↑↓ Select     Enter Open     T Terminal                  │
└─────────────────────────────────────────────────────────┘
```

## Target · Recovery system problem

```text
┌─ Quirkbench ─────────────────────────── Recovery system ──┐
│                                                          │
│ Cannot save experiment evidence to USB.                  │
│ No experiment will start.                                │
│                                                          │
│ Network       Workshop Wi-Fi · Connected                 │
│ Controller    Connected                                  │
│ Problem       Shared USB storage is full                    │
│                                                          │
│ > Troubleshooting                                        │
│   Network                                                │
│   Controller connection                                  │
│   Open terminal                                          │
│   Power                                                  │
│                                                          │
│ ↑↓ Select     Enter Open     T Terminal                   │
└──────────────────────────────────────────────────────────┘
```

```text
┌─ Troubleshooting ─────────────────────────────────────────┐
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
quirkbench target report show lab-laptop --latest

Enter Back
```

## Target · Local terminal

The user selects **Open terminal** or presses **T**.

```console
Quirkbench recovery system · Local root terminal
You can change any connected disk here. Type exit to return to the menu.

root@lab-laptop:~# journalctl -b -p err --no-pager
Oct 07 14:41:03 lab-laptop quirkbench: Cannot save evidence: No space left on device
root@lab-laptop:~# exit
```

```text
┌─ Quirkbench ─────────────────────────── Recovery system ──┐
│ Cannot save experiment evidence to USB.                  │
│                                                          │
│ > Troubleshooting                                        │
│   Network                                                │
│   Controller connection                                  │
│   Open terminal                                          │
│   Power                                                  │
│                                                          │
│ ↑↓ Select     Enter Open     T Terminal                   │
└──────────────────────────────────────────────────────────┘
```

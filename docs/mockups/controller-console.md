# Controller console

> **EXAMPLE ONLY.** Names, addresses, work and timings illustrate one session.
> Any compatible agent can use the controller; this screen adds no workflow requirements.

```console
$ quirkbench controller run
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│ QUIRKBENCH / CONTROLLER                                                                          │
├─────────────────────────┬────────────────────────────────────────────────────────────────────────┤
│ MENU                    │ CURRENT STATUS                                                         │
│ > O  Overview           │ Controller URL  https://controller:7443                                │
│ A  Activity             │ Targets         1 connected / 1 paired                                 │
│ C  Connections          │ Current work    1 active                                               │
│ Q  Stop controller      │ Running for     0 min                                                  │
├─────────────────────────┴────────────────────────────────────────────────────────────────────────┤
│ TARGETS & CURRENT WORK                                                                           │
│ test-computer  ·  candidate  ·  0s ago                                                           │
│                                                                                                  │
│ wifi-case · ssh-command · running · Collecting diagnostics                                       │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│ CONTROLLER & MCP ACTIVITY                                                                        │
│ 09:14:04  MCP ssh_command_start · returned                                                       │
│ 09:14:02  Target test-computer connected (candidate)                                             │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
│                                                                                                  │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ ↑↓ Scroll   O Overview   A Activity   C Connections   Q Stop   Ctrl+C Stop                       │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

```text
C Connections

Controller URL: https://controller:7443
Agent MCP: http://127.0.0.1:7442/mcp
MCP token file: /home/sam/.quirkbench/mcp-token
Controller log: /home/sam/.quirkbench/logs/controller.log

Keep this terminal open while using Quirkbench.
```

```text
Q Stop controller

STOP CONTROLLER?
Targets will lose their connection. Running target commands may continue.
Y Stop controller    Esc Cancel
```

```console
$ quirkbench controller run --no-tui
Controller: https://controller:7443
Agent MCP: http://127.0.0.1:7442/mcp
MCP token file: /home/sam/.quirkbench/mcp-token
Controller log: /home/sam/.quirkbench/logs/controller.log
Keep this terminal open. Ctrl+C or quirkbench controller stop stops it.
Target test-computer connected (recovery)
```

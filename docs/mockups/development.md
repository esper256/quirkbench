# Development on the controller computer

> **EXAMPLE ONLY.** These commands show one development route, not an end-user
> installation requirement. The installed application remains `quirkbench`.

```console
$ ./dev quirkbench --version
Preparing private development Python dependencies…
Quirkbench 1.1.0

$ ./dev quirkbench --version
Quirkbench 1.1.0

$ ./dev quirkbench setup
Address target computers will connect to [https://controller:7443]:
Controller config saved: /home/sam/.quirkbench/config.toml
Next: quirkbench controller run

$ ./dev quirkbench controller run
Controller: https://controller:7443
Agent MCP: http://127.0.0.1:7442/mcp
MCP token file: /home/sam/.quirkbench/mcp-token
Controller log: /home/sam/.quirkbench/logs/controller.log
Keep this terminal open. Ctrl+C or quirkbench controller stop stops the controller.
Target test-computer connected (recovery)
Target test-computer has not answered its status request for 10 seconds
Target test-computer is responding again after 12 seconds
```

After editing controller code, stop it with Ctrl+C, then:

```console
$ ./dev quirkbench controller run
```

For a locally built recovery:

```console
$ ./dev quirkbench recovery build
Recovery build ready. Boot has not been checked. Next: quirkbench recovery flash --built

$ ./dev quirkbench recovery flash --built
Recovery: x86_64 · 2026-10-22T12:00:00+00:00 · source 8e475fe431aa
Select an external drive to install or update. Internal system disks are excluded.
1. /dev/sdb SanDisk USB 32.0 GB Serial: example-drive-01
Drive number (Enter cancels):
Cancelled. Nothing was written.
```

For a packaging check, without installing an RPM on the host:

```console
$ ./dev package --development
Package files: /home/sam/dev/quirkbench/dist/release
Nothing was published or installed on the host.
```

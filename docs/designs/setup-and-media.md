# Setup, build, flash

Revisable [design](../README.md); commands describe the destination, not current availability.

```sh
quirkbench recovery build
quirkbench setup
quirkbench recovery flash
```

**Setup:** ask short terminal questions with useful defaults; Enter accepts each default. Ask for the target-reachable controller URL, generate missing connection credentials and save `~/.quirkbench/config.toml`. Setup saves configuration; it does not start a background service. Print `quirkbench controller run` as the next step. The controller runs in the foreground in a separate terminal. Re-running edits choices. Direct edits apply at restart; unrelated edits preserve credentials. No controller systemd integration, automatic backgrounding or service manager.

**Build:** automatically acquire the selected pinned inputs and build the generic recovery image. Cache by relevant code, included files, build instructions, packages and build tools—not paths or unrelated settings. A hit says the image is available; `--force` rebuilds without deleting the last good image first. Show phases/logs; incomplete output is not selectable.

This command builds the recovery image. Experiment candidates use the separate
[rpm-ostree and OSTree path](candidate-deployment.md); ordinary experiments do not
reflash the USB. Include the needed OSTree tools in recovery.

**Flash:** refuse missing setup before writes. Use the only compatible image or a short selector. Always ask which USB to erase and show one clear confirmation. Elevate only the disk writer, retain system-disk exclusion and recheck the selected device before writing. Verify/flush completion before declaring success.

Prepare the final [USB layout](recovery.md) during flashing. The recovery image
is generic: put local controller settings and pairing credentials in the shared
data partition, separately from recovery. Saved network settings go there too.
Pairing credentials are single-use, revocable and non-expiring. No target resizing
or RAM admission limit. After the fixed boot and recovery space, use the remaining
space jointly for candidates, settings, evidence and temporary files. No separate
evidence or unused library partition.

Scripting supplies explicit choices without prompts. Remove public plan/apply hashes, manual acquisition chains and immutable setup journals rather than hiding them behind a wizard.

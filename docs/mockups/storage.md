# Storage cleanup

> **EXAMPLE ONLY.** Names, sizes and cleanup choices illustrate the interface.

```console
$ quirkbench storage
Storage on this controller
Build cache                     10.91 GiB
Investigations                   4.72 GiB
Recovery images                  1.14 GiB
Unassigned diagnostics           0.7 MiB
Controller and build logs        2.8 MiB
Investigations:
  intermittent-fault

$ quirkbench storage clean --cache
Storage on this controller
Build cache                     10.91 GiB
Investigations                   4.72 GiB
Recovery images                  1.14 GiB
Unassigned diagnostics           0.7 MiB
Controller and build logs        2.8 MiB
Investigations:
  intermittent-fault
  cache/candidate-import-bare · 10.91 GiB
Remove these items? y/N [n]: y
Cleanup complete.

$ quirkbench investigation delete intermittent-fault
Delete investigation: intermittent-fault
Runs and diagnostics: 3 · Unused software versions: 1
Delete this investigation and its diagnostics? y/N [n]: y
Deleted investigation: intermittent-fault

$ quirkbench storage clean --recovery-images --yes
Cleanup complete.

$ quirkbench storage clean --unassigned-diagnostics --logs --yes
Cleanup complete.
```

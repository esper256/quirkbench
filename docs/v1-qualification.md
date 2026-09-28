# Layout revision 2 qualification — 2026-09-28

The six-partition image and software integration passed the checks below. This
qualifies build and VM infrastructure, not unattended target operation. The
[review](v1-layout-review.md) records the revised boundaries and issues found.

The local [qualification index](../.quirkbench/v1-layout/qualification.json)
contains hashes of the retained reports. These large local outputs are ignored by
Git; historical layout-1 evidence remains unchanged in its original directories.

| Check | Observed result |
| --- | --- |
| Software regressions | 405 passed in 35.17 seconds; local and HTTPS lifecycle/fault tests |
| Signed Fedora composition | Exact revision `c40210d12c18f2c9a95c9d40d05e51a3979ad62d1eef9ca1507340bc75d935ed` |
| Incremental deployment | 165 added objects, 3.575 seconds; signature, fresh state, idempotency and prior-attempt preservation passed |
| Six-partition UEFI image | All ten actual boot trials passed, including missing entry, loader failure, deliberate panic/reset and return to recovery |
| Preservation | Fixed recovery/EFI, library and internal sentinel preserved; effective firmware settings satisfy the reviewed MTC-only exception |
| Standard image supervisor | Production partition defaults commissioned; ten visible provisioning waits spanning more than 45 seconds, without service failure or reboot loop |
| Backup and restore | 36 retained artifacts and 10,410 OSTree objects; independent restored content and unchanged source backup |
| Controller inventory | All 2,786 recorded packages, boot configuration and running kernel unchanged |

The standard [4 GiB factory image](../.quirkbench/v1-layout/quirkbench-v1.img)
has a [checksum file](../.quirkbench/v1-layout/quirkbench-v1.img.sha256) and
[manifest](../.quirkbench/v1-layout/quirkbench-v1.img.json).
Its SHA-256 is `be1bd86cbbac29b3df05d24dd44a0c88069a9bf2f8cd4e55ea8566b8a2830578`.
It contains no experimental deployment or device credentials. Follow the
[provisioning instructions](debug-image.md) for an attended first use; the image
does not establish hardware watchdog coverage.

Reproduce the image checks using `make acceptance-v1-image` with a smoke image
and `make acceptance-standard-image` with a standard image. See the
[acceptance guide](../acceptance/README.md) for arguments. The standard fixture
ends through controlled QMP termination rather than graceful guest shutdown;
its firmware initialization is distinct from the settled ten-trial comparison.

Remaining gates are explicit:

- Qualify actual target USB boot, internal-storage protection, watchdog activation,
  earliest covered boot stage, handoff, runtime/shutdown reset and suspend.
  Current image kernels lack a usable hardware-watchdog driver; build and qualify
  the observed hardware's driver before claiming automatic hang recovery.
- Exercise the provisioned physical lifecycle and surviving evidence under real
  hangs/network loss. Software tests inject boot boundaries; the standard VM
  supervisor trial has no controller credentials.
- Review the no-kexec policy before implementing kdump. Panic reset proves neither
  crash-dump availability nor recovery from all hangs.
- Complete the campaign exceeding 30 hours and the three issue investigations.
  No target fix or unattended readiness is claimed.

Existing protected kernel builds, container recreation and initial full-transfer
fault qualification are retained historical inputs. The new reports do not claim
those operations were all repeated. Backup campaign pause is unexercised in this
particular empty-campaign fixture; software lifecycle tests cover pause/restart.

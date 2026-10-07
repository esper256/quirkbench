> **Legacy reference — not current requirements or agent instructions.**
> This document was archived on 2026-10-07. Consult [current documentation](../README.md).
> Commands and implementation claims below may be obsolete.

# Qualification fixtures

**Scheduling policy:** these expensive real-system fixtures run only for an
explicitly requested final major-version release, never automatically after an
edit or milestone task. Make targets require `RELEASE_QUALIFICATION=1` alongside
the inputs below. Direct script invocations follow the same policy. Routine work
uses focused software tests; see [testing and agent quota](testing-policy.md).
Do not keep an agent waiting on long runs unless their results block development.

The [remaining implementation briefs](product-roadmap.md) use focused
software fixtures for discovery, enrollment, background operations and sessions.
Their proposed suites are not current passing evidence. Target hardware inventory
and first device commissioning do not replace physical watchdog/endurance gates,
nor do they automatically trigger the full release suite.

Templates intentionally fail acceptance. Copy one outside this directory, fill observations from an actual run, and reference evidence files relative to the report with `{"path":"relative/file", "sha256":"64 lowercase hex"}`. The verifier checks report completeness and referenced bytes; it cannot authenticate an operator's observations or establish causality from a manifest alone. Higher-reasoning/human review still assesses evidence and limitations.

Hardware endurance requires a positive duration objective and rationale declared before the run, observed duration meeting that objective, the listed injected events, evaluated recovery capabilities, resource-use and safety evidence, and no silently unresolved attempts. Patch bundles require separately matched baseline/patched/revert/regression observations, source/build identities, actual patch files and exposure counts. Keep an unreproduced issue inconclusive instead of manufacturing a passing report.

QEMU uses `make acceptance-qemu` with real IMAGE, OVMF_CODE, OVMF_VARS and an empty WORK_DIR. Twelve trials cover initial firmware settling, recovery, candidate, subsequent recovery, missing-entry fallback, subsequent recovery, initramfs-load-failure fallback, subsequent recovery, a deliberate candidate kernel panic subsequent recovery, and refusal of a valid candidate bound to a different or missing target identity. These last two trials must prove recovery before candidate loading and consumed one-shot state. The load-failure case loads a valid kernel but references a missing initramfs; it must report the GRUB failure marker and recovery in the same boot; reaching the ordinary default alone is insufficient. Candidate execution measures the running kernel, matching module tree and userspace fixture. The panic trial requires kernel-emitted authorization/running-kernel identity, actual sysrq panic output and consumed one-shot state. Userspace markers can be lost during a panic and are recorded only as optional observations; the normal candidate trial separately verifies userspace and modules. All trials after initialization must preserve settled firmware settings and fixed USB recovery partitions. The firmware comparison described below permits only the exact firmware-owned MTC counter increment; raw snapshots remain retained and every other effective variable must match. This is a VM infrastructure gate; it does not qualify early-boot hangs or physical crash capture.

`make acceptance-ostree-repository REPOSITORY_WORK=/absolute/new-directory` checks real repository retention, independent backup/restore, repeatability and corruption rejection. It requires the OSTree executable and cannot be replaced by fake command results.

`make acceptance-ostree-deployment DEPLOYMENT_WORK=/absolute/new-directory DEPLOYMENT_MANIFEST=/absolute/deployment.json OSTREE_REPO=/absolute/repository PUBLIC_KEY=/absolute/signing-public.asc` exercises real mutually authenticated HTTPS, interrupted object download, a lost deployment acknowledgement, isolated attempt state and idempotent retry. Run it as UID 0 inside the rootless builder; it writes only regular-file fixtures. Its resulting prepared data tree can be used to assemble the QEMU image. Private TLS fixture keys stay outside that tree.

For a staging filesystem that automatically adds host SELinux labels, select `DEPLOYMENT_REPO_MODE=bare-user`. This records logical OSTree metadata independently of inherited host labels and keeps full integrity verification enabled. The fixture reports that choice; it does not qualify a physical USB filesystem or change the production adapter's default.

`make acceptance-m2` combines software, repository, strict-signature, deployment, complete-controller-backup and VM gates. Supply the inputs for each component target, including `SIGNATURE_WORK` for the signature fixture and `CONTROLLER_STATE`/`BACKUP_WORK` for the backup fixture. The accompanying qualification report must also record actual composition inputs, matching kernel/userspace identities, container recreation and host preservation. Passing unit tests alone is insufficient.

Strict signature verification also has a real fixture:

```sh
PYTHONPATH=src python3 acceptance/qualify-ostree-signatures.py --work /absolute/new-directory
```

Run inside the isolated builder with OSTree, GnuPG and `python3-gobject-base`. It generates disposable fixture keys and proves trusted cached commits are accepted while unsigned and untrusted cached commits are rejected. `ostree show --gpg-verify-remote` returning zero is explicitly not sufficient. These fixture keys are never target or agent credentials.

Repository and signature fixtures do not imply a successful OS composition or candidate boot.

A complete published result can also be qualified through `acceptance/qualify-ostree-controller-backup.py --controller EXISTING_STATE --repository PUBLISHED_REPO --manifest DEPLOYMENT_JSON --work NEW_DIRECTORY`. This exercises the real controller backup and restore APIs, retaining matching build evidence and independent OSTree objects. It checks source-backup immutability and exclusion of synthetic private files, partial uploads and unreferenced artifacts. It reports campaign pause as unexercised when the source has no campaigns; empty tables are not evidence of pause behavior.

`acceptance/qualify-ostree-incremental.py --prior-work PRIOR_DEPLOYMENT_FIXTURE --work NEW_DIRECTORY --manifest NEW_DEPLOYMENT_JSON --repository PUBLISHED_REPO --public-key SIGNING_PUBLIC_KEY` checks a signed changed-object update on a preserved copy of the prior regular-file fixture. It reuses the exact loopback URL and trust files, measures added objects and preparation time, and verifies fresh attempt state, idempotency and preservation of the earlier attempt. The original full transfer/fault report remains part of the evidence.

## Layout revision 2 and execution/reset integration

`make acceptance-v1-image IMAGE=... OVMF_CODE=... OVMF_VARS=... WORK_DIR=...`
runs software tests and the fresh six-partition QEMU image gate. The fixture grows
only its copied sparse image, observes first-boot commissioning, verifies all six
GPT identities, observes distinct experiment/evidence/library mounts, and preserves
fixed recovery/ESP and library bytes across the boot trials. It
never enlarges or writes the source image or a physical block device. Fixture
partition capacities may be smaller than the production defaults and are recorded.

`make acceptance-standard-image IMAGE=... OVMF_CODE=... OVMF_VARS=... WORK_DIR=...`
boots an unprovisioned, non-smoke factory image, commissions its production layout,
and observes the actual supervisor waiting for more than its 30-second service
watchdog interval. It verifies a single boot, repeated visible provisioning waits,
mount identities, fixed recovery and internal sentinel preservation. It then stops
the disposable VM through QMP; this fixture does not claim a graceful guest
shutdown, controller communication or physical watchdog coverage. The boot-cycle
gate separately compares settled persistent firmware settings.

These QEMU image fixtures still expect automatic first-boot commissioning. Recovery
now requires an attended capacity confirmation. The console screen and larger
sizing choices exist in software, but the fixture adapter remains pending P3a5.
The fixtures cannot qualify current image bytes until adapted.

`tests/test_physical_handoff.py` runs the same handoff/streamed-evidence lifecycle
through local and real HTTPS clients, injecting the boot boundary. It covers lost
acknowledgements, old generations, wrong revisions, controller restart, interrupted
arming, bounded offline finish and pause fences. These tests do not claim to reboot
real hardware. Library maintenance tests exercise resumable ranged content above
256 MiB, immutable publication and retained backup closure.

Watchdog runtime tests use temporary sysfs/configuration fixtures and systemd
notification sockets. They never arm the host watchdog. Real hardware qualification
must follow [the explicit watchdog matrix](watchdog-qualification.md):
activation, boot handoff, runtime reset, shutdown and suspend are separate outcomes.
Do not substitute a successful late panic reboot or software heartbeat for those
observations. Missing hardware remains an unmet gate, not a skipped passing test.

## Firmware preservation comparison

The OVMF acceptance gate compares decoded effective variables after initial firmware settling, while retaining raw before/after flash snapshots and hashes. EDK2 increments its own `MTC` variable on each boot and may rewrite flash log records. The sole permitted semantic difference is one increment modulo 2^32 of the four-byte, attributes-7 `MTC` under GUID `eb704011-1402-11d3-8e77-00a0c969723b`; unchanged is also accepted. All other variable names, values, attributes and authentication metadata must match exactly. Added/deleted variables or larger counter changes fail. This is firmware-owned bookkeeping, not permission for Quirkbench to write firmware settings. [EDK2 counter initialization](https://github.com/tianocore/edk2/blob/master/MdeModulePkg/Universal/MonotonicCounterRuntimeDxe/MonotonicCounter.c) explains the observed increment. Physical target firmware remains separately unqualified.

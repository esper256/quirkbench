# Qualification fixtures

Templates intentionally fail acceptance. Copy one outside this directory, fill observations from an actual run, and reference evidence files relative to the report with `{"path":"relative/file", "sha256":"64 lowercase hex"}`. The verifier checks report completeness and referenced bytes; it cannot authenticate an operator's observations or establish causality from a manifest alone. Higher-reasoning/human review still assesses evidence and limitations.

Hardware endurance requires more than 30 real hours, the listed injected events, evaluated recovery capabilities, resource-use and safety evidence, and no silently unresolved attempts. Patch bundles require separately matched baseline/patched/revert/regression observations, source/build identities, actual patch files and exposure counts. Keep an unreproduced issue inconclusive instead of manufacturing a passing report.

QEMU uses `make acceptance-qemu` with real IMAGE, OVMF_CODE, OVMF_VARS and an empty WORK_DIR. Ten trials cover initial firmware settling, recovery, candidate, subsequent recovery, missing-entry fallback, subsequent recovery, initramfs-load-failure fallback, subsequent recovery, a deliberate candidate kernel panic and subsequent recovery. The load-failure case loads a valid kernel but references a missing initramfs; it must report the GRUB failure marker and recovery in the same boot; reaching the ordinary default alone is insufficient. Candidate execution measures the running kernel, matching module tree and userspace fixture. The panic trial requires kernel-emitted authorization/running-kernel identity, actual sysrq panic output and consumed one-shot state. Userspace markers can be lost during a panic and are recorded only as optional observations; the normal candidate trial separately verifies userspace and modules. All trials after initialization must preserve settled firmware settings and fixed USB recovery partitions. The [reviewed firmware comparison](../docs/m2-ostree-review.md) permits only the exact firmware-owned MTC counter increment; raw snapshots remain retained and every other effective variable must match. This is a VM infrastructure gate; it does not qualify early-boot hangs or physical crash capture.

`make acceptance-ostree-repository REPOSITORY_WORK=/absolute/new-directory` checks real repository retention, independent backup/restore, repeatability and corruption rejection. It requires the OSTree executable and cannot be replaced by fake command results.

`make acceptance-ostree-deployment DEPLOYMENT_WORK=/absolute/new-directory DEPLOYMENT_MANIFEST=/absolute/deployment.json OSTREE_REPO=/absolute/repository PUBLIC_KEY=/absolute/signing-public.asc` exercises real mutually authenticated HTTPS, interrupted object download, a lost deployment acknowledgement, isolated attempt state and idempotent retry. Run it as UID 0 inside the rootless builder; it writes only regular-file fixtures. Its resulting prepared data tree can be used to assemble the QEMU image. Private TLS fixture keys stay outside that tree.

For a staging filesystem that automatically adds host SELinux labels, select `DEPLOYMENT_REPO_MODE=bare-user`. This records logical OSTree metadata independently of inherited host labels and keeps full integrity verification enabled. The fixture reports that choice; it does not qualify a physical USB filesystem or change the production adapter's default.

`make acceptance-m2` combines software, repository, strict-signature, deployment, complete-controller-backup and VM gates. Supply the inputs for each component target, including `SIGNATURE_WORK` for the signature fixture and `CONTROLLER_STATE`/`BACKUP_WORK` for the backup fixture. The accompanying qualification report must also record actual composition inputs, matching kernel/userspace identities, container recreation and host preservation. Passing unit tests alone is insufficient.

Strict signature verification also has a real fixture:

```sh
PYTHONPATH=src python3 acceptance/qualify-ostree-signatures.py --work /absolute/new-directory
```

Run inside the isolated builder with OSTree, GnuPG and `python3-gobject-base`. It generates disposable fixture keys and proves trusted cached commits are accepted while unsigned and untrusted cached commits are rejected. `ostree show --gpg-verify-remote` returning zero is explicitly not sufficient. These fixture keys are never target or agent credentials.

Current transition review and outstanding qualification are recorded in [the OSTree architecture review](../docs/m2-ostree-review.md). Repository and signature fixtures do not imply a successful OS composition or candidate boot.

A complete published result can also be qualified through `acceptance/qualify-ostree-controller-backup.py --controller EXISTING_STATE --repository PUBLISHED_REPO --manifest DEPLOYMENT_JSON --work NEW_DIRECTORY`. This exercises the real controller backup and restore APIs, retaining matching build evidence and independent OSTree objects. It checks source-backup immutability and exclusion of synthetic private files, partial uploads and unreferenced artifacts. It reports campaign pause as unexercised when the source has no campaigns; empty tables are not evidence of pause behavior.

`acceptance/qualify-ostree-incremental.py --prior-work PRIOR_DEPLOYMENT_FIXTURE --work NEW_DIRECTORY --manifest NEW_DEPLOYMENT_JSON --repository PUBLISHED_REPO --public-key SIGNING_PUBLIC_KEY` checks a signed changed-object update on a preserved copy of the prior regular-file fixture. It reuses the exact loopback URL and trust files, measures added objects and preparation time, and verifies fresh attempt state, idempotency and preservation of the earlier attempt. The original full transfer/fault report remains part of the evidence.

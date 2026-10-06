# Prepare and build from one recovery input bundle

Use `quirkbench dev recovery` for controller-free preparation and image
production. It wraps the existing signed-RPM retention, stock recipe and foreground
image builder. No daemon or controller state is needed. Software CI does not build
or boot images.

Prerequisites are a reviewed acquisition specification, its pinned Fedora public
key, `dnf5` for acquisition, `rpm`/`rpmkeys` and GnuPG for verification, and the
selected immutable local builder image in Podman or Docker. Use the repository's
[recovery operations](recovery-operations.md) to select the builder and specification.

```sh
# Stage the specification's repository bytes and print the exact download argv.
quirkbench dev recovery plan --spec selected-spec.json --output acquisition
# Execute the returned download_argv. This downloads packages, never installs them
# into the host. Keep TLS and repository signature verification enabled.

quirkbench dev recovery prepare \
  --spec acquisition/acquisition-spec.v1.json --packages acquisition/rpms \
  --public-key RPM-GPG-KEY-fedora --builder-image sha256:YOUR_BUILDER_IMAGE_ID \
  --epoch 1700000000 --output recovery-inputs
quirkbench dev recovery verify recovery-inputs --engine podman
quirkbench dev recovery build recovery-inputs --engine podman --output recovery-output
```

For changed Fedora package pins, supply an explicitly reviewed vendor inventory
with `prepare --vendor-inventory /path/to/reviewed.json`. See
[reviewed vendor inventories](recovery-base.md#reviewed-vendor-inventories). The
bundle carries this object through export/import and binds it through the storage
profile; observing installed bytes never grants approval.

Choose the source-date epoch for your selected input snapshot. `prepare` verifies
every RPM against the specification's fingerprint, creates the existing lock and
recipe, and writes `manifest.json` last. Interrupted preparation retains its input
bytes and signature diagnostics but has no completed manifest. Use a fresh output
path for a retry. Ordinary directory modes are accepted; no read-only permission
bits are used to pretend that files are immutable.

`verify` hashes the full declared closure, reports all missing/changed objects,
checks that the installed target runtime matches the recipe, and rechecks RPM
signatures. `--engine` additionally checks the pinned local builder. The result
separately reports whether that builder check ran. Build admission and resource
checks still occur in the existing foreground pipeline. The image remains unsigned,
unqualified and untested on hardware; no verification step grants target execution
or experiment approval. Build failures retain the existing foreground diagnostics
and cleanup workflow.

## Transfer and snapshot contract

```sh
quirkbench dev recovery export recovery-inputs --output exported-inputs
# Transfer that directory using ordinary filesystem tools.
quirkbench dev recovery import exported-inputs --output local-inputs \
  --manifest-sha256 EXPECTED_MANIFEST_SHA256
```

A v1 bundle contains canonical `manifest.json` and content-addressed `objects/`.
The manifest identifies the acquisition specification, recipe, immutable builder
configuration ID and exact size/hash inventory. The inventory closes over repository
specification bytes, RPMs and public key, rootfs lock, stock policy and its selected vendor inventory, dracut settings,
unit allowlist, runtime revision and exact target Python/boot-asset bytes. Transfer
copies only those objects into a new directory and commits the same manifest last;
it never extracts an archive or overwrites an existing destination.

An input snapshot's public identity is the SHA-256 of the canonical manifest.
Import requires that expected digest from the snapshot you selected through a
trusted channel. The bundle does not authenticate its own publisher. RPM verification
is against the explicitly selected fingerprint, not proof that the bundle is a
published Quirkbench release. Export/import verify byte integrity; `verify` and
`build` perform signature checks. No signing credentials belong in a bundle.

The matching Quirkbench runtime installation and the pinned builder image are
external prerequisites. Bundles retain the runtime bytes for identity and archival
verification, but never execute bundled Python supplied by an imported directory.
Install the matching reviewed application version and load the exact builder through
its normal image-distribution mechanism. A mismatch is reported before building.
Repository availability is needed only to acquire missing RPMs: a complete bundle
builds offline with the existing network-isolated worker.

A future release may distribute this immutable input snapshot alongside its pinned
builder and application versions. Creating or exporting a bundle does not publish
a release or claim boot qualification. Lower-level `dev recovery inputs`,
`dev recovery build` and `dev recovery cleanup` remain available for diagnosis.

Fresh bundles contain a stock v3 recipe and produce a controller-prepared v3 factory
artifact. `quirkbench recovery prepare` applies its final USB geometry; copying the
factory artifact alone does not prepare usable media. Retained v2 bundles preserve
their old recipe/layout interpretation. New recipe sizing exposes recovery root and
factory artifact size only; remaining USB capacity is allocated during preparation.

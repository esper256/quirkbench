# Supported release publication preparation

`quirkbench dev release check DIRECTORY --inputs CAS_ROOT --trust-bundle TRUST_JSON
[--baseline ID] [--timeout SECONDS] [--json]` is a read-only preflight for release-set
v2. It uses existing publisher, controller archive, stock recovery, OCI builder and
baseline readers. It does not install, initialize state, import containers, create
keys, sign, upload or authorize experiments. JSON uses the operation envelope;
`data` follows [publication-preflight v1](../schemas/publication-preflight.v1.schema.json).
Exit 0 means inspected assets and selected input bytes match; exit 2 either returns
an incomplete closure with exact missing/invalid digests or an explicit input error.
The report observes bytes at inspection time. Consumers must verify them again.
Unavailable independent trust is exit 4 (`UNAVAILABLE`); infrastructure I/O is
exit 5 (`INFRASTRUCTURE`, retryable), distinct from rejected inputs.

## Release inventory and compatibility

Stage one immutable version directory, externally hosted at the operator-selected
`https://HOST/PREFIX/VERSION/` (no new publisher service):

| Filename | Meaning and consumer |
| --- | --- |
| `release.json`, `release.sig` | Canonical release-set v2 plus detached publisher signature; `admin install` |
| `controller.tar.gz` | Packaged Python 3.11+ controller, installed resources and verified file manifest |
| `factory.img`, `factory.img.json`, `factory.img.release-candidate.json` | Existing stock Fedora factory assembler output, exact geometry/UUID/identity sidecars; `recovery download` |
| `builder.tar` | Existing native OCI archive; explicit `setup --builder-archive` input |
| `catalog.json` | Exact bytes of the controller's bundled baseline catalog; preflight input |

Builder and catalog filenames above are staging conventions, not new automatic
fetch interfaces. Preserve recovery filenames: its consumer fetches them exactly.
Release-set v2 binds controller version/archive SHA, x86_64/API 1/Python >=3.11,
reader interfaces (layout/recovery-config/recovery-candidate 2; catalog/device/
binding 1), UTC validity interval, all five asset
SHAs plus builder image/config digests. Use the existing
[statement example](../examples/controller-release-set.v2.json) and
[strict schema](../schemas/controller-release-set.v2.schema.json). All statements,
factory candidates and inner controller manifests remain **unqualified**. An outer
publisher signature authenticates bytes; it does not relabel the inner unsigned
archive or prove native compatibility.

Preflight selects exactly one catalog baseline (explicit `--baseline` when multiple
are listed). Its builder image must match the signed release. Its target recipe
manifests and reviewed callable code must match the actual shipped controller.
The derived builder must have no entrypoint, matching setup's launch policy. Its
exact archive/config identities are checked; `builder_image_digest` is the Fedora
base marker, which archive inspection cannot prove. Native builder setup separately
verifies that marker and runtime availability (`builder_base_marker_verified:false`).
The controller's bundled catalog must equal `catalog.json` byte for byte. Other
baselines remain explicitly unchecked until separately inspected.

`CAS_ROOT/objects/SHA256` must contain every catalog input: kernel SRPM/config,
userspace source, RPM snapshot, target/build/toolchain locks, repository/dracut
configuration, fixed reviewed `fedora-kernel-rpm-v1` build recipe, target recipes,
and every exact RPM in the snapshot. Snapshot names/NEVRAs must match the catalog
and target lock. Missing snapshot metadata means additional RPM digests may still
be unknown; an incomplete report never claims full closure. Use exact approved
bytes from the operator's retained input delivery; do not substitute host caches,
latest repositories, invented checksums or placeholder examples. This command does
not deliver those inputs or register their controller retention owners. That
handoff must use the existing acquisition/storage workflow and be verified before
candidate preparation. Repeated `artifact put` is not a bulk closure import: its
normal input-generation retention policy applies.

Streaming limits are 8 GiB per input and 128 GiB total input verification work, plus
128 GiB total asset verification work; failed hashes and nested/repeated reads count.
Input metadata reserves its bounded worst-case allowance (2 MiB for snapshot/lock,
1 MiB for fixed recipe), including failures. Controller archive is capped at 64 MiB
compressed and expanded. Metadata uses
existing bounds (catalog 4 MiB, image manifest 1 MiB, candidate 64 KiB). Wall time is
300 seconds by default, selectable 1–600 seconds, including bounded GPG calls.
Deadlines are cooperative around bounded reads and nested archive chunks; this is
not a hard interruption guarantee for a stalled filesystem syscall. Reports are
capped at 64 KiB, with first 20 missing/invalid entries and explicit
counts/truncation; input files remain guarded against mutation/path substitution.
Large delivery verification may require a faster retained filesystem; limits are
not a reason to skip checksum/trust validation.

## Operator runbook (authorization required before production actions)

Choose and record the production version/platform, HTTPS origin/version layout,
independently distributed publisher public key/full uppercase fingerprint and
validity/expiry policy. Also provide the exact baseline/source/RPM closure, native
OCI builder identities, stock recovery inputs, and qualification/evidence policy.
The packaged Fedora catalog contains pinned real identities; their existence in a
catalog does not demonstrate their availability. `dev release check` makes the absent
hashes actionable. A clean fixture run is software preparation, never delivery.

1. Provision or select a production signing key in an operator-controlled private
   GnuPG home. No application command generates it. Decide custodians, backup,
   revocation certificate and rotation/expiry policy outside release downloads.
   Keep this publisher key separate from controller TLS, target enrollment and
   OSTree composition signing keys; no key is inferred from another role.
2. Build the development controller archive from reviewed source with
   `make controller-archive OUTPUT=/absolute/new/controller.tar.gz`. The command
   packages software only. Produce stock recovery and its sidecars using the
   [existing acquisition/assembler](recovery-acquisition.md), only under separate
   image authorization. Export the existing pinned builder with
   `podman save --format oci-archive --output /absolute/new/builder.tar PINNED_IMAGE`.
   Record image and config identities; this is not permission to build/import one.
   Copy the **packaged** catalog unchanged to `catalog.json`.
3. Populate the existing v2 statement's exact asset hashes (`sha256sum`), version,
   interfaces, builder identities and UTC times. Serialize with
   `quirkbench.contracts.canonical(value) + b'\n'` after
   `quirkbench.controller_release.validate_statement(value)`; do not sign ordinary
   pretty-printed JSON. No production statement generator or signing service is
   introduced. Then, only with explicit signing authorization:
   `gpg --batch --no-options --homedir PRIVATE_SIGNING_HOME --local-user FULL_FINGERPRINT --detach-sign --output release.sig release.json`.
4. Export the public key separately with
   `gpg --batch --no-options --homedir PRIVATE_SIGNING_HOME --armor --export FULL_FINGERPRINT > publisher.asc`.
   Independently distribute `publisher.asc` and a validated
   [trust bundle](../schemas/publisher-trust-bundle.v1.schema.json) containing
   `release_base_url`, fingerprint, relative `.asc` filename, **public key byte
   SHA**, `not_before`, `expires_at`. Place them outside the download directory.
   Do not learn replacement trust from a release statement, HTTPS download or the
   key itself. Ship/configure bootstrap trust via an independently authenticated
   channel; the repository intentionally ships no production/test publisher key.
5. Run `dev release check` for every advertised baseline against the approved retained
   input tree. Resolve mismatched/missing bytes and retain exact source/command/
   result evidence. Native RPM signature/package semantics, builder import,
   recovery boot, candidate execution and physical acceptance are separate gates.
6. Publish the verified immutable version directory to the chosen HTTPS location
   only with upload authorization. Preserve old immutable versions for rollback;
   do not overwrite an existing version's bytes or disable TLS verification.
7. From a fresh home with independently provisioned trust and Python 3.11+/GPG,
   use the extracted launcher:
   `./install VERSION --trust-bundle /independent/trust.json --request-id fresh-install --json`.
   Use the returned installed `runtime_root/bin/quirkbench status --json`, then
   explicit `setup`/native service/builder preparation and `recovery download` as
   documented in [installation](controller-installation.md). Status must recheck
   installed signature/immutable runtime independently of the download cache.
   Confirm the input delivery/retention handoff before investigations. A fresh
   install or replay must fail on wrong trust, expired metadata, changed bytes or
   a different statement under the same request/version. Installing an older
   matching version is an explicit operator choice, not an automatic upgrade or
   rollback policy; activation still reconciles active work and readiness.

Rotation is explicit: independently authenticate a new public key/fingerprint/
byte SHA and validity interval, provision a new trust bundle, and sign new version
statements with that identity. Preserve the old bundle separately for historical
verification while valid, or explicitly withdraw it if compromised. Existing
installed releases verified under an incompatible/expired current bundle lose
signed readiness; do not silently grandfather them or accept both keys from the
release site. Stop dependent setup, obtain an authorized new compatible release,
and verify again. Never reuse target enrollment trust as publisher trust.

Production key choice/provisioning, exact input availability and consumer handoff,
image production, hosting/upload and native acceptance remain operator inputs.
Software preparation does not satisfy the physical gate or authorize a release.

# Recovery acquisition specifications

The current Fedora adapter accepts an explicit immutable specification:

```sh
quirkbench recovery-inputs acquire-plan /SELECTED_STATE/inputs/GENERATION --spec /absolute/candidate.json
```

This records a plan, not a download. Execute its returned `argv` only as an explicit
product operation. The specification's canonical digest is bound into its versioned
retention-owner identity and retained in CAS; both download and locking verify that
same identity. Missing or changed bytes never select a different candidate. The
repository directory must contain exactly its declared files, with matching hashes.

[RecoveryAcquisition v1](../schemas/recovery-acquisition.v1.schema.json) contains:

- `candidate_id`, implemented `platform_adapter_id`, Fedora release and exact kernel release.
- Full RPM signing-key fingerprint and the reviewed userspace package snapshot
  (`name`, exact `nevra`, `sha256`), using the existing snapshot validation.
- Repository records: a direct `.repo` `name`, exact UTF-8 `content`, SHA256 of those
  bytes and explicit enabled `ids`. Retain reviewed non-secret configuration only.

The reviewed catalog/input preparation supplies real package identities; do not
invent hashes or replace unavailable versions. Repository configuration can use
Fedora's release/architecture substitutions, whose values are selected explicitly
by this adapter. Signed package identities are verified before a usable v2 rootfs
lock is published. The key supplied to `lock --public-key` must match the spec's
fingerprint. Verification imports that one key into a fresh private RPM database
using `rpmkeys --import`, which initializes the database, then checks every RPM;
it does not use the host RPM trust database. Image assembly, boot-device storage policy and candidate exclusion
rules remain unchanged.

```sh
quirkbench recovery-inputs lock /SELECTED_STATE/inputs/GENERATION/rpms \
  --spec /absolute/candidate.json --public-key /absolute/reviewed-key \
  --builder-image-digest sha256:ACTUAL_DIGEST \
  --diagnostics /SELECTED_STATE/inputs/VERIFICATION_GENERATION
```

`lock` obtains the authoritative spec from the completed acquisition owner; an
optional `--spec` must match it. It does not trust a newly edited local candidate
file. Failed acquisition or signatures preserve diagnostics without usable inputs.

For compatibility, `acquire-plan` without `--spec` selects the named historical
Fedora candidate and freezes the observed Fedora/updates repository bytes into the
same bound v1 specification before producing a command. It reports
`historical-candidate-compatibility`; missing repositories require explicit `--spec`.
Previously registered owners and recorded commands remain legacy readers with their
original interpretation. They are not templates for new platform support.

Changing a release, kernel, repository or key means a separately reviewed candidate
specification and fresh generation. No downloads, kernel/image builds or release
qualification occur simply because this metadata or adapter is edited.

`recovery-inputs recipe` accepts an explicit `--id`; when omitted, its ID is
`stock-recovery-LOCK_SHA256`, derived from the verified lock. It carries no assumed
release or issue name. Previously generated recipe IDs and bytes remain unchanged.

## Reviewed pairing candidate

`recovery-inputs candidate-spec --candidate fedora44-pairing-v1 --repository
/absolute/reviewed.repo --repository-id SELECTED_REPO_ID` prints a complete v1
specification; redirect it to a new candidate JSON file and pass that to
`acquire-plan --spec`. Repeat `--repository-id` for each explicitly selected ID.
This command reads supplied repository bytes and installed reviewed metadata only;
it does not create controller state, download packages or publish an image.

This selection adds `openssl-1:3.5.8-1.fc44.x86_64` to the historical Fedora44
userspace closure without upgrading its libraries or kernel. Its signed RPM SHA256
is `7481bac5460237c7b2105c067b18e2ab9109e82a7129a6e0c11c42cfe3f6d9c7`.
Reviewed source: [Fedora signed Koji RPM](https://kojipkgs.fedoraproject.org/packages/openssl/3.5.8/1.fc44/data/signed/6d9f90a6/x86_64/openssl-3.5.8-1.fc44.x86_64.rpm),
verified against Fedora44 public fingerprint
`36F612DCF27F7D1A48A835E4DBFCF71C6D9F90A6`. The matching signed `openssl-libs`
RPM equals the historical libraries digest. `gnupg2` already supplies `gpg`.
The new snapshot is `stock-fedora44-pairing-rpm-candidate.v1.json`; historical
snapshot bytes, existing specifications and acquisition without `--spec` retain
their original meaning. Pairing needs this explicit selection, not a manual
package-list edit. Missing/nonexecutable tools or paths escaping the target sysroot
block stock staging and complete-image preparation, including cache replay.
Repository availability, signature verification and native pairing remain separate
outcomes; a signed input is not a boot or pairing acceptance result.

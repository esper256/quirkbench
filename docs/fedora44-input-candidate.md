# Fedora 44 kernel input candidate — 2026-09-28

This is an acquisition record, not an installed supported baseline or a
qualified image. The installed catalog remains empty. The candidate is for
the generic x86-64 UEFI external-USB recovery profile.

Fedora's [stable download page](https://fedoraproject.org/server/download/)
listed Fedora 44 on this date. Fedora's
[package-signing key list](https://fedoraproject.org/security/) identifies its
key as `36F612DCF27F7D1A48A835E4DBFCF71C6D9F90A6`.

## Commands and retained bytes

The initial sandbox repository query could not resolve Fedora's mirror service.
The same read-only metadata query succeeded with network access:

```sh
XDG_STATE_HOME=/tmp/quirkbench-dnf-state dnf5 --releasever=44 \
  --setopt=cachedir=/tmp/quirkbench-dnf-cache --disablerepo='*' \
  --enablerepo=fedora --enablerepo=updates repoquery --available \
  --latest-limit=1 --sourcerpm kernel
# kernel-7.2.7-200.fc44.src.rpm

XDG_STATE_HOME=/tmp/quirkbench-dnf-state dnf5 --releasever=44 \
  --setopt=cachedir=/tmp/quirkbench-dnf-cache --disablerepo='*' \
  --enablerepo=updates --enablerepo=updates-source download --srpm \
  --destdir=.quirkbench/inputs/fedora44-kernel-7.2.7 \
  kernel-7.2.7-200.fc44.x86_64
```

The retained SRPM is
`.quirkbench/inputs/fedora44-kernel-7.2.7/kernel-7.2.7-200.fc44.src.rpm`,
SHA256 `7dfc6f39d52fbae59e0024fc1bc1d666900b6848d7e1fff53813d8b6b03aa5e8`.
The downloaded bytes occupy about 159.3 MiB. The extracted tree and original
SRPM occupy about 327 MiB locally; both are ignored by Git.

An isolated RPM key database received only the host's Fedora 44 primary key,
whose full fingerprint matched Fedora's published list. Verification returned
`Header OpenPGP ...: OK`, `Header SHA256 digest: OK`, and
`Payload SHA256 digest: OK`:

```sh
rpm --dbpath /tmp/quirkbench-fedora44-rpmdb --import \
  /etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-44-primary
rpmkeys --dbpath /tmp/quirkbench-fedora44-rpmdb --checksig -v \
  .quirkbench/inputs/fedora44-kernel-7.2.7/kernel-7.2.7-200.fc44.src.rpm
rpm --dbpath /tmp/quirkbench-fedora44-rpmdb \
  --define '_topdir /var/home/eric/dev/quirkbench/.quirkbench/inputs/fedora44-kernel-7.2.7/extracted' \
  -ivh .quirkbench/inputs/fedora44-kernel-7.2.7/kernel-7.2.7-200.fc44.src.rpm
```

The SRPM contains `SOURCES/kernel-x86_64-fedora.config`, SHA256
`cd11b96fabf3cbdaf1063eff6b66fb012edba411762ad994e3eed6236911ce18`,
and `SPECS/kernel.spec`, SHA256
`2a5e8ca46b9ced20510bd068cb16c5a1fc7bfb9067ff403cf6414dd461470885`.
These are source inputs. The stock Fedora config enables internal storage
controllers and must never be used as the final protected recovery config.

An exploratory DNF5 target-userspace dependency solve selected 205 x86-64 and
noarch RPM URLs from Fedora 44 `fedora` and `updates` with weak dependencies
disabled. Its direct requests were `fedora-release`, `systemd`, `dbus-daemon`,
`NetworkManager`, `NetworkManager-tui`, `wpa_supplicant`, `ca-certificates`,
`python3`, `python3-gobject`, `ostree`, `ostree-libs`, `e2fsprogs`, `util-linux`,
`grub2-tools`, `gdisk`, `dosfstools`, `mtools`, `linux-firmware`, and `dracut`.
This is only a dependency review candidate: no package bytes were retained or
installed, the set lacks an audited optional-firmware/weak-dependency decision,
and mirror URLs are not an immutable RPM lock. `rpm-ostree` was excluded from
the target proposal because the current target deployment adapter invokes
`ostree`; controller-side composition is a separate environment.

```sh
XDG_STATE_HOME=/tmp/quirkbench-dnf-state dnf5 --releasever=44 \
  --setopt=cachedir=/tmp/quirkbench-dnf-cache \
  --setopt=install_weak_deps=False --disablerepo='*' \
  --enablerepo=fedora --enablerepo=updates download --resolve --alldeps \
  --arch=x86_64 --arch=noarch --url --urlprotocol=https \
  fedora-release systemd dbus-daemon NetworkManager NetworkManager-tui \
  wpa_supplicant ca-certificates python3 python3-gobject ostree ostree-libs \
  e2fsprogs util-linux grub2-tools gdisk dosfstools mtools linux-firmware \
  dracut > /tmp/quirkbench-f44-rpm-urls-target.txt
```

## Rootless builder base and retained RPM candidate

The Bazzite controller host's rootless Podman store now contains the Fedora 44
container image pulled as `registry.fedoraproject.org/fedora:44`. Its inspected
amd64 image manifest digest is
`sha256:fb31d002de20bfa7742b8c9b0d0ff723bb9fa2534fd43ecac0101a35f703fef0`
and config ID is
`7a28ae28b2bd1c1c30f163d5d8a264c3923978c7d7dec32f7b168ce5e2f19812`.
A short rootless run using the digest reference read `VERSION_ID=44` and
`VARIANT_ID=container` from `/etc/os-release`. These are candidate base bytes,
not a dedicated Quirkbench builder with the reviewed toolchain and marker.
The floating `:44` tag must not be used in a locked recipe.

```sh
distrobox-host-exec podman pull registry.fedoraproject.org/fedora:44
distrobox-host-exec podman image inspect registry.fedoraproject.org/fedora:44
distrobox-host-exec podman run --rm --pull=never \
  registry.fedoraproject.org/fedora@sha256:fb31d002de20bfa7742b8c9b0d0ff723bb9fa2534fd43ecac0101a35f703fef0 \
  cat /etc/os-release
```

A second DNF5 solve retained weak dependencies and explicitly added `parted`
and `NetworkManager-wifi`; Fedora 44 ships the latter as a separate RPM. Its
248 URL rows are saved as `urls.txt` in the candidate records directory. The
downloaded 248 RPMs occupy about 385 MiB under the ignored
`.quirkbench/inputs/fedora44-rpm-candidate/` directory. The isolated Fedora 44
RPM key database at `/tmp/quirkbench-fedora44-rpmdb` verified each RPM's header
signature with fingerprint `36F612DCF27F7D1A48A835E4DBFCF71C6D9F90A6`,
header SHA256 and payload SHA256. The verification and download logs were
copied into the candidate records directory as `signatures.log` and
`download.log`.

```sh
XDG_STATE_HOME=/tmp/quirkbench-dnf-state dnf5 --releasever=44 \
  --setopt=cachedir=/tmp/quirkbench-dnf-cache \
  --setopt=install_weak_deps=True --disablerepo='*' \
  --enablerepo=fedora --enablerepo=updates download --resolve --alldeps \
  --arch=x86_64 --arch=noarch --destdir=.quirkbench/inputs/fedora44-rpm-candidate \
  fedora-release systemd dbus-daemon NetworkManager NetworkManager-tui \
  NetworkManager-wifi wpa_supplicant ca-certificates python3 python3-gobject \
  ostree ostree-libs e2fsprogs util-linux grub2-tools gdisk dosfstools mtools \
  linux-firmware dracut parted
rpmkeys --dbpath /tmp/quirkbench-fedora44-rpmdb --checksig -v \
  .quirkbench/inputs/fedora44-rpm-candidate/*.rpm
```

The read-only candidate inspector checked that the downloaded filenames exactly
matched the resolved URL filenames, queried each RPM header, rejected duplicate
identities and hashed the bytes. It wrote candidate metadata under the ignored
`.quirkbench/inputs/fedora44-candidate-records/` directory:

| Record | SHA256 |
| --- | --- |
| `rpm-snapshot.v1.json` | `d0977c45a368e09a965f5bf8874f0eafd2652ad369dc095e6cf00ba0c2106953` |
| `target-rpm-lock.txt` | `fca5fc645a43d2e65ef365c838f1274ac7bf98259c7b63c951e449edc6349ea7` |
| `urls.txt` | `edf8180a9a8a70b7bd99988f6aeab394213cd731a7053f80f0a2f8bafae9bd86` |
| `signatures.log` | `55fb2cb1885379c2a9ebbcd1b9206e9b3f34c1c330881962c4ff3953249a4474` |

These hashes describe retained candidate bytes. They do not establish that the
package set boots, contains every required runtime capability, or has been
approved for the installed baseline catalog. The RPMs have not been retained
in controller CAS or installed into a recovery rootfs.

## Built builder candidate

The existing [builder Containerfile](../environments/Containerfile) now rejects
a tag-based base reference and checks the Fedora release inside the pinned base.
The rootless Podman build completed with exit code 0 from the base digest above.
The locally built image's inspected manifest digest is
`sha256:a07bad43009c174d043d988892267983f2ac3976547975a5426a8008e85b4473`;
its config ID is
`9849a6e8d23e70a92d1843519afe73085032aa987871075a888a77f13691caf0`.
It reports Fedora 44 and carries `quirkbench-fedora-rootless-build-v1` plus the
exact base-digest marker. The installed candidate contains 595 RPMs. Its
toolchain reports GCC 16.2.1, GNU ld 2.46.1, GNU Make 4.4.1 and dracut
108-8.fc44.

The ignored `.quirkbench/inputs/fedora44-builder-candidate/` directory retains
the build command, log, exit status, exact installed RPM lock and toolchain lock.
It also retains the two preflight rejection logs: a moving `:44` tag and a
digest-pinned Fedora 44 base falsely declared as release 43 both stopped before
DNF ran.
The directory contains a 402,117,632-byte OCI archive. Its manifest digest
is `sha256:070abcc5ae2096fda0bf4fed3e945449213273301b6773ba094ac89c1141669b`
after OCI export; its config digest matches the local image ID above. The
recipe's `builder_image_digest` binds the **Fedora base digest marker**, while
the built image and its package/toolchain locks are separate retained inputs.

| Builder candidate record | SHA256 |
| --- | --- |
| `builder-rpm-lock.txt` | `1ff2255735cf3e4e33cf2eb73708ebfd3fb9b8dd88f8527995ce0c59a2c37848` |
| `toolchain-lock.json` | `87eb704a94b520b1262b3911fed66c29e478bef06d5e44f6a0d7d8991b824fb3` |
| `builder.oci.tar` | `3ab46bf3afcd9c09bb77c9603cef330d9c80e456f2cdb383193af49947acd13f` |

The package header audit of the 248 recovery RPMs found providers for systemd,
D-Bus, NetworkManager, `nmcli`, `nmtui`, `partprobe`, `sgdisk`, `mkfs.ext4`,
`ostree`, Python and dracut. Its read-only report is
`.quirkbench/inputs/fedora44-candidate-records/runtime-path-providers.json`,
SHA256 `f9c64979a72bea191cfa563a5830ded5d3c90c217b74bba729df3bb7397a4afe`.
This checks package payload paths, not installed runtime behavior or boot.

A later read-only OCI inspection of the retained older builder archive verified
its sole manifest, `amd64`/Linux config digest
`sha256:9849a6e8d23e70a92d1843519afe73085032aa987871075a888a77f13691caf0`
and both referenced layer digests. The ignored
`fedora44-builder-candidate/oci-inspection-v1.status.json` records the exact
archive SHA-256 above, inspector source SHA-256, result and time; its own SHA-256
is `d5aa20a77e4f79876e311d3df5db1fa7d98db3361119006b8778b037270487fa`.
This verifies retained candidate bytes, not the rebuilt builder that source
preparation now requires.

## Builder containment finding

A dry run of `distrobox assemble create --dry-run --file
environments/assemble.ini` showed that this Distrobox profile would use
`--privileged`, bind the host `/dev`, and share host PID and network namespaces.
The command was **not** executed; `distrobox list` still showed only the existing
`dev` box. Rootless user mapping alone does not meet the recovery synthesis rule
that physical block devices must not be attached to its builder.

A separate, short rootless Podman probe using the pinned candidate image,
`--network=none`, no host mounts, `--cpus=4` and `--memory=4g` reported no block
device nodes, `cpu.max=400000 100000`, and `memory.max=4294967296`. Container
UID 0 mapped to controller-host UID 1000. This proves the restricted profile
can start; it does not yet validate the input/output mount design or authorize
a real rootfs/image stage. The operator approved a separate restricted Podman
worker while retaining Distrobox for development. The P3a1 command adapter
now stages copies of exact locked inputs under a private claimed directory,
plans only five fixed binds with `:Z` labeling, clears remote Podman connection
settings and disables Podman cgroup creation. A worker-boundary review found
that the first draft's arbitrary writable mount, disabled SELinux labeling
and independent Podman cgroup placement were unsafe; those draft choices were
removed. The revised plan has focused software coverage but is not executed.
The systemd user-service adapter now requests and checks CPU, memory, swap
and task cgroup limits. It still needs live container-process membership and
termination evidence, a durable rootfs dispatcher and logs before execution
is enabled.

The local rootfs command planner now verifies the current SQLite worker claim
and the caller's service cgroup before returning an argv. Input staging takes
catalog and rootfs-lock CAS digests instead of host file paths; it reads each
metadata object through a bounded no-follow descriptor and copies the locked
closure with bounded, hashed bulk copies. These checks do not establish that
the pinned builder image, its descendants or the final output are ready for
production execution.

## Remaining baseline work

### Offline protected-config and Fedora source-prep diagnostic

The retained `kernel-x86_64-fedora.config` and reviewed
`target-assets/recovery-kernel.fragment` now merge locally. Fedora Kconfig has
15 legitimate mixed-case symbol names, which the original parser rejected;
the base and final-config readers now accept them while the recovery override
set stays exact. The ignored `merged-recovery.config` is 266,972 bytes with
SHA-256 `95d6cc99896aa934dc4582946566b8cca600b5b3e8fe1461f1080f22f3f92029`.
Its input/digest record `merged-recovery-config.record.json` has SHA-256
`77f687d38980270ab28242f8b101f9dea4e6b0688a8a37912468118dbef94406`.
That record describes the earlier fragment bytes and is superseded by the
provisional selector diagnostic below. It is **pre-`olddefconfig`** and has not
passed the resolved-config or module audit.

One offline `%prep` diagnostic used the retained local builder image, the
signed SRPM copied into a private stage, `--pull=never`, `--network=none`,
rootless Podman, and only that stage as a bind mount. It unpacked the Fedora
source and applied `patch-7.2-redhat.patch`, then exited 1 when the spec reached
the absent `%py3_shebang_fix` macro, before Fedora's generated-config step.
The source tree is **incomplete and unusable for a kernel build**. The ignored
`fedora44-source-prep/` directory retains `command.json`, `prep.sh`, `prep.log`,
`exit.status` and `status.json` (SHA-256
`2339b951dacf0b869bca76fe2cb3fc1fa13e3d7aa197df2bc9eb7f9c00401b59`).
No repository request or image pull occurred. This one-off diagnostic used an
ephemeral Podman cgroup; production recovery workers still require the
controller-owned user service and complete process containment.

The existing local builder image ID
`9849a6e8d23e70a92d1843519afe73085032aa987871075a888a77f13691caf0`
does not contain `python3-rpm-macros`, `kernel-rpm-macros` or
`python3-jsonschema`. The Containerfile now names these packages explicitly,
and source staging checks the required shebang macro before unpacking. A future
builder rebuild changes its image identity and RPM/toolchain locks; the existing
OCI archive remains evidence for the earlier candidate only.

An offline `olddefconfig` diagnostic on the partially prepared patched tree
showed that Fedora's `CONFIG_KEXEC_HANDOVER` selects `CONFIG_KEXEC_FILE`, while
the NVMe RDMA, FC, TCP and target-loop options select `CONFIG_NVME_FABRICS`
and therefore the hidden `CONFIG_NVME_CORE`. These selectors overrode the
earlier fragment's exclusions. The reviewed fragment and final-config audit
now also require `KEXEC_HANDOVER`, `NVME_RDMA`, `NVME_FC`, `NVME_TCP` and
`NVME_TARGET_LOOP` to be off. This excludes recovery-side remote NVMe and
NVMe loopback as well as kexec handover; ordinary network and USB storage
support remain separate.

A second offline `olddefconfig` diagnostic used the revised fragment, the
same local builder image and only the private source-prep stage as a bind.
It exited 0, and `validate_recovery_final_config` accepted the resolved
config for the generic x86-64 UEFI/USB profile. The revised fragment SHA-256
is `9a95dae8dd6c335038c1f67a0b4600af8d35166d09afe195f50d458f47947f0c`;
the merged input is `868e062aacd20c6082c9a2fe857e66fa27e4facd08e31aaa8874b5cb1adea7db`,
and the resolved config is `1dec792a0cfa1312469e4ef52d5103ab2299e2efdf0d1feaac55ca810769d0a2`.
The ignored `config-resolve-v2.status.json` records the exact command, local
image ID, script, input and output hashes, log and exit status; its SHA-256 is
`542fc3c822773c378d083a7696783cf675900935e6a7e6aa3bca5e2ab0df98ed`.
This is **provisional** because `%prep` did not complete. The revised fragment
also changes the recipe/lock identity: previously retained fragment and merged
config records must not be used for a new build. Repeat `olddefconfig` and the
module audit after a rebuilt builder completes Fedora's full source prep.

On 2026-09-29, a local-only retention pass copied the exact 248 candidate RPMs
and ten associated source, lock, configuration and recipe files into the
separate ignored `.quirkbench/inputs/fedora44-candidate-cas/` store. It read
only retained files, made no repository or network request, and verified all
258 published CAS objects after copying. The manifest at
`.quirkbench/inputs/fedora44-candidate-records/cas-retention.json` has SHA-256
`ac34fcccb3d95bc66db16b58c3a862f990340a53d201846cc553ee0f555decad`
and describes 569,820,307 source bytes. The exact command, exit code 0, script
identity and completion time are in `cas-retention-status.json`; the ignored
`retain-cas.py` script has SHA-256
`e449a2fb11a23598d3c24d2a4534a1115efe283fea33e5f67abe662a0eba7372`.
This is a candidate input store, not an installed supported baseline or a
completed backup of controller state. The builder OCI archive remains retained
separately with the identity above.

At that point, the CAS set did not settle the reviewed repository configuration,
build-recipe record, protected resolved kernel configuration, installed Fedora
unit/generator policy or rootfs behavior. The catalog could not be activated
from those bytes alone.

An offline RPM header audit of the same 248 retained bytes inventoried systemd
payloads without installing packages. The local RPM snapshot and target lock
were rehashed and still match the SHA-256 values above. Its ignored report,
`.quirkbench/inputs/fedora44-candidate-records/systemd-payload-inventory.json`,
has SHA-256
`1f2b613fc4a4580caf80bfa881478abff17d92829c64f74922676c0041d0ee3d`.
It records 87 vendor enablement links, no `/etc/systemd/system` symlinks, 17
vendor system generators, four preset files and 282 vendor unit files. In
particular, the payload includes links for `systemd-repart.service` and
`systemd-repart.socket`, and files for
`systemd-gpt-auto-generator` and `systemd-factory-reset-generator`.
These are package contents, not proof that an installed recovery system
activates them. The exact vendor link graph, generators and masks still needed
review before catalog admission. Runtime allowlists were empty and rejected
vendor links and generator bytes until that review.

At this earlier point, builder, closure, source and installed runtime review
remained. The `dev` Distrobox did not expose `podman` on its PATH, but the
Bazzite controller host had rootless Podman and systemd user services.
`rpmbuild` belonged inside the builder and did not need host installation.

## 2026-09-29 first-boot input update

The paragraphs above describe the earlier candidate state. The rebuilt Fedora
44 rootless builder now includes the missing RPM macros. Its retained OCI
archive is `.quirkbench/inputs/fedora44-builder-rebuild-20260929/builder.oci.tar`
(402,904,064 bytes; SHA-256
`e879d3822960da0b1cb59e77deae2d2a0f602455d96a4892a9628e1f91cc584d`),
with local configuration ID
`sha256:6e51e11c610ebfb6560c231ced072827ade8eaea4a1e82ca0447e691021325db`.
The exact builder RPM lock SHA-256 is
`63919756df48cfdf5ceadcfffb22ef52d85ed8ba47bf97956d53840394b67a03`;
the toolchain lock SHA-256 is
`87eb704a94b520b1262b3911fed66c29e478bef06d5e44f6a0d7d8991b824fb3`.
The original Fedora base digest remains
`sha256:fb31d002de20bfa7742b8c9b0d0ff723bb9fa2534fd43ecac0101a35f703fef0`.

Full Fedora `%prep` completed in the restricted rootless worker. Its prepared
source and final `olddefconfig` accepted the protected generic profile; the
resolved configuration SHA-256 is
`1dec792a0cfa1312469e4ef52d5103ab2299e2efdf0d1feaac55ca810769d0a2`.
The reviewed fragment SHA-256 is
`65a6e41e5712ddc46ff6ba1b6baa27bb03251d8dabbda55083422066dcbe89c9`.
The isolated Fedora key database verified all 248 retained target RPM
signatures, headers and payload digests. An offline locked DNF5 installroot
completed in `.quirkbench/inputs/fedora44-rootfs-diagnostic-v2-20260929/`.
Its 87 vendor enablement links, 17 generator binaries and 33 scriptlet-created
`/etc/systemd/system` links were inventoried against exact Fedora 44 bytes.
The generic runtime removes unwanted scriptlet enables and masks automatic
repartition, factory reset, TPM clear, firmware-update and other unrelated
boot actions. Unknown Fedora vendor graphs fail closed.

The installed catalog now contains the exact `fedora44-firstboot-v1` baseline,
and the rootfs lock and current recovery recipe live in
`.quirkbench/inputs/fedora44-firstboot-candidate-20260929/`. Their referenced
objects are retained in the private candidate CAS. This selects inputs for an
**unqualified first-boot recovery image**; it does not qualify a target,
experimental kernel, release or unattended use. The protected kernel build is
running in the bounded host user service
`quirkbench-fedora44-kernel-build-20260929.service`, with persistent logs and
an eventual `exit.status` in its matching input directory. Its result must be
checked before any image is assembled.

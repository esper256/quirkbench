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

## Remaining baseline work

Build and review a dedicated rootless builder from the pinned Fedora 44 base,
audit this candidate RPM closure and retain approved bytes in controller CAS;
prepare the SRPM source under that builder; apply and audit the reviewed recovery
fragment; inspect modules and generic initramfs; then activate one catalog entry
only when all retained bytes and protection checks match. The current `dev`
Distrobox does not expose `podman` or `distrobox` on its PATH, but the Bazzite
controller host has both. The controller service belongs under the host user
manager; a rootless builder can be launched from there. `rpmbuild` belongs inside
that builder and need not be installed on the controller host. Focused software
development can continue.

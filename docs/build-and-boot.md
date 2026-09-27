# Build and boot prototype (M2 unfinished)

**Do not commission this prototype as the finished USB lab.** The two-partition
layout and recovery shell are scaffolding. M2 must supply fixed recovery, a
separate GRUB state partition, journaled data, restartable expansion and the
target service before hardware use. No image was built or booted during M1.

This recipe builds target artifacts in a rootless Fedora Distrobox container, constructs
a **regular file** GPT image, and runs a UEFI trial in QEMU. It never writes a
physical USB drive, mounts a host disk, installs a host kernel, invokes DKMS, or
changes host EFI boot variables. Physical USB writing and hardware qualification
are separate, unimplemented steps.

## Prerequisites and inputs

The workstation must already have Podman and Distrobox for the build container.
The container image provides `make`, `dracut`, `sgdisk`, `mtools`, `mkfs.ext4`,
`grub-mkstandalone`, `grub-editenv`, and `dnf`. QEMU trials require
`qemu-system-x86_64`, `qemu-img`, and explicit OVMF code and variables-template
files. No credentials belong in the Containerfile, build arguments, rootfs, or
provenance manifest.

Use a kernel source archive with a verified upstream checksum. Keep the archive,
unpacked source, object tree, Fedora target sysroot, and output directory under the
project workspace. Provide an **independently built and tested** recovery
kernel, initramfs, and `.config`, as well as the debug kernel's `.config`.
Both kernel configs must satisfy `validate_kernel_config`. In particular,
NVMe, ATA, MMC, virtio PCI/SCSI/block, SCSI low-level controllers, swap,
hibernation, kexec, /dev/mem, and EFI variable persistence support must be
disabled in **both** kernels. USB xHCI, USB mass storage, SCSI disk, GPT, FAT,
ext4, EFI, and initramfs support must be built in. A config file alone cannot
prove that an arbitrary kernel binary was built from it; bind artifacts to the
same source and build in the provenance record.

## Create the build container

Choose a verified immutable Fedora base digest and build the local image:

```sh
podman build \
  --build-arg BASE_IMAGE='fedora@sha256:<verified-64-hex-digest>' \
  -t localhost/quirkbench-build:local \
  -f environments/Containerfile .
distrobox assemble create --file environments/assemble.ini
distrobox enter quirkbench-build
```

The `<...>` placeholder must be replaced. The Containerfile installs packages
inside the container only. Capture the base image digest and installed package
versions after resolution; repeatable rebuilds also require an RPM snapshot or
package mirror that retains those exact versions. The provenance code rejects a
mutable base image name in its record.

## Build in staged steps

Inside the container, use absolute paths for every argument. First construct
the minimal Fedora target rootfs with an explicit Fedora release. Package
installation requires UID 0 **inside the rootless container user namespace**;
it never uses host `sudo` or the host package manager. Start the Distrobox,
then run the script as container UID 0 via rootless Podman, passing the project
path mounted in that container. The script refuses an existing output.

```sh
podman exec --user 0 quirkbench-build \
  /absolute/path/to/quirkbench/target-assets/build-rootfs.sh \
  <fedora-release-number> /absolute/path/to/quirkbench/work/rootfs
```

The following Python sketch runs the explicit kernel commands inside that
container. Keep `PYTHONPATH` pointed at this project's `src` directory. The
source tree must contain `scripts/config`. The target sysroot must be the
Fedora rootfs just built, with its installed dracut userspace and systemd.

```python
from pathlib import Path
import subprocess
from quirkbench.build import (
    KernelBuild, build_lock, capture_package_lock, capture_target_package_lock,
    run_commands, validate_kernel_config, write_provenance,
)

project = Path('/absolute/path/to/quirkbench')
work = project / 'work'
build = KernelBuild(
    source=work / 'linux-source',
    build_dir=work / 'kernel-obj',
    sysroot=work / 'rootfs',
    output_dir=work / 'artifacts',
)
for directory in (build.build_dir, build.output_dir):
    directory.mkdir(parents=True, exist_ok=True)

with build_lock(build.build_dir):
    configure = build.configure_plan()
    run_commands(configure)
    validate_kernel_config(build.build_dir / '.config')
    compile_commands = build.compile_plan()
    run_commands(compile_commands, config_to_validate=build.build_dir / '.config')
    release = subprocess.check_output(
        ('make', '-C', str(build.source), f'O={build.build_dir}',
         'ARCH=x86_64', 'kernelrelease'), text=True,
    ).strip()
    initramfs_commands = build.initramfs_plan(
        release, dracut_config=project / 'target-assets/dracut.conf'
    )
    run_commands(initramfs_commands, config_to_validate=build.build_dir / '.config')
capture_package_lock(work / 'artifacts/packages.lock')
capture_target_package_lock(build.sysroot, work / 'artifacts/target-packages.lock')
write_provenance(
    work / 'artifacts/kernel-provenance.json',
    source_archive=work / 'linux-source.tar.xz',
    config=build.build_dir / '.config',
    artifacts=build.artifacts(release),
    base_image_digest='sha256:<verified-64-hex-digest>',
    packages_lock=work / 'artifacts/packages.lock',
    target_packages_lock=work / 'artifacts/target-packages.lock',
    commands=(*configure, *compile_commands, *initramfs_commands),
)
```

`run_commands` refuses compilation or initramfs construction without protected
config validation. `olddefconfig` can change requested switches, so validate
the **resulting** `.config` after configure and again at the compile boundary.
The resource planner caps jobs at half the CPUs and half the available RAM,
with a 2 GiB reserve and about 2 GiB per compile job; it fails when it cannot
verify enough memory. `build_lock` serializes stages sharing one object tree.
`dracut --sysroot` uses the Fedora target userspace, not the workstation.
Install this project's target service into that rootfs before imaging. The
minimal rootfs contains no baked-in user credentials.

## Construct the regular-file image

`create_image` requires a new output path, a marked Fedora target rootfs with
`/sbin/init`, two distinct kernel/initramfs pairs, and both protected configs:

```python
from pathlib import Path
from quirkbench.image import ImageInputs, create_image

work = Path('/absolute/path/to/quirkbench/work')
manifest = create_image(ImageInputs(
    output=work / 'artifacts/quirkbench-usb.img',
    debug_kernel=work / 'kernel-obj/arch/x86/boot/bzImage',
    debug_initramfs=work / 'artifacts/initramfs-<release>.img',
    recovery_kernel=work / 'recovery/vmlinuz',
    recovery_initramfs=work / 'recovery/initramfs.img',
    rootfs_dir=work / 'rootfs',
    debug_config=work / 'kernel-obj/.config',
    recovery_config=work / 'recovery/.config',
))
print(manifest)
```

The image has a GPT, a FAT32 EFI System Partition with the removable UEFI path
`/EFI/BOOT/BOOTX64.EFI`, and an ext4 root partition. The code uses sparse
regular files and copies filesystem bytes into the image at calculated offsets.
It uses no loop devices. It refuses an existing image or manifest and paths
under `/dev`, `/proc`, `/sys`, `/run`, `/media`, or `/mnt`.

GRUB starts at the fixed **Recovery** entry. The **Debug once** entry is
selected only when `next_entry=debug` was set in the USB ESP's `grubenv` and
GRUB can clear, save, and reload that setting. If the environment read/write
check fails, it remains on Recovery. Recovery starts a local shell with
`init=/bin/sh`; this deliberately does not require a stored password. To arm
the next boot, edit the copied image's FAT ESP through a regular-file FAT tool
or, after a controlled physical qualification step, use `grub-editenv` on the
USB ESP's `EFI/BOOT/grubenv` to set `next_entry=debug`. Do not use `grub-reboot`
against a host installation or use `efibootmgr`. Verify one-shot reset on the
actual firmware before relying on it. The [GNU GRUB environment-block manual](https://www.gnu.org/software/grub/manual/grub/html_node/Environment-block.html)
describes the storage limits for `save_env`.

## UEFI trial and evidence

`run_qemu` creates a disposable qcow2 overlay for the USB image, copies the
OVMF variables template, and creates a 64 MiB internal-disk sentinel file.
The QEMU command uses TCG and regular files only. It hashes the sentinel and
template before and after the trial and raises an error if either changed.

```python
from pathlib import Path
from quirkbench.qemu import QemuInputs, run_qemu

work = Path('/absolute/path/to/quirkbench/work')
trial = work / 'qemu-trial-001'
trial.mkdir()
result = run_qemu(QemuInputs(
    image=work / 'artifacts/quirkbench-usb.img',
    ovmf_code=Path('/absolute/path/to/OVMF_CODE.fd'),
    ovmf_vars_template=Path('/absolute/path/to/OVMF_VARS.fd'),
    work_dir=trial,
    timeout_seconds=120,
))
print(result.serial_log, result.timed_out, result.exit_code)
```

A completed QEMU process or timeout alone is **not** a successful boot claim.
Inspect `serial.log` for a target-produced boot marker, confirm the recovery
entry boots first, arm `next_entry=debug` in a fresh overlay, and confirm that
the next boot selects Debug once and the following boot returns to Recovery.
The trial also needs to verify that the target reports no internal block
devices and cannot write EFI variables. None of these guest observations have
been performed in this repository environment. Secure Boot signing, USB media
writing, firmware-specific behavior, power-loss recovery, and real hardware
qualification remain unimplemented.

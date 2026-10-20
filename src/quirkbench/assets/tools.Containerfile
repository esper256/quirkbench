FROM @BASE@
RUN dnf -y install rpm-build python3 python3-devel python3-aiohttp python3-asyncssh python3-cryptography ostree rpm-ostree skopeo e2fsprogs dosfstools grub2-tools grub2-tools-extra grub2-efi-x64-modules util-linux dracut dracut-network && dnf clean all

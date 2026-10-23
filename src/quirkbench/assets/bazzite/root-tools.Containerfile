FROM @BASE@
RUN dnf -y install ostree-devel libarchive-devel gcc libguestfs-tools-c kernel-core && dnf clean all

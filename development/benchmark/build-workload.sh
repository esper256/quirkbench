#!/bin/bash
# Runs only inside the disposable workload tools container.
set -euo pipefail
export SOURCE_DATE_EPOCH=1792713600
mkdir -p /work/extracted /work/rpmbuild/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}
for version in 7.2.9 7.2.10; do
  release=$version-200.fc44.x86_64
  mkdir -p /work/extracted/$release
  (cd /work/extracted/$release; rpm2cpio /inputs/kernel-core-$release.rpm | cpio -idmu --quiet)
  bash /usr/src/kernels/$release/scripts/extract-vmlinux \
    /work/extracted/$release/lib/modules/$release/vmlinuz > /work/extracted/$release/vmlinux
  readelf -n /work/extracted/$release/vmlinux > /work/extracted/$release/notes.txt
  for revision in 0 1 2; do
    # Initial kernel needs only revision zero. Later steps share the second kernel.
    if [ "$version" = 7.2.9 ] && [ "$revision" != 0 ]; then continue; fi
    module=/work/module-$version-$revision
    mkdir -p "$module"
    cp /recipe/probe.c "$module/qb_benchmark_probe.c"
    printf 'obj-m := qb_benchmark_probe.o\nccflags-y := -DQB_REVISION=%s\n' "$revision" > "$module/Makefile"
    make -C /usr/src/kernels/$release M="$module" -j2 modules
    # This checks the compiled module uses the expected kernel ABI before packaging.
    test "$(modinfo -F vermagic "$module/qb_benchmark_probe.ko" | cut -d' ' -f1)" = "$release"
    cp "$module/qb_benchmark_probe.ko" /work/rpmbuild/SOURCES/probe.ko
    cat > /work/rpmbuild/SPECS/payload.spec <<EOF
Name: qb-benchmark-payload
Version: 1
Release: ${version}_${revision}
Summary: Controlled Quirkbench debugging comparison payload
License: GPL-2.0-only
Source0: probe.ko
Source1: strace
%description
A diagnostic module and unchanged official strace binary for a disposable VM.
%install
mkdir -p %{buildroot}/usr/lib/modules/$release/extra %{buildroot}/usr/libexec/qb-benchmark
cp %{SOURCE0} %{buildroot}/usr/lib/modules/$release/extra/qb_benchmark_probe.ko
cp %{SOURCE1} %{buildroot}/usr/libexec/qb-benchmark/strace
chmod 755 %{buildroot}/usr/libexec/qb-benchmark/strace
%files
/usr/lib/modules/$release/extra/qb_benchmark_probe.ko
/usr/libexec/qb-benchmark/strace
EOF
    rpmbuild -bb --define '_topdir /work/rpmbuild' --define '_buildhost qb-benchmark' \
      --define 'debug_package %{nil}' --define '__os_install_post %{nil}' \
      --define 'use_source_date_epoch_as_buildtime 1' /work/rpmbuild/SPECS/payload.spec
  done
done
rpm -qa | sort > /work/toolchain-packages.txt

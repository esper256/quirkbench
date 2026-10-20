Name: quirkbench-target
Version: 0.2.0
Release: 1
Summary: Quirkbench target connection and recovery tools
License: LicenseRef-Quirkbench
BuildArch: noarch
Requires: python3 >= 3.12
Requires: python3-aiohttp
Requires: python3-asyncssh
Requires: python3-cryptography
Requires: NetworkManager
Requires: NetworkManager-tui
Requires: util-linux
Requires: ostree
Requires: grub2-tools

%description
Outbound target connection, bounded recovery diagnostics and candidate deployment.

%install
mkdir -p %{buildroot}/usr/lib/quirkbench %{buildroot}/usr/bin %{buildroot}/usr/lib/systemd/system %{buildroot}/usr/lib/modprobe.d %{buildroot}/usr/lib/sysctl.d %{buildroot}/usr/lib/systemd/system.conf.d
cp -r /work/quirkbench %{buildroot}/usr/lib/quirkbench/
for item in target console; do
cat > %{buildroot}/usr/bin/quirkbench-$item <<SCRIPT
#!/usr/bin/python3
import sys
sys.path.insert(0, '/usr/lib/quirkbench')
from quirkbench.$item import main
main()
SCRIPT
chmod 755 %{buildroot}/usr/bin/quirkbench-$item
done
cp /work/quirkbench/assets/quirkbench-*.service %{buildroot}/usr/lib/systemd/system/
cat > %{buildroot}/usr/lib/modprobe.d/quirkbench.conf <<'CONFIG'
blacklist nvme
blacklist ahci
blacklist mmc_block
CONFIG
cat > %{buildroot}/usr/lib/sysctl.d/90-quirkbench.conf <<'CONFIG'
kernel.panic = 10
CONFIG
cat > %{buildroot}/usr/lib/systemd/system.conf.d/quirkbench.conf <<'CONFIG'
[Manager]
RuntimeWatchdogSec=60s
RebootWatchdogSec=2min
CONFIG

%files
/usr/lib/quirkbench
/usr/bin/quirkbench-target
/usr/bin/quirkbench-console
/usr/lib/systemd/system/quirkbench-*.service
/usr/lib/modprobe.d/quirkbench.conf
/usr/lib/sysctl.d/90-quirkbench.conf
/usr/lib/systemd/system.conf.d/quirkbench.conf

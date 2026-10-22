Name: quirkbench-target
Version: 1.1.0
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
mkdir -p %{buildroot}
cp -a /work/root/. %{buildroot}/

%files
%defattr(-,root,root,-)
/usr/lib/quirkbench
/usr/bin/quirkbench-target
/usr/bin/quirkbench-console
/usr/lib/systemd/system/quirkbench-*.service
/usr/lib/systemd/system-generators/quirkbench-startup-generator
/usr/lib/modprobe.d/quirkbench.conf
/usr/lib/sysctl.d/90-quirkbench.conf
/usr/lib/systemd/system.conf.d/quirkbench.conf

Name:           quirkbench
Version:        @VERSION@
Release:        @RELEASE@%{?dist}
Summary:        A Linux investigation lab for coding agents
License:        LicenseRef-Quirkbench
URL:            https://github.com/esper256/quirkbench
Source0:        quirkbench-%{version}.tar.gz
BuildArch:      noarch
BuildRequires:  python3-devel >= 3.12
BuildRequires:  python3-build
BuildRequires:  python3-hatchling
BuildRequires:  python3-installer
BuildRequires:  python3-wheel
Requires:       python3 >= 3.12
Requires:       podman
Requires:       sudo
Requires:       parted
Requires:       util-linux
Requires:       e2fsprogs
Requires:       dosfstools
Requires:       grub2-tools-minimal

%description
One foreground controller provides agent tools and connects to external-media
Linux experiments. Each user owns their configuration and investigation files.

%prep
%autosetup

%build
%{python3} -m build --wheel --no-isolation

%install
%{python3} -m installer --prefix /usr --destdir %{buildroot} dist/*.whl
# Target entry points are installed by the separate target RPM or recovery builder.
rm %{buildroot}%{_bindir}/quirkbench-target %{buildroot}%{_bindir}/quirkbench-console

%files
%{_bindir}/quirkbench
%{_bindir}/quirkbench-disk-writer
%{python3_sitelib}/quirkbench/
%{python3_sitelib}/quirkbench-*.dist-info/
%doc README.md docs/agent-guide.md docs/controller-installation.md

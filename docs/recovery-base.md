# Recovery image decision and synthesis contract

**Decision: use a minimal Fedora-derived recovery appliance, synthesized with DNF5,
dracut, systemd and GRUB through Quirkbench's existing build/image adapters.** No KIWI,
Lorax/Anaconda, desktop live-ISO remaster or second production image builder in v1.
This is the implementation decision, not an open tool-selection task.

Recovery is fixed for an investigation. Its job is setup, safe deployment preparation,
reconciliation and reliable evidence upload. It never runs experimental recipes or
promotes a candidate into recovery. Experiments remain exact signed rpm-ostree commits.
The [roadmap](product-roadmap.md) and [implementation contracts](implementation-contracts.md)
retain authority over enrollment, attempts, persistent state and protection.

## Why this choice

| Requirement | Chosen implementation |
| --- | --- |
| Fast boot | Minimal services, direct read-only ext4 root on USB, small non-host-only initramfs, no desktop/installer/update agent, no copying the whole root into RAM |
| Broad compatibility | Pinned Fedora kernel sources/configuration, broad modular peripheral/network support and Fedora firmware packages; reviewed protection changes only |
| Infrequent maintenance | Versioned recovery releases; normally refresh the Fedora base annually, before its support ends; earlier security/hardware fixes when needed |
| Simple networking | NetworkManager, nmtui, Fedora Wi-Fi authentication dependencies and explicit DNS/CA/clock handling |
| Predictable workloads | systemd supervision, bounded workers, immutable recovery code, isolated RAM runtime, independent evidence and durable upload retries |
| Few environmental surprises | No desktop, installer, automatic OS/firmware updates, automount or suspend; SELinux explicitly disabled in recovery |

A complete live-image remaster would supply a working general-purpose environment,
but we would still replace its storage, persistence and experiment boot policies.
Our recovery uses the same maintained Fedora components without carrying desktop or
installer workflows. Package selection and integration are our responsibility;
using upstream packages does not alone prove a usable appliance.

[KIWI's custom partition support](https://osinside.github.io/kiwi/working_with_images/custom_partitions.html)
has its own layout, fstab and expansion conventions. [Lorax livemedia-creator](https://weldr.io/lorax/livemedia-creator.html)
uses Anaconda/Kickstart and can produce disk/filesystem images as well as live media.
Both are capable tools. For this layout, adopting either would add an integration
boundary around already implemented identity-guarded assembly and commissioning.
That is an engineering judgment based on their documented interfaces and our code,
not a measured performance comparison or a claim that these tools cannot work.
Keep one small adapter around standard filesystem/partition tools; do not implement
filesystems, a package manager, bootloader, networking stack or physical USB writer.

## Platform and kernel policy

The first release supports x86-64 UEFI computers booting external USB storage.
Broad support means a generic image within that platform, not every architecture
or firmware. ARM, legacy BIOS and other boot transports require explicit adapters.
Boot compatibility and eligibility for unattended experiments are separate: recovery
can offer setup on hardware lacking a usable watchdog or dependable target UUID.

Start from a pinned Fedora kernel source RPM and that release's x86-64 configuration.
Rebuild using recorded sources/toolchain and a reviewed recovery fragment; do not
start from a small VM defconfig and guess individual laptop drivers. Preserve broad
USB, HID/I2C keyboard, framebuffer/console, Ethernet and Wi-Fi support. Include DMI
sysfs for the initial binding mechanism and a reviewed set of watchdog drivers;
shipping a driver is not authorization to arm it. Keep networking firmware packages
intact initially. Out-of-tree-only devices are explicit unsupported cases.

Retain the current internal-controller exclusions and firmware-write prohibitions,
plus positive external-device identity and write destination allowlists. Resolve
Kconfig dependencies and inspect the final kernel, modules and initramfs. A required
driver/protection conflict blocks support; broad compatibility never silently enables
internal storage access. The recovery kernel is not rebuilt for each enrolled target.
An alternate protection mechanism needs a separately reviewed architecture change.

Use dracut's generic/non-host-only mode, the explicit target sysroot and module tree.
The initramfs needs the supported boot-storage path and recovery-root mount, not
Wi-Fi authentication or controller contact. Load peripheral/network drivers from
the root filesystem through udev after switch-root. Do not prune boot support using
the controller's hardware or embed its host-only configuration. Module/firmware
compression follows pinned Fedora defaults unless measured boot data justifies a change.

## Userspace and security policy

Use Fedora RPMs for systemd/udev, D-Bus, NetworkManager, nmtui, Wi-Fi authentication,
firmware, CA certificates, Python and GI/libostree, and the existing deployment,
upload, partition and filesystem tools. Record the complete resolved package closure,
including weak dependencies deliberately retained for hardware support. No desktop,
Anaconda, SSH server, compiler toolchain, local model or package-update service is
needed at runtime. Keep diagnostic/model packs optional on the library partition.
Do not aggressively strip components that essential tools need merely to save image bytes.

**Disable SELinux in recovery using the recovery-only `selinux=0` boot argument.**
Record that policy in release provenance; do not rely on accidental absence of a
policy package or on runtime `setenforce`. Fedora documents boot-time disablement
in its [SELinux runtime-disable change](https://fedoraproject.org/wiki/Changes/Remove_Support_For_SELinux_Runtime_Disable).
This avoids recovery-specific policy maintenance, relabeling and unexpected denials
on custom mounts. SELinux is a security boundary, not merely a frill: this choice
reduces containment if recovery software is compromised. Read-only filesystems and
service restrictions are not equivalent MAC protection. The appliance assumes an
owner-controlled physical console and authenticated controller on a trusted network.

Do not change controller/container SELinux settings as a consequence of this decision.
Candidate SELinux state is an explicit build/experiment property, recorded with results;
recovery policy must not silently override experiments investigating security policy.
No automatic global relabel, package installation or firmware update on target boot.
Keep credential permissions, exact signatures, TLS validation and storage exclusions.
Use systemd resource limits and service restrictions compatible with required mounts,
network operations and watchdog handling; validate these against the actual workload.

## Runtime and reliability

Mount recovery ext4 read-only with `noload`, and disable filesystem checks that write
the fixed image. Preserve the fixed EFI/recovery partition hashes. `/run`, `/tmp` and
recovery `/var` are bounded RAM state; no persistent overlay spans the whole OS.
Use systemd's transient machine-id handling for the read-only root. Never use that
machine-id for target binding. Route NetworkManager profiles and resolver state to
writable RAM paths; `/etc/resolv.conf` points to the chosen NetworkManager-managed
resolver file. Do not accidentally ship a controller resolver file or rely on an
unconfigured systemd-resolved stub. D-Bus and the local console must work offline.

Start a local setup/status TUI automatically on the physical console, with no installed-OS
account or password required. It invokes nmtui rather than implementing connection
editing. Explicit local maintenance may open a privileged shell; remote root login
is not provided. Status is visible before pairing. Networking needs LAN access to
the controller, not public internet. Clock plausibility is checked before TLS; allow
operator-supplied system time or a configured reachable time source, never a certificate
verification bypass or an implicit firmware-clock write.

Restore only the selected private network profile into RAM after target binding is
checked. Recovery mounts evidence before optional experiment/library storage; failure
of those optional mounts must not prevent setup, credential access or uploads.
Use bounded device-specific readiness checks; remove global udev-settle and network-online
waits from the critical path as implementation permits. A missing cable or controller
must not delay the local UI indefinitely. Commissioning may take longer than a normal
boot and must show measured progress separately.

Systemd supervises the Quirkbench control loop. Recovery uploads are independent of
slow deployment preparation: run preparation in a bounded managed worker while the
control loop services liveness, status and the evidence outbox. One owner retains
attempt/boot authority; do not introduce competing schedulers or replay a physical
attempt after worker restart. Failure of recovery itself produces a visible durable
reason and attended intervention, not a reboot loop. This concurrency integration is
remaining work, not a guarantee of the current synchronous adapter.

Use the existing sealed-chunk/resumable acknowledgement protocol. Acknowledgement
means controller-durable storage; retain local evidence until then. Storage exhaustion,
credential failure and network loss remain distinct. Bound RAM logs and private spool
usage, preserve essential upload capacity, and stop new work instead of deleting pending
evidence. Watchdog handling follows its separately qualified systemd ownership policy.
No design can guarantee upload after loss of the USB or permanent network failure;
report retained evidence and unavailable paths explicitly.

## How the image is synthesized

P3a implements a strict `RecoveryRecipe` v1 and one orchestration entry point over
existing adapters. The recipe names the platform, Fedora release, builder digest,
RPM snapshot/lock, kernel source/configuration/fragment hashes, runtime revision,
dracut configuration, package/unit allowlists, layout capacities and recovery policy.
Use typed validated references, never shell text from a target inventory. The command
returns a durable operation ID through P2; stages publish progress and retain resumable
results. This recipe/entry point is planned, not an existing CLI command.

The synthesis stages are:

1. **Lock inputs.** Select a supported Fedora release, immutable builder OCI digest,
   retained RPM repository snapshot, kernel source RPM/config, protection fragment,
   Quirkbench revision and resource limits. Resolve and retain complete RPM identities
   and bytes. A moving repository URL or release number is not a reproducible lock.
2. **Create staged userspace.** In the dedicated rootless Fedora builder, run DNF5
   `--installroot` into a new disposable directory using the recipe package closure.
   Extend `target-assets/build-rootfs.sh` to consume the locked recipe; never run it
   against the controller root or attach physical block devices. Preserve output/logs
   outside the disposable container. Replay must refuse unavailable locked packages.
3. **Build the recovery kernel.** Use the existing resource-bounded build adapter with
   pinned Fedora sources/config plus the protection fragment. Stage matching modules,
   firmware and runtime inputs only into the target sysroot. Retain symbols, sources,
   final Kconfig and provenance. Verify exclusions after dependency resolution.
4. **Install recovery integration.** Install versioned Quirkbench units/runtime, offline
   console setup, NetworkManager RAM state, resolver configuration and policy masks.
   Image-specific partition identity files are injected by the assembly adapter, not
   discovered from the controller. Start from a curated unit allowlist. Remove factory
   machine-id contents, credentials,
   network profiles, installed-OS discovery and build-environment residue. Keep essential
   upload/deploy tools on recovery, never only in experiment/library storage.
5. **Build initramfs.** Run dracut with `--no-hostonly`, the explicit target sysroot and
   recovery configuration. Inspect modules, root discovery restrictions and firmware
   content. Never include a controller cmdline, root UUID, keys or network profile.
6. **Assemble regular-file media.** Feed the validated root tree, kernel, initramfs and
   provenance to the existing `quirkbench image` adapter. It uses sgdisk, filesystem
   tools and grub-mkimage for the compact GPT image, fixed recovery and SMBIOS-bound
   one-shot loader. The established commissioning code creates the final six roles
   on first boot after the local capacity screen confirms and journals geometry. This
   screen runs from RAM before evidence storage or enrollment exists; retries preserve
   the same plan and existing filesystems. No KIWI/installer resize service is introduced.
7. **Publish.** Verify complete staged output and provenance, synchronize, then publish
   `.img` (optionally `.img.xz`), checksum and release manifest. Sign distribution
   metadata with a release key kept on the controller. Factory media has no controller
   trust pin, enrollment authorization, device credentials or experimental deployment
   requirement. Etcher performs physical writing/verification.

The release manifest binds recipe/schema revision, architecture, source/RPM/toolchain
identities, SELinux/protection policy, runtime and firmware packages, kernel/initramfs
hashes, layout version, image checksum and qualified capabilities/limitations. Publish
actual observed sizes. Keep existing partition defaults initially; a locked userspace
closure that exceeds recovery capacity must fail clearly or require an explicit reviewed
capacity change, never silent truncation or omission of required tools.

## Maintenance and acceptance

Refresh the base on an approximately annual cadence, choosing a supported Fedora
release with sufficient remaining lifetime. Fedora releases have roughly thirteen
months of updates, so track the selected release's actual EOL rather than treating a
calendar anniversary as permission to keep unsupported software. See the
[Fedora lifecycle](https://docs.fedoraproject.org/en-US/releases/lifecycle/).
Publish earlier rebuilds for significant security or hardware fixes. No background
updates change a commissioned recovery during an investigation.

V1 recovery upgrades use newly flashed media, not an in-place recovery updater. Pause,
reconcile and upload or independently back up pending evidence before reusing a drive.
Back up private configuration separately; replacement media requires explicit enrollment
and qualification reconciliation. Never ask the user to overwrite the only copy of
unacknowledged evidence. Candidate OSTree updates continue without reflashing.

Measure kernel-start-to-console, evidence-ready, network-ready and controller-connected
times separately from firmware time and first-boot commissioning. Record cold/warm boot,
USB and network conditions; avoid a universal boot-seconds promise across hardware.
Release qualification covers actual read-only recovery, offline setup, Ethernet/Wi-Fi
and DNS, pairing/reconnect, upload during a slow preparation, corrupt/unavailable optional
volumes, missing network, service/worker failures, moved media and unchanged protected
storage/firmware. Kernel and watchdog qualification remain separate.

Follow the [testing policy](testing-policy.md): focused fixtures during implementation;
expensive boot/composition/endurance gates only on an explicitly requested final major
release. This decision is documented, not yet a newly built or qualified recovery image.

# Recovery image decision and synthesis contract

**Decision: use a minimal Fedora-derived recovery appliance, synthesized with DNF5,
dracut, systemd and GRUB through Quirkbench's existing build/image adapters, using
pinned stock Fedora kernel/module packages.** No custom recovery kernel compile is
required. Upstream live-image reuse is permitted through a bounded integration
proposal demonstrating simpler delivery under the same storage/boot contract;
Do not introduce a second builder incidentally. The existing assembly remains default.

Recovery is fixed for an investigation. Its job is setup, safe deployment preparation,
reconciliation and reliable evidence upload. It never runs experimental recipes or
promotes a candidate into recovery. Experiments remain exact signed rpm-ostree commits.
The [roadmap](product-roadmap.md) and [implementation contracts](implementation-contracts.md)
retain authority over enrollment, attempts, persistent state and protection.

## Why this choice

| Requirement | Chosen implementation |
| --- | --- |
| Fast boot | Minimal services, direct read-only ext4 root on USB, small non-host-only initramfs, no desktop/installer/update agent, no copying the whole root into RAM |
| Broad compatibility | Pinned stock Fedora kernel/module packages, broad peripheral/network support and matching Fedora firmware packages |
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

The first delivery supports x86-64 UEFI targets booting external USB storage.
Broad compatibility is an intent within that platform, not universal tested support.
ARM, legacy BIOS and other transports need explicit adapters. Attended operation
can proceed without automatic reset coverage, with manual-reset limits visible;
candidate boot still requires a dependable pre-kernel target identity gate.

Install retained, pinned Fedora kernel RPMs, matching modules and firmware into the
recovery sysroot. Record package identities, hashes and the kernel release; no SRPM
rebuild or exclusion fragment is required for recovery. Preserve broad boot, input,
console and network support rather than pruning for one target. Recovery is fixed
for an investigation and updated only through explicit maintenance releases.

Apply the authoritative [storage protection policy](architecture.md#storage-protection-policy):
passive kernel disk enumeration and partition-table reads are permitted. Userspace
filesystem and block operations are confined to expected Quirkbench roles on the
positively identified boot device. P3a2 restricts dracut, udev helpers and all
recovery services before any filesystem probe. Unsupported or ambiguous boot
identities block visibly. Candidate
kernels retain their independently reviewed internal-controller exclusions.

Use dracut's generic/non-host-only mode with the explicit target sysroot and matching
module tree. Restrict early root discovery and mounting to the validated boot-device
path. The initramfs must not probe or repair other filesystems, activate swap/resume,
or inherit the controller's root UUID, cmdline, keys or host-only configuration.
Peripheral/network modules may load after switch-root under the recovery policy.
Out-of-tree-only peripherals remain explicit unsupported cases.

The stock initrd mount adapter runs Fedora's fstab generator with only the reviewed
root parameters and empty fstab inputs. It validates the generated read-only ext4
root before removing its dependency on the masked fsck service; repair remains
disabled. Unexpected output leaves a required blocked root job. Device arrival and
mounting each have a 30-second bound, and mount failure enters emergency diagnostics.
The guarded udev PARTUUID path still supplies physical boot-device authorization.

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
Keep credential permissions, exact signatures, TLS validation and boot-device restrictions.
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
is not provided. Status is visible before configuration or pairing. Networking needs LAN access to
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

`RecoveryRecipe` v2 implements the stock successor to v1. The fixed recovery-image
coordinator now joins the stages over
existing adapters. The recipe names the platform, Fedora release, builder digest,
RPM snapshot/lock, stock kernel/module package identities and hashes, runtime revision,
dracut configuration, package/unit allowlists, layout capacities and recovery policy.
Use typed validated references, never shell text from a target inventory. The command
returns a durable operation ID through P2; stages publish progress and retain resumable
results. V2 input generation, immutable image admission and the optional fixed executor on
the existing authenticated controller service exist; see the [recovery operations](recovery-operations.md).

The synthesis stages are:

1. **Lock inputs.** Select a supported Fedora release, immutable builder OCI digest,
   retained RPM repository snapshot, stock kernel/module/firmware package identities,
   Quirkbench revision and resource limits. Resolve and retain complete RPM identities
   and bytes. A moving repository URL or release number is not a reproducible lock.
2. **Create staged userspace.** In the dedicated rootless Fedora builder, run DNF5
   `--installroot` into a new disposable directory using the recipe package closure.
   Use the versioned stock staging path behind `target-assets/build-rootfs.sh`; never run it
   against the controller root or attach physical block devices. Preserve output/logs
   outside the disposable container. Replay must refuse unavailable locked packages.
3. **Install the stock recovery kernel.** Install the locked Fedora kernel packages,
   matching modules and firmware only into the target sysroot. Retain package bytes,
   release/configuration identities and available debugging package references.
   Verify package/module consistency; do not invoke the experimental kernel builder.
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
   one-shot loader. New media preparation creates the final six roles on the
   controller after explicit selected-device/geometry confirmation, and publishes
   a versioned completion record outside the fixed recovery root. The target
   validates this geometry without partitioning or formatting. Retain historical
   commissioning readers without making first-boot partitioning a normal setup
   path. Zero shipped library payload reserves only filesystem overhead; remaining
   aligned capacity is split equally between experiments and evidence. No RAM-based
   admission limit applies to prepared media. Shortage blocks the affected write
   and retains unuploaded evidence; neither resize nor silent eviction is a remedy.
7. **Publish.** Verify complete staged output and provenance, synchronize, then publish
   `.img` (optionally `.img.xz`), checksum and release manifest. Sign distribution
   metadata with a release key kept on the controller. Factory media has no controller
   trust pin, enrollment authorization, device credentials or experimental deployment
   requirement. Controller preparation writes/verifies the selected USB and stages
   public trust and a single-use initial enrollment secret in mutable control
   storage. Generic flashing alone does not provide a prepared enrollment handoff.

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
and DNS, manual authenticated setup/reconnect, upload during a slow preparation, corrupt/unavailable optional
volumes, missing network, service/worker failures, moved media and unchanged protected
storage/firmware. Kernel and watchdog qualification remain separate.

Follow the [testing policy](testing-policy.md): focused fixtures during implementation;
expensive boot/composition/endurance gates only on an explicitly requested final major
release. This contract does not qualify any image bytes.


## Recipe execution compatibility

Recovery image generation executes only stock Fedora schema-v2 recipes. The old
custom-kernel recovery compiler, its introspection-based cache and interrupted-v1
resume machinery have been retired. Select a new v2 recipe and fresh workspace;
an old recipe is never silently converted or given new hashes. Historical v1
recipe/release readers remain available for evidence inspection. Candidate and
experiment kernel compilation, including Fedora SRPM preparation, is unchanged.

## Reviewed vendor inventories

New recovery input retention binds a version 1 vendor inventory through a version 2
storage profile. The rootfs lock and recipe remain version 2; their existing storage
profile hash now also binds the inventory. Version 1 storage profiles retain their
original reader and boot-policy behavior. Their historical Fedora44 fallback is
pinned to the original inventory digest; editing that data cannot silently change
legacy acceptance. Use an explicitly selected new inventory for changed packages.

`profiles/vendor-fedora44.v1.json` contains the previously reviewed Fedora 44
package pins, systemd generator hashes and vendor enablement maps. These are image
input data, not application policy. The selected signed RPM closure must contain
those exact package names, NEVRAs and hashes. Additional packages (such as the
pairing profile's OpenSSL executable) remain subject to the final installed unit
and generator audits. The inventory cannot change the application's storage guard,
required generator masks or recovery service policy.

For a changed Fedora package selection, review a new inventory JSON alongside the
package pins and boot behavior. Pass it explicitly with
`quirkbench dev recovery inputs lock --vendor-inventory /path/to/reviewed.json` and the
other lock arguments. The schema is `recovery-vendor-inventory.v1.schema.json`.
Do not generate approval by copying whatever an unreviewed rootfs happens to
contain. Generator changes require reviewing their effects on internal-device
probing; new enabled behavior must satisfy the same boot/storage contract.

The build stages the selected inventory and verifies its hash and Fedora release.
Installation and the final factory-root audit reject changed generators or unknown
enablement. No inventory update establishes native boot or hardware qualification.

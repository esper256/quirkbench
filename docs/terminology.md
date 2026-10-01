# Terminology and hardware scope

Quirkbench is a general-purpose Linux hardware investigation tool. Its architecture
must not depend on a manufacturer, model, form factor or one reported issue.

| Term | Meaning |
| --- | --- |
| **Controller** | The Linux computer that owns investigation state, runs the coding agent, builds and publishes images/deployments, schedules experiments and stores evidence. |
| **Target** | The computer under investigation. It boots Quirkbench recovery and experimental deployments, executes experiments and returns evidence. It may be a laptop, desktop, workstation, server or another Linux-capable computer. |
| **Builder** | The isolated build component running on the controller. It is a role/component, not a third physical computer in v1. |
| **Accessory** | Optional device/computer providing media presentation or diagnostic channels. It is not a controller, target or scheduling authority. Accessories are outside the current implementation plan. |
| **Controller service** | The process exposing the target protocol and owning controller operations; distinguish it from the controller computer when relevant. |
| **Target supervisor** | The service running in recovery or an experimental deployment on the target. |
| **Hardware profile** | Versioned capabilities, build requirements, protection rules and limitations for a hardware/architecture/boot combination. It is data used by generic machinery. |
| **Target ID** | Controller-assigned identity for an enrolled target; unrelated to a vendor name. Examples use `target-01`. |
| **Recovery** | The target's fixed fallback environment. This is not the controller. |
| **Candidate / experimental deployment** | An exact OS revision authorized for an attempt on a target. |

Use **controller** and **target** consistently in prose, comments, identifiers and
new interfaces. Avoid role aliases such as "debug host", "build host", "victim",
"test laptop" or bare "host" where the computer's role is intended. Use **target
computer** when distinguishing the machine from systemd targets or build targets.

Some existing technical names legitimately retain other terms:

- `--host`, URL hostnames and socket `host` parameters mean a network bind/address
  component, not a Quirkbench machine role. CLI help must say so.
- Container host OS, VM host/guest and SSH host keys describe established technical
  relationships. State the relationship explicitly where ambiguity is possible.
- Existing `device_id`, `--device`, `DeviceClient`, registration routes and
  `library-maintenance` arguments identify an enrolled target. Preserve these
  public names and stored data for compatibility; do not rename wire fields merely
  to change prose. Describe them as target IDs. "Device" also legitimately names a
  peripheral, such as a watchdog or audio device.
- Historical artifact filenames, measurements and serialized provenance remain
  unchanged. A terminology edit does not requalify the bytes of a new image.

## Generic design, explicit supported profiles

Generic scope is a design requirement, not a claim that every Linux-capable device
already boots today's image. Current production build/image code assumes x86-64,
UEFI/GRUB and external USB storage; QEMU qualification covers that infrastructure.
Physical reset and peripheral support still require target-specific observation.
Other architectures, firmware boot methods and storage transports need explicit
backend/profile support and their own qualification. Never silently build an
x86-64 image for an unsupported target or relax protection to make it boot.

Inventory and planning contracts must represent architecture, firmware boot method,
storage/network topology and required peripherals independently of vendor/model.
Build/boot assumptions belong behind selected platform adapters. Profiles may
contain narrowly matched vendor quirks with evidence, but the controller, scheduler,
protocol and generic recipes must not branch on an assumed target model. Resolve
actual drivers and capabilities; do not treat a profile name as qualification.

Protection and reset decisions are made per target. Some targets may lack a usable
watchdog, require a different boot backend or have a storage topology incompatible
with the candidate driver-exclusion policy or recovery boot-device policy. See the
[storage policy](architecture.md#storage-protection-policy). Report unsupported capabilities and block
the affected operation until reviewed support exists. A generic tool must be able
to describe these limitations rather than pretend one mechanism works everywhere.

Generic examples and acceptance templates use neutral identities. Discovery/planning
fixtures cover different vendors and form factors, absent optional capabilities and
unsupported platforms. Fixture coverage does not qualify those physical platforms.

A **media instance** is one enrolled external drive. It is distinct from a factory release, filesystem IDs and accessory identity.
A **target binding** detects accidental movement to another computer; it is neither
a login credential nor hardware attestation. USB host/device describe bus roles,
not Quirkbench controller/target roles; a Pi can be a USB device for gadget media
and a USB host for a separate diagnostic channel.

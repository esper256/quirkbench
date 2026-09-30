# External hardware: deferred USB gadget media

**Post-v1 design only; unimplemented and unqualified.** Direct USB storage remains
the sole v1 media implementation. This is the authoritative extension design for a
future Raspberry Pi USB gadget adapter, not a new release requirement or a claim of
Pi/target compatibility. Keep the [v1 roadmap](product-roadmap.md) unchanged.

## Media presentation, not OS deployment

An **accessory** is an optional computer/device providing media presentation or
diagnostic channels. It is neither the Quirkbench controller nor the target. A Pi
adapter would present the existing GPT image as one USB mass-storage logical unit
(LUN), preserving the six-role layout and target-visible block-device semantics.
OSTree, fixed recovery and the attempt state machine remain unchanged.

Existing checks use USB ancestry, same-disk partition identities and target binding,
not an SSD model. Gadget compatibility is therefore plausible, but enumeration,
drivers, geometry and boot behavior still require qualification. Do not add a gadget
bypass or infer support from a descriptor, VID/PID or device name.

| Boundary | Owner and responsibility |
| --- | --- |
| Investigation | Controller owns attempts, authorization, reconciliation and durable evidence acknowledgement. |
| Deployment and boot selection | Target recovery commissions storage, prepares exact OSTree revisions and arms GRUB once; recovery stays the default. |
| Media presentation | Future accessory adapter provisions private backing capacity and manages export/attachment under controller maintenance authorization. |
| Diagnostics | Independently enabled accessory channels contribute attributed observations, not execution authority. |

Keep the future provisioning/lifecycle boundary separate from `DeploymentBackend`
and `BootControl`. The Pi does not compose deployments, prepare candidates or edit
GRUB state. Deployment paths remain target-local; a Pi backing-file path must never
be interpreted as a target mount or controller workspace. No speculative framework,
public command, database migration or frozen wire-record change is needed in v1.

HID, CDC and gadget Ethernet are independently optional; virtual storage requires
none of them. Normal experiments still transfer OSTree objects rather than replacing
the backing image.

## Backing-storage capacity and ownership

Before first export, provision a private backing image from the verified factory
image with sufficient allocated storage for the intended target-visible capacity.
Preserve the factory identities and commissioning format. Target recovery retains
attended geometry selection, filesystem creation and the restartable journal. A
sparse file's apparent size does not prove available backing storage. Check both
target capacity requirements and accessory storage availability.

Advertised capacity stays fixed while attached. Preserve the same backing contents
across target reboots, including consumed one-shot state, private control records and
pending evidence. Never restore the factory image, rearm an attempt or discard data
automatically after disconnection or restart.

Only the target accesses the exported filesystem. The Pi may serve block requests,
but must not concurrently mount, repair, resize, replace or independently modify its
backing contents. Never share the backing device with another target or scrape its
live filesystem for evidence.

Administrative changes require a paused investigation, confirmed target shutdown,
detached gadget and closed backing device. Persist and reconcile exclusive ownership
across accessory restarts. Lost contact, an idle USB bus or a missing acknowledgement
does not establish shutdown or ownership. Refuse maintenance when uncertain.
Reattachment preserves the selected media and attempt fences; it does not authorize
another experiment.

## Protection, identity and trust

Keep USB ancestry, positive boot-media identity, same-disk partition verification,
early target binding, recovery boot-device confinement, candidate controller exclusions
and privileged destination allowlists under the [storage policy](architecture.md#storage-protection-policy).
Gadget support does not permit internal discovery or firmware writes.

Keep physical target binding, enrolled media instance, immutable factory release and
accessory management identity distinct. USB descriptors, Pi identity and backing
filenames cannot substitute for target authentication or physical target binding.
Cloned enrolled storage does not create another enrollment. Factory provisioning,
duplicate detection and explicit retargeting keep their existing rules and evidence
attribution.

The accessory is trusted infrastructure: its backing image contains target credentials,
network configuration and pending evidence. Restrict access and protect private backups;
never put the backing image into a public investigation export. AI credentials and
controller signing private keys remain on the controller.

## Durability, failures and backup

Target flushes must reach persistent backing storage through the gadget, accessory
kernel/filesystem and physical storage. Qualify write/flush semantics and the completed
durability operations that survive process failure, reboot and power loss. An ordinary
cached-write acknowledgement alone is not a power-loss durability claim. Never disable
synchronization to improve speed.

Backing-space exhaustion or I/O errors must not be falsely acknowledged as successful
writes. Disconnect/restart produces explicit failure or uncertainty when completion
is unknown. Preserve completed work and reconcile the existing attempt; never repeat
physical execution automatically. Show backing capacity and accessory health separately
from target progress and recovery arrival.

A live backing-file copy is not a consistent backup. Capture backing storage at the
maintenance boundary above after establishing a consistent cut; interrupted shutdown
may require recovery first. Keep controller backup completeness reporting for database,
CAS/OSTree objects, private identities and target-only evidence. Restoring a backing
image does not replace that report or authorize execution.

Only controller acknowledgement means evidence is durably stored on the controller.
Accessory reception, target flush and controller acknowledgement are separate events.
Accessory evidence follows existing retain-until-acknowledged semantics and reports
gaps/overflow explicitly.

## Observation is not control

Gadget Ethernet preserves target HTTPS endpoint verification, credentials and exact
revision authorization. The network path does not replace enrollment or controller trust.

Accessory logs have separately authenticated provenance identifying accessory/firmware,
target/media association and attempt when known; unknown attribution remains explicit.
They cannot impersonate target heartbeats, results or recovery registration. Accessory
reachability does not prove target responsiveness, and USB enumeration does not prove
recovery arrival.

HID is not independent reset. CDC depends on the relevant target USB/software path and
is not automatically an early or panic-safe console. DbC is a separate qualified USB-host
diagnostic path, not a capability inherited from gadget mode. Independent power is
required before claiming accessory survival through target shutdown.

This design grants no automatic firmware navigation, physical reset, independent
scheduling or extra attempt authorization. Later actuation requires separate scoped
authorization and failure review. Record accessory channels in experiment provenance:
they may affect idle, suspend and USB behavior.

## Deferred implementation and acceptance

Use [X1–X5 handoff packets](implementation-handoff.md#deferred-usb-gadget-extension),
outside the v1 dependency chain. Reuse Linux gadget facilities. Concrete Pi configuration
and new schema definitions belong to future packets, not unfinished v1 prerequisites.

| Future scenario | Observable acceptance |
| --- | --- |
| Recovery → candidate → recovery and failed-candidate fallback | Exact-revision isolated attempts and consumed one-shot state; fixed recovery unchanged. |
| Interrupted commissioning | Confirmed geometry, recognized filesystems and evidence survive retry; no blind formatting. |
| Target reset, Pi restart or disconnect | Same backing contents within qualified limits; uncertain attempts never repeat automatically. |
| Exhausted backing storage | Visible I/O failure/capacity block; no false durability acknowledgement or discarded pending evidence. |
| Concurrent access, resize or replacement | Maintenance rejected while exported, target active or ownership uncertain. |
| Duplicate media or moved target | Enrollment and early binding prevent unauthorized continuation; accessory identity cannot bypass them. |
| Flush, power-loss and backup faults | Durable flushes distinguished from cached writes; consistent backups preserve private-state/completeness rules. |
| Lost evidence acknowledgement | Idempotent retries preserve content and attribution until controller acknowledgement. |
| Accessory alive, target unavailable | Separate UI states; no fabricated target heartbeat, reset success or recovery arrival. |
| Internal-disk and firmware sentinels | Equivalent protection to direct storage; missing qualification is never a pass. |

Use fakes and focused software tests during development. Hardware/media qualification
requires separate authorization under the [testing policy](testing-policy.md). Never
run heavy gates automatically or spend agent turns polling long runs.

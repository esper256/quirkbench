# Watchdog qualification

Unattended grants and physical reset coverage are optional planned capabilities,
not prerequisites for attended investigations. An available operator/manual-reset
path does not authorize unqualified watchdog activation. Preserve exact-profile
checks. The current validator's recovery exclusions still need a versioned adaptation
to stock recovery's boot-device-only [storage policy](architecture.md#storage-protection-policy).
This is an implementation limitation, not permission to relax its validator.

Quirkbench uses systemd as the only userspace hardware watchdog owner. The
supervisor sends `sd_notify` service heartbeats; it never opens `/dev/watchdog`.
An active device and a successful notification test do not qualify recovery from
a kernel hang. Current target hardware coverage remains **untested**.

## What is already set up

The service units, activation code and profile checks exist. Physical reset support
must be established for each target and kernel profile. The service watchdog checks the supervisor's notifications; the
hardware watchdog resets a machine that stops servicing its timer. Systemd's
[service setting](https://github.com/systemd/systemd/blob/main/man/systemd.service.xml)
and [hardware settings](https://github.com/systemd/systemd/blob/main/man/systemd-system.conf.xml)
are distinct. Hardware settings have no effect without a supported device.

The primary purpose is resetting the **experimental OS** into fixed recovery.
Recovery may service that same hardware timer, particularly across handoff. It is
not a second watchdog or an independent reset device. No controller/network outage
should turn healthy recovery into a reboot loop. Discovery in recovery or the optional installed-OS collector
must never open `/dev/watchdog*`: opening can activate a timer, as described by the
[kernel watchdog API](https://docs.kernel.org/watchdog/watchdog-api.html).

## Planned experimental-kernel authorization

The [forward plan](product-roadmap.md) retains exact-build qualification as the
default and adds a separate `WatchdogAuthorization` under brief P5. An operator
may explicitly permit a campaign to activate the watchdog on experimental kernels
after commissioning its platform/baseline. Authorization binds each exact revision
and build ID; it does not copy a baseline's passing qualification to a new kernel.
Report baseline coverage, current activation and candidate uncertainty separately.
Relevant hardware/firmware/reset/power changes require attended review; screening
cannot prove all kernel changes harmless. Unattended experimental use requires the
explicit risk scope, and unsupported cases remain human-intervention conditions.

This extension is not implemented. The current rules below still apply. It needs
versioned capability negotiation, stale/revoked authorization tests and
higher-reasoning review before activation; older targets must fail explicitly.
There is no requirement to repeat an entire destructive qualification campaign on
every build or every recovery boot.

[Contract C6](implementation-contracts.md#c6--watchdog-authorization-and-revocation-p5)
specifies the planned signed grant fields, separate route, cached activation order,
policy epochs and offline revocation limits. P5 implementers must use those rules;
neither free-form provenance nor a copied baseline profile is authorization.

## Current profile contract

`RecoveryProfile` records hardware and kernel identities, requested timeout,
watchdog identity, lockup settings, separate coverage results, and an immutable
qualification evidence artifact. Start with 120 seconds. Observe the actual
timeout through sysfs; hardware may round it. Missing timeout, state or countdown
attributes remain unknown. Boot status alone is not proof of a kernel crash.

The profile's `policy_sha256` covers the hardware, kernel release and loaded GNU
build ID, watchdog, timeout and
lockup settings. A profile with passed coverage must supply the matching
`qualification_policy_sha256`; editing those settings invalidates the report.
Explicit `qualification_run=True` permits a supervised trial with an unqualified
profile. Production activation requires qualified activation and runtime reset.

Use the additive `recovery_profiles` object in `evidence/control/runtime.json`
when recovery and candidate kernels differ. Its keys are exact kernel releases;
each value is a complete recovery profile whose `kernel_release` matches that
key. The matching entry takes precedence over the legacy `recovery_profile`
field. The target compares `kernel_build_id` with the GNU build ID from the
loaded kernel's `/sys/kernel/notes`, so a changed kernel cannot reuse a report
merely by retaining the same release string. Obtain this identity with
`quirkbench.watchdog.running_kernel_build_id()` while running that kernel.

A missing profile, different loaded build ID or unavailable GNU note produces
explicitly unqualified coverage and prevents watchdog activation, including a
requested qualification trial. It does not prevent evidence uploads. Older
profiles lacking the build ID remain readable; the original requested profile
and the invalidation reason are retained in attempt provenance alongside the
effective unqualified profile. They need new qualification before activation.
Malformed profiles still fail validation rather than silently changing policy.

## Build prerequisites

A recovery image or VM boot result does not imply a functional watchdog driver or
lockup detectors. Verify the final kernel configuration and observed hardware.
Choose the driver after observing the target hardware; no driver is assumed to
match the target merely because of its model name. Keep the normal storage and
firmware protection policy. For the reviewed initial driver families,
`validate_watchdog_kernel(config, driver=...)` requires built-in watchdog core,
sysfs, the chosen driver, and both lockup detectors, **as well as** the existing
protection gate. Run this after `olddefconfig`, not just against an input fragment.
An unavailable Kconfig symbol needs an explicit profile review, not bypassing the
gate. This does not change the kexec prohibition or enable kdump.

## Required physical trials

Run controlled failures only on the externally booted lab target with a person
able to reset it. Retain target/controller timestamps, serial or network diagnostics,
observed configuration, reset latency, recovery arrival and evidence hashes for
each trial. Keep raw evidence separate from the final coverage report.

| Trial | Observable acceptance |
|---|---|
| Activation | Positive USB identity, matching device/kernel, observed active state and actual timeout |
| Early boot/handoff | Identify earliest active stage; demonstrate continuation across initramfs handoff before claiming it |
| Runtime reset | Deliberately withhold hardware keepalive in a controlled trial; measure reset and fixed recovery arrival |
| Kernel hangs | Inject separately qualified representative lockups; distinguish reset success from diagnostic survival |
| Shutdown reset | Exercise a stuck shutdown and measure recovery arrival |
| Supervisor stall | Stop supervisor progress; service watchdog invokes failure hook, records reason, requests recovery without replay |
| Healthy long work | Work longer than watchdog timeout while loop is responsive; no false reset or fabricated progress |
| Suspend/resume | Test each intended sleep mode and duration; record unsupported modes and false resets |
| Offline recovery | Controller unavailable: visible retries and retained evidence, no recovery reboot loop |

Never turn one passing trial into coverage for all rows. The earliest covered
stage stays unqualified until activation is proven, and initramfs coverage also
requires its own passing handoff trial. Mark failed/unsupported rows explicitly.
Do not run unattended suspend experiments under an unqualified suspend profile.

## Failure semantics

The caller-driven supervisor heartbeat is independent of measurable experiment
progress. A phase deadline does not move when a heartbeat arrives. Expiry stops
service heartbeat emission and requests recovery through the normal failure
path. The failure hook has no automatic recipe replay or candidate arming path.
Recovery supervisor failures require human intervention rather than rebooting in
a loop. Completed candidates request reboot even if full evidence storage prevents
persisting the failure marker; console diagnostics explicitly mark that record as
not durable. A hardware reset cannot guarantee a crash dump.

Runtime activation writes only a target `/run/systemd/system.conf.d` drop-in and
explicitly selected `/proc/sys/kernel` settings after positive boot verification,
then uses bounded `systemctl daemon-reexec`. Failure may leave the watchdog armed;
the runtime must report the observation and prevent new scheduling. Configuration
generation and unit tests never activate the controller watchdog.

References: [Linux watchdog sysfs ABI](https://github.com/torvalds/linux/blob/master/Documentation/ABI/testing/sysfs-class-watchdog),
[systemd manager watchdog settings](https://github.com/systemd/systemd/blob/main/man/systemd-system.conf.xml).

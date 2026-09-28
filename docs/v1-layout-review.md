# Layout revision 2 and physical execution review

This revision preserves the v1 experiment/result envelopes and adds a library
selection artifact, versioned pack/recovery-profile documents and three physical
handoff operations. Image boot/commissioning identity changes to version 2; old
prototype images must be rebuilt. The controller adds migrations without rewriting
prior campaign evidence. Historical M2 image evidence remains under its old paths.

Reviewed persistence and boot boundaries:

- GRUB consumes selection before entering a candidate. Only verified recovery
  prepares/arms deployments. Every attempt uses a new isolated OSTree stateroot.
- Controller handoff records exact revision, origin boot and generation before
  arming. A candidate must be the immediately following authorized boot. Expired
  or stale duplicate handoffs cannot resurrect authorization.
- Completed results and recovery arrival are independent. A lost start or result
  acknowledgement cannot authorize physical replay. Interrupted arming disarms
  and produces uncertainty even if the controller restarted in the same recovery boot.
- Recovery/experiment mode cannot change within one boot identity. After restart,
  scheduling remains paused until reconciliation and explicit resume.
- Sealed evidence bytes precede references. Streaming and terminal results share
  the same acknowledgement path; controller backup retains library content and
  OSTree closure. Target credentials remain outside exported spool objects.
- Optional library maintenance has a durable controller scheduling fence, a target
  journal lock and an external filesystem lock. It runs only in verified recovery,
  uses journal-enabled maintenance mounts, then restores read-only access. A missing
  acknowledgement never authorizes overwriting an existing pack or filesystem.
- Commissioning journals exact geometry before mutations. Preexisting formats must
  have expected identities; an indeterminate interrupted format is a visible human
  gate, not a force-format retry. Evidence is mounted before optional filesystems.
- Watchdog profiles bind hardware, loaded kernel GNU build ID and settings to qualification evidence. Profiles are selected separately per kernel release; missing, stale or legacy profiles are explicitly unqualified and cannot block recovery uploads. Sysfs
  observations never invent armed/countdown state. Configured idle recovery still pulses the supervisor without claiming progress. Systemd alone owns the hardware
  device; supervisor heartbeat comes from its event loop, not a background thread.
- Current qualified VM kernels have no usable watchdog driver/lockup detectors.
  Physical qualification requires a newly configured protected kernel and actual
  hardware tests. Kdump remains unavailable under the existing kexec prohibition.

Software regression scenarios exercise these observable boundaries with local and
HTTPS transports. Real image boot results and their exact artifact identities are
recorded separately; neither test class establishes Acer recovery or a >30-hour
campaign. Source changes during composition must reject publication; an interrupted
revision-2 compose demonstrated that check while preserving its draft output.

The first real six-partition boot run also exposed truncation of the kernel's
printed command line: the additional partition identities pushed the panic fixture
authorization beyond the printed prefix. Boot fragments now put candidate/revision
and explicit smoke/fault identity before bulky storage paths. The strict panic
proof remains required; a reset without that proof is not a passing panic trial.

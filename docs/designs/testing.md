# Testing strategy

## Testing approach

- Start with broad, fast integration tests. No unit tests initially.
- Cover one significant part of the mockup journey per integration test.
- Use test fixtures and mocks to keep execution fast. No real image builds,
  kernel compilation, flashing or QEMU in these tests.
- Combine journeys when one test's output is entangled with the next test's input.
  Split them only when a concrete specification describes exactly what passes
  between them and lets us check both sides independently.
- Do not invent detailed specifications merely to divide tests. Python is the
  chosen language; detailed implementation and test-tool choices remain open.

These are the user's testing directions. Tests establish application behavior,
not successful hardware boot.

## Build for integration testing

Design Quirkbench and its integration tests together. Production code may be
structured specifically to make these tests straightforward. Tests should not
have to work around an application built without regard for testing.

Make external command execution and other expensive dependencies easy to replace
with test-controlled behavior. Keep Quirkbench's actual decisions, file handling
and user responses in the code being exercised. Share small, clear interfaces;
avoid scattered test patches or a separate product implementation for tests.

### Keep the application easy to exercise

Pass storage roots and a few replaceable operations into the application at
startup. Use ordinary Python functions or objects, not a general plugin framework
or production branches that ask whether tests are running.

| Part | Application design | Test replacement |
| --- | --- | --- |
| Ordinary files | Derive locations from configured roots; use normal file operations | Real temporary directories, including one standing in for mounted shared USB storage |
| Partitions, mounts and reboot | Small Linux-specific functions perform these actions | Record requests or report failure without touching host devices |
| Builds and OS tools | Shared command runner executes expensive external tools | Controlled command results and small output files |
| Investigation commands | Own processes independently of controller connections | Real small child processes |
| Connections | Configurable addresses and ports | Real local MCP, SSH and HTTPS connections |
| Long deadlines | Replaceable application time calculations | Controlled time, with a few real short timeout checks |

Do not put every file operation behind a filesystem proxy. Real files keep rename,
publication, upload and deletion behavior in the test. To test a storage error,
replace the particular write or publication operation; do not recreate a filesystem.

Keep decisions in application code and machine actions in the small Linux module.
Tests replace mounting or rebooting, not “prepare candidate” or “recover successfully.”
Quirkbench must still select the candidate, prepare files, handle failures and decide
what may be booted or deleted. Directory fixtures do not prove mount isolation,
boot selection or power-loss durability.

CLI, MCP and TUI call the same application functions. Controller and target can
start independently over existing saved files, so tests can restart either without
resetting the other. Expose useful completion signals for startup and work; tests
wait for these instead of guessing with sleeps.
The shared lab shortens reconnect and status intervals while running the real
connection loops. The terminal fixture shortens curses’ Escape-key delay.
Production defaults stay unchanged; keep real short waits when testing timeouts.

Prove this structure with the first recovery/deployment journey. If it needs
extensive patching or a miniature operating system, simplify the application
structure before extending it.

## Test hygiene comes first

Readable, maintainable tests are a top priority. Build shared libraries used only
by tests for ordinary setup, external mocks, invoking Quirkbench and common checks.
Hide repetitive machinery there, not in hundreds of lines in each test.

A test should show only its setup, what makes this case different, execution of
real Quirkbench code and checks of the behavior that matters. For example:

```python
def test_recovery_returns_evidence_and_clears_uploaded_copy(quirkbench):
    # Setup: a finished run with evidence still on the USB.
    run = quirkbench.finished_run(evidence={"wifi.log": b"Connection failed\n"})

    # Execute recovery and read results through the real CLI.
    run.recover()
    result = quirkbench.cli("investigation", "show", run.investigation_name)

    # Verify successful receipt, actual contents and removal of the USB copy.
    result.assert_success()
    assert "Evidence received" in result.stdout
    assert (run.results_dir / "wifi.log").read_bytes() == b"Connection failed\n"
    assert not (run.usb_evidence_dir / "wifi.log").exists()
```

This is illustrative pytest code, not an implemented interface. Shared test helpers
supply a configured installation, paired target and temporary files through real
application setup. They replace expensive OS work, not successful upload records.
`recover()` runs real recovery code through upload and cleanup; `cli()` invokes
actual command handling. Failures include captured diagnostics.

Keep each test to setup, execution and meaningful checks. If it needs pages of
boilerplate, improve `tests/support`. Keep special inputs and assertions visible.
Use exact bytes for evidence, but avoid whole-screen snapshots or comparisons of
unrelated fields. Fixtures describe useful scenarios, not every internal detail.
This example checks confirmed upload; a separate failure case checks preservation
when receipt is unconfirmed.

## Confront the hardest part first

The hardest part is crossing from a running candidate into recovery: commands may
be interrupted, evidence is incomplete, files must survive, and only completed
software may become bootable. A mock that simply says “reboot succeeded” can hide
nearly every important mistake. Making every test boot a real OS is too costly.

Keep the recovery/deployment journey covered:

1. Run real setup/pairing and connect a local MCP client to the controller and a
   separate recovery process through reverse SSH. Read bounded diagnostic output
   using the real inspection handler. Reject arbitrary shell/argument substitution.
2. Prepare a small candidate fixture, replacing external image/package work. Run
   Quirkbench's real deployment and file-selection logic, replacing mount, bootloader
   and reboot operations. Incomplete preparation must not request a candidate boot.
3. Restart the target application in candidate mode over the same shared directory,
   then request return to recovery. Check boot IDs, stable pairing, report upload,
   fresh deployment files, retained repeat boots and explicit discard. Lose one
   upload reply and preserve unconfirmed files.
4. Restart the controller during deployment or upload; reconnect and report saved
   progress without blindly repeating writes or boot requests.

Use ordinary temporary directories and child processes, not an entire simulated
Linux boot. A process restart proves application behavior, not firmware boot or
power-loss survival. Read-only mount behavior needs the separate image check.

Use the same helpers for concurrent commands. Add terminal cases when interactive
input is implemented.
Restart the controller while a target command continues, then retrieve its saved
result without relaunching it. Interrupt a candidate and confirm recovery reports
old commands as interrupted while uploading surviving output.

Changes to the Bazzite/OSTree mount and GRUB boot arrangement need an authorized
image check; keep slow flash and boot checks separate from the fast suite. Record unperformed checks as
missing evidence, not a pass from directory fixtures. Live package replacement is
checked when that later capability is implemented.

## Spend realism where it catches our mistakes

| Area | Fast test approach | What it does not prove |
| --- | --- | --- |
| MCP, SSH, HTTPS and process ownership | Real libraries, local connections, separate controller/target processes and small real commands | Every agent client's waiting behavior or a physical network |
| Evidence, retry and cleanup | Real temporary files and uploads; deliberately lose a reply or interrupt a transfer | USB durability during power failure |
| Recovery TUI and human CLI | Real application handlers and screen actions; simulated keys and responses from network/disk tools | Console visibility during boot or actual Wi-Fi drivers |
| Build, packages, mounts, boot and flash | Replace expensive or privileged tool calls with small recorded outcomes and files; run real Quirkbench orchestration | Successful OS assembly, live package changes or hardware boot |
| Packaging and entry points | Install the built Python package once per test session; launch its actual commands | Availability on every Linux distribution |

The fast suite needs no root, downloads, image builds, kernel compilation, flashing
or QEMU. Real processes must never invoke host disk, mount or reboot operations.
Keep those actions behind a small explicit interface; unexpected calls fail tests.
Use the TUI library's testing support where available, plus one real terminal
smoke test for input, child-program return and redraw. Do not test the whole UI
through brittle screen recordings.

Mocks describe an external tool's outcome, not our expected success story. For
example, a failed deployment must leave no selectable candidate because production
code handles the failure, not because the fixture removes it. Preserve real file
publication and cleanup logic. Do not teach mocks a second copy of rpm-ostree.
When tool output needs parsing, keep a small representative sample from the real
tool and refresh it when changing supported versions.

## Journey coverage

| Journey | Meaningful result | Selected variations |
| --- | --- | --- |
| Install, configure, prepare USB and pair | Editable config saved; prepared media has connection settings; real pairing establishes a target | Cached image reused; controller unavailable; external preparation fails |
| Inspect recovery and provision a candidate | MCP returns recovery diagnostics, prepares a candidate and requests deployment, one-time boot and return | Incomplete deployment; initial connection failure; reconnect; recovery shell request rejected |
| Investigate and change software | MCP context leads to concurrent commands, file transfer, a changed candidate and received evidence | One command times out or is stopped; changed boot interrupts work; evidence arrives after command completion |
| Interrupt and continue | Saved output and attribution survive reconnection; uncertain work is not replayed; pause stops new work and finishes active commands | Controller restart; candidate crash; lost upload acknowledgement; force stop |
| Recovery and next run | Real recovery UI explains the problem; upload and cleanup preserve the right files; explicit discard permits a fresh deployment; repeat boot retains persistent edits | Missing settings; storage failure; incomplete candidate; internal debugging report without an investigation |

Use a few short tests per journey, sharing setup, rather than one enormous test or
every possible combination. A normal journey plus failures at important transitions
is more valuable than exhaustive permutations. Prioritize loss of evidence,
unintended execution, unusable next steps and inability to recover. A previous bug
gets a new case only when existing coverage would not detect its underlying mistake.

The interactive journey checks separate command environments, shared files,
independent timeouts and quiet waiting. A confirmed new boot ends the old command
wait even while evidence is uploading. Recovery checks that cleanup preserves active
work, unconfirmed evidence and connection settings; candidate edits cannot silently
become the next run's defaults. Simulate storage errors without filling the host disk.
Check that a never-connected candidate requests recovery after its startup deadline,
while a later disconnect does not. Exercise local MCP token and browser-request
checks through the real server without a large security test matrix.

Later journeys may reuse real setup/pairing through shared helpers. Do not seed
private database records to save a few milliseconds. Split journeys only when the
saved files or API between them have a clear, independently checked meaning; do
not invent a new specification just to split a test. Tests never depend on another
test having run. Share immutable package/tool fixtures, not active investigations.

Check results the user or agent can use: exact evidence bytes, command execution
count, retained/deleted files, a useful next action and honest failure status.
Avoid whole-response snapshots, exact command-call sequences and assertions on
incidental internal structure. Keep the individual tests as short as the example
above; ordinary pytest helpers in `tests/support` hide repeated machinery.

## Keep execution fast and failures useful

- Aim for the routine suite to finish in about **30 seconds** with dependencies
  present. Treat **60 seconds** as a reason to fix or split out slow work, not a
  reason to weaken assertions. Measure before setting rigid CI limits.
- Have small test programs signal when ready and wait for explicit release. Use
  bounded waits instead of sleeps and repeated polling. Keep one real short
  command-timeout case; use controlled application clocks for long deadlines.
- Start fresh controller/target processes for restart cases. Other cases may share
  startup through a fixture if their files and work remain independent. Use normal
  in-process CLI invocation often, actual packaged entry points in a few journeys.
- On failure, collect controller/target output, current work and relevant file
  locations automatically. Bound every wait and clean up child processes. Do not
  automatically rerun failures until they happen to pass.
- Acquire dependencies separately. CI runs the portable fast suite on every code
  change; avoid complicated selection rules while the suite is small. Missing
  required dependencies fail clearly rather than silently skipping coverage.

MCP tests prove the server and SDK client exchange, not that a particular agent
will wake without polling. Check one supported real agent client early and again
when changing its wait integration; no paid model loop in routine tests.

## Separate image and hardware checks

Keep a slower recovery-image check, potentially using QEMU, outside routine tests.
Trigger it when the actual recovery image bytes change, using an image digest and
saved result. An unchanged image reuses its previous result; without a successful
result it remains unchecked. Failed checks may be rerun explicitly. Building an
image to discover whether it changed is not part of the fast suite.

That check covers boot-to-menu, included tools, mounts, one-time candidate boot,
return to recovery and evidence survival using a tiny test workload. Changes to
candidate OS preparation may need an explicitly requested boot check even if the
recovery image is unchanged. Physical checks cover USB flashing, Wi-Fi, firmware
and actual watchdog/crash behavior that a VM cannot establish. This document does
not authorize running any of these slow or physical checks now.

Do not pursue a coverage percentage or perfect realism. Add realism when it closes
an important known gap; add a mock when external cost prevents useful routine
coverage. If making a fast test realistic requires a miniature OS or a general
simulation framework, stop that approach and use a separate focused real check.

The implemented [opt-in recovery boot check](../../boot_tests/README.md) boots an
already built image with QEMU and OVMF, then checks recovery connection and mounts
through a temporary controller. It reuses successful results for unchanged boot
inputs and saves a console screenshot. It is outside the normal test suite.
Candidate boot/return and physical hardware remain separate checks.

Recovery TUI regressions run in the fast suite: real MCP requests update its status,
and a pseudo-terminal exercises navigation, resize and shell return. Boot-console
routing is checked at its configuration boundary. Use a one-time manual QEMU
confirmation after display/boot changes; do not turn each TUI iteration into a VM test.

## Recovery disk stalls

Hold one real recovery record write with a test-controlled signal. While it is
held, exercise MCP status and the combined diagnostic report over local SSH,
and send a report over HTTPS. Verify no boot occurs before the write completes.
Release the signal and complete deployment once. Use no sleep or expired timeout
to simulate the stall; test timeouts only prevent a broken test hanging forever.

The fast journey covers separate deployment and boot, recovery restart before
boot, unexpected return with uploaded diagnostics, retained edits on a repeat
boot, separate command/evidence identities, and explicit discard. Emergency
inspection must work when spawning diagnostic programs fails. Read saved output
in bounded pages; do not assert exact diagnostic prose.

## Target packaging

Exercise the real staged target files outside the checkout, including deferred
imports used by deployment and command execution. Fast build tests replace only
external tools: controller edits reuse recovery, target edits invalidate it, and
changed builders or damaged cached RPMs trigger package rebuilding. Recovery
staging must not invoke the RPM builder. Actual image boot remains a separate check.

Admission checks interrupt actual controller record writes, restart and retry the
journey. They check rollback, one run per request, no replay after a committed but
unsent action, and rejection when recovery restarts before deployment dispatch.

Recovery base checks exercise first selection, unchanged-package rebuilds, explicit
updates, failed acquisition and missing/changed retained RPMs. Run the acquisition
script with small process substitutes; no downloads or image builds. Connection
checks use a different runtime identity and an omitted operation to verify that
identity differences remain usable while unsupported work is never dispatched.

Live debugging checks run a collector and uploaded probe concurrently, download
binary evidence, then return through confirmed upload and cleanup. Hold an output
write with a signal to check control and stop without waiting for a fake timeout.
Also cover connection loss, new boots, incomplete/changing downloads, read-only
SFTP evidence, debugging boot arguments and early probe files. Verify target-service
priority settings and actual child execution. Elevated service scheduling and live
module replacement still need the separate authorized hardware check.

Recovery failure checks hold disk work with test signals while exercising menu,
connection, report upload and confirmed power controls. Substitute only the final
kernel power request. Exercise emergency HTTPS upload in a fresh Python process
with recovery-file reads denied, so earlier tests cannot preload its dependencies.
A separate manual QEMU drive-removal check verifies the built image; it stays
outside the routine suite.

Group tests by journey: deployment, cancellation, commands, preparation, recovery
console, media and diagnostics. Keep shared interruption setup in `tests/support`.
Each lab has fresh state and a real controller. Reuse its HTTP connection pool to
avoid repeated client setup; keep MCP sessions within their calling tasks and
close the pool on controller restart. Do not share mutable state between tests.

Use Ruff for basic Python errors and formatting. CI lists the slowest tests and
allows a 180-second outer limit to accommodate runner variation; retain the fast
suite goal, not a new target duration. Test deadlines still catch hangs.
Operator journeys cover config errors, access refusal, status/stop, connection
refresh, cleanup/export and notification. Hold a database call with test signals
to check that SSH still responds. Run the actual boot generator against a test
kernel command line; mock only privileged reboot. This does not prove firmware boot.

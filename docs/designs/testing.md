# Testing strategy

## Agreed starting point

- Start with broad, fast integration tests. No unit tests initially.
- Cover one significant part of the mockup journey per integration test.
- Use test fixtures and mocks to keep execution fast. No real image builds,
  kernel compilation, flashing or QEMU in these tests.
- Combine journeys when one test's output is entangled with the next test's input.
  Split them only when a concrete specification describes exactly what passes
  between them and lets us check both sides independently.
- Do not invent detailed specifications merely to divide tests. Python is the
  chosen language; detailed implementation and test-tool choices remain open.

These are the user's testing directions. This document is a starting design,
not a detailed implementation plan or a claim about existing coverage.

## Build for integration testing

Design Quirkbench and its integration tests together. Production code may be
structured specifically to make these tests straightforward. Tests should not
have to work around an application built without regard for testing.

Make external command execution and other expensive dependencies easy to replace
with test-controlled behavior. Keep Quirkbench's actual decisions, file handling
and user responses in the code being exercised. Share small, clear interfaces;
avoid scattered test patches or a separate product implementation for tests.

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
    result = quirkbench.cli("experiment", "results", run.experiment_id)

    # Verify successful receipt, actual contents and removal of the USB copy.
    result.assert_success()
    assert "Evidence: Received" in result.stdout
    assert (run.results_dir / "wifi.log").read_bytes() == b"Connection failed\n"
    assert not (run.usb_evidence_dir / "wifi.log").exists()
```

This is Python in pytest style, using proposed shared test helpers. Their names
and the example CLI arguments describe intended use, not existing interfaces.
The `quirkbench` fixture supplies an isolated configured installation and paired
target, and cleans up afterward. `finished_run` uses shared journey setup with
expensive external work mocked. It does not fabricate successful upload records.
`recover` runs actual recovery code through receipt and cleanup, without booting
an image; failures or a bounded wait expiring fail the test with diagnostics.
The CLI helper invokes the real command handler and captures its output.
`assert_success` checks the exit status and shows output on failure. Directory
properties locate real files; they do not return mocked results.

This is the target for test readability and compactness. If a test falls short,
improve the shared test tooling rather than accepting boilerplate in the test.
Keep setup, execution and verification distinct; CLI calls are execution.

Keep defaults, process management and waiting inside shared test tooling. Keep
case-specific inputs and meaningful assertions visible in the test. Use ordinary
Python assertions when they already express the check clearly. The exact bytes
matter here because evidence must arrive unchanged; matching the whole CLI output
does not. This example checks successful cleanup, not the separate failure case
where the controller has not confirmed receipt.

Check meaningful results and effects, not entire outputs or saved files against
exact fixture copies. For example, evidence receipt should mean the expected data
was actually stored, not just that a success message appeared. Use exact equality
where the exact value matters; avoid checks that break on harmless wording,
formatting or unrelated fields. Fixtures supply useful scenarios, not a duplicate
description of every implementation detail. Failures should explain what was
expected and what happened.

## Additional testing requirements

- Allow a separate, potentially slow recovery-image test, possibly using QEMU.
  Run it only when the actual recovery image file has changed, not merely when
  related source files change. Unchanged image contents should not trigger it.
- Test the recovery TUI, file deletion and evidence uploading quickly, without
  QEMU or similarly heavy infrastructure. Exercise the real Quirkbench behavior
  for those tasks, including failure cases.

These record desired coverage and cost limits only. How to detect image changes,
run these tests and provide their test fixtures remains undecided. This does not
authorize running slow tests now.

## Integration tests

| Test | Starts with | Ends with |
| --- | --- | --- |
| Install, configure and connect a target | A clean controller computer environment, an installable Quirkbench package and a simulated blank USB drive | Setup saved, generic recovery built or reused, USB prepared, recovery started, networking configured and target paired with the controller. |
| Investigate, run and inspect results | A connected target and a human's problem description | Investigation authorized; agent follows the supplied instructions, submits a baseline test, reads evidence, supplies a changed build, runs another experiment and receives evidence suitable for assessing an investigation patch. Includes progress, recovery, confirmed upload and USB cleanup. |
| Interrupt and continue an investigation | An authorized investigation with an experiment being prepared or run | Pause/resume, force stop, lost connections and controller restarts lead to an understandable result or a clear request for human action, without repeating a run by accident or losing unuploaded evidence. Use a few focused variations of this journey. |
| Diagnose recovery itself | Recovery starts with a storage or connection problem | The human can see the problem, configure temporary networking, open a terminal, collect a Quirkbench internal debugging report and send it after pairing. |

The connected-target starting point is provisional. Until its saved files and
connection behavior have a concrete specification, the later tests must use the
real setup and pairing flow from the first test, with expensive work mocked.
Reuse that setup rather than inventing independently maintained saved records.
Apply the same rule to investigation setup and experiment preparation.

Mocks replace expensive or unavailable external work, not the Quirkbench behavior
being checked. The investigation test supplies the agent's decisions and small
build outputs; it does not run an AI model or prove that a patch fixes hardware.
These tests check that Quirkbench carries the journey through. Existing physical
checks retain responsibility for actual boot, device behavior and flashing.

The [mockups](../mockups/README.md) show the intended user experience. Test its
meaning and usable next steps, not every decorative character. Refine this list
as the design becomes concrete; this document does not select a test framework.

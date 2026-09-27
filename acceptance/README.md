# Qualification fixtures

Templates intentionally fail acceptance. Copy one outside this directory, fill observations from an actual run, and reference evidence files relative to the report with `{"path":"relative/file", "sha256":"64 lowercase hex"}`. The verifier checks report completeness and referenced bytes; it cannot authenticate an operator's observations or establish causality from a manifest alone. Higher-reasoning/human review still assesses evidence and limitations.

Hardware endurance requires more than 30 real hours, the listed injected events, evaluated recovery capabilities, resource-use and safety evidence, and no silently unresolved attempts. Patch bundles require separately matched baseline/patched/revert/regression observations, source/build identities, actual patch files and exposure counts. Keep an unreproduced issue inconclusive instead of manufacturing a passing report.

QEMU uses the `make acceptance-qemu` fixture with real IMAGE, OVMF_CODE, OVMF_VARS, WORK_DIR and EXPECT_SERIAL inputs. Use separate trials for recovery, consumed one-shot, failed candidate, and subsequent recovery. The single invocation checks its supplied marker and sentinel; it does not complete the entire boot sequence gate.

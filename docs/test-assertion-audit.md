# Requirement-based test audit (#49)

This first pass starts from `e9c8152` and addresses
[issue #49](https://github.com/esper256/quirkbench/issues/49). It changes test
fixtures/assertions, not production behavior or permissions. Historical #3, #9,
#13 and #16 remain regression evidence; their fixes are not reverted or repeated.

## Scope and decisions

The broad scan covered 147 `test_*.py` modules: 48 contain mode/umask checks,
14 native-mock patterns, 46 directory-listing patterns, 30 call-sequence patterns,
and 122 error-message matching patterns (overlapping categories). Matching code
was triaged against its observable requirement; the scan is not a claim that every
assertion was exhaustively proved or that a smaller test count is better.

Focused review covered build storage/cache, recovery storage/listing, state paths,
SQLite lifetime/read-only queries, source capture/preparation/distribution, worker
claims/lifecycle, and related runtime/endpoint fixtures. The wider scan inspected
examples in setup/install, target revocation, enrollment/credentials, release/download,
boot/watchdog, recovery source/kernel stages, and endpoint/evidence handling.

| Rewritten case | Incidental assumption | Requirement and retained defect detection |
| --- | --- | --- |
| Build/recovery storage and runtime native stat mocks | A hand-built object with only the fields used by today's caller adequately represents `stat` | Preserve a real `os.stat_result`, including tuple behavior, inode/device/link count and optional nanosecond/device fields; override only simulated ownership or block identity. Existing wrong-owner/device, symlink, mount and GPT identity rejection tests remain. |
| Runtime/distribution symlink target permissions | `mkdir(mode=0755)` starts at 0755 under every supported umask | Set and verify a deliberately permissive fixture target, then require the original mode and outside contents to remain unchanged on refusal. An illicit chmod to 0700 must fail the test under both 022 and 077. |
| Build storage/worker claim permission faults | Setting a named mode is enough evidence that the intended fault happened | Establish an admitted private/non-writable state, add only the relevant group-read/write bit, verify the change, and require rejection. Worker claims restore the captured original mode and prove the original claim is readable again. Sticky scratch states are explicitly verified. |
| Revocation/empty-home directory snapshots | Filesystem iteration order is stable between observations | Compare membership, preserving no-new/no-deleted-entry evidence without freezing directory enumeration order. |
| Repeated endpoint rollback | Its valid private directory always contains exactly nine files | First require successful unchanged rollback replay, then add an unknown record and require refusal. The closed namespace remains enforced; the incidental count/name of the test is removed. |
| Initialized setup status | Read-only SQLite must leave SHM bookkeeping bytes unchanged | Permit only the exact controller WAL/SHM paths. Preserve application-file bytes, all logical rows (including lifecycle epochs), schema/version and journal mode, and reject mutating adapters. Existing WAL bytes must remain unchanged; a newly created WAL may contain no transaction frames. Sidecars must remain regular files. |

The tiny shared `stat_with` fixture utility retains native API behavior; it is not a
new mocking framework. Directory membership comparisons still detect additions or
removals. Empty/singleton namespaces, fixed output contracts and explicit resource
limits are not loosened merely because they contain counts.

## Required evidence kept

- [C2 ownership/read-only constraints](implementation-contracts.md#c2--local-operations-ownership-and-restart-p2): exact owner epoch/generation, service identity, private staging modes, stop-before-reuse and publication fencing. SQLite connection-close checks retain live references and test use-after-close on success and failure; they do not rely on garbage collection.
- [C2 read-only semantics](implementation-contracts.md#home-state-read-only-monitoring-and-disposable-retention): existing recovery-listing tests preserve current committed WAL visibility, reject SQL writes, preserve application bytes and forbid added transaction frames. These historical fixes remain unchanged.
- Source capture/preparation/distribution: exact bytes/modes/base identity, provenance, writer handoff, late mutation refusal and outside-path preservation. The existing `change_mode` helper already toggles a real bit; the capture fixture's executable-to-nonexecutable mutation is also real under both umasks. Neither was rewritten.
- [Storage policy](architecture.md#storage-protection-policy): exact boot disk/partition identity, geometry, refusal before device access, and no writes/chmods through untrusted paths.
- Credential/staging mode checks are privacy contracts. Ordered protocol/stop/publication/verification checks protect authorization and freshness. Stable wire fields/error categories and useful diagnostic fragments remain checked. No blanket removal of specific assertions or helper tests was justified.

## Validation and bounded follow-up

Use the exact affected-file commands/results and source identities recorded in the
linked PR. The ten-module baseline collected 250 cases and passed in 41.87 seconds
under Python 3.12.14 / umask 022. After edits the same 250 cases passed under
022 in 40.77 seconds and 077 in 42.09 seconds. Test count is retained; timings are
observations, not gates.

A short `filesystem` suite in the existing [CI manifest](../ci/suites.json) covers
previously unmapped storage/runtime/SQLite cases: 171 cases accounted for 2.28
seconds of pytest phase time in the baseline, measured before inclusion. Existing source-worker and endpoint-control suites
cover the other changed files. Focused CI continues to use Python 3.13/077; local
022/077 evidence complements the supported smoke interpreter matrix.

Temporary mutation probes deliberately introduce an unauthorized chmod and a SQL
schema-version write: both modified tests failed as expected under each of 022
and 077, confirming that meaningful violations are still detected. Probe failures are expected validation evidence, kept outside the
checkout; no mutant or production-code change is committed.

No disputed contract was relaxed. Further audit can review other assertions as
those subsystems change; this is not a mandate to rewrite justified security or
performance regression tests. Full software, native/image/QEMU and hardware/release
campaigns are outside this audit's validation scope. Follow the
[testing policy](testing-policy.md) for future slices.

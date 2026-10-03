# First usable attended journey: acceptance guide

This guide defines user-visible acceptance. Current work, dependencies, claims and
completion evidence live in [tracker #29](https://github.com/esper256/quirkbench/issues/29)
and its linked issues/PRs. Do not append progress diaries or historical test logs here.
The [roadmap](product-roadmap.md) sets scope; [C0–C7](implementation-contracts.md)
and [C8](product-interface.md) define the contracts.

A first usable version lets a person complete the supported installed journey
without a source checkout, handwritten private build manifests or the original
developer's chat. External coding agents are the primary path. All physical
attempts require exact-candidate operator approval.

## Acceptance by user outcome

The issue links identify ownership, not a claim that the outcome is complete.
Named command forms are desired interfaces until executable help/services establish
availability. Preserve existing public commands and versioned readers.

| Outcome | Acceptance | Work owner |
| --- | --- | --- |
| Fresh controller | Verify/install compatible software into a clean home; resumable setup selects private persistent state, trust/resources and native services; zero enrolled targets; dependencies and logout/restart behavior explicit | [#40](https://github.com/esper256/quirkbench/issues/40), [#41](https://github.com/esper256/quirkbench/issues/41) |
| Connected target | Obtain exact compatible recovery, use a standard writer, explicitly confirm external media/capacity, configure network and verify fingerprint before pairing; complete private settings survive restart | [#40](https://github.com/esper256/quirkbench/issues/40) |
| Truthful readiness | Separate recovery/connection/enrollment/binding/experiment eligibility; stale reports and unsupported hardware are explicit; preserve revocation, retarget, endpoint repair and old evidence attribution | [#40](https://github.com/esper256/quirkbench/issues/40), [#43](https://github.com/esper256/quirkbench/issues/43) |
| Investigation and sources | Record problem/limits/target/baseline; prepare a separate editable workspace from supported pinned distribution inputs or existing source; retain actual Git base, modes/deletions and provenance; refuse concurrent capture mutation | Existing source foundations; [#30](https://github.com/esper256/quirkbench/issues/30), [#33](https://github.com/esper256/quirkbench/issues/33), [#40](https://github.com/esper256/quirkbench/issues/40) |
| Baseline round trip | Candidate inputs/rootfs → durable build/compose → review exact experiment/attempt → explicit approval → recovery/evidence; missing pinned bytes never substitute; success does not imply reproduction | [#30](https://github.com/esper256/quirkbench/issues/30), [#31](https://github.com/esper256/quirkbench/issues/31), [#32](https://github.com/esper256/quirkbench/issues/32) |
| External-agent loop | Fresh agent can use brief/context/recipes/schema, hand off immutable source and submit durable proposals; bounded history, observations and rejected approaches survive restart; no managed call or duplicate dispatch | [#33](https://github.com/esper256/quirkbench/issues/33), [#34](https://github.com/esper256/quirkbench/issues/34), [#35](https://github.com/esper256/quirkbench/issues/35) |
| Scientific result | Compare baseline/diagnostic/patched/regression/revert with exact identities, exposure counts, confounders, missing observations and uncertainty; support non-audio use and inconclusive results | [#36](https://github.com/esper256/quirkbench/issues/36) |
| Patch/report export | Apply patches to the actual recorded clean base; attribute tested source/evidence and label unvalidated bytes; retain required source/symbol provenance and exclude private credentials; export is not backup | [#37](https://github.com/esper256/quirkbench/issues/37) |
| Ordinary operation | Filter monitor by investigation; pause/drain/resume explicitly; reconcile safe shutdown locally and through controller; upload backlog is distinct from local durable evidence and safe eject | [#38](https://github.com/esper256/quirkbench/issues/38) |
| Backup and storage | Preserve positional APIs while adding guided usage; capture a consistent source cut, explain private identity needs/offline uncertainty, restore paused, and protect active/required/pinned data during cleanup | [#39](https://github.com/esper256/quirkbench/issues/39) |

## Evidence levels

1. **Focused software acceptance:** actual application services joined with injected
   privileged/native/target boundaries; both success and interruption/replay failures.
   A parser/schema fixture or mocked success output alone is insufficient.
2. **Installed software journey:** [#42](https://github.com/esper256/quirkbench/issues/42)
   joins the outcomes without checkout-only resources. This broad integration
   milestone warrants the existing full software matrix against an identified candidate.
3. **First usable native acceptance:** [#43](https://github.com/esper256/quirkbench/issues/43)
   demonstrates the journey on an explicitly supplied supported host/target with
   exact artifact identities. [#41](https://github.com/esper256/quirkbench/issues/41)
   owns real publisher and pinned-input availability. Both need operator inputs;
   cloud tests cannot close their native requirements.
4. **General-release qualification:** [#44](https://github.com/esper256/quirkbench/issues/44)
   is a separate final major-version gate, launched only on explicit request.

An issue may close its bounded software scope while native acceptance remains open
elsewhere. Record the exact command/result, source identity, required review and
limitations in the PR/issue. Existing evidence is reusable only when its inputs still
match; historical machine-local logs are not automatically accessible evidence.

Do not remove the README's aspirational warning until shipped-artifact acceptance
supports the claimed journey. Keep unavailable optional modes explicitly labeled.
The complete [testing policy](testing-policy.md) governs scope and expensive gates.

## Preserved foundations and history

Inventory/catalog/planning, controller operations/services, signed acquisition,
stock recovery, enrollment/retarget/endpoint maintenance, source preparation/capture,
investigation creation and the candidate-rootfs adapter already have implementation.
Inspect current code before adding work; the issues identify remaining integration.

Old P0–P8 identifiers remain in the contracts for compatibility. Their completed
packet histories are in [Git history](https://github.com/esper256/quirkbench/tree/08c03bd992092aadc4125ba83f5417acda4e0d67/docs),
not an instruction to repeat them. Optional managed invocation and unattended
qualification remain outside this acceptance guide.

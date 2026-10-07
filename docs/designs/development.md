# Develop by simplifying

Follow the [glossary and writing guidance](../glossary.md) in docs and discussions.

[Requirements](../requirements/product.md) need human approval to change. Agents may revise designs unless a change adds massive complexity or substantially reduces capability, performance or ease of use; explain such a tradeoff and ask first.

Read relevant code, not the entire archive. Coordinate issues/PRs and preserve others' work. Archived contracts, old issues and existing tests do not override current requirements.

Implement the chosen [rpm-ostree/OSTree path](candidate-deployment.md), simplifying
existing code. Check repeat-experiment costs early using saved build output; this
does not authorize image builds or hardware runs. Do not implement a second
deployment method for hypothetical future flexibility.

Complete one useful change at a time: human setup/build/flash; investigation authorization and agent-authored tests; recovery and evidence collection. Reuse working services, update connected components together and delete superseded commands, records, checks and tests. Remove unnecessary work rather than chase a line-count target.

No new migrations, compatibility readers or legacy conversion tools: use fresh development state. This does not authorize deleting owner files, changing a live installation or running hardware.

Start the redesigned product with [fast journey integration tests](testing.md),
not unit tests. Use test fixtures and mocks for expensive external work. Existing
tests are implementation references, not a requirement to preserve their structure.
No elaborate rename/security matrices or new test framework. While working in the
current implementation, use affected `make test TESTS=...` cases. Documentation
needs link checks and `git diff --check`.

No image builds, flashing, QEMU or kernel compilation for routine changes. Full release tests need an explicit major-release request and `RELEASE_QUALIFICATION=1`. Separately requested builds/flashes/experiments are product operations, not permission for that suite. Keep logs; don't repeatedly poll long jobs or rerun broad tests without diagnosis.

One independent **gpt-6-astra / medium** review after a substantial implementation effort, before merge, not per-step reviews. Resolve findings in that review; repeat only for major changes to the reviewed design or guarantees. Missing review blocks merge, not independent development. Push/merge/live operations require authorization.

Designs describe intended behavior; help, source and actual checks establish current availability. Documentation edits alone do not implement new authorization or execution behavior.

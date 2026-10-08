# Quirkbench documentation

Use plain English and the [glossary](glossary.md) for product and development terms.

Start with the short [product requirements](requirements/product.md). Changes to that document require human approval.

See the [example screens and commands](mockups/README.md) for the intended experience,
from installation to an investigation patch.

Then read only the design relevant to the task. Designs describe **how Quirkbench should work**, not a claim that the current code implements them. Agents may improve them without approval except for suspect changes: massive added complexity or a substantial loss of capability, performance or ease of use. Explain such a change and ask the human first.

| Design | Focus |
| --- | --- |
| [Product and experience](designs/experience.md) | Enjoyable human interface; product roles and scope |
| [Setup, build and flash](designs/setup-and-media.md) | Three simple commands; editable configuration; prepared USB |
| [Agent experiment loop](designs/experiment-loop.md) | Agent-authored experiments, authorization, evidence, pause |
| [Experiment builds](designs/experiment-builds.md) | Baseline builds, agent-built changes, deployment requirements and candidate hooks |
| [Candidate deployment](designs/candidate-deployment.md) | Chosen OSTree/rpm-ostree approach, transfer speed and cleanup |
| [Target recovery](designs/recovery.md) | Practical protection, dashboard, failure recovery and debug reports |
| [Controller API](designs/controller-api.md) | Target pairing, work delivery, progress and evidence uploads |
| [Application internals](designs/internals.md) | Conventional Unix behavior, identity, storage and reuse |
| [Component sequence](designs/component-sequence.md) | How setup, experiments, recovery and the agent work together |
| [Testing strategy](designs/testing.md) | Fast integration tests covering the mockup journeys |
| [Development](designs/development.md) | Fast iteration, subtraction, reviews and implementation transition |

[Legacy documents in the repository](https://github.com/esper256/quirkbench/tree/main/docs/legacy) preserve previous manuals, plans, contracts and operating notes. They have **no current design authority**, even where they say “must,” “approved,” or “contract.” Consult a specific archived file only for a concrete implementation question; do not read the archive as onboarding or inherit its constraints. GitHub issues and existing tests can also contain superseded assumptions.

This rewrite branch has no runnable application yet. Commit `b807199` preserves
the previous implementation for reference. This documentation reorganization does not implement the new product design or authorize live flashing, resetting state or running targets. Thin [agent](agent-guide.md) and [controller](controller-installation.md) entry points are retained for installed tooling.

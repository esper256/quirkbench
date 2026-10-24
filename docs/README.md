# Quirkbench documentation

Use plain English and the [glossary](glossary.md) for product and development terms.

Start with the short [product requirements](requirements/product.md). Changes to that document require human approval.

See the [example screens and commands](mockups/README.md) for the intended experience,
from installation to an investigation patch.
For current use, start with the [agent guide](agent-guide.md) or
[installation](controller-installation.md). Package maintainers use [release packaging](releases.md). See the [1.1 changelog](../CHANGELOG.md)
for changes since the first agent investigation.

The [1.2 plan](designs/1.2-plan.md) prioritizes the next improvements from real
investigation feedback and recorded use.

Then read only the design relevant to the task. Designs describe **how Quirkbench should work**, not a claim that the current code implements them. Agents may improve them without approval except for suspect changes: massive added complexity or a substantial loss of capability, performance or ease of use. Explain such a change and ask the human first.

| Design | Focus |
| --- | --- |
| [Product and experience](designs/experience.md) | Enjoyable human interface; product roles and scope |
| [Setup, build and flash](designs/setup-and-media.md) | Three simple commands; editable configuration; prepared USB |
| [Interactive debugging](designs/interactive-debugging.md) | MCP, live control, decisions and acceleration measurements |
| [Agent investigation loop](designs/experiment-loop.md) | Inspection, live probes, software changes, evidence and pause |
| [Experiment builds](designs/experiment-builds.md) | Baseline builds, agent-built changes, deployment requirements and candidate hooks |
| [Candidate deployment](designs/candidate-deployment.md) | Live changes, writable candidates, deployment choices and cleanup |
| [Target recovery](designs/recovery.md) | Dedicated fallback, practical protection, diagnostics and recovery |
| [Controller API](designs/controller-api.md) | MCP and target connections, pairing and simple evidence uploads |
| [Application internals](designs/internals.md) | Conventional Unix behavior, identity, storage and reuse |
| [Component sequence](designs/component-sequence.md) | How setup, experiments, recovery and the agent work together |
| [Testing strategy](designs/testing.md) | Fast integration tests covering the mockup journeys |
| [Development](designs/development.md) | Fast iteration, subtraction, reviews and implementation transition |

[Legacy documents](legacy/README.md) are historical reference, not current design
instructions. Consult a specific file only for a concrete implementation question.

Dated observations record historical checks, not current setup requirements.
Mockups include future screens; use actual tool descriptions for available actions.

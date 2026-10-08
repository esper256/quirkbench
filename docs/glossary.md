# Words we use

Use plain English plus the terms below in documentation, help, reviews, plans and
agent messages. Prefer one name for each thing. Ordinary words need no entry.

Write short, complete sentences. Name who acts and what happens. Explain the next
action. Avoid compressed noun strings and unexplained abbreviations. This takes
inspiration from [ASD-STE100](https://www.asd-ste100.org/about.html), without its
strict vocabulary or a claim of compliance.

Replace jargon outside this glossary with plain words. For example, replace
“gate” with “required check,” “handoff” with “instructions for the next agent,” and
“brief” with “summary.” Add a term only when a recurring technical distinction
needs a name, not to justify complicated prose. Exact commands, code names and
quoted errors stay exact; explain them in plain words when needed.

This is writing guidance, not new product requirements. Definitions do not prove
that a feature exists. Keep entries short and change them with the designs.

| Term | Meaning |
| --- | --- |
| Quirkbench project | The source code, documentation and development work that produce Quirkbench. |
| Quirkbench binary | The `quirkbench` program used to enter commands and view results. |
| Quirkbench controller | The running service that coordinates investigations, prepares experiments and collects results. |
| Controller config | The editable file containing settings for the Quirkbench controller. |
| Agent | An AI coding tool that plans experiments, edits code and interprets results. |
| Quirkbench user's agent | The coding agent using Quirkbench to investigate the user's target, not to develop Quirkbench. |
| Controller computer | The computer that runs the Quirkbench controller and prepares experiments. |
| Target | The computer on which experiments run. |
| Investigation | Work on one problem, with a target and human-approved limits. |
| Experiment | A test question, selected software, test programs and instructions for running them. |
| Run | One execution of an experiment on a target. |
| Baseline | The investigation's unchanged starting software and build settings, used for comparison and reuse. |
| Candidate | Software prepared for testing in an experiment. |
| Candidate source | The code the agent edits to build software for an experiment. |
| Investigation workspace | An ordinary directory containing candidate source, build instructions and investigation test programs. |
| Build instructions | Commands and settings for producing software that meets the deployment requirements. |
| Build | Turn code and packages into runnable software. |
| Build output | Files produced by a build, such as a kernel, matching modules and debugging symbols. |
| RPM | A software package containing files and information about dependencies and installation. |
| rpm-ostree | The tool Quirkbench uses to assemble RPMs and configuration into a candidate. |
| OSTree | The tool Quirkbench uses to store candidate versions, share unchanged files and deploy them. |
| Deployment requirements | Required files, layout and compatibility details for Quirkbench to assemble and deploy a candidate. |
| Candidate hook | A named point in candidate execution where an investigation test program can run. |
| Capture | A saved copy of submitted build output, candidate source, test programs and build settings for one experiment. |
| Image | A file containing a system that can be written to a device and booted. |
| Flash | Write an image to a selected device, replacing its contents. |
| Recovery system | The working system on the target computer that starts experiments and collects diagnostics after they end. |
| Recovery image | The file written to USB to install the recovery system. |
| Recovery partition | The USB partition holding the recovery system, kept separate from experimental software. |
| Shared data partition | USB storage shared by candidates, recovery settings, evidence and temporary files. |
| Watchdog | A timer that restarts the target if the running system stops keeping it alive. |
| Pairing | Connecting a target and controller so each can recognize the other. |
| Evidence | Logs, measurements and observations collected from a run. |
| Quirkbench internal debugging report | Logs and facts about failures in Quirkbench itself, separate from experiment evidence. |
| Investigation patch | Code changes developed during an investigation to fix or diagnose the target's problem. |
| Investigation pause | Stop new investigation work; let the current run finish, return to recovery and upload evidence. |
| Force stop | Try to interrupt current investigation work immediately; a frozen target may need a manual reset. |
| Cache | Saved results that can be recreated if deleted. |
| Worker | A process that performs a controller task. |
| Operation | An internal record of background work, used for troubleshooting. |
| ID | A value that identifies one thing; it is not its file location. |
| Hash | A value calculated from bytes to detect changes or identify content. |
| Fingerprint | A short representation of a public key or certificate used to check identity. |
| API | Defined requests and replies through which programs work together. |
| HTTPS | HTTP requests protected by an encrypted connection that checks server identity. |
| CLI | Command-line interface: commands typed into a terminal. |
| TUI | Text user interface: menus and controls displayed in a terminal. |
| JSON | A text format used for structured input and output. |
| Requirement | A human-approved product goal or limit in the requirements document. |
| Design | A revisable description of how the product should meet its requirements. |
| Issue | A tracked problem or task in GitHub. |
| Branch | A named line of Git changes. |
| Commit | A saved set of changes in Git. |
| Pull request (PR) | A proposed set of Git changes for review and merge. |
| Merge | Combine approved changes into another Git branch. |
| Review | Examine changes for correctness, clarity and fit with the requirements. |
| Refactor | Change code structure while preserving its intended behavior. |
| Regression | Previously working behavior broken by a change. |
| Test fixture | Data or a prepared environment used by a test. |
| Mock | A test replacement for a real component. |
| Mockup | An example screen or command sequence showing the intended user experience. |
| Integration test | A test that checks real components working together. |
| Smoke test | A small, fast check that basic behavior works. |
| CI | Continuous integration: automated checks run for repository changes. |

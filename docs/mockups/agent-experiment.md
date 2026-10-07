# Agent experiment interface

> **EXAMPLE ONLY — intended design, not implemented commands or a frozen format.**
> Package names, paths, hooks, timings and the audio problem are examples. Other
> agents, low-level components and diagnostic programs use the same interface.

## Read the investigation

```console
$ quirkbench investigation show microphone-dropout --json
{
  "investigation": "microphone-dropout",
  "problem": "Microphone capture stops intermittently after several minutes.",
  "target": "lab-laptop",
  "authorization": {
    "status": "active",
    "changes": ["kernel", "audio services", "diagnostic programs"],
    "excluded": ["internal disks", "persistent firmware", "Quirkbench itself"]
  },
  "baseline": "baseline-1",
  "workspace": "/home/sam/quirkbench-work/microphone-dropout",
  "target_information": "target.json",
  "instructions": ["INVESTIGATION.md", "BUILD.md", "DEPLOY.md", "tests/README.md"],
  "experiment_template": "experiment.example.json",
  "next": "Read these files, then submit an experiment file. Relative paths above use workspace."
}

$ cat INVESTIGATION.md BUILD.md DEPLOY.md tests/README.md
```

```text
Investigation: microphone-dropout
Problem: Microphone capture stops intermittently after several minutes.

First inspect target.json and existing evidence. Try to reproduce before fixing.
Choose test duration yourself. No per-run human approval is needed within scope.
Do not edit Quirkbench, internal disks or persistent firmware.

Software:
- baseline-1 selects the saved unchanged software.
- packages.add requests stock packages; Quirkbench resolves and records versions.
- packages.replace supplies your locally built replacement RPMs.
- Build changed software yourself. For PipeWire: ./build-pipewire.sh
  Read build.log and resolve compilation errors. Preserve the build directory.
- The build script publishes completed RPMs, source records and build settings in
  output/. Submit that output; Quirkbench will not rebuild your changed code.
- programs copies standalone programs and supporting files into the candidate.
  No RPM is needed for these additions. Supply executable permissions and any
  runtime dependencies. Quirkbench does not guess missing libraries.

Execution:
- Specify a program name and arguments; no implicit shell interpretation.
- Each step has a candidate hook and a timeout. Steps at the same hook run in order.
- after-services runs after ordinary candidate services have started.
- before-recovery runs during an orderly return to recovery. A crash may bypass it.
- Earlier diagnostic changes belong in candidate source and its boot files.
- run_timeout covers candidate execution, including boot and all steps. Failure
  recovery is best effort if the candidate cannot enforce it.
- A nonzero step exit or timeout ends the test and requests recovery. Available
  before-recovery steps may collect diagnostics within the remaining time.

Files and results:
- QUIRKBENCH_EVIDENCE_DIR: write anything needed back on the controller.
- QUIRKBENCH_WORK_DIR: temporary files, removed after recovery collects results.
- Leave recovery settings, boot selection and Quirkbench-managed files alone.
- stdout/stderr, kernel and service logs are collected automatically where possible.
- Optional progress: write one JSON object per line to QUIRKBENCH_PROGRESS_FILE:
  {"message":"Recording microphone", "completed":3, "total":10, "unit":"minutes"}
  Ordinary logs need no special format. Missing progress does not fail the test.
- An exit code is the program's result, not Quirkbench's judgment that a bug is fixed.

Submission:
- Paths in an experiment file are relative to that file unless absolute.
- Submission captures files; it never runs a build command from the file.
- Keep submitted output unchanged until capture completes. Other editing can continue.
- --request-id identifies a submission. Retry identical input with the same ID after
  a lost reply; changed input needs a new ID. Do not resubmit merely to wait.
- All input errors are returned together where possible, with field names and fixes.
- experiment wait stays quiet until results or intervention are available. If its
  wait ends first, call it again with the same ID. It does not repeat the experiment.
```

## Submit a diagnostic experiment

The agent writes a recorder and a diagnostic script, builds the recorder, and saves this file.

```console
$ cat experiment.json
```

```json
{
  "question": "Does microphone capture stall with stock software, and what do kernel and PipeWire logs show at that time?",
  "baseline": "baseline-1",
  "packages": {
    "add": ["alsa-utils", "pipewire-utils"],
    "replace": []
  },
  "programs": [
    {"name": "record-microphone", "path": "tests/bin/record-microphone"},
    {"name": "collect-audio-status", "path": "tests/collect-audio-status.sh"}
  ],
  "steps": [
    {
      "hook": "after-services",
      "program": "record-microphone",
      "args": ["--duration", "600", "--report-progress"],
      "timeout": "11m"
    },
    {
      "hook": "before-recovery",
      "program": "collect-audio-status",
      "args": [],
      "timeout": "30s"
    }
  ],
  "run_timeout": "15m"
}
```

```console
$ quirkbench experiment submit microphone-dropout --file experiment.json --request-id reproduce-1 --json
{
  "request_id": "reproduce-1",
  "status": "accepted",
  "capture": "pending",
  "next_command": "quirkbench experiment wait microphone-dropout --request-id reproduce-1 --json",
  "instructions": "/home/sam/quirkbench-work/microphone-dropout/instructions/wait.md"
}

$ quirkbench experiment wait microphone-dropout --request-id reproduce-1 --json
{
  "request_id": "reproduce-1",
  "experiment_id": "experiment-1",
  "run_id": "run-1",
  "status": "finished",
  "capture": "complete",
  "target": "recovery",
  "step_results": [
    {"program": "record-microphone", "exit_code": 1, "summary": "Capture stopped after 347 seconds"},
    {"program": "collect-audio-status", "exit_code": 0}
  ],
  "evidence": {
    "status": "received",
    "directory": "/home/sam/quirkbench-work/microphone-dropout/results/run-1",
    "files": ["recording.wav", "capture.json", "audio-status.txt", "kernel.log", "services.log", "test.log"],
    "missing": []
  },
  "resolved_packages": "/home/sam/quirkbench-work/microphone-dropout/results/run-1/packages.json",
  "next": "Read the evidence and decide the next experiment.",
  "instructions": "/home/sam/quirkbench-work/microphone-dropout/instructions/results.md"
}
```

## Test changed software

The agent reads the evidence, edits PipeWire and builds replacement RPMs.

```console
$ ./build-pipewire.sh
Build complete.
Replacement RPMs and build records: output/

$ python - <<'PY'
import json
from pathlib import Path

experiment = json.loads(Path("experiment.json").read_text())
experiment["question"] = "Does the PipeWire change prevent the observed capture stall?"
experiment["packages"]["replace"] = [
    {"build": "output/build.json", "rpms": "output/rpms/"}
]
Path("experiment-fix.json").write_text(json.dumps(experiment, indent=2) + "\n")
PY

$ quirkbench experiment submit microphone-dropout --file experiment-fix.json --request-id fix-1 --json
{
  "request_id": "fix-1",
  "status": "accepted",
  "capture": "pending",
  "next_command": "quirkbench experiment wait microphone-dropout --request-id fix-1 --json",
  "instructions": "/home/sam/quirkbench-work/microphone-dropout/instructions/wait.md"
}
```

## Correct all reported input errors together

```json
{
  "status": "invalid",
  "work_started": false,
  "errors": [
    {"field": "programs[0].path", "message": "File not found: tests/bin/record-microphone. Build the program or correct its path."},
    {"field": "steps[0].timeout", "message": "Step timeout exceeds run_timeout. Increase run_timeout or shorten the step."}
  ],
  "next": "Correct the file and retry. This request ID has not been accepted.",
  "instructions": "/home/sam/quirkbench-work/microphone-dropout/DEPLOY.md"
}
```

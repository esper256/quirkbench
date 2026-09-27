export PYTHONPATH := $(CURDIR)/src

PYTHON ?= .venv/bin/python

.PHONY: test acceptance-m1 demo monitor acceptance-qemu acceptance-hardware acceptance-patch

test:
	$(PYTHON) -m pytest

# A single explicit boot fixture. Run separate fixtures for recovery and debug
# one-shot assertions; neither a timeout nor a zero exit proves a guest boot.
acceptance-qemu:
	@test -n "$(IMAGE)" || { echo "IMAGE must name an absolute regular-file USB image" >&2; exit 2; }
	@test -n "$(OVMF_CODE)" || { echo "OVMF_CODE must name an absolute OVMF code file" >&2; exit 2; }
	@test -n "$(OVMF_VARS)" || { echo "OVMF_VARS must name an absolute OVMF variables template" >&2; exit 2; }
	@test -n "$(WORK_DIR)" || { echo "WORK_DIR must name an existing empty absolute directory" >&2; exit 2; }
	@test -n "$(EXPECT_SERIAL)" || { echo "EXPECT_SERIAL must be an exact expected guest serial marker" >&2; exit 2; }
	@IMAGE="$(IMAGE)" OVMF_CODE="$(OVMF_CODE)" OVMF_VARS="$(OVMF_VARS)" WORK_DIR="$(WORK_DIR)" EXPECT_SERIAL="$(EXPECT_SERIAL)" $(PYTHON) -c 'import os; from pathlib import Path; from quirkbench.qemu import QemuInputs, run_qemu; result = run_qemu(QemuInputs(Path(os.environ["IMAGE"]), Path(os.environ["OVMF_CODE"]), Path(os.environ["OVMF_VARS"]), Path(os.environ["WORK_DIR"]))); log = result.serial_log.read_text(errors="replace"); marker = os.environ["EXPECT_SERIAL"]; assert marker in log, f"expected serial marker absent: {marker!r}; inspect {result.serial_log}"; print(f"serial marker found: {marker!r}; internal sentinel unchanged; OVMF template unchanged; log={result.serial_log}")'

# No hardware cases are skipped inside this gate; separate qualification gates
# fail visibly when their required real-world inputs are absent.
acceptance-m1: test

DEMO_STATE ?= .quirkbench/demo
demo:
	$(PYTHON) -m quirkbench --state "$(DEMO_STATE)" demo

monitor:
	$(PYTHON) -m quirkbench --state "$(DEMO_STATE)/controller" watch demo

acceptance-hardware:
	@test -n "$(REPORT)" || { echo "REPORT must name a completed hardware endurance report" >&2; exit 2; }
	$(PYTHON) acceptance/check_report.py hardware-endurance "$(REPORT)"

acceptance-patch:
	@test -n "$(REPORT)" || { echo "REPORT must name a completed evidence-backed patch report" >&2; exit 2; }
	$(PYTHON) acceptance/check_report.py patch-bundle "$(REPORT)"

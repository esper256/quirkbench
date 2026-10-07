export PYTHONPATH := $(CURDIR)/src

PYTHON ?= .venv/bin/python
# Curated existing regressions; explicit paths avoid collecting slow integration
# modules just to deselect them. Keep this list small (see testing policy).
SMOKE_TESTS := tests/test_contracts.py tests/test_store.py tests/test_state_config.py \
 tests/test_controller_review.py tests/test_controller_setup.py \
 tests/test_cli.py::test_monitor_from_separate_interpreter \
 tests/test_cli.py::test_invalid_command_fails_without_system_changes \
 tests/test_cli_redesign.py::test_root_and_every_action_help_are_complete_and_stateless
TESTS ?= $(SMOKE_TESTS)

# Expensive real-system fixtures are explicit final major-version release gates.
# Fail during parsing, before aggregate prerequisites can launch (even with -j).
RELEASE_TARGETS := acceptance-m2 acceptance-v1-image acceptance-qemu acceptance-standard-image acceptance-ostree-repository acceptance-ostree-signatures acceptance-ostree-deployment acceptance-ostree-controller-backup
ifneq ($(filter $(RELEASE_TARGETS),$(MAKECMDGOALS)),)
ifneq ($(RELEASE_QUALIFICATION),1)
$(error Release-only qualification: requires an explicitly requested final major-version release and RELEASE_QUALIFICATION=1; see docs/designs/development.md)
endif
endif

.PHONY: test smoke test-full acceptance-m1 acceptance-qemu acceptance-hardware acceptance-patch acceptance-m2 acceptance-ostree-repository acceptance-ostree-signatures acceptance-ostree-deployment acceptance-ostree-controller-backup

# Routine default is smoke; TESTS=... retains focused development checks.
test:
	$(PYTHON) -m pytest $(TESTS)

smoke:
	$(PYTHON) -m pytest $(SMOKE_TESTS)

# Explicit software milestone gate. TESTS never narrows full acceptance.
test-full:
	$(PYTHON) -m pytest tests --durations=25

# Software-only development distribution; never runs image or hardware gates.
.PHONY: controller-archive
controller-archive:
	@test -n "$(OUTPUT)" || { echo "OUTPUT must name a new controller archive" >&2; exit 2; }
	$(PYTHON) environments/build-controller-archive.py --output "$(OUTPUT)"

# Real UEFI boot cycle: recovery, candidate, missing/load failure and panic fallback.
acceptance-qemu:
	@test -n "$(IMAGE)" -a -n "$(OVMF_CODE)" -a -n "$(OVMF_VARS)" -a -n "$(WORK_DIR)" || { echo "IMAGE, OVMF_CODE, OVMF_VARS and empty WORK_DIR are required" >&2; exit 2; }
	$(PYTHON) -m quirkbench dev image qualify "$(IMAGE)" --manifest "$(IMAGE).json" --ovmf-code "$(OVMF_CODE)" --ovmf-vars "$(OVMF_VARS)" --work "$(WORK_DIR)"

acceptance-ostree-repository:
	@test -n "$(REPOSITORY_WORK)" || { echo "REPOSITORY_WORK must name a new directory" >&2; exit 2; }
	$(PYTHON) acceptance/qualify-ostree-repository.py --work "$(REPOSITORY_WORK)"

acceptance-ostree-signatures:
	@test -n "$(SIGNATURE_WORK)" || { echo "SIGNATURE_WORK must name a new directory" >&2; exit 2; }
	$(PYTHON) acceptance/qualify-ostree-signatures.py --work "$(SIGNATURE_WORK)"

acceptance-ostree-deployment:
	@test -n "$(DEPLOYMENT_WORK)" -a -n "$(DEPLOYMENT_MANIFEST)" -a -n "$(OSTREE_REPO)" -a -n "$(PUBLIC_KEY)" || { echo "DEPLOYMENT_WORK, DEPLOYMENT_MANIFEST, OSTREE_REPO and PUBLIC_KEY are required" >&2; exit 2; }
	$(PYTHON) acceptance/qualify-ostree-deployment.py --work "$(DEPLOYMENT_WORK)" --manifest "$(DEPLOYMENT_MANIFEST)" --repository "$(OSTREE_REPO)" --public-key "$(PUBLIC_KEY)" --repository-mode "$(DEPLOYMENT_REPO_MODE)"

DEPLOYMENT_REPO_MODE ?= bare

acceptance-ostree-controller-backup:
	@test -n "$(CONTROLLER_STATE)" -a -n "$(BACKUP_WORK)" -a -n "$(DEPLOYMENT_MANIFEST)" -a -n "$(OSTREE_REPO)" || { echo "CONTROLLER_STATE, BACKUP_WORK, DEPLOYMENT_MANIFEST and OSTREE_REPO are required" >&2; exit 2; }
	$(PYTHON) acceptance/qualify-ostree-controller-backup.py --controller "$(CONTROLLER_STATE)" --work "$(BACKUP_WORK)" --manifest "$(DEPLOYMENT_MANIFEST)" --repository "$(OSTREE_REPO)"

# No blanket skips: these gates require actual tools and a composed image.
acceptance-m2: acceptance-m1 acceptance-ostree-repository acceptance-ostree-signatures acceptance-ostree-deployment acceptance-ostree-controller-backup acceptance-qemu

# No hardware cases are skipped inside this gate; separate qualification gates
# fail visibly when their required real-world inputs are absent.
acceptance-m1: test-full

acceptance-hardware:
	@test -n "$(REPORT)" || { echo "REPORT must name a completed hardware endurance report" >&2; exit 2; }
	$(PYTHON) acceptance/check_report.py hardware-endurance "$(REPORT)"

acceptance-patch:
	@test -n "$(REPORT)" || { echo "REPORT must name a completed evidence-backed patch report" >&2; exit 2; }
	$(PYTHON) acceptance/check_report.py patch-bundle "$(REPORT)"

# Fresh layout-v2 gate; never reuse historical layout-v1 boot evidence.
.PHONY: acceptance-v1-image
acceptance-v1-image: test-full acceptance-qemu

# Non-smoke image: real commissioning and healthy unprovisioned supervisor wait.
.PHONY: acceptance-standard-image
acceptance-standard-image:
	@test -n "$(IMAGE)" -a -n "$(OVMF_CODE)" -a -n "$(OVMF_VARS)" -a -n "$(WORK_DIR)" || { echo "IMAGE, OVMF_CODE, OVMF_VARS and empty WORK_DIR are required" >&2; exit 2; }
	$(PYTHON) acceptance/qualify-standard-image.py --image "$(IMAGE)" --ovmf-code "$(OVMF_CODE)" --ovmf-vars "$(OVMF_VARS)" --work "$(WORK_DIR)"

# Shared CI/local selections; EVIDENCE must be a new directory outside checkout.
.PHONY: print-smoke-tests test-suite
print-smoke-tests:
	@echo $(SMOKE_TESTS)

test-suite:
	$(PYTHON) -m ci.run run --suite "$(SUITE)" --output "$(EVIDENCE)"

# Repository-owned development setup; no full suite, controller or target startup.
BOOTSTRAP_PYTHON ?= python3
VENV ?= .venv
.PHONY: bootstrap
bootstrap:
	$(BOOTSTRAP_PYTHON) environments/bootstrap.py --python "$(BOOTSTRAP_PYTHON)" --venv "$(VENV)" $(BOOTSTRAP_ARGS)

# Cached userspace tools only; downloads and RPM extraction are separate preparation.
.PHONY: test-recovery-native
test-recovery-native:
	@native_evidence=$$(mktemp -d /tmp/quirkbench-native-evidence.XXXXXXXX); \
	 echo "Evidence: $$native_evidence/evidence"; \
	 $(PYTHON) -m ci.run run --suite recovery-native --timeout 60 --output "$$native_evidence/evidence"

export PYTHONPATH := $(CURDIR)/src

PYTHON ?= .venv/bin/python

.PHONY: test acceptance-m1 demo monitor acceptance-qemu acceptance-hardware acceptance-patch acceptance-m2 acceptance-ostree-repository acceptance-ostree-signatures acceptance-ostree-deployment acceptance-ostree-controller-backup

test:
	$(PYTHON) -m pytest

# Real UEFI boot cycle: recovery, candidate, missing/load failure and panic fallback.
acceptance-qemu:
	@test -n "$(IMAGE)" -a -n "$(OVMF_CODE)" -a -n "$(OVMF_VARS)" -a -n "$(WORK_DIR)" || { echo "IMAGE, OVMF_CODE, OVMF_VARS and empty WORK_DIR are required" >&2; exit 2; }
	$(PYTHON) -m quirkbench qualify-image "$(IMAGE)" --manifest "$(IMAGE).json" --ovmf-code "$(OVMF_CODE)" --ovmf-vars "$(OVMF_VARS)" --work "$(WORK_DIR)"

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

# Fresh layout-v2 gate; never reuse historical layout-v1 boot evidence.
.PHONY: acceptance-v1-image
acceptance-v1-image: test acceptance-qemu

# Non-smoke image: real commissioning and healthy unprovisioned supervisor wait.
.PHONY: acceptance-standard-image
acceptance-standard-image:
	@test -n "$(IMAGE)" -a -n "$(OVMF_CODE)" -a -n "$(OVMF_VARS)" -a -n "$(WORK_DIR)" || { echo "IMAGE, OVMF_CODE, OVMF_VARS and empty WORK_DIR are required" >&2; exit 2; }
	$(PYTHON) acceptance/qualify-standard-image.py --image "$(IMAGE)" --ovmf-code "$(OVMF_CODE)" --ovmf-vars "$(OVMF_VARS)" --work "$(WORK_DIR)"

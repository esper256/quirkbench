"""Composer safety/observable publication behavior; real compose is the M2 gate."""
from dataclasses import replace
import io
import json
from pathlib import Path
import tarfile

import pytest

from quirkbench.build import BuildError, REQUIRED_CONFIG, sha256_file
from quirkbench.compose import ComposeInputs, ComposeRunner, FedoraComposer, extract_payload, rpm_spec, _repo_names, compose_lock


@pytest.fixture(autouse=True)
def reserve_for_small_tmpfs(monkeypatch):
    # Tiny unit-test payloads use /tmp; production reserve remains 20 GiB.
    monkeypatch.setattr("quirkbench.compose.DISK_RESERVE", 1024 * 1024)
    monkeypatch.setattr("quirkbench.compose.builder_base_digest", lambda: "sha256:" + "b" * 64)


def archive(path, entries):
    with tarfile.open(path, "w:xz") as output:
        for name, value in entries.items():
            member = tarfile.TarInfo(name)
            if isinstance(value, tuple):
                member.type, member.linkname = tarfile.SYMTYPE, value[0]
                output.addfile(member)
            else:
                member.size, member.mode = len(value), 0o755
                output.addfile(member, io.BytesIO(value))


def inputs(tmp_path):
    paths = {key: tmp_path / key for key in ("kernel", "config", "initramfs", "modules", "userspace", "build_provenance")}
    paths["kernel"].write_bytes(b"kernel bytes")
    paths["initramfs"].write_bytes(b"ostree enabled initrd")
    paths["config"].write_text("\n".join(f"{key}={value}" if value != "n" else f"# {key} is not set" for key, value in REQUIRED_CONFIG.items()))
    archive(paths["modules"], {"kernel/test.ko": b"module", "modules.dep": b"kernel/test.ko:"})
    archive(paths["userspace"], {"usr/bin/experiment": b"experimental userspace"})
    evidence = {role: paths[role] for role in ("build_provenance", "config", "modules")}
    for role in ("vmlinux", "system_map", "kernel_source", "userspace_source"):
        evidence[role] = tmp_path / role
        evidence[role].write_bytes(role.encode())
    outputs = {key: {"sha256": sha256_file(path)} for key, path in paths.items() if key != "build_provenance"}
    outputs.update({role: {"sha256": sha256_file(evidence[role])} for role in ("vmlinux", "system_map")})
    paths["build_provenance"].write_text(json.dumps({"kernel_release": "6.12-test", "outputs": outputs,
        "inputs": {"source_archive": {"sha256": sha256_file(evidence["kernel_source"])},
                   "userspace_source_archive": {"sha256": sha256_file(evidence["userspace_source"])}}}))
    signing = tmp_path / "signing"
    signing.mkdir()
    repo = tmp_path / "fedora.repo"
    repo.write_text("[fedora]\nbaseurl=https://example.test/fedora\ngpgcheck=1\nsslverify=1\n")
    return ComposeInputs(paths, {key: sha256_file(path) for key, path in paths.items()}, "6.12-test", "43", "lab", "A" * 40,
                         signing, repo, sha256_file(repo), 1000,
                         evidence_paths=evidence, evidence_sha256={role: sha256_file(path) for role, path in evidence.items()})


def test_inputs_reject_changed_component_and_misattributed_provenance(tmp_path):
    value = inputs(tmp_path)
    value.validate()
    value.artifact_paths["kernel"].write_bytes(b"other kernel")
    with pytest.raises(BuildError, match="digest"):
        value.validate()
    hashes = {**value.artifact_sha256, "kernel": sha256_file(value.artifact_paths["kernel"])}
    with pytest.raises(BuildError, match="provenance"):
        replace(value, artifact_sha256=hashes).validate()


@pytest.mark.parametrize("entries", [
    {"../escaped": b"bad"}, {"/etc/shadow": b"bad"}, {"etc/shadow": b"bad"},
    {"home/agent/.codex/auth.json": b"bad"}, {"usr/escape": ("../../../outside",)},
    {"usr/.ssh/id_rsa": b"bad"},
])
def test_payload_cannot_escape_or_publish_credentials(tmp_path, entries):
    packed = tmp_path / "payload.tar.xz"
    archive(packed, entries)
    with pytest.raises(BuildError):
        extract_payload(packed, tmp_path / "stage", userspace=True)
    assert not (tmp_path / "escaped").exists()


def test_payload_preserves_component_bytes_without_strip_or_install_scripts(tmp_path):
    packed = tmp_path / "payload.tar.xz"
    archive(packed, {"usr/bin/experiment": b"ELF with symbols"})
    payload = tmp_path / "stage"
    extract_payload(packed, payload, userspace=True)
    spec = rpm_spec("quirkbench-experiment", "1.0", payload)
    assert (payload / "usr/bin/experiment").read_bytes() == b"ELF with symbols"
    assert "%post" not in spec and "%pre" not in spec.replace("%prep", "")
    assert "%global __os_install_post %{nil}" in spec
    assert "/usr/bin/experiment" in spec


def fake_runner(monkeypatch, calls, *, fail_phase=None):
    monkeypatch.setattr("quirkbench.compose._require_container", lambda: None)
    monkeypatch.setattr("quirkbench.compose.ResourceLimits.from_cgroup", lambda: None)
    def run(self, argv, *, phase, cwd, env, timeout=3600):
        calls.append((phase, argv))
        (cwd / (phase + ".log")).write_text("fixture output\n")
        if phase == fail_phase:
            raise BuildError("injected interruption")
        if phase == "compose-package-inventory":
            return "rpm-ostree\t0:2026.1-1.fc43\tx86_64\n"
        if phase == "download-dependencies":
            assert (cwd / "rpm-cache").is_dir()
            (cwd / "dependency-lock.json").write_text("{}")
            cache = cwd / "rpm-cache"
            cache.mkdir(exist_ok=True)
            (cache / "dep.rpm").write_bytes(b"dependency RPM fixture")
        if phase == "inspect-initramfs":
            return "dracut modules:\nostree\n"
        if phase.startswith("package-"):
            top = Path(argv[3].split(" ", 1)[1])
            (top / "RPMS" / (top.name + ".rpm")).write_bytes(phase.encode())
        if phase.startswith("inspect-replacement-"):
            return "alsa-lib-0:1.2.13-99.fc43.x86_64"
        if phase == "resolve-revision":
            return "a" * 64 + "\n"
        if phase == "init-published-repo":
            repo = Path(argv[1].split("=", 1)[1])
            repo.mkdir()
            (repo / "config").write_text("[core]\nmode=archive\n")
        return ""
    monkeypatch.setattr(ComposeRunner, "run", run)


def test_composition_signs_exact_revision_before_publication(tmp_path, monkeypatch):
    value = inputs(tmp_path)
    calls = []
    fake_runner(monkeypatch, calls)
    composer = FedoraComposer(tmp_path / "workspace", tmp_path / "published")
    manifest = composer.compose(value)
    evidence = manifest.provenance["build_evidence"]["artifacts"]
    assert evidence["compose_dependency_lock"] == sha256_file(composer.evidence_files["compose_dependency_lock"])
    with tarfile.open(composer.evidence_files["compose_dependency_rpms"]) as snapshot:
        assert len(snapshot.getmembers()) == 1
    assert manifest.revision == "a" * 64
    assert manifest.provenance["config_sha256"] == value.artifact_sha256["config"]
    assert manifest.protection_profile == "usb-excluded-controllers-v1"
    phases = [phase for phase, _ in calls]
    assert phases.index("sign-revision") < phases.index("publish-revision") < phases.index("verify-publication")
    publish = next(args for phase, args in calls if phase == "publish-revision")
    assert publish[-1] == manifest.revision
    tree = json.loads(next((tmp_path / "workspace").glob("compose-*/tree.json")).read_text())
    assert "rpm" in tree["packages"]  # rpm-ostree runs rpm for target sysusers
    assert "nss-altfiles" in tree["packages"]  # immutable /usr/lib/passwd identities
    assert tree["no-initramfs"] is True
    finalize = composer.evidence_files["compose_finalize_hook"].read_text()
    assert value.artifact_sha256["initramfs"] in finalize
    assert "sha256sum -c -" in finalize
    assert "ln " in finalize
    assert tree["add-commit-metadata"]["quirkbench.config-sha256"] == value.artifact_sha256["config"]


def test_failed_compose_does_not_publish_or_return_manifest_and_retry_succeeds(tmp_path, monkeypatch):
    value = inputs(tmp_path)
    calls = []
    fake_runner(monkeypatch, calls, fail_phase="sign-revision")
    composer = FedoraComposer(tmp_path / "workspace", tmp_path / "published")
    with pytest.raises(BuildError, match="interruption"):
        composer.compose(value)
    assert not (tmp_path / "published").exists()
    assert not list((tmp_path / "workspace").glob("compose-*/deployment.json"))
    fake_runner(monkeypatch, calls)
    assert composer.compose(value).revision == "a" * 64
    assert len(list((tmp_path / "workspace").glob("compose-*"))) == 2


def test_old_initramfs_without_ostree_cannot_publish(tmp_path, monkeypatch):
    value = inputs(tmp_path)
    monkeypatch.setattr("quirkbench.compose._require_container", lambda: None)
    monkeypatch.setattr("quirkbench.compose.ResourceLimits.from_cgroup", lambda: None)
    monkeypatch.setattr(ComposeRunner, "run", lambda *args, **kwargs: "systemd\n")
    with pytest.raises(BuildError, match="ostree dracut module"):
        FedoraComposer(tmp_path / "workspace", tmp_path / "published").compose(value)
    assert not (tmp_path / "published").exists()


def test_runner_timeout_preserves_diagnostics(tmp_path):
    events = []
    with pytest.raises(BuildError, match="exceeded"):
        ComposeRunner(tmp_path, events.append).run(["python3", "-u", "-c", "import time; print('before hang'); time.sleep(10)"],
            phase="timeout", cwd=tmp_path, env={"PATH": "/usr/bin:/bin"}, timeout=1)
    assert b"before hang" in (tmp_path / "timeout.log").read_bytes()
    assert events[0]["status"] == "running"


def test_native_component_replacement_pins_local_repository_selection(tmp_path, monkeypatch):
    value = inputs(tmp_path)
    rpm = tmp_path / "alsa-lib.rpm"
    rpm.write_bytes(b"rebuilt ALSA package fixture")
    value = replace(value, replacement_rpms={rpm: sha256_file(rpm)})
    calls = []
    fake_runner(monkeypatch, calls)
    manifest = FedoraComposer(tmp_path / "workspace", tmp_path / "published").compose(value)
    tree = json.loads(next((tmp_path / "workspace").glob("compose-*/tree.json")).read_text())
    assert tree["repo-packages"] == [{"repo": "quirkbench-experiment", "packages": ["alsa-lib-0:1.2.13-99.fc43.x86_64"]}]
    assert manifest.provenance["rpm_sha256"]["alsa-lib.rpm"] == sha256_file(rpm)


def test_repository_transport_and_unknown_protection_fail_closed(tmp_path):
    value = inputs(tmp_path)
    with pytest.raises(BuildError, match="protection"):
        replace(value, protection_profile="disable-all-checks").validate()
    value.fedora_repo_file.write_text("[fedora]\nbaseurl=http://example.test/insecure\ngpgcheck=1\n")
    with pytest.raises(BuildError, match="HTTPS"):
        replace(value, fedora_repo_sha256=sha256_file(value.fedora_repo_file)).validate()


def test_custom_kernel_release_is_capability_not_invalid_rpm_evr(tmp_path):
    payload = tmp_path / "payload"
    kernel = payload / "usr/lib/modules/6.12.60-quirkbench-ostree/vmlinuz"
    kernel.parent.mkdir(parents=True)
    kernel.write_bytes(b"kernel")
    spec = rpm_spec("kernel-quirkbench", "0.1000.abc", payload, kernel_release="6.12.60-quirkbench-ostree")
    assert "Provides: kernel = 6.12.60\n" in spec
    assert "Provides: kernel = 0." not in spec
    assert "Provides: kernel-uname-r(6.12.60-quirkbench-ostree)" in spec
    assert "Provides: kernel-uname-r =" not in spec


def test_disabled_fedora_debug_and_source_repositories_stay_disabled(tmp_path):
    repo = tmp_path / "fedora.repo"
    repo.write_text("[fedora]\nbaseurl=https://example.test/fedora\ngpgcheck=1\n"
                    "[fedora-debug]\nenabled=0\nbaseurl=https://example.test/debug\ngpgcheck=1\n")
    assert _repo_names(repo) == ["fedora"]


def test_publication_requires_source_and_symbol_evidence_closure(tmp_path):
    value = inputs(tmp_path)
    with pytest.raises(BuildError, match="evidence closure"):
        replace(value, evidence_paths={}).validate()
    altered = dict(value.evidence_paths)
    alternate = tmp_path / "other-vmlinux"
    alternate.write_bytes(b"symbols from another build")
    altered["vmlinux"] = alternate
    with pytest.raises(BuildError, match="attribute evidence"):
        replace(value, evidence_paths=altered, evidence_sha256={**value.evidence_sha256, "vmlinux": sha256_file(alternate)}).validate()


def test_composer_lock_has_visible_bounded_wait(tmp_path, monkeypatch):
    ticks = iter([0, 0, 0, 61])
    monkeypatch.setattr("quirkbench.compose.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("quirkbench.compose.time.sleep", lambda seconds: None)
    def busy(*args):
        raise BlockingIOError()
    monkeypatch.setattr("quirkbench.compose.fcntl.flock", busy)
    events = []
    with pytest.raises(BuildError, match="deadline"):
        with compose_lock(tmp_path / "build.lock", events.append):
            pytest.fail("busy controller build lock must not be acquired")
    assert events[0]["status"] == "running"
    assert events[1]["status"] == "waiting"
    assert events[1]["remaining_s"] == 60

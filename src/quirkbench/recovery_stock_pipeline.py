"""Stock recovery staging; never prepares sources or invokes a compiler."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import stat

from .build import BuildError, Command, _safe_build_path, sha256_file
from .contracts import canonical, digest
from .recovery_stock import audit_stock_modules, preflight_recipe
from .recovery_module_audit import recovery_modules_directory
from .store import atomic_write


def _file(root, *names):
    for name in names:
        path = root / name
        if path.exists():
            real = path.resolve()
            if not real.is_relative_to(root) or not real.is_file():
                raise BuildError("stock kernel package path escapes sysroot")
            return real
    raise BuildError("stock kernel package payload missing: " + str(names))



def audit_pairing_executables(root):
    """Require pairing tools in the target sysroot, without executing host binaries."""
    root = Path(root)
    for name in ('openssl', 'gpg'):
        path = root/'usr/bin'/name
        try:
            real = path.resolve(strict=True)
            mode = real.stat().st_mode
            usable = real.is_relative_to(root) and stat.S_ISREG(mode) and mode & 0o111
        except (OSError, RuntimeError):
            usable = False
        if not usable:
            raise BuildError('stock pairing executable missing, nonexecutable or outside sysroot: usr/bin/'+name)

def run_base(recipe, store, stage, *, runner, limits, rootfs_installer, cache=None):
    checked = preflight_recipe(recipe, store)
    stage = Path(stage)
    _safe_build_path(stage)
    if (not stage.is_absolute() or stage.resolve() != stage or stage.exists()
            or stage.is_symlink() or not stage.parent.is_dir()):
        raise BuildError("stock stage must be a new canonical directory")
    if (type(limits.cpus) is not int or limits.cpus < 1 or type(limits.jobs) is not int or limits.jobs < 1
            or type(limits.memory_bytes) is not int or limits.memory_bytes < 4*1024**3):
        raise BuildError("stock staging requires bounded recovery resources")
    from .build_pipeline import BoundedRunner
    if isinstance(runner, BoundedRunner) and runner.workspace != stage:
        raise BuildError("bounded stock workspace differs from stage")
    stage.mkdir(mode=0o700)
    cache_identity = {"kind": "stock-rpm-base-v2", "rootfs_lock_sha256": recipe["rootfs_lock_sha256"],
                      "implementation": digest(Path(__file__).read_bytes() +
                                               Path(__file__).with_name("recovery_rootfs.py").read_bytes() +
                                               Path(__file__).with_name("recovery_stock.py").read_bytes())}
    lineage = "stock-" + digest(canonical({"kind": "stock-rpm-base-v2", "rootfs_lock": recipe["rootfs_lock_sha256"]}))[:32]
    if cache is not None:
        from .build_cache import BuildStageCache
        if not isinstance(cache, BuildStageCache):
            raise BuildError("stock cache must be a private BuildStageCache")
        with cache.lock(lineage):
            restored = cache.load(lineage, "stock-base-v2", cache_identity,
                                  {"rootfs": stage / "rootfs", "artifacts": stage / "artifacts"})
        if restored is not None:
            restored["rootfs"] = str(stage / "rootfs")
            restored["recipe_digest"] = checked["recipe_digest"]
            restored["source_date_epoch"] = recipe["source_date_epoch"]
            verify_base(recipe, store, stage, restored)
            return restored
    rootfs = stage / "rootfs"
    if rootfs_installer(None, checked["rootfs_lock"], store, rootfs) != rootfs:
        raise BuildError("stock installer returned another sysroot")
    if (rootfs.is_symlink() or not rootfs.is_dir()
            or (rootfs / "etc/quirkbench-rootfs").read_text().strip() != "quirkbench-fedora-target-v1"):
        raise BuildError("stock rootfs marker missing")
    audit_pairing_executables(stage / "rootfs")
    release = checked["rootfs_lock"]["kernel_release"]
    kernel = _file(rootfs, f"usr/lib/modules/{release}/vmlinuz", f"lib/modules/{release}/vmlinuz", f"boot/vmlinuz-{release}")
    config = _file(rootfs, f"usr/lib/modules/{release}/config", f"lib/modules/{release}/config", f"boot/config-{release}")
    artifacts = stage / "artifacts"
    artifacts.mkdir()
    for name, source in (("kernel", kernel), ("config", config)):
        shutil.copyfile(source, artifacts / name)
        if sha256_file(source) != sha256_file(artifacts / name):
            raise BuildError("stock package payload changed while staging")
    audit = audit_stock_modules(artifacts / "config", rootfs, release)
    result = {"schema_version": 2, "recipe_digest": checked["recipe_digest"],
            "source_date_epoch": recipe["source_date_epoch"], "rootfs": str(rootfs),
            "kernel_stage": {"schema_version": 2, "kernel_release": release,
                "rpm_snapshot_sha256": checked["rootfs_lock"]["rpm_snapshot_sha256"],
                "module_audit": audit,
                "outputs": {"kernel": sha256_file(artifacts / "kernel"),
                            "config": sha256_file(artifacts / "config")}}}

    if cache is not None:
        with cache.lock(lineage):
            cache.publish(lineage, "stock-base-v2", cache_identity,
                          {"rootfs": rootfs, "artifacts": artifacts}, result)
    return result


def verify_base(recipe, store, stage, base):
    checked = preflight_recipe(recipe, store)
    stage = Path(stage)
    if (not isinstance(base, dict) or base.get("schema_version") != 2
            or base.get("recipe_digest") != checked["recipe_digest"]
            or base.get("rootfs") != str(stage / "rootfs")
            or base.get("source_date_epoch") != recipe["source_date_epoch"]):
        raise BuildError("stock base record differs from recipe")
    kernel = base.get("kernel_stage", {})
    audit_pairing_executables(stage / "rootfs")
    release = checked["rootfs_lock"]["kernel_release"]
    if (kernel.get("kernel_release") != release
            or kernel.get("rpm_snapshot_sha256") != checked["rootfs_lock"]["rpm_snapshot_sha256"]
            or kernel.get("module_audit") != audit_stock_modules(stage / "artifacts/config", stage / "rootfs", release)
            or kernel.get("outputs") != {name: sha256_file(stage / "artifacts" / name)
                                         for name in ("kernel", "config")}):
        raise BuildError("stock kernel inputs changed after staging")
    # Copied artifacts must still correspond to the installed package payload.
    rootfs = stage / "rootfs"
    for name, candidates in (("kernel", (f"usr/lib/modules/{release}/vmlinuz", f"lib/modules/{release}/vmlinuz", f"boot/vmlinuz-{release}")),
                             ("config", (f"usr/lib/modules/{release}/config", f"lib/modules/{release}/config", f"boot/config-{release}"))):
        if sha256_file(_file(rootfs, *candidates)) != kernel["outputs"][name]:
            raise BuildError("installed stock payload differs from staged artifact")
    return checked


def run_initramfs(recipe, store, stage, base, runtime, *, runner, limits):
    checked = verify_base(recipe, store, stage, base)
    if (runtime.get("recipe_digest") != checked["recipe_digest"]
            or runtime.get("runtime_revision_sha256") != recipe["runtime_revision_sha256"]):
        raise BuildError("stock runtime record differs from recipe")
    from .recovery_runtime_revision import audit_installed_runtime
    from .recovery_initramfs_audit import audit_recovery_initramfs_tree
    audit_installed_runtime(stage / "rootfs", checked["runtime_revision"])
    from .recovery_storage import audit_guard
    audit_guard(stage/"rootfs",require_module=True)
    release = base["kernel_stage"]["kernel_release"]
    conf = stage / "stock-dracut.conf"
    conf.write_bytes(store.get(recipe["dracut_config_sha256"]))
    confdir = stage / "dracut-conf.d"
    confdir.mkdir()
    output = stage / "artifacts" / f"initramfs-{release}.img"
    logs = stage / "logs"
    logs.mkdir(exist_ok=True)
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
           "SOURCE_DATE_EPOCH": str(recipe["source_date_epoch"]), "TZ": "UTC", "LC_ALL": "C"}
    dracut_base=stage/'rootfs/usr/lib/dracut'
    if (dracut_base.is_symlink() or not dracut_base.is_dir()
            or not (dracut_base/'dracut-functions.sh').is_file()
            or not (dracut_base/'modules.d/99quirkbench-storage/module-setup.sh').is_file()):
        raise BuildError('stock sysroot lacks retained dracut and storage guard module')
    env['dracutbasedir']=str(dracut_base)

    command = Command(("dracut", "--force", "--reproducible", "--no-hostonly",
                       "--sysroot", str(stage / "rootfs"), "--conf", str(conf),
                       "--confdir", str(confdir), "--kmoddir", str(stage / "rootfs/lib/modules" / release),
                       "--kver", release, str(output)), stage / "artifacts")
    runner.run(command, phase="initramfs-stock-recovery", log=logs / "initramfs-stock.log",
               timeout_s=1800, env=env, limits=limits, on_activity=lambda *_: None)
    if output.is_symlink() or not output.is_file() or not output.stat().st_size:
        raise BuildError("stock initramfs output missing")
    verify_base(recipe, store, stage, base)
    before = sha256_file(output)
    tree = stage / "initramfs-inspect"
    tree.mkdir()
    runner.run(Command(("lsinitrd", "--unpack", str(output)), tree), phase="audit-recovery-initramfs",
               log=logs / "audit-stock.log", timeout_s=600, env=env, limits=limits,
               on_activity=lambda *_: None)
    audit = audit_recovery_initramfs_tree(tree, release, checked["profile"])
    if sha256_file(output) != before:
        raise BuildError("stock initramfs changed during inspection")
    return {"schema_version": 2, "kernel_release": release,
            "kernel_config_sha256": base["kernel_stage"]["outputs"]["config"],
            "module_files_digest": base["kernel_stage"]["module_audit"]["module_files_digest"],
            "initramfs_sha256": before, "initramfs_bytes": output.stat().st_size,
            "dracut_config_sha256": recipe["dracut_config_sha256"], "archive_audit": audit,
            "source_date_epoch": recipe["source_date_epoch"]}


def prepare_image(recipe, store, stage, record, output):
    from .image import ImageInputs
    from .recovery_capacity import validate_recovery_capacity
    from .recovery_runtime_revision import audit_installed_runtime
    checked = verify_base(recipe, store, stage, record)
    rootfs, artifacts = stage / "rootfs", stage / "artifacts"
    release = checked["rootfs_lock"]["kernel_release"]
    initramfs = artifacts / f"initramfs-{release}.img"
    ir = record.get("initramfs_stage", {})
    if (record.get("runtime_revision_sha256") != recipe["runtime_revision_sha256"]
            or ir.get("schema_version") != 2 or ir.get("kernel_release") != release
            or ir.get("kernel_config_sha256") != record["kernel_stage"]["outputs"]["config"]
            or ir.get("module_files_digest") != record["kernel_stage"]["module_audit"]["module_files_digest"]
            or ir.get("dracut_config_sha256") != recipe["dracut_config_sha256"]
            or ir.get("source_date_epoch") != recipe["source_date_epoch"]
            or not isinstance(ir.get("archive_audit"), dict)
            or ir["archive_audit"].get("kernel_release") != release
            or ir.get("initramfs_sha256") != sha256_file(initramfs)
            or ir.get("initramfs_bytes") != initramfs.stat().st_size):
        raise BuildError("stock image initramfs/runtime stage differs")
    from .recovery_initramfs_audit import audit_recovery_initramfs_tree
    if ir['archive_audit']!=audit_recovery_initramfs_tree(stage/'initramfs-inspect',release,checked['profile']):
        raise BuildError('stock archive audit changed after staging')
    audit_installed_runtime(rootfs, checked["runtime_revision"])
    from .recovery_image_plan import audit_factory_root
    audit_factory_root(rootfs, checked)
    from .recovery_storage import audit_guard
    audit_guard(rootfs,require_module=True)
    if not output.is_absolute() or output.is_relative_to(stage):
        raise BuildError("stock image output must be separate from stage")
    profile_path = artifacts / "storage-policy.json"
    atomic_write(profile_path, canonical(checked["profile"]))
    provenance = artifacts / "recovery-provenance.json"
    layout = recipe["layout"]
    capacity = validate_recovery_capacity(rootfs, artifacts / "kernel", initramfs, layout["root_mib"])
    atomic_write(provenance, canonical({"schema": 2, "kernel_origin": "stock-rpm",
        "recipe_digest": checked["recipe_digest"], "base_image_digest": recipe["builder_image_digest"],
        "storage_policy_sha256": recipe["storage_policy_sha256"],
        "runtime_revision_sha256": recipe["runtime_revision_sha256"],
        "rootfs_lock_sha256": recipe["rootfs_lock_sha256"],
        "rpm_snapshot_sha256": checked["rootfs_lock"]["rpm_snapshot_sha256"],
        "capacity_preflight": capacity,
        "inputs": {"config": {"sha256": sha256_file(artifacts / "config")}},
        "outputs": {"kernel": {"sha256": sha256_file(artifacts / "kernel")},
                    "initramfs": {"sha256": sha256_file(initramfs)}}}) + b"\n")
    inputs = ImageInputs(output=output, recovery_kernel=artifacts / "kernel",
        recovery_config=artifacts / "config", recovery_initramfs=initramfs, rootfs_dir=rootfs,
        recovery_provenance=provenance, recovery_storage_policy=profile_path,
        recovery_profile_id=checked["profile"]["profile_id"],
        recovery_profile_digest=digest(canonical(checked["profile"])),
        recovery_kernel_release=release, recovery_module_files_digest=ir["module_files_digest"],
        size_mib=layout["factory_size_mib"], root_mib=layout["root_mib"],
        experiment_mib=layout["experiment_mib"], library_mib=layout["library_mib"],
        log_budget_mib=layout["log_budget_mib"])
    inputs.validate()
    return inputs

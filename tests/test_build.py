from pathlib import Path
import hashlib

import pytest

from quirkbench.build import (
    BuildError, KernelBuild, REQUIRED_CONFIG, _validate_command,
    validate_kernel_config,
)


@pytest.mark.parametrize('memory_gib,quota,jobs', [(4, '400000 100000', 2),
                                                  (8, '400000 100000', 4),
                                                  (4, '100000 100000', 1)])
def test_job_selection_uses_worker_capacity_and_cpu_quota(monkeypatch, tmp_path,
                                                        memory_gib, quota, jobs):
    import quirkbench.build as module

    readings = {
        '/proc/meminfo': 'MemAvailable: 25165824 kB\n',
        '/sys/fs/cgroup/memory.max': str(memory_gib * 1024**3),
        '/sys/fs/cgroup/memory.current': str(memory_gib * 1024**3 - 1024),
        '/sys/fs/cgroup/cpu.max': quota,
    }
    original = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda path, *a, **kw:
                        readings[str(path)] if str(path) in readings else original(path, *a, **kw))
    monkeypatch.setattr(module.os, 'cpu_count', lambda: 16)
    assert module.recommended_jobs() == jobs
    build = KernelBuild(*(tmp_path / name for name in ('source', 'obj', 'root', 'out')))
    assert f'-j{jobs}' in build.compile_plan()[0].argv
    assert KernelBuild(build.source, build.build_dir, build.sysroot,
                       build.output_dir, jobs=1).effective_jobs == 1
    with pytest.raises(BuildError, match='exceeds resource limit'):
        KernelBuild(build.source, build.build_dir, build.sysroot,
                    build.output_dir, jobs=jobs + 1).compile_plan()


def test_job_budget_rejects_insufficient_memory():
    from quirkbench.build import kernel_job_budget
    with pytest.raises(BuildError, match='defer kernel build'):
        kernel_job_budget(4, 1024**3)


@pytest.mark.parametrize('available_gib,jobs', [(12, 4), (6, 2)])
def test_bounded_worker_reserves_desktop_headroom_without_double_halving(monkeypatch,
                                                                     available_gib, jobs):
    import quirkbench.build as module
    readings = {
        '/proc/meminfo': f'MemTotal: 33554432 kB\nMemAvailable: {available_gib * 1024**2} kB\n',
        '/sys/fs/cgroup/memory.max': str(8 * 1024**3),
        '/sys/fs/cgroup/cpu.max': '400000 100000',
    }
    original = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda path, *a, **kw:
                        readings[str(path)] if str(path) in readings else original(path, *a, **kw))
    monkeypatch.setattr(module.os, 'cpu_count', lambda: 16)
    assert module.recommended_jobs() == jobs


def _write_config(path: Path, overrides: dict[str, str] | None = None) -> None:
    values = dict(REQUIRED_CONFIG)
    values.update(overrides or {})
    path.write_text("\n".join(
        f"{key}={value}" if value != "n" else f"# {key} is not set"
        for key, value in values.items()) + "\n")


def test_protected_kernel_config_accepts_usb_and_rejects_internal_drivers(tmp_path: Path) -> None:
    config = tmp_path / ".config"
    _write_config(config)
    validate_kernel_config(config)
    for symbol in ("CONFIG_BLK_DEV_NVME", "CONFIG_ATA", "CONFIG_MMC",
                   "CONFIG_VIRTIO_BLK", "CONFIG_EFIVAR_FS"):
        _write_config(config, {symbol: "m"})
        with pytest.raises(BuildError, match=symbol):
            validate_kernel_config(config)
    _write_config(config, {"CONFIG_USB_STORAGE": "m"})
    with pytest.raises(BuildError, match="CONFIG_USB_STORAGE"):
        validate_kernel_config(config)


def test_build_plan_is_staged_and_target_scoped(tmp_path: Path) -> None:
    paths = [tmp_path / name for name in ("source", "obj", "sysroot", "out")]
    for path in paths:
        path.mkdir()
    build = KernelBuild(*paths)
    configure = build.configure_plan()
    compile_commands = build.compile_plan()
    assert configure[0].argv[-1] == "x86_64_defconfig"
    assert "-d" in configure[1].argv
    assert "ATA" in configure[1].argv
    assert configure[-1].argv[-1] == "olddefconfig"
    assert any(arg == f"INSTALL_MOD_PATH={paths[2]}" for arg in compile_commands[1].argv)
    assert not any(arg.startswith("INSTALL_MOD_STRIP=") for arg in compile_commands[1].argv)
    dracut_config = tmp_path / "dracut.conf"
    dracut_config.write_text('hostonly="no"\n')
    dracut_confdir = tmp_path / "dracut-conf.d"
    dracut_confdir.mkdir()
    initrd = build.initramfs_plan("6.12.1-lab", dracut_config=dracut_config,
                                  dracut_confdir=dracut_confdir)[0]
    assert "--no-hostonly" in initrd.argv
    assert "--reproducible" in initrd.argv
    assert str(paths[2] / "lib/modules/6.12.1-lab") in initrd.argv
    assert str(dracut_config) in initrd.argv
    assert initrd.argv[8:10] == ("--confdir", str(dracut_confdir))
    (paths[2] / "etc").mkdir()
    (paths[2] / "etc/os-release").write_text("ID=fedora\n")
    assert _validate_command(initrd)
    (dracut_confdir / "injected.conf").write_text('hostonly="yes"\n')
    with pytest.raises(BuildError, match="must be empty"):
        build.initramfs_plan("6.12.1-lab", dracut_config=dracut_config,
                             dracut_confdir=dracut_confdir)
    with pytest.raises(BuildError, match="must be empty"):
        _validate_command(initrd)
    with pytest.raises(ValueError):
        build.initramfs_plan("../../host", dracut_config=dracut_config,
                             dracut_confdir=dracut_confdir)


def test_all_commands_validated_before_any_execute(tmp_path, monkeypatch):
    import quirkbench.build as module
    source = tmp_path / 'source'
    source.mkdir()
    obj = tmp_path / 'obj'
    obj.mkdir()
    valid_compile = module.Command(('make','-C',str(source),f'O={obj}','ARCH=x86_64','-j1','bzImage','modules','vmlinux'),source)
    forbidden = module.Command(('make','install'),source)
    monkeypatch.setattr(module,'_require_container',lambda:None)
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**kw:pytest.fail('must validate entire batch before execution'))
    with pytest.raises(module.BuildError):
        module.run_commands([valid_compile,forbidden],config_to_validate=tmp_path/'config')


def test_canonical_account_home_under_var_is_usable_but_system_paths_remain_forbidden(monkeypatch):
    from types import SimpleNamespace
    from quirkbench import build
    monkeypatch.setattr(build.pwd,'getpwuid',lambda uid:SimpleNamespace(pw_dir='/var/home/different-user'))
    build._safe_build_path(Path('/var/home/different-user/.local/state/quirkbench/workspaces/source'))
    for path in ('/var','/var/lib/rpm','/var/home/other-user/work','/dev/sda','/mnt/target'):
        with pytest.raises(BuildError):build._safe_build_path(Path(path))

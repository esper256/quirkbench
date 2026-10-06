from pathlib import Path

import pytest

from quirkbench.image import ImageError, ImageInputs, grub_config
from quirkbench.build import REQUIRED_CONFIG


def _inputs(tmp_path: Path) -> ImageInputs:
    files = []
    for name in ("recovery-kernel", "recovery-initrd"):
        file = tmp_path / name
        file.write_bytes(name.encode())
        files.append(file)
    root = tmp_path / "root"
    (root / "sbin").mkdir(parents=True)
    (root / "sbin/init").write_text("init")
    (root / "etc").mkdir()
    (root / "etc/os-release").write_text("ID=fedora\n")
    (root / "etc/quirkbench-rootfs").write_text("quirkbench-fedora-target-v1\n")
    for name in ('usr/sbin/NetworkManager', 'usr/bin/nmtui'):
        program=root/name;program.parent.mkdir(parents=True,exist_ok=True)
        program.write_text('fixture');program.chmod(0o755)
    configs = []
    for name in ("recovery.config",):
        cfg = tmp_path / name
        cfg.write_text("\n".join(f"{key}={value}" if value != "n" else f"# {key} is not set" for key, value in REQUIRED_CONFIG.items()) + "\n")
        configs.append(cfg)
    return ImageInputs(tmp_path / "usb.img", *files, root, *configs)


def test_image_requires_independent_recovery_and_new_regular_output(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    inputs.validate()
    inputs.output.write_text("existing")
    with pytest.raises(ImageError, match="overwrite"):
        inputs.validate()


def test_prepared_format_uses_actual_factory_size_not_unused_legacy_budgets(tmp_path):
    from dataclasses import replace
    inputs = _inputs(tmp_path)
    prepared = replace(inputs, controller_prepared=True, experiment_mib=0, library_mib=0, log_budget_mib=0)
    prepared.validate()
    with pytest.raises(ImageError, match='capacities'):
        replace(prepared, controller_prepared=False).validate()


def test_factory_image_rejects_private_gpg_home(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    private = inputs.rootfs_dir / 'root/.gnupg'
    private.mkdir(parents=True)
    (private / 'private-keys-v1.d').mkdir()
    with pytest.raises(ImageError, match='credentials'):
        inputs.validate()


def test_grub_defaults_to_recovery_and_checks_one_shot_clear():
    cfg=grub_config("01234567-89ab-cdef-0123-456789abcdef")
    assert "set default=0" in cfg
    assert "set fallback=0" in cfg
    assert not any(line.lstrip().startswith("search ") for line in cfg.splitlines())
    assert '$root' in cfg
    assert "^([^,)]+),gpt1$" in cfg
    assert "save_env --file=$state/quirkbench/next.env next_entry candidate_id" in cfg
    clear=cfg.index("save_env")
    assert clear < cfg.index("unset next_entry") < cfg.index("load_env",cfg.index("unset next_entry"))
    assert "set default=1" in cfg
    assert '-a -f $data/quirkbench/boot/$chosen_candidate.cfg ]; then' in cfg
    assert cfg.index('smbios --type 1') < cfg.index('set default=1')
    assert "source $data/quirkbench/boot/$chosen_candidate.cfg" in cfg
    assert "rootflags=noload fsck.mode=skip rd.skipfsck" in cfg
    recovery_lines = [line for line in cfg.splitlines() if line.lstrip().startswith('linux $esp/vmlinuz-recovery')]
    assert len(recovery_lines) == 3 and all(' selinux=0 ' in line for line in recovery_lines)
    assert "init=/bin/sh" not in cfg
    fallback=cfg[cfg.index("echo 'QUIRKBENCH_GRUB candidate-load-failed'"):]
    assert 'linux $esp/vmlinuz-recovery' in fallback and 'initrd $esp/initramfs-recovery.img' in fallback
    assert 'quirkbench.mode=recovery' in fallback and '\n    boot\n' in fallback


@pytest.mark.parametrize('smoke', [False, True])
def test_recovery_diagnostics_preserve_boot_roles_and_candidate_selection(smoke):
    from quirkbench.recovery_storage import boot_roles

    roles = [f'00000000-0000-0000-0000-{number:012d}' for number in range(1, 7)]
    cfg = grub_config(roles[1], esp_uuid=roles[0], state_uuid=roles[2],
                      data_uuid=roles[3], library_uuid=roles[4], evidence_uuid=roles[5],
                      stock_recovery=True, smoke=smoke)
    assert 'set timeout_style=menu\nset timeout=5\n' in cfg
    assert cfg.count('set default=1') == 1
    # Keep candidate at index 1 when armed: diagnostics must follow its conditional
    # menu definition, never displace it or become an automatic fallback.
    assert cfg.index('--id=recovery {') < cfg.index('--id=candidate {') < cfg.index('--id=recovery-debug {')
    assert cfg.index('save_env') < cfg.index('smbios --type 1') < cfg.index('--id=recovery-debug {')
    lines = [line.strip().split(' ', 2)[2] for line in cfg.splitlines()
             if line.strip().startswith('linux $esp/vmlinuz-recovery ')]
    normal, fallback, debug = lines
    assert normal == fallback
    assert debug.startswith(normal + ' ')
    assert set(debug[len(normal):].split()) == {
        'rd.debug', 'rd.info', 'loglevel=7', 'ignore_loglevel',
        'systemd.show_status=1', 'systemd.log_level=debug', 'systemd.log_target=journal-or-kmsg',
        'systemd.journald.forward_to_console=1', 'systemd.journald.max_level_console=debug',
        'rd.udev.log_level=debug', 'udev.log_level=debug',
    }
    for arguments in lines:
        assert boot_roles(arguments) == roles
        assert [word for word in arguments.split() if word.startswith('console=')] == [
            'console=ttyS0,115200', 'console=tty0']
        assert ('quirkbench.smoke=1' in arguments) == smoke
    assert cfg.count('initrd $esp/initramfs-recovery.img') == 3
    diagnostic_entry = cfg[cfg.index('menuentry \'Quirkbench recovery - verbose'):]
    assert 'source ' not in diagnostic_entry and 'save_env' not in diagnostic_entry


def test_factory_four_partitions_reserve_space_for_commissioning():
    from quirkbench.image import partition_layout
    parts=partition_layout(4096,1024)
    assert [p['number'] for p in parts]==[1,2,3,4]
    assert [p['label'] for p in parts]==['QUIRKBENCH-ESP','QUIRKBENCH-RECOVERY','QUIRKBENCH-STATE','QUIRKBENCH-EXPERIMENTS']
    assert all(a['end']<b['start'] for a,b in zip(parts,parts[1:]))
    assert parts[2]['end']-parts[2]['start']+1==32*1024*1024//512


def test_image_assembly_requires_provenance_before_any_external_command(tmp_path,monkeypatch):
    import quirkbench.image as module
    inputs=_inputs(tmp_path)
    monkeypatch.setattr(module,'_tool',lambda name:name)
    monkeypatch.setattr(module,'_run',lambda *args:pytest.fail('must refuse missing provenance before external writes'))
    with pytest.raises(ImageError,match='provenance'):
        module.create_image(inputs)


def test_image_rejects_changed_staged_runtime_before_writing(tmp_path, monkeypatch):
    import quirkbench.image as module
    from quirkbench.boot import install_recovery_runtime_base

    inputs = _inputs(tmp_path)
    install_recovery_runtime_base(inputs.rootfs_dir)
    (inputs.rootfs_dir/'usr/lib/quirkbench/quirkbench/runtime.py').write_text('changed')
    monkeypatch.setattr(module, '_verify_provenance', lambda *args: None)
    monkeypatch.setattr(module, '_tool', lambda name: name)
    monkeypatch.setattr(module, '_run', lambda *args: pytest.fail('must not write image'))
    with pytest.raises(ImageError, match='staged recovery runtime differs'):
        module.create_image(inputs)
    assert not inputs.output.exists()


def test_smoke_image_binds_expected_kernel_and_userspace_to_deployment(tmp_path):
    from dataclasses import asdict, replace
    from types import SimpleNamespace
    import hashlib
    import json
    from test_boot import prepared, CONFIG
    from quirkbench.deployment import DeploymentManifest
    from quirkbench.image import _prepare_data
    value=prepared(tmp_path)
    manifest=DeploymentManifest('ostree',value.revision,'lab',{'kernel_release':'6.12-test'},'usb-excluded-controllers-v1')
    value=replace(value,manifest_digest=manifest.sha256)
    folder=tmp_path/'quirkbench/attempts'/value.attempt_id;folder.mkdir(parents=True)
    (folder/'intent.json').write_text(json.dumps(manifest.to_dict()))
    record=asdict(value);record['boot_entry']=str(value.boot_entry.relative_to(tmp_path))
    (tmp_path/'quirkbench/prepared.json').write_text(json.dumps(record))
    deployed=tmp_path/'ostree/deploy/attempt/deploy'/('b'*64+'.0')
    modules=deployed/'usr/lib/modules/6.12-test';modules.mkdir(parents=True)
    (modules/'modules.dep').touch()
    (modules/'config').write_text('CONFIG_MAGIC_SYSRQ=y\n')
    health=deployed/'usr/bin/quirkbench-health';health.parent.mkdir(parents=True);health.write_bytes(b'fixture')
    result=_prepare_data(SimpleNamespace(smoke=True),tmp_path,CONFIG.to_dict())
    assert result[:4]==(value.deployment_id,value.revision,'6.12-test',hashlib.sha256(b'fixture').hexdigest())
    panic=(tmp_path/'quirkbench/boot'/(result[4]+'.cfg')).read_text()
    assert 'quirkbench.smoke=1 quirkbench.fault=panic' in panic
    failure=(tmp_path/'quirkbench/boot'/(result[5]+'.cfg')).read_text()
    assert 'if initrd $data/quirkbench/qualification-missing-initrd; then' in failure
    assert 'if linux $data/boot/ostree/fedora-test/vmlinuz ' in failure
    record['manifest_digest']='e'*64
    (tmp_path/'quirkbench/prepared.json').write_text(json.dumps(record))
    with pytest.raises(ImageError,match='manifest'):
        _prepare_data(SimpleNamespace(smoke=True),tmp_path,CONFIG.to_dict())


def test_image_staging_preserves_hardlinks_and_logical_ostree_metadata(tmp_path):
    import os
    from quirkbench.image import _copy_tree
    source=tmp_path/'source';source.mkdir()
    first=source/'object';first.write_bytes(b'logical metadata fixture')
    os.setxattr(first,'user.ostreemeta',b'logical-xattrs')
    os.link(first,source/'checkout')
    first.chmod(0o4755)
    destination=tmp_path/'copied'
    _copy_tree(source,destination)
    copied=destination/'object'
    assert copied.stat().st_ino==(destination/'checkout').stat().st_ino
    assert copied.stat().st_uid==first.stat().st_uid
    assert copied.stat().st_gid==first.stat().st_gid
    assert copied.stat().st_mode==first.stat().st_mode
    assert os.getxattr(copied,'user.ostreemeta')==b'logical-xattrs'


def test_image_tool_silent_timeout_reaps_process(tmp_path):
    import os,sys,time
    from quirkbench.image import _run
    pidfile=tmp_path/'pid'
    script="import os,time; from pathlib import Path; Path(%r).write_text(str(os.getpid())); time.sleep(30)" % str(pidfile)
    start=time.monotonic()
    with pytest.raises(TimeoutError,match='deadline'):
        _run(sys.executable,'-c',script,timeout_s=.15)
    assert time.monotonic()-start<4
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()),0)


def test_image_lock_wait_is_visible_and_bounded(tmp_path,capsys):
    import fcntl,time
    from quirkbench.image import image_lock
    path=tmp_path/'image.lock'
    with path.open('a') as first:
        fcntl.flock(first,fcntl.LOCK_EX)
        start=time.monotonic()
        with pytest.raises(ImageError,match='lock deadline'):
            with image_lock(path,timeout_s=.05):
                pytest.fail('concurrent image writer acquired held lock')
        assert time.monotonic()-start<1
    assert '"status": "waiting"' in capsys.readouterr().err
    with image_lock(path,timeout_s=.05):
        pass


def test_partition_copy_reports_measured_bytes(tmp_path,capsys):
    from quirkbench.image import _copy_slice
    source=tmp_path/'source';source.write_bytes(b'payload')
    target=tmp_path/'target';target.write_bytes(b'0'*20)
    _copy_slice(source,target,3)
    assert target.read_bytes()==b'000payload0000000000'
    assert '"copied_bytes": 7' in capsys.readouterr().err


def test_interrupted_publication_rejects_changed_runtime_builder(tmp_path,monkeypatch):
    import json
    import quirkbench.image as module
    from quirkbench.build import sha256_file
    inputs=_inputs(tmp_path)
    inputs.output.write_bytes(b'completed image awaiting manifest')
    monkeypatch.setattr(module,'_builder_identity',lambda:'a'*64)
    record={'input_identity':module._input_identity(inputs),'image_sha256':sha256_file(inputs.output),'builder_identity':'a'*64}
    Path(str(inputs.output)+'.pending.json').write_text(json.dumps(record))
    monkeypatch.setattr(module,'_builder_identity',lambda:'b'*64)
    with pytest.raises(ImageError,match='retry inputs'):
        module.create_image(inputs)
    assert not Path(str(inputs.output)+'.json').exists()
    assert inputs.output.read_bytes()==b'completed image awaiting manifest'


def test_source_change_during_image_staging_refuses_all_publication(tmp_path,monkeypatch):
    import shutil
    import quirkbench.image as module
    inputs=_inputs(tmp_path)
    changed=[False]
    def changing_copy(source,destination):
        shutil.copytree(source,destination)
        changed[0]=True
    def fake_command(*argv,**kwargs):
        if argv[0]=='grub-mkimage':
            Path(argv[argv.index('--output')+1]).write_bytes(b'fixture loader')
        return ''
    monkeypatch.setattr(module,'_verify_provenance',lambda *a:None)
    monkeypatch.setattr(module,'_tool',lambda name:name)
    monkeypatch.setattr(module,'_run',fake_command)
    monkeypatch.setattr(module,'_copy_tree',changing_copy)
    monkeypatch.setattr(module,'_copy_slice',lambda *a:None)
    monkeypatch.setattr(module,'sha256_file',lambda _: 'c'*64)
    monkeypatch.setattr(module,'_input_identity',lambda _: 'd'*64)
    monkeypatch.setattr(module,'_builder_identity',lambda:('b' if changed[0] else 'a')*64)
    monkeypatch.setattr(module.shutil,'disk_usage',lambda _:shutil._ntuple_diskusage(100*1024**3,0,100*1024**3))
    with pytest.raises(ImageError,match='changed during assembly'):module.create_image(inputs)
    assert not inputs.output.exists()
    assert not Path(str(inputs.output)+'.json').exists()
    assert not Path(str(inputs.output)+'.pending.json').exists()


@pytest.mark.parametrize('missing', ['usr/sbin/NetworkManager', 'usr/bin/nmtui'])
def test_old_rootfs_cannot_publish_an_image_without_new_network_stack(tmp_path, missing):
    inputs=_inputs(tmp_path)
    (inputs.rootfs_dir/missing).unlink()
    with pytest.raises(ImageError,match='networking prerequisite missing'):
        inputs.validate()

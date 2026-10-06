"""Installation identity, hostile archives and stopped-owner activation failure paths."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import zipfile

import pytest

from quirkbench.controller import Controller
from quirkbench.controller_archive import build_controller_archive
from quirkbench.controller_install import activate, install, rollback, verify_installation, installation_report
from quirkbench.contracts import Conflict, canonical
from quirkbench.store import atomic_write


def make_archive(directory, payload=b'fixture'):
    """Build the small software archive at an explicitly supplied destination."""
    wheel=directory/'input.whl'
    names=('cli.py','job_worker.py','job_operations.py','job_coordinator.py','job_cache.py',
           'controller_service.py','run-bounded-podman.sh',
           'recovery_worker.py','assets/quirkbench-recovery.service',
            'schemas/experiment.v1.schema.json','examples/experiment.json','guide/agent-guide.md',
            'guide/controller-installation.md','guide/recovery-acquisition.md','guide/build-and-boot.md')
    with zipfile.ZipFile(wheel,'w') as out:
        for name in names:
            raw=payload
            out.writestr('quirkbench/'+name,raw)
        out.writestr('quirkbench-0.1.0.dist-info/METADATA','Name: quirkbench\nVersion: 0.1.0\n')
    output=directory/'arbitrary-name.tar.gz'
    build_controller_archive(wheel,output)
    return output


@pytest.fixture(autouse=True)
def local_configuration(tmp_path,monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'config'))


@pytest.fixture
def archive(tmp_path):
    return make_archive(tmp_path)


def test_install_identity_repeat_and_corrupt_existing_refusal(tmp_path,archive):
    data=tmp_path/'another-user/data'
    first=install(archive,data_home=data)
    assert first==install(archive,data_home=data)
    runtime=Path(first['runtime_root'])
    assert runtime.name=='0.1.0-'+hashlib.sha256(archive.read_bytes()).hexdigest()
    (runtime/'lib/quirkbench/cli.py').write_bytes(b'changed')
    with pytest.raises(ValueError,match='bytes differ'): install(archive,data_home=data)
    assert (runtime/'lib/quirkbench/cli.py').read_bytes()==b'changed'


def test_archive_ancestor_alias_retains_canonical_installation_and_activation(tmp_path,archive):
    record,root,c,conf,binary,old=configured(tmp_path,archive)
    alias=tmp_path/'home-alias';alias.symlink_to(tmp_path,target_is_directory=True)
    through_alias=install(alias/archive.name,data_home=alias/'data')
    assert through_alias==record
    assert Path(record['runtime_root']).resolve()==Path(record['runtime_root'])
    assert activate(through_alias,root,config_home=conf,bin_home=binary,
        runner=Services(root,record),ready=ready)['activated']


@pytest.mark.parametrize('change',['bytes','leaf','ancestor','ancestor-loop','missing'])
def test_archive_alias_or_payload_change_during_capture_is_rejected(tmp_path,archive,monkeypatch,change):
    import quirkbench.controller_install as installer
    alias=tmp_path/'home-alias';alias.symlink_to(tmp_path,target_is_directory=True)
    original=installer.read_file
    def replace_after_read(root,name,**kwargs):
        raw=original(root,name,**kwargs)
        if change=='bytes':archive.write_bytes(raw+b'changed')
        elif change=='leaf':
            moved=archive.with_suffix('.original');archive.rename(moved);archive.symlink_to(moved)
        elif change=='ancestor':
            other=tmp_path/'other';other.mkdir();alias.unlink();alias.symlink_to(other,target_is_directory=True)
        elif change=='ancestor-loop':alias.unlink();alias.symlink_to(alias,target_is_directory=True)
        else:archive.unlink()
        return raw
    monkeypatch.setattr(installer,'read_file',replace_after_read)
    with pytest.raises(ValueError,match='archive input changed during capture'):
        install(alias/archive.name,data_home=tmp_path/'data')
    assert not (tmp_path/'data').exists()


def test_archive_looping_ancestor_is_a_typed_input_error(tmp_path):
    alias=tmp_path/'loop';alias.symlink_to(alias,target_is_directory=True)
    with pytest.raises(ValueError,match='controller archive must be an accessible'):
        install(alias/'controller.tar.gz',data_home=tmp_path/'data')
    assert not (tmp_path/'data').exists()


@pytest.mark.parametrize('bad_name', ['../escape','/absolute','quirkbench-controller-0.1.0/../escape'])
def test_archive_traversal_is_rejected_before_install(tmp_path,bad_name):
    output=tmp_path/'bad.tar.gz'
    with tarfile.open(output,'w:gz') as out:
        info=tarfile.TarInfo(bad_name);info.size=1;out.addfile(info,io.BytesIO(b'x'))
    with pytest.raises(ValueError,match='unsafe'): install(output,data_home=tmp_path/'data')
    assert not (tmp_path/'data').exists()


def test_archive_checksum_mismatch_and_symlink_are_rejected(tmp_path,archive):
    raw=[]
    with tarfile.open(archive) as source:
        for member in source:
            data=source.extractfile(member).read()
            if member.name.endswith('/cli.py'): data=b'corrupt';member.size=len(data)
            raw.append((member,data))
    bad=tmp_path/'changed.tar.gz'
    with tarfile.open(bad,'w:gz') as out:
        for member,data in raw:out.addfile(member,io.BytesIO(data))
    with pytest.raises(ValueError,match='checksum'): install(bad,data_home=tmp_path/'data')
    link=tmp_path/'linked.tar.gz';link.symlink_to(archive)
    with pytest.raises(ValueError,match='regular'): install(link,data_home=tmp_path/'data')


class Services:
    def __init__(self,root,runtime,*,inject=None):
        self.root=root;self.runtime=runtime;self.calls=[];self.inject=inject
    def __call__(self,argv,**kwargs):
        self.calls.append(argv[2:])
        if argv[2]=='stop' and self.inject:self.inject();self.inject=None
        output='ActiveState=inactive\nMainPID=0\n' if argv[2]=='show' else ''
        return subprocess.CompletedProcess(argv,0,output,'')


def configured(tmp_path,archive):
    record=install(archive,data_home=tmp_path/'data')
    old_dir=tmp_path/'old-archive';old_dir.mkdir()
    old_record=install(make_archive(old_dir,payload=b'old'),data_home=tmp_path/'data')
    old=Path(old_record['runtime_root'])/'bin'
    root=tmp_path/'state';controller=Controller(root,reserve_bytes=0)
    tls=root/'private/controller-tls'/('setup-'+__import__('hashlib').sha256(b'fixture').hexdigest()[:32])
    tls.mkdir(parents=True,mode=0o700)
    for name in ('controller.crt','controller.key'):atomic_write(tls/name,b'private')
    config={'software':{key:old_record[key] for key in ('version','archive_sha256')},
            'tls_identity':{'kind':'setup','request_id':'fixture'},'credential_registry':True,'recovery_enabled':True}
    atomic_write(root/'private/controller-service.json',canonical(config))
    conf=tmp_path/'config';unit=conf/'systemd/user/quirkbench-controller.service';unit.parent.mkdir(parents=True)
    atomic_write(unit,('[Service]\nExecStart='+str(old/'quirkbench-controller-service')+' --state '+str(root)+'\nKillMode=control-group\n').encode())
    from quirkbench.controller_install import select_runtime
    select_runtime(Path(old_record['runtime_root']),config_home=conf)
    binary=tmp_path/'bin';binary.mkdir();(binary/'quirkbench').symlink_to(old/'quirkbench')
    return record,root,controller,conf,binary,config


def ready(_):return {'background_work_ready':True,'service_installation':'verified'}


def test_activation_aligns_all_paths_and_preserves_private_settings(tmp_path,archive):
    record,root,c,conf,binary,old=configured(tmp_path,archive)
    services=Services(root,record)
    answer=activate(record,root,config_home=conf,bin_home=binary,runner=services,ready=ready)
    assert answer['activated'] and not answer['background_work_ready']
    assert answer['controller_start_required'] and services.calls==[]
    config=json.loads((root/'private/controller-service.json').read_bytes())
    runtime=Path(record['runtime_root'])
    assert config['software']=={key:record[key] for key in ('version','archive_sha256')}
    assert not {'runtime','job_worker','recovery_worker','cert','key'} & set(config)
    for key in ('tls_identity','credential_registry','recovery_enabled'):assert config[key]==old[key]
    assert (binary/'quirkbench').resolve()==runtime/'bin/quirkbench'
    assert 'archive_sha256' in json.loads((conf/'quirkbench/installation.json').read_bytes())
    assert not (root/'private/installation-activation.json').exists()
    assert (tmp_path/'data/quirkbench/controller'/('0.1.0-'+old['software']['archive_sha256'])/'lib/quirkbench/cli.py').read_bytes()==b'old'


def test_configured_builder_survives_activation_restart_and_retention(tmp_path,archive):
    from quirkbench.retention import register
    from quirkbench.retention_settings import set_setting
    from quirkbench.maintenance import prune
    from test_recovery_podman import builder_archive,IMAGE
    record,root,c,conf,binary,config=configured(tmp_path,archive)
    builder=c.store.put(builder_archive()).sha256
    register(root,'input',[builder],owner='input:'+builder)
    config.update(builder_image_digest=IMAGE,builder_config_digest=IMAGE,builder_archive_sha256=builder)
    atomic_write(root/'private/controller-service.json',canonical(config))
    set_setting(root,'input_generations',1)
    for raw in (b'newer input',b'newest input'):
        value=c.store.put(raw).sha256;register(root,'input',[value],owner='input:'+value)
    # Exercise retirement and orphan expiry, without waiting for policy time.
    os.utime(c.store.path(builder),(1,1))
    activate(record,root,config_home=conf,bin_home=binary,runner=Services(root,record),ready=ready)
    assert json.loads((root/'private/controller-service.json').read_bytes())['builder_archive_sha256']==builder
    for _ in range(2):
        restarted=Controller(root,reserve_bytes=0)
        with restarted.lifecycle() as owner:
            assert owner.housekeep() is not None
        assert c.store.path(builder).read_bytes()==builder_archive()
    config=json.loads((root/'private/controller-service.json').read_bytes())
    for field in ('builder_image_digest','builder_config_digest','builder_archive_sha256'):config.pop(field)
    atomic_write(root/'private/controller-service.json',canonical(config))
    assert 'artifacts/objects/'+builder in prune(root)['removed']
    assert not c.store.path(builder).exists()


def test_missing_configured_builder_is_reported_by_housekeeping(tmp_path,archive):
    from quirkbench.maintenance import prune
    record,root,c,conf,binary,config=configured(tmp_path,archive)
    config['builder_archive_sha256']='a'*64
    atomic_write(root/'private/controller-service.json',canonical(config))
    report=prune(root)
    assert any('configured builder archive unavailable: '+'a'*64 in reason for reason in report['blocked'])
    assert not c.store.path('a'*64).exists()


def queue_operation(c):
    with c.transaction() as db:
        db.execute("INSERT INTO operations(id,request_id,request_digest,input_digest,kind,state,created,updated) VALUES('busy','busy',?,?,'build','QUEUED',0,0)",('a'*64,'b'*64))


def test_busy_activation_refuses_before_stopping(tmp_path,archive):
    record,root,c,conf,binary,old=configured(tmp_path,archive);queue_operation(c)
    services=Services(root,record)
    with pytest.raises(Conflict,match='outstanding'):
        activate(record,root,config_home=conf,bin_home=binary,runner=services,ready=ready)
    assert services.calls==[]
    assert json.loads((root/'private/controller-service.json').read_bytes())==old


def test_work_arriving_before_activation_lock_prevents_path_switch(tmp_path,archive):
    record,root,c,conf,binary,old=configured(tmp_path,archive)
    services=Services(root,record)
    def arrival(step):
        if step=='intent_recorded':queue_operation(c)
    with pytest.raises(Conflict,match='outstanding'):
        activate(record,root,config_home=conf,bin_home=binary,runner=services,ready=ready,fault_hook=arrival)
    assert json.loads((root/'private/controller-service.json').read_bytes())==old


def test_failed_publication_restores_old_configuration_and_launcher(tmp_path,archive):
    record,root,c,conf,binary,old=configured(tmp_path,archive)
    def fail(step):
        if step=='configuration_published':raise Conflict('publication interrupted')
    with pytest.raises(Conflict,match='publication interrupted'):
        activate(record,root,config_home=conf,bin_home=binary,fault_hook=fail)
    assert json.loads((root/'private/controller-service.json').read_bytes())==old
    assert (binary/'quirkbench').resolve()==tmp_path/'data/quirkbench/controller'/('0.1.0-'+old['software']['archive_sha256'])/'bin/quirkbench'
    assert json.loads((conf/'quirkbench/last-activation.json').read_bytes())['phase']=='ROLLED_BACK'


def test_interrupted_activation_requires_explicit_rollback(tmp_path,archive):
    record,root,c,conf,binary,old=configured(tmp_path,archive)
    def crash(_):raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        activate(record,root,config_home=conf,bin_home=binary,runner=Services(root,record),fault_hook=crash)
    assert (root/'private/installation-activation.json').exists()
    with pytest.raises(Conflict,match='unfinished'):
        activate(record,root,config_home=conf,bin_home=binary,runner=Services(root,record),ready=ready)
    assert rollback(root,config_home=conf,runner=Services(root,record),ready=ready)['rolled_back']
    assert json.loads((root/'private/controller-service.json').read_bytes())==old


def test_failed_initial_activation_preserves_absent_launcher_directory(tmp_path,archive):
    record,root,c,conf,binary,old=configured(tmp_path,archive)
    (binary/'quirkbench').unlink();binary.rmdir()
    def fail(step):
        if step=='configuration_published':raise Conflict('publication failed')
    with pytest.raises(Conflict,match='publication failed'):
        activate(record,root,config_home=conf,bin_home=binary,fault_hook=fail)
    assert not binary.exists()
    assert json.loads((root/'private/controller-service.json').read_bytes())==old


def test_selected_installation_directory_accepts_ordinary_alias(tmp_path,archive):
    checkout=tmp_path/'checkout';checkout.mkdir();(checkout/'.git').mkdir()
    data=tmp_path/'data';data.mkdir();(data/'quirkbench').symlink_to(checkout,target_is_directory=True)
    record=install(archive,data_home=data)
    assert Path(record['runtime_root']).is_relative_to(checkout)
    assert (checkout/'.git').is_dir()


def test_cli_activation_does_not_take_outer_shared_lock(tmp_path,archive,monkeypatch,capsys):
    from quirkbench import cli,controller_install
    record,root,c,conf,binary,old=configured(tmp_path,archive)
    monkeypatch.setattr(controller_install,'install',lambda archive:record)
    original=controller_install.activate
    monkeypatch.setattr(controller_install,'activate',lambda record,root:original(record,root,config_home=conf,
        bin_home=binary,runner=Services(root,record),ready=ready))
    assert cli.main(['--state', str(root), 'dev', 'install', str(archive), '--activate', '--json'])==0
    assert json.loads(capsys.readouterr().out)['data']['activated']


def test_rollback_refuses_new_work_after_interruption_before_stopping(tmp_path,archive):
    record,root,c,conf,binary,old=configured(tmp_path,archive)
    def crash(_):raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        activate(record,root,config_home=conf,bin_home=binary,runner=Services(root,record),fault_hook=crash)
    queue_operation(c)
    services=Services(root,record)
    with pytest.raises(Conflict,match='outstanding'):
        rollback(root,config_home=conf,runner=services,ready=ready)
    assert services.calls==[]
    assert (root/'private/installation-activation.json').exists()

"""Versioned joined input contracts are mechanical before their adapters."""
import json
from pathlib import Path
import pytest
import jsonschema
from quirkbench import investigation_pipeline as pipeline
from quirkbench.contracts import ContractError

ROOT=Path(__file__).resolve().parents[1]
RECORDS=('fixed-build-recipe','investigation-build-input','investigation-compose-input','investigation-artifact-link')

@pytest.mark.parametrize('kind',RECORDS)
def test_joined_schema_examples_and_strict_runtime_match(kind):
    value=json.loads((ROOT/'examples'/f'{kind}.json').read_bytes())
    schema=json.loads((ROOT/'schemas'/f'{kind}.v1.schema.json').read_bytes())
    jsonschema.Draft202012Validator(schema).validate(value)
    assert pipeline.validate(value)==value
    with pytest.raises(ContractError):pipeline.validate({**value,'unknown':None})
    with pytest.raises(ContractError):pipeline.validate({**value,'schema_version':True})

from test_candidate_rootfs_operation import setup as candidate_setup,assembly_setup
from test_recovery_inventory import observations,collect,report


@pytest.fixture
def joined(candidate_setup,observations,monkeypatch,tmp_path):
    from quirkbench import baseline_catalog,baseline_inputs,investigations,controller_service,builder_setup,distribution_prepare_operation
    from quirkbench.recipe_registry import installed_registry
    from quirkbench.recovery_rootfs import _rpm_row
    from quirkbench.contracts import canonical,digest
    from quirkbench.job_coordinator import JobCoordinator
    from test_builder_setup import Workers
    from test_source_operation import worker
    from test_distribution_source_worker import injected
    from test_candidate_rootfs_worker import execution
    from quirkbench import recovery_worker,source_workspace,candidate_rootfs_operation
    c,entry,value,builder,snapshot=candidate_setup
    entry['build_recipe']={'recipe_id':'fedora-kernel-rpm-v1','digest':c.store.put(canonical(pipeline.FIXED_RECIPE)).sha256}
    registry=installed_registry(Path(__file__).resolve().parents[1]/'src/quirkbench/recipes',candidate=True)
    entry['target_recipes']=[{'recipe_id':'system-observation','digest':registry.records['system-observation'][1]}]
    raw=registry.records['system-observation'][2].read_bytes();c.store.put(raw)
    existing={p['name'] for p in snapshot['packages']}
    for name in ('rpm','nss-altfiles','systemd'):
        if name not in existing:
            snapshot['packages'].append({'name':name,'nevra':name+'-0:1-1.fc'+entry['fedora_release']+'.x86_64','sha256':c.store.put(('fake '+name+' RPM').encode()).sha256})
    snapshot['packages'].sort(key=lambda p:(p['name'],p['nevra']))
    entry['packages']=[{'name':p['name'],'nevra':p['nevra']} for p in snapshot['packages']]
    entry['rpm_snapshot_sha256']=c.store.put(canonical(snapshot)).sha256
    entry['target_rpm_lock_sha256']=c.store.put(('\n'.join(sorted(_rpm_row(p['name'],p['nevra']) for p in snapshot['packages']))+'\n').encode()).sha256
    from quirkbench.build import REQUIRED_CONFIG
    entry['repo_config_sha256']=c.store.put(b'[fedora]\nbaseurl=https://example.test/fedora\ngpgcheck=1\nsslverify=1\n').sha256
    entry['kernel_config_sha256']=c.store.put(('\n'.join(k+'='+v for k,v in sorted(REQUIRED_CONFIG.items()))+'\n').encode()).sha256
    from test_build_pipeline import _inputs
    build_fixture=tmp_path/'build-foundation';build_fixture.mkdir()
    entry['userspace_source_sha256']=c.store.put(_inputs(build_fixture).userspace_source_tar.read_bytes()).sha256
    value,_=baseline_inputs.input_record(c.store,entry)
    catalog={'schema_version':1,'catalog_revision':'joined-fixture','entries':[entry]}
    monkeypatch.setattr(baseline_catalog,'installed_catalog',lambda:catalog)
    c.register(report(collect(observations),mode='recovery'))
    investigations.start(c,'investigation','target-1','start',workspace='kernel')
    (c.root/'private/signing').mkdir(parents=True,exist_ok=True)
    config={**builder,'key':str(c.root/'private/controller.key'),'reserve_gib':0,
        'composition_signing':{'home':str(c.root/'private/signing'),'fingerprint':'A'*40},
        'repositories':{'lab':str(c.root/'repositories/lab')}}
    monkeypatch.setattr(controller_service,'configuration',lambda _:config)
    monkeypatch.setattr(builder_setup,'reserve_bytes',lambda _:0)
    with c.lifecycle() as owner:
        services=Workers();coordinator=JobCoordinator(owner,services)
        monkeypatch.setattr(recovery_worker,'execute_rootfs',injected)
        prepared=distribution_prepare_operation.submit(c,'investigation','kernel',entry,builder,'prepare',ready=lambda _:None)
        c.resume('investigation');claim=coordinator.tick()
        assert worker(c,claim,monkeypatch)==0,(Path(claim['stage_dir'])/'diagnostics/stage-result.json').read_text()
        services.done=True;assert coordinator.tick()['state']=='SUCCEEDED'
        captured=source_workspace.handoff(c,'kernel','capture',quiesced=True,ready=lambda _:None)
        services.done=False;claim=coordinator.tick();assert worker(c,claim,monkeypatch)==0
        services.done=True;assert coordinator.tick()['state']=='SUCCEEDED'
        candidate=candidate_rootfs_operation.submit(c,value,'candidate',builder=builder,ready=lambda _:None)
        services.done=False;claim=coordinator.tick()
        monkeypatch.setattr(recovery_worker,'execute_rootfs',execution((c.root,Path(claim['stage_dir']),c.store,entry,value,builder,snapshot),[]))
        assert worker(c,claim,monkeypatch)==0,(Path(claim['stage_dir'])/'diagnostics/stage-result.json').read_text()
        services.done=True;assert coordinator.tick()['state']=='SUCCEEDED'
    return c,entry,builder,snapshot,captured['operation_id'],candidate['operation_id'],config


def test_join_admission_replay_uses_frozen_source_and_no_native_inspection(joined,monkeypatch):
    c,entry,builder,snapshot,source,candidate,config=joined
    from quirkbench import baseline_inputs,source_workspace
    def forbidden(*a,**kw):raise AssertionError('admission hashed large bytes or inspected native runtime')
    monkeypatch.setattr(baseline_inputs,'verify_object',forbidden)
    response=pipeline.submit(c,'investigation','build','build-command',source=source,candidate=candidate,ready=lambda _:None)
    row=c.operation_status(response['operation_id'])['data']
    assert row['campaign']=='investigation' and row['device']=='target-1'
    assert row['state']=='QUEUED'
    with c.transaction() as db:
        saved,workspace=source_workspace.record(c,'kernel',db)
    assert saved['writer_state']=='QUIESCED'
    source_workspace.release(c,'kernel')
    (c.root/'workspaces/kernel/init/main.c').write_text('later live edit is not selected')
    assert pipeline.submit(c,'investigation','build','build-command',source=source,candidate=candidate,ready=forbidden)==response
    with pytest.raises(ContractError):pipeline.submit(c,'investigation','build','other-build',source=source,candidate='missing',ready=lambda _:None)


def configure_build(monkeypatch):
    """Replace native tools, retaining actual worker + BuildPipeline business logic."""
    import subprocess
    from types import SimpleNamespace
    from quirkbench.build_pipeline import BuildPipeline,ResourceLimits
    from quirkbench.build_pipeline import BoundedRunner
    from quirkbench import recovery_worker,job_worker
    from test_build_pipeline import FakeRunner
    class IncrementalRunner(FakeRunner):
        def run(self,command,**kw):
            if kw['phase']!='compile-kernel':return super().run(command,**kw)
            self.phases.append(kw['phase']);kw['log'].write_text(kw['phase'])
            objects=Path(next(arg[2:] for arg in command.argv if arg.startswith('O=')))
            for relative in ('arch/x86/boot/bzImage','vmlinux','Module.symvers','System.map'):
                output=objects/relative;output.parent.mkdir(parents=True,exist_ok=True);output.write_text(relative)
            kw['on_activity']('compile-kernel',1,1)
    runner=IncrementalRunner()
    monkeypatch.setattr(BuildPipeline,'_verify_environment',lambda *a:None)
    monkeypatch.setattr(BuildPipeline,'_verify_symbols',lambda *a:None)
    monkeypatch.setattr(BoundedRunner,'run',lambda self,*a,**kw:runner.run(*a,**kw))
    monkeypatch.setattr(ResourceLimits,'from_cgroup',classmethod(lambda cls:ResourceLimits(1,4*1024**3,1)))
    monkeypatch.setattr('quirkbench.build.recommended_jobs',lambda:1)
    monkeypatch.setattr('quirkbench.build_pipeline.shutil.disk_usage',lambda p:SimpleNamespace(free=100*1024**3))
    native=subprocess.check_output
    monkeypatch.setattr(subprocess,'check_output',lambda argv,**kw:'6.12.60-quirkbench\n' if argv[0]=='make' else native(argv,**kw))
    def execute(argv,log,**kw):
        assert '--network=none' in argv
        kw['verify']()
        stage=Path(argv[argv.index('--stage-dir')+1]);kind=argv[argv.index('--inner')+1]
        # The joined controller fixture explicitly configures a zero reserve.
        # Match container_worker, which forwards its retained execution reserve.
        result=job_worker.inner(kind,stage,Path(argv[argv.index('--cache')+1]),reserve_bytes=0)
        kw['verify']();return {'exit_code':result}
    monkeypatch.setattr(recovery_worker,'execute_rootfs',execute)
    return runner


@pytest.fixture
def bounded_build(monkeypatch):
    return configure_build(monkeypatch)


def complete_job(c,owner,monkeypatch, *,kind='build',after_worker=None,allow_failure=False):
    stages=('job_inputs','kernel_build' if kind=='build' else 'os_compose')
    from quirkbench.job_coordinator import JobCoordinator
    from test_builder_setup import Workers
    from test_source_operation import worker
    services=Workers();coordinator=JobCoordinator(owner,services)
    c.resume('investigation')
    for expected in stages:
        services.done=False;claim=coordinator.tick()
        assert claim['stage']==expected,claim
        assert worker(c,claim,monkeypatch)==0,(Path(claim['stage_dir'])/'diagnostics/stage-result.json').read_text()
        if after_worker:after_worker(claim,coordinator)
        services.done=True;result=coordinator.tick()
        if allow_failure and result['state']=='FAILED':return result
        assert result['state'] in ('QUEUED','SUCCEEDED'),(result,pipeline.document(c.store,result['error_digest']) if result.get('error_digest') else None)
    return result


def test_joined_build_runs_existing_workers_and_retains_attribution(joined,bounded_build,monkeypatch):
    c,entry,builder,snapshot,source,candidate,config=joined
    with c.lifecycle() as owner:
        response=pipeline.submit(c,'investigation','build','build-command',source=source,candidate=candidate,ready=lambda _:None)
        result=complete_job(c,owner,monkeypatch)
    assert result['id']==response['operation_id'] and result['state']=='SUCCEEDED'
    row=c.operation_status(result['id'])['data']
    links=[]
    for identity in row['references']['output']:
        try:value=json.loads(c.store.get(identity))
        except (UnicodeError,ValueError):continue
        if isinstance(value,dict) and value.get('record_type')=='investigation-artifact-link':links.append(value)
    assert len(links)==1 and links[0]['source_capture_operation_id']==source
    assert links[0]['candidate_operation_id']==candidate and links[0]['baseline_sha256']
    assert bounded_build.phases
    with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==0


def configure_compose(joined,monkeypatch):
    """Native RPM/OSTree command injection, without replacing FedoraComposer."""
    import subprocess,shutil
    from types import SimpleNamespace
    from quirkbench.compose import ComposeRunner
    from quirkbench import compose
    from quirkbench.recovery_rootfs import _rpm_row
    from quirkbench.contracts import canonical
    from test_compose import fake_runner
    c,entry,builder,snapshot,source,candidate,config=joined
    calls=[];fake_runner(monkeypatch,calls)
    monkeypatch.setattr(compose,'DISK_RESERVE',0)
    from quirkbench.build_pipeline import ResourceLimits
    monkeypatch.setattr(ResourceLimits,'from_cgroup',classmethod(lambda cls:ResourceLimits(1,4*1024**3,1)))
    monkeypatch.setattr(compose,'builder_base_digest',lambda:builder['builder_image_digest'])
    adapter=ComposeRunner.run;native_run=subprocess.run;native_output=subprocess.check_output
    evr='0:1-1.fc'+entry['fedora_release']
    generated=[{'name':name,'nevra':name+'-'+evr+'.x86_64'} for name in ('kernel-quirkbench','quirkbench-experiment-userspace')]
    packages=snapshot['packages']+generated
    rows=''.join(sorted(_rpm_row(p['name'],p['nevra'])+'\n' for p in packages))
    def checkout(parent,target):
        target.mkdir()
        for name in ('kernel-quirkbench','quirkbench-experiment-userspace'):
            shutil.copytree(parent/name/'SOURCES/payload',target,dirs_exist_ok=True,symlinks=True)
        module=target/'usr/lib/modules/6.12.60-quirkbench'
        shutil.copyfile(target/'usr/lib/quirkbench/initramfs/6.12.60-quirkbench.img',module/'initramfs.img')
    def run(self,argv,*,phase,cwd,env,timeout=3600):
        if phase=='inspect-pinned-baseline':return ''.join(sorted(_rpm_row(p['name'],p['nevra'])+'\n' for p in snapshot['packages']))
        if phase.startswith('inspect-generated-'):
            return _rpm_row(Path(argv[-1]).stem,Path(argv[-1]).stem+'-'+evr+'.x86_64')+'\n'
        if phase=='inspect-composed-baseline':return rows
        if phase=='download-dependencies':
            for p in snapshot['packages']:shutil.copyfile(c.store.path(p['sha256']),cwd/'rpm-cache'/(p['sha256']+'.rpm'))
            locked=list(snapshot['packages'])
            for p in generated:
                path=cwd/'packages'/(p['name']+'.rpm');identity=compose.sha256_file(path)
                shutil.copyfile(path,cwd/'rpm-cache'/(identity+'.rpm'));locked.append({**p,'sha256':identity})
            (cwd/'dependency-lock.json').write_bytes(canonical({'packages':{p['name']:{'evra':p['nevra'][len(p['name'])+1:],'digest':'sha256:'+p['sha256']} for p in locked}}))
            return ''
        if phase=='init-compose-repo':
            repo=Path(argv[1].split('=',1)[1]);repo.mkdir();(repo/'config').write_text('[core]\nrepo_version=1\nmode=bare-user\n')
            return ''
        if phase=='checkout-pinned-baseline':checkout(cwd,Path(argv[-1]));return ''
        return adapter(self,argv,phase=phase,cwd=cwd,env=env,timeout=timeout)
    monkeypatch.setattr(ComposeRunner,'run',run)
    def owner_run(argv,**kw):
        if argv[0]!='ostree':return native_run(argv,**kw)
        calls.append(('owner',argv))
        repo=Path(argv[1].split('=',1)[1])
        if 'checkout' in argv:
            parent=repo.parent
            if repo.name=='repository':
                parent=next(p for p in (repo.parent.parent/'work').iterdir() if p.name.startswith('compose-'))
            checkout(parent,Path(argv[-1]))
        if 'init' in argv:repo.mkdir();(repo/'config').write_text('[core]\nrepo_version=1\nmode=archive\n')
        return SimpleNamespace(stdout='',returncode=0)
    def owner_output(argv,**kw):
        if argv[0]=='ostree':return 'a'*64+'\n'
        if argv[0]=='rpm':
            if '-qa' in argv:return rows
            name=Path(argv[-1]).stem;return _rpm_row(name,name+'-'+evr+'.x86_64')+'\n'
        return native_output(argv,**kw)
    monkeypatch.setattr(subprocess,'run',owner_run);monkeypatch.setattr(subprocess,'check_output',owner_output)
    return calls


@pytest.fixture
def bounded_compose(joined,bounded_build,monkeypatch):
    return configure_compose(joined,monkeypatch)


def test_source_candidate_build_compose_retained_deployment(joined,bounded_compose,monkeypatch):
    c,entry,builder,snapshot,source,candidate,config=joined
    with c.lifecycle() as owner:
        build=pipeline.submit(c,'investigation','build','joined-build',source=source,candidate=candidate,ready=lambda _:None)
        assert complete_job(c,owner,monkeypatch)['state']=='SUCCEEDED'
        composed=pipeline.submit(c,'investigation','compose','joined-compose',build=build['operation_id'],repository='lab',ready=lambda _:None)
        assert pipeline.submit(c,'investigation','compose','joined-compose',build=build['operation_id'],repository='lab',ready=lambda _:None)==composed
        result=complete_job(c,owner,monkeypatch,kind='compose')
        assert result['state']=='SUCCEEDED'
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM deployment_refs WHERE owner=?',(composed['operation_id'],)).fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==0
    assert any('gpg-sign' in argv for phase,argv in bounded_compose)
    value=pipeline.document(c.store,result['final_output_digest'])
    assert value['deployment']['provenance']['baseline_sha256']


def test_workspace_retirement_backup_and_expired_history_do_not_reconstruct(joined,tmp_path):
    from quirkbench.contracts import Conflict
    c,entry,builder,snapshot,source,candidate,config=joined
    with c.transaction() as db:
        prep=db.execute('SELECT o.* FROM source_preparations p JOIN operations o ON o.id=p.operation WHERE p.workspace_id=?',('kernel',)).fetchone()
        refs={r[0] for r in db.execute('SELECT digest FROM refs WHERE owner=?',('workspace:kernel',))}
        assert not db.execute('SELECT 1 FROM operation_refs WHERE operation=?',(prep['id'],)).fetchone()
        intent=pipeline.document(c.store,prep['input_digest']);original_input=intent['arguments']['preparation_sha256']
        assert {prep['input_digest'],original_input,prep['final_output_digest']}<=refs
    backup=tmp_path/'backup';c.backup(backup)
    assert (backup/'artifacts/objects'/prep['input_digest']).read_bytes()==c.store.path(prep['input_digest']).read_bytes()
    assert (backup/'artifacts/objects'/original_input).read_bytes()==c.store.path(original_input).read_bytes()
    with c.transaction() as db:db.execute('DELETE FROM refs WHERE owner=? AND digest=?',('workspace:kernel',prep['input_digest']))
    c.store.path(prep['input_digest']).unlink()
    with pytest.raises(Conflict,match='expired preparation metadata'):
        pipeline.submit(c,'investigation','build','expired-build',source=source,candidate=candidate,ready=lambda _:None)
    assert not c.store.path(prep['input_digest']).exists()


def test_installed_commands_derive_inputs_and_replay_without_private_manifests(joined,monkeypatch,capsys):
    from quirkbench import cli,controller_service,baseline_inputs
    from quirkbench.contracts import Conflict
    c,entry,builder,snapshot,source,candidate,config=joined
    monkeypatch.setattr(controller_service,'require_ready',lambda _:None)
    monkeypatch.setattr(baseline_inputs,'verify_object',lambda *a,**kw:pytest.fail('large admission hash'))
    argv=['investigation', 'build', 'kernel', 'investigation', '--capture', source, '--candidate', candidate, '--request-id', 'cli-build', '--json', '--reserve-gib', '0']
    assert cli._main(argv, state_root=str(c.root))==0;first=json.loads(capsys.readouterr().out)
    assert cli._main(argv, state_root=str(c.root))==0;assert json.loads(capsys.readouterr().out)==first
    assert 'join_input_sha256' in pipeline.document(c.store,c.operation_status(first['operation_id'])['data']['input_digest'])['arguments']
    with pytest.raises(Conflict):pipeline.submit(c,'investigation','build','candidate',source=source,candidate=candidate,ready=lambda _:None)
    parsed=cli.parser().parse_args(['investigation', 'build', 'system', 'investigation', '--build', first['operation_id'], '--repository', 'lab', '--request-id', 'compose'])
    assert parsed.repository=='lab' and not hasattr(parsed,'manifest')
    prepared=['investigation', 'build', 'prepare', 'investigation', '--request-id', 'cli-candidate', '--json', '--reserve-gib', '0']
    assert cli._main(prepared, state_root=str(c.root))==0;answer=json.loads(capsys.readouterr().out)
    assert c.operation_status(answer['operation_id'])['data']['kind']=='candidate_prepare'


def test_missing_builder_and_mismatched_candidate_identity_block_admission(joined):
    from quirkbench.contracts import Conflict,canonical
    c,entry,builder,snapshot,source,candidate,config=joined
    row=c.operation_status(candidate)['data'];value=pipeline.document(c.store,row['final_output_digest'])
    wrong=c.store.put(canonical({**value,'builder_config_digest':'sha256:'+'f'*64}))
    with c.transaction() as db:
        db.execute('UPDATE operations SET final_output_digest=? WHERE id=?',(wrong.sha256,candidate))
        db.execute('INSERT INTO refs VALUES(?,?)',(candidate,wrong.sha256))
        db.execute("INSERT INTO operation_refs VALUES(?,'output',?)",(candidate,wrong.sha256))
    with pytest.raises(Conflict,match='builder, baseline or actual base differs'):
        pipeline.submit(c,'investigation','build','wrong-candidate',source=source,candidate=candidate,ready=lambda _:None)
    with c.transaction() as db:db.execute('UPDATE operations SET final_output_digest=? WHERE id=?',(row['final_output_digest'],candidate))
    c.store.path(builder['builder_archive_sha256']).unlink()
    with pytest.raises(ContractError,match='bytes unavailable'):
        pipeline.submit(c,'investigation','build','missing-builder',source=source,candidate=candidate,ready=lambda _:None)


@pytest.mark.parametrize('kind',('build','compose'))
@pytest.mark.parametrize('target',('index','link','bytes'))
def test_terminal_store_callback_cannot_mutate_joined_outputs(joined,bounded_compose,monkeypatch,kind,target):
    c,entry,builder,snapshot,source,candidate,config=joined
    original=c.store.put;mutated=[]
    def malicious(raw):
        artifact=original(raw)
        try:value=json.loads(raw)
        except (ValueError,UnicodeError):return artifact
        if isinstance(value,dict) and 'public_artifacts' in value and not mutated:
            for identity in value['public_artifacts']:
                data=c.store.path(identity).read_bytes()
                try:output=json.loads(data)
                except (ValueError,UnicodeError):output=None
                match=(target=='bytes' and output is None or
                    target=='link' and isinstance(output,dict) and output.get('record_type')=='investigation-artifact-link' or
                    target=='index' and isinstance(output,dict) and ('deployment' in output or 'kernel' in output))
                if match:
                    path=c.store.path(identity);path.chmod(0o600);path.write_bytes(b'changed by terminal CAS callback');mutated.append(identity);break
        return artifact
    with c.lifecycle() as owner:
        build=pipeline.submit(c,'investigation','build','build-terminal',source=source,candidate=candidate,ready=lambda _:None)
        if kind=='compose':
            complete_job(c,owner,monkeypatch)
            pipeline.submit(c,'investigation','compose','compose-terminal',build=build['operation_id'],repository='lab',ready=lambda _:None)
        monkeypatch.setattr(c.store,'put',malicious)
        result=complete_job(c,owner,monkeypatch,kind=kind,allow_failure=True)
        assert result['state']=='FAILED' and mutated
        with c.transaction() as db:
            assert not db.execute("SELECT 1 FROM operation_refs WHERE operation=? AND role='output'",(result['id'],)).fetchone()
    assert pipeline.document(c.store,result['error_digest'])['code']=='JOB_STAGE_FAILED'


@pytest.mark.parametrize('swap_stage_after_cas',(False,True))
@pytest.mark.parametrize('member_name',('etc/shadow','./etc/shadow'))
def test_adoption_checks_retained_sysroot_and_excludes_shadow(joined,bounded_build,monkeypatch,swap_stage_after_cas,member_name):
    import io,tarfile
    from quirkbench.contracts import canonical,digest
    c,entry,builder,snapshot,source,candidate,config=joined
    original=c.store.put_file;restored=[]
    def forge(claim,coordinator):
        if claim['stage']!='job_inputs':return
        record_path=Path(claim['stage_dir'])/'diagnostics/stage-result.json';record=json.loads(record_path.read_bytes())
        from quirkbench import job_worker
        item=record['result']['files']['target_sysroot'];path=Path(claim['stage_dir'])/job_worker.captured_input('target_sysroot');good=path.read_bytes()
        with tarfile.open(path,'a') as archive:
            member=tarfile.TarInfo(member_name);member.size=6;member.mode=0o600;archive.addfile(member,io.BytesIO(b'secret'))
        item['sha256']=digest(path.read_bytes());record_path.write_bytes(canonical(record))
        if swap_stage_after_cas:
            def put_file(source_path,**kw):
                artifact=original(source_path,**kw)
                if Path(source_path)==path:path.write_bytes(good);restored.append(True)
                return artifact
            monkeypatch.setattr(c.store,'put_file',put_file)
    with c.lifecycle() as owner:
        pipeline.submit(c,'investigation','build','shadow-build',source=source,candidate=candidate,ready=lambda _:None)
        result=complete_job(c,owner,monkeypatch,after_worker=forge,allow_failure=True)
        assert result['state']=='FAILED' and not result['prepared_digest']
    assert 'excluded credentials' in pipeline.document(c.store,result['error_digest'])['message']
    assert bool(restored)==swap_stage_after_cas


@pytest.mark.parametrize('role',('compose_dependency_rpms','compose_dependency_lock','compose_tree'))
def test_owner_rejects_forged_dependency_evidence_before_signing(joined,bounded_compose,monkeypatch,role):
    import tarfile,io
    from quirkbench.contracts import canonical,digest
    c,entry,builder,snapshot,source,candidate,config=joined
    def forge(claim,coordinator):
        if claim['stage']!='os_compose':return
        record_path=Path(claim['stage_dir'])/'diagnostics/stage-result.json';record=json.loads(record_path.read_bytes())
        data=record['result'];item=data['evidence'][role];path=Path(claim['stage_dir'])/'output/artifacts/objects'/item['sha256']
        if role=='compose_dependency_rpms':
            with tarfile.open(path,'r:') as archive:
                members=[(member,archive.extractfile(member).read()) for member in archive]
            with tarfile.open(path,'w') as archive:
                for index,(member,raw) in enumerate(members):archive.addfile(member,io.BytesIO(b'x'*len(raw) if index==0 else raw))
        elif role=='compose_dependency_lock':path.write_bytes(canonical({'packages':{}}))
        else:
            tree=json.loads(path.read_bytes());tree['repos'].append('unreviewed-external');path.write_bytes(canonical(tree))
        item['sha256']=digest(path.read_bytes());path.rename(path.with_name(item['sha256']));provenance=data['deployment']['provenance']
        provenance['build_evidence']['artifacts'][role]=item['sha256']
        if role=='compose_dependency_lock':provenance['dependency_lock_sha256']=item['sha256']
        if role=='compose_tree':provenance['treefile_sha256']=item['sha256']
        record_path.write_bytes(canonical(record))
    with c.lifecycle() as owner:
        build=pipeline.submit(c,'investigation','build','build-for-forgery',source=source,candidate=candidate,ready=lambda _:None)
        complete_job(c,owner,monkeypatch)
        pipeline.submit(c,'investigation','compose','forged-compose',build=build['operation_id'],repository='lab',ready=lambda _:None)
        result=complete_job(c,owner,monkeypatch,kind='compose',after_worker=forge,allow_failure=True)
        assert result['state']=='FAILED'
        with c.transaction() as db:assert not db.execute('SELECT 1 FROM deployment_refs WHERE owner=?',(result['id'],)).fetchone()
    assert not any('gpg-sign' in argv for _,argv in bounded_compose)


@pytest.mark.parametrize('mutation',['unit','extra-module','link','marker'])
def test_owner_rejects_candidate_payload_mutation_before_signing(joined,bounded_compose,monkeypatch,mutation):
    import subprocess
    c,entry,builder,snapshot,source,candidate,config=joined
    native=subprocess.run
    def mutate(argv,**kwargs):
        result=native(argv,**kwargs)
        if argv[0]=='ostree' and 'checkout' in argv:
            root=Path(argv[-1])
            if mutation=='unit':(root/'usr/etc/systemd/system/quirkbench-candidate.service').write_bytes(b'changed unit')
            elif mutation=='extra-module':(root/'usr/lib/quirkbench/quirkbench/extra.py').write_bytes(b'extra')
            elif mutation=='marker':(root/'usr/lib/quirkbench/deployment-build.json').write_bytes(b'changed marker')
            else:
                path=root/'usr/etc/resolv.conf';path.unlink();path.symlink_to('/run/other')
        return result
    monkeypatch.setattr(subprocess,'run',mutate)
    with c.lifecycle() as owner:
        build=pipeline.submit(c,'investigation','build','payload-build',source=source,candidate=candidate,ready=lambda _:None)
        complete_job(c,owner,monkeypatch)
        pipeline.submit(c,'investigation','compose','payload-compose',build=build['operation_id'],repository='lab',ready=lambda _:None)
        result=complete_job(c,owner,monkeypatch,kind='compose',allow_failure=True)
        assert result['state']=='FAILED'
        with c.transaction() as db:assert not db.execute('SELECT 1 FROM deployment_refs WHERE owner=?',(result['id'],)).fetchone()
    assert not any('gpg-sign' in argv for _,argv in bounded_compose)


def test_restart_is_paused_and_interrupted_build_requires_explicit_resume(joined,bounded_build,monkeypatch):
    from quirkbench.job_coordinator import JobCoordinator
    from quirkbench.job_operations import resume
    from test_builder_setup import Workers
    c,entry,builder,snapshot,source,candidate,config=joined
    with c.lifecycle():
        response=pipeline.submit(c,'investigation','build','restart-build',source=source,candidate=candidate,ready=lambda _:None)
    with c.lifecycle() as owner:
        coordinator=JobCoordinator(owner,Workers());assert coordinator.tick() is None
        assert c.operation_status(response['operation_id'])['data']['state']=='INTERRUPTED'
        c.resume('investigation');assert coordinator.tick() is None
        resume(owner,response['operation_id'])
        assert complete_job(c,owner,monkeypatch)['state']=='SUCCEEDED'


def test_failed_work_replays_failure_and_new_request_uses_existing_services(joined,bounded_build,monkeypatch):
    from quirkbench.build import BuildError
    from quirkbench.job_coordinator import JobCoordinator
    from test_builder_setup import Workers
    from test_source_operation import worker
    c,entry,builder,snapshot,source,candidate,config=joined
    original=bounded_build.run
    def fail(command,**kw):
        if kw['phase']=='compile-kernel':raise BuildError('injected native compilation failure')
        return original(command,**kw)
    monkeypatch.setattr(bounded_build,'run',fail)
    with c.lifecycle() as owner:
        response=pipeline.submit(c,'investigation','build','failed-build',source=source,candidate=candidate,ready=lambda _:None)
        services=Workers();coordinator=JobCoordinator(owner,services);c.resume('investigation')
        claim=coordinator.tick();assert worker(c,claim,monkeypatch)==0;services.done=True;assert coordinator.tick()['state']=='QUEUED'
        services.done=False;claim=coordinator.tick();assert worker(c,claim,monkeypatch)==1
        services.done=True;assert coordinator.tick()['state']=='FAILED'
        replay=pipeline.submit(c,'investigation','build','failed-build',source=source,candidate=candidate,ready=lambda _:None)
        assert replay['operation_id']==response['operation_id'] and replay['data']['state']=='FAILED'
        monkeypatch.setattr(bounded_build,'run',original)
        retry=pipeline.submit(c,'investigation','build','retry-build',source=source,candidate=candidate,ready=lambda _:None)
        assert retry['operation_id']!=response['operation_id']
        assert complete_job(c,owner,monkeypatch)['state']=='SUCCEEDED'


def test_capture_actual_base_cannot_be_substituted(joined):
    from quirkbench.contracts import Conflict,canonical
    c,entry,builder,snapshot,source,candidate,config=joined
    row=c.operation_status(source)['data'];capture=pipeline.document(c.store,row['final_output_digest'])
    other=c.store.put(canonical({**capture,'base_oid':'f'*40}))
    with c.transaction() as db:
        db.execute('INSERT INTO refs VALUES(?,?)',(source,other.sha256))
        db.execute("INSERT INTO operation_refs VALUES(?,'output',?)",(source,other.sha256))
        db.execute('UPDATE operations SET final_output_digest=? WHERE id=?',(other.sha256,source))
    with pytest.raises(Conflict,match='differs from investigation workspace'):
        pipeline.submit(c,'investigation','build','wrong-base',source=source,candidate=candidate,ready=lambda _:None)


def test_joined_service_unavailability_has_c2_blocked_response(joined,monkeypatch,capsys):
    from quirkbench import cli,controller_service
    from quirkbench.contracts import Conflict
    c,entry,builder,snapshot,source,candidate,config=joined
    def unavailable(_):raise Conflict('supported service unavailable')
    monkeypatch.setattr(controller_service,'require_ready',unavailable)
    result=cli._main(['investigation', 'build', 'kernel', 'investigation', '--capture', source, '--candidate', candidate, '--request-id', 'blocked-build', '--json', '--reserve-gib', '0'], state_root=str(c.root))
    answer=json.loads(capsys.readouterr().out)
    assert result==4 and answer['error']['code']=='BLOCKED' and answer['error']['retryable'] is True
    assert answer['operation_id'] is None


@pytest.mark.parametrize('action',('prepare-candidate','build','compose'))
def test_joined_storage_pressure_has_c2_blocked_response(joined,bounded_build,monkeypatch,capsys,action):
    from quirkbench import cli,controller_service
    from quirkbench.store import ArtifactStore,StoragePressure
    c,entry,builder,snapshot,source,candidate,config=joined
    monkeypatch.setattr(controller_service,'require_ready',lambda _:None)
    arguments=[]
    if action=='build':arguments=['--capture',source,'--candidate',candidate]
    elif action=='compose':
        with c.lifecycle() as owner:
            build=pipeline.submit(c,'investigation','build','pressure-build',source=source,candidate=candidate,ready=lambda _:None)
            complete_job(c,owner,monkeypatch)
        arguments=['--build',build['operation_id'],'--repository','lab']
    def pressure(self,*args,**kwargs):raise StoragePressure('free-space reserve reached')
    monkeypatch.setattr(ArtifactStore,'put',pressure)
    result=cli._main(['investigation', 'build', {'prepare-candidate':'prepare','build':'kernel','compose':'system'}[action], 'investigation', *arguments, '--request-id', 'pressure-' + action, '--json'], state_root=str(c.root))
    answer=json.loads(capsys.readouterr().out)
    assert result==4 and answer['error']=={'code':'BLOCKED','message':'free-space reserve reached','retryable':True}
    assert answer['operation_id'] is None


@pytest.mark.parametrize('version',[None,1])
def test_owner_rejects_worker_selected_legacy_payload_validation(joined,bounded_compose,monkeypatch,version):
    from quirkbench.compose import ComposeInputs
    from quirkbench.contracts import canonical
    c,entry,builder,snapshot,source,candidate,config=joined
    def downgrade(claim,coordinator):
        if claim['stage']!='os_compose':return
        stage=Path(claim['stage_dir']);path=stage/'diagnostics/stage-result.json'
        record=json.loads(path.read_bytes());provenance=record['result']['deployment']['provenance']
        if version is None:provenance.pop('composition_identity_version')
        else:provenance['composition_identity_version']=version
        raw=json.loads((stage/'worker-manifest.json').read_bytes())
        provenance['build_identity']=ComposeInputs.from_mapping(raw).identity(version=1)
        path.write_bytes(canonical(record))
    with c.lifecycle() as owner:
        build=pipeline.submit(c,'investigation','build','downgrade-build',source=source,candidate=candidate,ready=lambda _:None)
        complete_job(c,owner,monkeypatch)
        pipeline.submit(c,'investigation','compose','downgrade-compose',build=build['operation_id'],repository='lab',ready=lambda _:None)
        result=complete_job(c,owner,monkeypatch,kind='compose',after_worker=downgrade,allow_failure=True)
        assert result['state']=='FAILED'
    assert not any('gpg-sign' in argv for _,argv in bounded_compose)

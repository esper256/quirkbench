"""Joined candidate admission/worker/stopped publication; native calls injected."""
import json
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator
from quirkbench.contracts import ContractError, Conflict, canonical
from quirkbench import candidate_rootfs_operation as operation, job_worker, candidate_rootfs_worker
from quirkbench.controller import Controller
from quirkbench.job_coordinator import JobCoordinator
from quirkbench.job_operations import resume
from quirkbench.worker_claim import read_active_worker_claim
from test_candidate_rootfs_worker import setup as assembly_setup, execution
from test_builder_setup import Workers, BOOT


@pytest.fixture
def setup(assembly_setup):
    root,stage,store,entry,value,builder,snapshot=assembly_setup
    # The adapter-only fixture creates intermediate parents with mkdir(parents).
    # A joined service fixture must explicitly satisfy the private worker-root
    # contract under either umask, rather than relying on a restrictive host.
    controller=Controller(root,reserve_bytes=0,boot_id_reader=lambda:BOOT)
    return controller,entry,value,builder,snapshot


def submit(setup,request='candidate-1',**kwargs):
    c,entry,value,builder,snapshot=setup
    return operation.submit(c,value,request,builder=builder,ready=lambda _:None,**kwargs)


def dispatch(setup,owner,monkeypatch):
    c,entry,value,builder,snapshot=setup
    services=Workers();coordinator=JobCoordinator(owner,services)
    response=submit(setup);claim=coordinator.tick()
    assert claim['stage']=='candidate_rootfs'
    stage=Path(claim['stage_dir'])
    simulated=(c.root,stage,c.store,entry,value,builder,snapshot)
    from quirkbench import recovery_worker
    monkeypatch.setattr(recovery_worker,'execute_rootfs',execution(simulated,[]))
    def verify(*args,**kwargs):
        return read_active_worker_claim(*args,**kwargs,boot_id_reader=lambda:BOOT,
            cgroup_reader=lambda:'0::/user.slice/'+claim['worker_unit']+'/runtime\n')
    monkeypatch.setattr(job_worker,'read_active_worker_claim',verify)
    return response,claim,coordinator,services


def work(setup,claim):
    return job_worker.run_worker(setup[0].root,claim['id'],claim['worker_epoch'],claim['worker_generation'],claim['stage_dir'])


def test_admission_is_prompt_replayable_and_retains_full_closure(setup,monkeypatch):
    c,entry,value,builder,snapshot=setup
    from quirkbench import baseline_inputs
    monkeypatch.setattr(baseline_inputs,'verify_object',lambda *a,**k:pytest.fail('large bytes hashed during admission'))
    first=submit(setup);assert submit(setup)==first
    row=c.operation_status(first['operation_id'])['data']
    assert row['state']=='QUEUED' and row['device'] is None and row['campaign'] is None
    assert {item['sha256'] for item in snapshot['packages']}<=set(row['references']['input'])
    assert entry['build_recipe']['digest'] in row['references']['input']
    changed={**value,'baseline_sha256':'f'*64}
    # Canonical intent replay/conflict remains owned by the existing service.
    with pytest.raises(Conflict):
        operation.submit(c,changed,'candidate-1',builder=builder,ready=lambda _:None)
    with c.transaction() as db:assert db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==1


def test_omitted_builder_replay_uses_original_binding_and_different_request_conflicts(setup,monkeypatch):
    c,entry,value,builder,snapshot=setup
    from quirkbench import controller_service
    monkeypatch.setattr(controller_service,'configuration',lambda _:builder)
    first=operation.submit(c,value,'default-builder',ready=lambda _:None)
    monkeypatch.setattr(controller_service,'configuration',lambda _:pytest.fail('replay read current configuration'))
    assert operation.submit(c,value,'default-builder',ready=lambda _:None)==first
    changed={**builder,'builder_config_digest':'sha256:'+'f'*64}
    with pytest.raises(Conflict):operation.submit(c,value,'default-builder',builder=changed,ready=lambda _:None)


def test_default_signed_builder_uses_retained_proof_without_native_admission_work(setup,tmp_path,monkeypatch):
    from test_builder_setup import intent,complete,builder_archive
    from quirkbench import builder_setup,controller_service,installed_release,recovery_podman
    c,entry,value,builder,snapshot=setup
    path=tmp_path/'builder.tar';path.write_bytes(builder_archive())
    with c.lifecycle() as owner:
        row=intent(c,path);completed=complete(c,owner,row,monkeypatch)
    args=json.loads(c.store.get(completed['input_digest']))['arguments']
    from quirkbench import baseline_inputs
    entry={**entry,'builder_image_digest':args['builder_image_digest']}
    value,_=baseline_inputs.input_record(c.store,entry)
    release={'verification':{'statement':{'schema_version':2,
        **{key:args[key] for key in operation.BUILDER_FIELDS}},'statement_sha256':args['release_statement_sha256']}}
    monkeypatch.setattr(recovery_podman,'_verify_retained_builder_archive',lambda *a,**k:pytest.fail('archive hashed during admission'))
    monkeypatch.setattr(builder_setup,'inspect_image',lambda *a,**k:pytest.fail('native inspection during admission'))
    retained=builder_setup.retained_builder(c.root,release)
    assert 'ready' not in retained and retained['operation_id']==row['id']
    monkeypatch.setattr(controller_service,'configuration',lambda _:{'runtime':'/runtime/bin/quirkbench'})
    monkeypatch.setattr(installed_release,'inspect_selected',lambda _:release)
    response=operation.submit(c,value,'signed-default',ready=lambda _:None)
    saved=json.loads(c.store.get(c.operation_status(response['operation_id'])['data']['input_digest']))['arguments']
    assert all(saved[key]==args[key] for key in operation.BUILDER_FIELDS)


def test_request_id_owned_by_another_kind_is_a_conflict(setup):
    c=setup[0];c.admit_operation('candidate-1','other',{})
    with pytest.raises(Conflict,match='another operation kind'):submit(setup)


def test_joined_worker_stops_and_publishes_retained_sysroot_without_attempt_authority(setup,monkeypatch):
    c,entry,value,builder,snapshot=setup
    with c.lifecycle() as owner:
        response,claim,coordinator,services=dispatch(setup,owner,monkeypatch)
        assert work(setup,claim)==0
        with pytest.raises(Conflict,match='shutdown required'):coordinator.consume(claim)
        assert not c.operation_status(claim['id'])['data']['references']['output']
        services.done=True;assert coordinator.tick()['state']=='SUCCEEDED'
        row=c.operation_status(claim['id'])['data']
        result=operation.validate_result(json.loads(c.store.get(row['final_output_digest'])))
        assert result['candidate_input_sha256']==candidate_rootfs_worker.sha256_file_record(value)
        assert all(result[key]==builder[key] for key in operation.BUILDER_FIELDS)
        assert set(row['references']['output'])=={result['sysroot_archive_sha256'],row['final_output_digest']}
        import tarfile
        with tarfile.open(c.store.path(result['sysroot_archive_sha256'])) as archive:
            assert 'usr/lib/quirkbench/candidate-rootfs-input.json' in archive.getnames()
            assert not any(item.islnk() for item in archive)
        assert not Path(claim['stage_dir']).exists() and len(services.stopped)==1
        assert submit(setup)['operation_id']==response['operation_id']
        with c.transaction() as db:
            assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==0
            assert db.execute('SELECT COUNT(*) FROM campaigns').fetchone()[0]==0
            assert db.execute('SELECT kind,state,workspace_id,input_generation,stage_retained FROM storage_groups WHERE owner=?',(claim['id'],)).fetchone()[:]==('input','SUCCEEDED',None,None,0)


@pytest.mark.parametrize('mutation',['input','result','tree','link','external-hardlink','special'])
def test_stopped_changed_inputs_results_or_tree_never_publish(setup,monkeypatch,mutation):
    c,entry,value,builder,snapshot=setup
    with c.lifecycle() as owner:
        response,claim,coordinator,services=dispatch(setup,owner,monkeypatch)
        assert work(setup,claim)==0
        stage=Path(claim['stage_dir']);root=stage/'candidate-output/rootfs'
        if mutation=='input':c.store.path(snapshot['packages'][0]['sha256']).write_bytes(b'changed')
        elif mutation=='result':
            path=stage/'diagnostics/stage-result.json';data=json.loads(path.read_bytes());data['result']['input_sha256']='f'*64;path.write_bytes(canonical(data))
        elif mutation=='tree':(root/'sbin/init').write_bytes(b'changed')
        elif mutation=='link':root.rename(stage/'old-root');root.symlink_to(stage/'old-root')
        elif mutation=='external-hardlink':
            import os
            os.link(root/'sbin/init',stage/'outside-alias')
        else:
            import os
            os.mkfifo(root/'fifo')
        services.done=True
        try:coordinator.tick()
        except Conflict:pass
        row=c.operation_status(claim['id'])['data']
        assert row['state']!='SUCCEEDED' and not row['references']['output']
        assert row['final_output_digest'] is None and stage.exists()


def test_failed_assembly_retains_diagnostics_and_retry_requires_new_request(setup,monkeypatch):
    c=setup[0]
    with c.lifecycle() as owner:
        response,claim,coordinator,services=dispatch(setup,owner,monkeypatch)
        from quirkbench import recovery_worker
        monkeypatch.setattr(recovery_worker,'execute_rootfs',lambda *a,**k:{'exit_code':1})
        assert work(setup,claim)==1
        services.done=True;assert coordinator.tick()['state']=='FAILED'
        row=c.operation_status(claim['id'])['data'];assert row['error_digest'] and not row['references']['output']
        with pytest.raises(Conflict):resume(owner,claim['id'])
        assert submit(setup)['operation_id']==claim['id']
        assert submit(setup,'candidate-retry')['operation_id']!=claim['id']
        with c.transaction() as db:assert db.execute('SELECT state FROM storage_groups WHERE owner=?',(claim['id'],)).fetchone()[0]=='FAILED'


def test_restart_fences_claim_and_requires_stop_then_explicit_resume(setup,monkeypatch):
    c=setup[0]
    with c.lifecycle() as owner:
        response,claim,coordinator,services=dispatch(setup,owner,monkeypatch)
        assert work(setup,claim)==0
    with c.lifecycle() as successor:
        with pytest.raises(Conflict):coordinator.consume(claim)
        with pytest.raises(Conflict):resume(successor,claim['id'])
        successor.reconcile_units(services)
        fresh=JobCoordinator(successor,services);assert fresh.tick() is None
        resume(successor,claim['id']);services.done=False
        new=fresh.tick();assert new['worker_generation']>claim['worker_generation'] and new['stage_dir']!=claim['stage_dir']
        assert not c.operation_status(claim['id'])['data']['references']['output']
        from quirkbench import recovery_worker
        c,entry,value,builder,snapshot=setup;stage=Path(new['stage_dir'])
        monkeypatch.setattr(recovery_worker,'execute_rootfs',execution((c.root,stage,c.store,entry,value,builder,snapshot),[]))
        monkeypatch.setattr(job_worker,'read_active_worker_claim',lambda *a,**k:read_active_worker_claim(*a,**k,
            boot_id_reader=lambda:BOOT,cgroup_reader=lambda:'0::/user.slice/'+new['worker_unit']+'/runtime\n'))
        assert work(setup,new)==0
        services.done=True;assert fresh.tick()['state']=='SUCCEEDED'


@pytest.mark.parametrize('mutation',['tree','package','archive','owner','crash','terminal-tree'])
def test_last_storage_callback_cannot_publish_changed_or_partial_candidate(setup,monkeypatch,mutation):
    c=setup[0]
    with c.lifecycle() as owner:
        response,claim,coordinator,services=dispatch(setup,owner,monkeypatch)
        assert work(setup,claim)==0
        changed=[False];publications=[0]
        def mutate(phase):
            if phase!='after_publish' or changed[0]:return
            publications[0]+=1
            if mutation=='terminal-tree' and publications[0]!=3:return
            changed[0]=True
            if mutation in ('tree','terminal-tree'):(Path(claim['stage_dir'])/'candidate-output/rootfs/sbin/init').write_bytes(b'late change')
            elif mutation=='package':c.store.path(setup[4]['packages'][0]['sha256']).write_bytes(b'late change')
            elif mutation=='archive':
                # The first owner CAS publication is the captured sysroot archive.
                for path in c.store.objects.iterdir():
                    if path.stat().st_size>10000:path.write_bytes(b'late change')
            elif mutation=='owner':owner.close()
            else:raise OSError('interrupted publication')
        c.store.fault_hook=mutate;services.done=True
        try:coordinator.tick()
        except (Conflict,OSError):pass
        row=c.operation_status(claim['id'])['data']
        assert changed[0] and row['state']!='SUCCEEDED'
        assert row['references']['output']==[] and row['final_output_digest'] is None


def test_sysroot_capture_preserves_internal_links_modes_and_excludes_credentials(tmp_path):
    import os,tarfile
    root=tmp_path/'root';root.mkdir(mode=0o700);(root/'etc').mkdir();(root/'usr').mkdir()
    (root/'usr/tool').write_bytes(b'package bytes');(root/'usr/tool').chmod(0o4755)
    os.link(root/'usr/tool',root/'usr/alias')
    (root/'etc/resolv.conf').symlink_to('/run/NetworkManager/resolv.conf')
    (root/'etc/shadow').write_bytes(b'private locked passwords')
    path=tmp_path/'root.tar';original,nodes=operation.capture_archive(root,path,lambda:None)
    operation.verify_archive(path,nodes)
    with tarfile.open(path) as archive:
        assert 'etc/shadow' not in archive.getnames()
        assert archive.getmember('usr/tool').mode==0o4755
        assert archive.getmember('usr/alias').isfile()
        assert archive.getmember('etc/resolv.conf').linkname=='/run/NetworkManager/resolv.conf'
    os.link(root/'etc/shadow',root/'usr/private-alias')
    with pytest.raises(ContractError,match='credential'):operation.namespace(root)


def test_archive_rejects_extra_excluded_credentials_and_capture_leaf_replacement(tmp_path):
    import os,tarfile,io
    root=tmp_path/'root';root.mkdir(mode=0o700);leaf=root/'file';leaf.write_bytes(b'bytes')
    original,nodes=operation.capture_archive(root,tmp_path/'root.tar',lambda:None)
    with tarfile.open(tmp_path/'root.tar','a') as archive:
        entry=tarfile.TarInfo('etc/shadow');entry.size=6;archive.addfile(entry,io.BytesIO(b'secret'))
    with pytest.raises(ContractError,match='namespace'):operation.verify_archive(tmp_path/'root.tar',nodes)
    calls=[0]
    def replace():
        calls[0]+=1
        if calls[0]==2:
            leaf.unlink();os.mkfifo(leaf)
    with pytest.raises(Conflict):operation.capture_archive(root,tmp_path/'changed.tar',replace)


def test_capture_enforces_configured_space_reserve(tmp_path,monkeypatch):
    from quirkbench import builder_setup
    from quirkbench.store import StoragePressure
    root=tmp_path/'root';root.mkdir(mode=0o700);(root/'file').write_bytes(b'bytes')
    seen=[]
    def full(path,count,reserve):
        seen.append(reserve);raise StoragePressure('configured reserve reached')
    monkeypatch.setattr(builder_setup,'check_space',full)
    with pytest.raises(StoragePressure):operation.capture_archive(root,tmp_path/'output.tar',lambda:None,reserve=1234)
    assert seen==[1234]


def test_queued_restart_requires_explicit_resume_and_backup_keeps_closure(setup,monkeypatch,tmp_path):
    c=setup[0]
    with c.lifecycle():response=submit(setup)
    with c.lifecycle() as owner:
        coordinator=JobCoordinator(owner,Workers());assert coordinator.tick() is None
        assert c.operation_status(response['operation_id'])['data']['state']=='INTERRUPTED'
        resume(owner,response['operation_id'])
        _,claim,coordinator,services=dispatch(setup,owner,monkeypatch)
        assert claim['id']==response['operation_id'] and work(setup,claim)==0
        services.done=True;assert coordinator.tick()['state']=='SUCCEEDED'
        backup=tmp_path/'backup';c.backup(backup)
        row=c.operation_status(claim['id'])['data']
        for identity in row['references']['input']+row['references']['output']:
            assert (backup/'artifacts/objects'/identity).read_bytes()==c.store.path(identity).read_bytes()
    restored=Controller.restore(backup,tmp_path/'restored',reserve_bytes=0,boot_id_reader=lambda:BOOT)
    assert restored.operation_status(claim['id'])['data']['state']=='SUCCEEDED'
    assert restored.operation_status(claim['id'])['data']['final_output_digest']==row['final_output_digest']


def cli_args(setup,tmp_path,request='cli-candidate',**changed):
    from types import SimpleNamespace
    c,entry,value,builder,snapshot=setup
    path=tmp_path/'input.json';path.write_bytes(canonical(value))
    # Test the retained internal adapter; standalone candidate CLI was removed.
    args=SimpleNamespace(command='candidate-rootfs',state=c.root,input=path,request_id=request,json=True,
                         builder_image_digest=builder['builder_image_digest'],builder_config_digest=builder['builder_config_digest'],
                         builder_archive=builder['builder_archive_sha256'],reserve_gib=0,wait=False)
    for key,value in changed.items():setattr(args,key,value)
    return args


def test_candidate_cli_returns_durable_envelope_and_conflict_exit(setup,tmp_path,monkeypatch,capsys):
    from quirkbench import controller_service,job_cli
    monkeypatch.setattr(controller_service,'require_ready',lambda _:None)
    args=cli_args(setup,tmp_path)
    assert job_cli.run(args)==0
    first=json.loads(capsys.readouterr().out);assert first['ok'] and first['operation_id']
    assert job_cli.run(args)==0
    assert json.loads(capsys.readouterr().out)['operation_id']==first['operation_id']
    args.builder_config_digest='sha256:'+'f'*64
    assert job_cli.run(args)==3
    error=json.loads(capsys.readouterr().out);assert not error['ok'] and error['error']['code']=='CONFLICT'


def test_candidate_cli_wait_keeps_operation_id_and_result_inside_envelope(setup,tmp_path,monkeypatch,capsys):
    from quirkbench import controller_service,job_cli
    monkeypatch.setattr(controller_service,'require_ready',lambda _:None)
    args=cli_args(setup,tmp_path,wait=True)
    retained=json.loads((Path(__file__).resolve().parents[1]/'examples/candidate-rootfs-result.json').read_bytes())
    monkeypatch.setattr(job_cli,'wait',lambda root,operation_id:retained)
    assert job_cli.run(args)==0
    captured=capsys.readouterr();accepted=json.loads(captured.err);completed=json.loads(captured.out)
    assert completed=={'schema_version':1,'ok':True,'operation_id':accepted['operation_id'],'data':retained,'error':None}
    def interrupted(*a):raise Conflict('Job interrupted; resume explicitly')
    monkeypatch.setattr(job_cli,'wait',interrupted)
    assert job_cli.run(args)==4
    failed=json.loads(capsys.readouterr().out)
    assert failed['operation_id']==accepted['operation_id'] and failed['error']['code']=='BLOCKED'


@pytest.mark.parametrize('failure',['blocked','invalid','duplicate','infrastructure','space'])
def test_candidate_cli_errors_keep_c2_envelope_and_exit_codes(setup,tmp_path,monkeypatch,capsys,failure):
    from quirkbench import controller_service,job_cli
    args=cli_args(setup,tmp_path)
    if failure!='blocked':monkeypatch.setattr(controller_service,'require_ready',lambda _:None)
    if failure=='invalid':args.input.write_bytes(b'{"unknown":true}')
    elif failure=='duplicate':args.input.write_bytes(b'{"schema_version":1,"schema_version":1}')
    elif failure in ('infrastructure','space'):
        from quirkbench.store import StoragePressure
        def broken(*a,**k):raise StoragePressure('configured reserve') if failure=='space' else OSError('test storage unavailable')
        monkeypatch.setattr(operation,'submit',broken)
    expected={'blocked':(4,'BLOCKED'),'invalid':(2,'INVALID_INPUT'),'duplicate':(2,'INVALID_INPUT'),
              'infrastructure':(5,'INFRASTRUCTURE'),'space':(4,'BLOCKED')}
    status,code=expected[failure];assert job_cli.run(args)==status
    answer=json.loads(capsys.readouterr().out)
    assert set(answer)=={'schema_version','ok','operation_id','data','error'}
    assert answer['ok'] is False and answer['data'] is None and answer['operation_id'] is None
    assert answer['error']['code']==code
    with setup[0].transaction() as db:assert db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==0


def test_candidate_result_runtime_and_schema_agree():
    from quirkbench.candidate_rootfs_operation import validate_result
    root=Path(__file__).resolve().parents[1]
    value=json.loads((root/'examples/candidate-rootfs-result.json').read_bytes())
    schema=json.loads((root/'schemas/candidate-rootfs-result.v1.schema.json').read_bytes())
    Draft202012Validator(schema).validate(value)
    assert validate_result(value)==value
    for changed in ({**value,'schema_version':True},{**value,'extra':True},
                    {**value,'builder_config_digest':'latest'},
                    {**value,'target_tree_sha256':'wrong'}):
        assert list(Draft202012Validator(schema).iter_errors(changed))
        with pytest.raises(ContractError):validate_result(changed)

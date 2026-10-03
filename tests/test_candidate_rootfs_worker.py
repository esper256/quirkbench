"""Fixed local candidate assembly; package/container execution is explicitly injected."""
from pathlib import Path
import pytest
from quirkbench import candidate_rootfs_worker as worker,baseline_inputs,builder_setup
from quirkbench.contracts import ContractError,Conflict,canonical
from quirkbench.build import BuildError
from quirkbench.store import ArtifactStore,atomic_write
from test_recovery_rootfs import locked_fixture,_rpm_line
from test_recovery_podman import builder_archive,IMAGE


def candidate_inputs(tmp_path,monkeypatch):
    root=tmp_path/'state';root.mkdir(mode=0o700)
    catalog,lock,reader,unused,snapshot=locked_fixture(root/'retained')
    store=ArtifactStore(root/'artifacts',reserve_bytes=0)
    for source in unused.objects.iterdir():store.put(source.read_bytes())
    entry=catalog['entries'][0];value,refs=baseline_inputs.input_record(store,entry)
    builder={'builder_image_digest':entry['builder_image_digest'],'builder_config_digest':IMAGE,
             'builder_archive_sha256':store.put(builder_archive()).sha256}
    stage=root/'workers/op/1';stage.mkdir(mode=0o700,parents=True)
    (stage/'diagnostics').mkdir(mode=0o700)
    monkeypatch.setattr(builder_setup,'reserve_bytes',lambda _:0)
    return root,stage,store,entry,value,builder,snapshot


@pytest.fixture
def setup(tmp_path,monkeypatch):
    return candidate_inputs(tmp_path,monkeypatch)


def execution(setup,commands):
    root,stage,store,entry,value,builder,snapshot=setup
    def execute(argv,log,**kwargs):
        commands.append(argv);kwargs['verify']()
        marker=stage/'container';marker.write_text('quirkbench-fedora-rootless-build-v1')
        base=stage/'base';base.write_text(entry['builder_image_digest'])
        def runner(argv,timeout_s,**kw):
            if argv[0]=='dnf5':
                target=Path(next(arg.removeprefix('--installroot=') for arg in argv if arg.startswith('--installroot=')))
                (target/'sbin').mkdir();(target/'sbin/init').touch();return ''
            return ''.join(sorted(_rpm_line(item) for item in snapshot['packages']))
        worker.inner(stage/'candidate-inputs/cas',stage/'candidate-inputs/input.json',stage/'candidate-output',
            runner=runner,marker=marker,base_marker=base,euid=0)
        return {'exit_code':0}
    return execute


def run(setup,*,execute=None,verify=lambda:None):
    root,stage,store,entry,value,builder,snapshot=setup
    return worker.prepare(root,stage,value,builder,verify,lambda *a:None,9999999999,execute=execute)


def test_fixed_candidate_worker_plan_and_independent_output_validation(setup):
    commands=[];result=run(setup,execute=execution(setup,commands))
    root,stage,store,entry,value,builder,snapshot=setup
    assert worker.validate_result(stage/'candidate-output',value,result)==stage/'candidate-output/rootfs'
    command=commands[0]
    assert '--network=none' in command and '--pull=never' in command and '--user=0' in command
    assert builder['builder_config_digest'] in command and '--security-opt=no-new-privileges' in command
    mounts=[command[n+1] for n,item in enumerate(command) if item=='--volume']
    assert len(mounts)==3 and mounts[0].endswith(':/workspace/inputs:ro,z')
    assert mounts[1].endswith(':/workspace/output:rw,z') and mounts[2].endswith(':/workspace/code/quirkbench:ro,z')
    assert all('/private' not in mount and 'controller.sqlite' not in mount for mount in mounts)
    assert not any('--device' in item or '--privileged' in item for item in command)
    assert result['input_sha256']==worker.sha256_file_record(value)


@pytest.mark.parametrize('kind',['base','config','archive','rpm'])
def test_changed_package_or_builder_prevents_native_dispatch(setup,kind):
    root,stage,store,entry,value,builder,snapshot=setup
    if kind=='base':builder['builder_image_digest']='sha256:'+'f'*64
    elif kind=='config':builder['builder_config_digest']='sha256:'+'f'*64
    elif kind=='archive':store.path(builder['builder_archive_sha256']).write_bytes(b'changed')
    else:store.path(snapshot['packages'][0]['sha256']).write_bytes(b'changed')
    commands=[]
    with pytest.raises((ContractError,BuildError)):run(setup,execute=lambda *a,**kw:commands.append(a))
    assert not commands


def test_expired_claim_prevents_input_staging_and_dispatch(setup):
    def lost():raise Conflict('claim lost')
    with pytest.raises(Conflict,match='claim lost'):run(setup,verify=lost,execute=lambda *a,**kw:pytest.fail('launched'))
    assert not (setup[1]/'candidate-inputs').exists()


def test_failed_native_assembly_does_not_return_usable_rootfs(setup):
    with pytest.raises(ContractError,match='assembly failed'):run(setup,execute=lambda *a,**kw:{'exit_code':1})
    assert not (setup[1]/'candidate-output/rootfs-result.json').exists()


@pytest.mark.parametrize('mutation',['bytes','record','link','result'])
def test_stopped_consumer_refuses_changed_or_rebound_sysroot(setup,mutation):
    result=run(setup,execute=execution(setup,[]));root,stage,store,entry,value,builder,snapshot=setup
    output=stage/'candidate-output';target=output/'rootfs'
    if mutation=='bytes':(target/'sbin/init').write_bytes(b'changed')
    elif mutation=='record':(target/'usr/lib/quirkbench/candidate-rootfs-input.json').write_bytes(b'{}')
    elif mutation=='link':target.rename(output/'old');target.symlink_to(output/'old')
    else:result['input_sha256']='f'*64
    with pytest.raises(ContractError):worker.validate_result(output,value,result)


def test_default_inner_checks_container_before_native_package_work(setup,monkeypatch):
    from quirkbench import build
    def outside():raise BuildError('dedicated container required')
    monkeypatch.setattr(build,'_require_container',outside)
    stage=setup[1];output=stage/'candidate-output';output.mkdir(mode=0o700)
    with pytest.raises(BuildError,match='container required'):worker.inner(stage/'cas',stage/'input.json',output)
    assert not (output/'rootfs').exists()


@pytest.mark.parametrize('field',['input','builder'])
def test_callbacks_cannot_change_pinned_worker_binding(setup,field):
    def mutate():
        if field=='input':setup[4]['baseline_sha256']='f'*64
        else:setup[5]['builder_image_digest']='sha256:'+'f'*64
    with pytest.raises(Conflict,match='binding changed'):run(setup,verify=mutate,execute=lambda *a,**kw:pytest.fail('launched'))


def test_objects_directory_replacement_cannot_redirect_input_copy(setup,tmp_path):
    root,stage,store,entry,value,builder,snapshot=setup
    outside=tmp_path/'outside';outside.mkdir(mode=0o700)
    objects=stage/'candidate-inputs/cas/objects';moved=stage/'old-objects'
    def mutate():
        if objects.exists() and not moved.exists():
            objects.rename(moved);objects.symlink_to(outside)
    with pytest.raises(ContractError):run(setup,verify=mutate,execute=lambda *a,**kw:pytest.fail('launched'))
    assert not list(outside.iterdir())


def test_coherent_launch_record_replacement_is_rejected_before_package_work(setup):
    import json
    from quirkbench.recovery_rootfs import CASReader
    root,stage,store,entry,value,builder,snapshot=setup
    alternative=json.loads(canonical(entry));alternative['baseline_id']='different-reviewed-baseline'
    alternate_sha=store.put(canonical(alternative)).sha256
    old_recipe=entry['build_recipe']['digest']
    entry['build_recipe']['digest']=alternate_sha
    entry['target_recipes'].append({**entry['target_recipes'][0],'recipe_id':'retained-original-recipe','digest':old_recipe})
    entry['target_recipes'].sort(key=lambda item:item['recipe_id'])
    original,refs=baseline_inputs.input_record(store,entry);value.clear();value.update(original)
    replacement={**value,'baseline_sha256':alternate_sha}
    record=stage/'candidate-inputs/input.json';called=[]
    def mutate():
        if record.exists():
            # This is a complete other baseline using the staged closure.
            baseline_inputs.resolve(CASReader(stage/'candidate-inputs/cas'),replacement)
            atomic_write(record,canonical(replacement))
    with pytest.raises(Conflict,match='launch record changed'):
        run(setup,verify=mutate,execute=lambda *a,**kw:called.append(a))
    assert not called


def test_early_stage_replacement_cannot_redirect_mkdir(setup,tmp_path):
    root,stage,store,entry,value,builder,snapshot=setup
    outside=tmp_path/'outside';outside.mkdir(mode=0o700);moved=stage.parent/'old-stage'
    def mutate():
        if not moved.exists():stage.rename(moved);stage.symlink_to(outside)
    with pytest.raises(ContractError):run(setup,verify=mutate,execute=lambda *a,**kw:pytest.fail('launched'))
    assert not list(outside.iterdir())

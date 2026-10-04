"""Portable image generation rejects unsafe inputs and retains interrupted work."""
import json
from pathlib import Path

import pytest

from quirkbench.build import BuildError
from quirkbench.contracts import canonical
from quirkbench import recovery_foreground as foreground
from test_recovery_stock import stock_fixture


@pytest.fixture(autouse=True)
def selected_capacity(monkeypatch):
    from quirkbench.resource_budget import Capacity
    monkeypatch.setattr('quirkbench.resource_budget.capacity',lambda:Capacity(foreground.os.cpu_count() or 1,16*1024**3))


class Engine:
    def __init__(self, image):
        self.image = image
        self.identity = 'b'*64
        self.record = None
        self.calls = []
        self.running = True
        self.exit = 1
        self.changed = False

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        action = argv[1]
        if action == 'inspect' and argv[2] == self.image:
            return json.dumps([{'Id': self.image, 'Architecture': 'amd64', 'Os': 'linux', 'Config': {}}])
        if action == 'create':
            self.record = {'name': argv[argv.index('--name')+1]}
            option=lambda name:next(a.split('=',1)[1] for a in argv if a.startswith(name+'='))
            self.limits={'Memory':int(option('--memory')), 'MemorySwap':int(option('--memory-swap')),
                         'NanoCpus':int(option('--cpus'))*10**9, 'PidsLimit':4096,
                         'PidMode':'private', 'RestartPolicy':{'Name':'no'}, 'Privileged':False}
            return self.identity+'\n'
        if action == 'inspect':
            return json.dumps([{'Id': self.identity, 'Image': self.image, 'Config': {'Labels': {
                foreground.LABEL: 'different' if self.changed else self.record['name']}},
                'HostConfig':self.limits,
                'State': {'Running': self.running, 'Pid': 100 if self.running else 0, 'ExitCode': self.exit}}])
        if action == 'stop':
            self.running = False
            return self.identity
        if action == 'rm':
            return self.identity
        raise AssertionError('unexpected engine command '+repr(argv))


def test_podman_foreground_gate_and_recorded_manager_cleanup(inputs,monkeypatch):
    from quirkbench import container_containment
    from quirkbench.process_identity import WorkerServiceError
    kwargs,engine=inputs;calls=[];configured=['systemd'];populated=[False]
    def run(argv,**kw):
        calls.append(argv);offset=3 if argv[2].startswith('--cgroup-manager=') else 2
        args=argv[offset:]
        if args==['info','--format','json']:
            manager=argv[2].split('=',1)[1] if offset==3 else configured[0]
            return json.dumps({'host':{'cgroupVersion':'v2','cgroupManager':manager}})
        if args[0]=='start':engine.running=True;return engine.identity
        return engine(['docker',*args],**kw)
    monkeypatch.setattr(container_containment,'capture',lambda value,identity,*a,**k:'/libpod-'+identity)
    def empty(group):
        if populated[0]:raise WorkerServiceError('container still contains descendants')
    monkeypatch.setattr(container_containment,'stopped',empty)
    kwargs.update(engine='podman',run=run)
    def interrupted(argv,log,**kw):
        assert argv[3:5]==['logs','--follow'];log.write_text('interrupted\n');raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):foreground.build(**kwargs,execute=interrupted)
    output=kwargs['output'];record=json.loads((output/'build.json').read_bytes())
    assert record['schema_version']==2 and record['cgroup_manager']=='systemd' and record['payload_released']
    create=next(argv for argv in calls if 'create' in argv)
    assert ['/usr/bin/python3','-I','/__quirkbench_entry.py']==create[create.index(kwargs['image'])+1:create.index(kwargs['image'])+4]
    configured[0]='cgroupfs';calls.clear();populated[0]=True
    with pytest.raises(WorkerServiceError,match='descendants'):foreground.cleanup(output,run=run)
    assert not json.loads((output/'build.json').read_bytes())['removed']
    populated[0]=False
    assert foreground.cleanup(output,run=run)['removed']
    assert all(argv[2]=='--cgroup-manager=systemd' for argv in calls)


@pytest.fixture
def inputs(tmp_path):
    recipe, lock, _, store = stock_fixture(tmp_path/'inputs')
    recipe_sha = store.put(canonical(recipe)).sha256
    engine = Engine(recipe['builder_image_digest'])
    kwargs = dict(cas_root=store.root, recipe_sha256=recipe_sha,
                  image=recipe['builder_image_digest'], output=tmp_path/'output',
                  engine='docker', run=engine)
    return kwargs, engine


@pytest.mark.parametrize('change', ['image', 'cpus', 'memory_gib', 'timeout'])
def test_invalid_build_inputs_never_create_container(inputs, change):
    kwargs, engine = inputs
    kwargs[change] = 'moving:latest' if change == 'image' else 0
    with pytest.raises(BuildError):
        foreground.build(**kwargs)
    assert not kwargs['output'].exists()
    assert not engine.calls


def test_changed_builder_cannot_run_selected_recipe(inputs):
    kwargs, engine = inputs
    kwargs['image'] = 'sha256:'+'c'*64
    engine.image = kwargs['image']
    with pytest.raises(BuildError, match='recipe differs'):
        foreground.build(**kwargs)
    assert not kwargs['output'].exists()
    assert all(call[1]=='inspect' for call in engine.calls)


def test_interrupted_build_stops_whole_container_and_retains_diagnostics(inputs):
    kwargs, engine = inputs
    def interrupted(argv, log, **options):
        log.write_text('interrupted during installroot\n')
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        foreground.build(**kwargs, execute=interrupted)
    output = kwargs['output']
    record = json.loads((output/'build.json').read_bytes())
    assert record['stopped'] and not record['complete'] and not record['removed']
    assert (output/'build.log').read_text() == 'interrupted during installroot\n'
    assert not (output/'recovery.img').exists()
    create = next(call for call in engine.calls if call[1]=='create')
    assert '--network=none' in create and '--memory='+str(4*1024**3) in create and '--pids-limit=4096' in create
    mounts = [create[i+1] for i,x in enumerate(create) if x=='--volume']
    assert len(mounts)==2 and all(mount.endswith(':ro,Z') for mount in mounts)
    assert not any(call[1] in ('cp','rm') for call in engine.calls)
    assert foreground.cleanup(output, run=engine)['removed']
    assert (output/'build.log').exists()


def test_failed_container_is_not_exported_even_when_attach_exits_zero(inputs):
    kwargs, engine = inputs
    def finished(*args, **options):
        engine.running = False
        return {'exit_code': 0}
    with pytest.raises(BuildError, match='image build failed'):
        foreground.build(**kwargs, execute=finished)
    assert not any(call[1]=='cp' for call in engine.calls)
    assert not json.loads((kwargs['output']/'build.json').read_bytes())['complete']


def test_cleanup_refuses_changed_container_identity(inputs):
    kwargs, engine = inputs
    with pytest.raises(KeyboardInterrupt):
        foreground.build(**kwargs, execute=lambda *a,**k: (_ for _ in ()).throw(KeyboardInterrupt()))
    engine.changed = True
    before = len(engine.calls)
    with pytest.raises(BuildError, match='identity differs'):
        foreground.cleanup(kwargs['output'], run=engine)
    assert all(call[1]=='inspect' for call in engine.calls[before:])


def test_checkout_output_and_ancestor_alias_retain_failed_build_without_touching_source(inputs):
    kwargs, engine = inputs
    parent=kwargs['output'].parent
    (parent/'.git').mkdir();(parent/'.git/HEAD').write_text('ref: refs/heads/main\n')
    (parent/'source.c').write_text('source stays intact')
    alias=parent/'alias';alias.symlink_to(parent,target_is_directory=True)
    kwargs['output']=alias/kwargs['output'].name
    with pytest.raises(BuildError,match='image build failed'):
        foreground.build(**kwargs,execute=lambda *a,**k:{'exit_code':1})
    output=parent/kwargs['output'].name
    assert json.loads((output/'build.json').read_bytes())['stopped']
    foreground.cleanup(output,run=engine)
    assert (parent/'source.c').read_text()=='source stays intact'
    assert (parent/'.git/HEAD').read_text()=='ref: refs/heads/main\n'
    assert output.is_dir()  # Cleanup removes its container, never the user directory.


def test_cli_needs_no_controller_selection(monkeypatch, tmp_path, capsys):
    from quirkbench.cli import main
    import quirkbench.state_config as config
    monkeypatch.setattr(config, 'configure_state_root', lambda *a,**k: pytest.fail('must not configure controller'))
    monkeypatch.setattr(foreground, 'build', lambda **kwargs: {'image': str(tmp_path/'recovery.img'), 'signed': False})
    assert main(['recovery-image-build', '--store', str(tmp_path/'cas'), '--recipe', 'a'*64,
                 '--builder-image', 'sha256:'+'b'*64, '--output', str(tmp_path/'output')]) == 0
    assert json.loads(capsys.readouterr().out)['signed'] is False


def test_engine_runner_bounds_native_output():
    import sys
    with pytest.raises(BuildError, match='bounded output|exceeds budget'):
        foreground._run([sys.executable, '-c', 'import os; os.write(1,b"x"*(17*1024**2))'])


def test_engine_runner_reports_stderr_and_failure():
    import sys
    with pytest.raises(BuildError, match='selected image unavailable'):
        foreground._run([sys.executable, '-c', 'import sys; print("selected image unavailable",file=sys.stderr);sys.exit(3)'])


@pytest.mark.parametrize('change', ['none', 'profile', 'manifest', 'recipe'])
def test_exported_artifacts_match_selected_inputs(tmp_path, monkeypatch, change):
    """Sparse synthetic image exercises real checksum/export checks, without a build."""
    from test_stock_recovery_flow import assembled_stock
    from quirkbench.recovery_stock_release import create_candidate
    from quirkbench.contracts import digest
    import os
    _, recipe, store, record, image_inputs, manifest = assembled_stock(tmp_path/'source', monkeypatch)
    candidate = create_candidate(recipe, store, record, image_inputs)
    from quirkbench.store import ArtifactStore
    writer = ArtifactStore(store.objects.parent, reserve_bytes=0)
    recipe_sha = writer.put(canonical(recipe)).sha256
    payload = image_inputs.output
    with payload.open('r+b') as stream:
        stream.truncate(candidate['image_size_bytes'])
    checksum = foreground.sha256_file(payload)
    candidate['image_sha256'] = checksum
    manifest['image_sha256'] = checksum
    if change == 'profile':
        candidate['profile_digest'] = manifest['recovery_profile_digest'] = 'f'*64
    if change == 'recipe':
        candidate['recipe_digest'] = 'f'*64
    candidate['image_manifest_sha256'] = digest(canonical(manifest))
    if change == 'manifest':
        manifest['boot_policy'] = 'corrupted after completion'
    result = {'recipe_sha256': recipe_sha, 'signed': False, 'candidate': candidate}
    engine = Engine(recipe['builder_image_digest'])
    engine.exit = 0
    def run(argv, **options):
        if argv[1] != 'cp':
            return engine(argv, **options)
        engine.calls.append(argv)
        target = Path(argv[-1])
        if target.name == 'recovery.img':
            os.link(payload, target)  # Preserve sparseness; no privileged build adapter.
        elif target.name == 'recovery.img.json':
            target.write_bytes(canonical(manifest))
        elif target.name == 'recovery.img.sha256':
            target.write_text(checksum+'  recovery.img\n')
        else:
            target.write_bytes(canonical(result))
        return ''
    def execute(*args, **options):
        engine.running = False
        return {'exit_code': 0}
    output = tmp_path/'output'
    kwargs = dict(cas_root=store.objects.parent, recipe_sha256=recipe_sha,
                  image=recipe['builder_image_digest'], output=output,
                  engine='docker', run=run, execute=execute)
    if change == 'none':
        answer = foreground.build(**kwargs)
        assert answer['sha256'] == checksum and not answer['signed'] and not answer['boot_tested']
        assert json.loads((output/'build.json').read_bytes())['removed']
    else:
        with pytest.raises((BuildError, ValueError), match='provenance|manifest'):
            foreground.build(**kwargs)
        assert not json.loads((output/'build.json').read_bytes())['complete']
        assert not any(call[1]=='rm' for call in engine.calls)


def test_default_cpu_cap_works_on_small_linux_hosts(inputs, monkeypatch):
    kwargs, engine = inputs
    monkeypatch.setattr(foreground.os, 'cpu_count', lambda: 2)
    with pytest.raises(KeyboardInterrupt):
        foreground.build(**kwargs, execute=lambda *a,**k: (_ for _ in ()).throw(KeyboardInterrupt()))
    create = next(call for call in engine.calls if call[1]=='create')
    assert '--cpus=1' in create


def test_unapplied_resource_limit_never_starts_build(inputs):
    kwargs,engine=inputs
    def run(argv,**options):
        result=engine(argv,**options)
        if argv[1]=='create':engine.limits['Memory']=0
        return result
    kwargs['run']=run
    with pytest.raises(BuildError,match='containment'):
        foreground.build(**kwargs,execute=lambda *a,**k:pytest.fail('unbounded build must not start'))
    assert not any(call[1] in ('start','cp') for call in engine.calls)


def test_lost_create_acknowledgement_reconciles_recorded_name(inputs):
    kwargs,engine=inputs
    def run(argv,**options):
        result=engine(argv,**options)
        if argv[1]=='create':raise OSError('lost create acknowledgement')
        return result
    kwargs['run']=run
    with pytest.raises(BuildError,match='lost create'):
        foreground.build(**kwargs)
    record=json.loads((kwargs['output']/'build.json').read_bytes())
    assert record['stopped'] and not record['complete'] and 'container_id' not in record
    assert not any(call[1] in ('start','cp') for call in engine.calls)
    assert any(call[1:]==['inspect',record['name']] for call in engine.calls)
